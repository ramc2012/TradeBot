"""30-minute expiry-wide GEX/OI progression + strike×time heatmap data.

For the chosen expiry, takes a band of strikes around ATM, loads their 30-min
CE/PE history (`option_premium_candles` via option_history_service), aligns to a
common time grid, and runs the pure `compute_progression` aggregator to produce
the net-GEX-over-time series (regime-shaded), per-strike gamma-density and OI
matrices (the strike×time heatmaps), and the single-strike OI series.

option_premium_candles ingest is episodic/patchy, so this degrades gracefully:
returns `available=False` when the band has too little history.
"""
from __future__ import annotations

import asyncio
import bisect
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import text

from db.database import AsyncSessionLocal
from directional_options.gex_engine import compute_progression
from market_data.option_chain import option_chain_service
from market_data.option_history import option_history_service
from market_data.symbols import to_app_symbol

IST = ZoneInfo("Asia/Kolkata")
_YEAR_SECONDS = 365.0 * 24 * 3600
_EXPIRY_CLOSE = dtime(15, 30)


def _parse_dt(value: Any) -> Optional[datetime]:
    s = str(value or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(IST).replace(tzinfo=None)
    return dt


def _label(dt: datetime, multiday: bool) -> str:
    return dt.strftime("%d-%b %H:%M") if multiday else dt.strftime("%H:%M")


async def _load_spot_series(root: str, *, ref_spot: float, limit: int = 600) -> dict[datetime, float]:
    """Index 30-min close series from underlying_spot_candles, keyed by IST-naive
    datetime. option_premium_candles often lacks underlying_price, so this is the
    reliable per-bucket spot. Guards the known garbage prints (±20% of ref_spot)
    and dedups duplicate timestamps (keep freshest)."""
    out: dict[datetime, float] = {}
    if ref_spot <= 0:
        return out
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    SELECT DISTINCT ON (time) time, close
                    FROM underlying_spot_candles
                    WHERE underlying = :u AND interval = '30minute'
                    ORDER BY time DESC, synced_at DESC NULLS LAST
                    LIMIT :limit
                    """
                ),
                {"u": root, "limit": limit},
            )
            for row in result.fetchall():
                if row.time is None or row.close is None:
                    continue
                try:
                    close = float(row.close)
                except (TypeError, ValueError):
                    continue
                if abs(close - ref_spot) / ref_spot > 0.20:  # garbage-print guard
                    continue
                dt = row.time
                if dt.tzinfo is not None:
                    dt = dt.astimezone(IST).replace(tzinfo=None)
                out[dt] = close
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[gex-prog] spot series load failed for {root}: {exc}")
    return out


def _nearest_spot(spot_map: dict[datetime, float], keys: list[datetime], dt: datetime) -> Optional[float]:
    if not keys:
        return None
    if dt in spot_map:
        return spot_map[dt]
    i = bisect.bisect_left(keys, dt)
    best: Optional[datetime] = None
    for cand in (keys[i - 1] if i > 0 else None, keys[i] if i < len(keys) else None):
        if cand is None:
            continue
        if best is None or abs((cand - dt).total_seconds()) < abs((best - dt).total_seconds()):
            best = cand
    if best is not None and abs((best - dt).total_seconds()) <= 16 * 60:
        return spot_map[best]
    return None


def _looks_like_snapshot_rows(rows: list[dict[str, Any]]) -> bool:
    """LTP pseudo-candle signature: open == high == low == close on ~every row.

    option_history_service.load_candles silently falls back to
    atm_option_watchlist_snapshots when the table has no real candles for the
    contract; those fabricated rows carry a single LTP in all four fields.
    Real exchange candles virtually never do across a whole series."""
    if not rows:
        return False
    flat = sum(
        1 for r in rows
        if r.get("open") == r.get("high") == r.get("low") == r.get("close")
    )
    return flat >= max(1, int(0.9 * len(rows)))


async def _leg_candles(
    root: str, exp_date: date, strike: float, opt: str, *, interval: str, limit: int
) -> tuple[list[dict[str, Any]], str]:
    """(rows, source) for one option leg.

    Prefers the dense 3-minute feed resampled to 30 minutes: the front weekly
    often has ZERO native 30m rows (the chain-candle builder ingests 3m), so a
    direct 30m load silently degrades to LTP snapshot pseudo-candles presented
    as spec-grade history (2026-06-10 audit — the whole front-weekly
    progression was snapshot-derived)."""
    async def load(iv: str, lim: int) -> list[dict[str, Any]]:
        try:
            return await option_history_service.load_candles(
                underlying=root, expiry=exp_date, strike=float(strike),
                option_type=opt, interval=iv, limit=lim,
                allow_broker_refresh=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[gex-prog] load_candles {root} {strike}{opt} {iv}: {exc}")
            return []

    if interval == "30minute":
        rows3 = await load("3minute", 800)
        if len(rows3) >= 30 and not _looks_like_snapshot_rows(rows3):
            try:
                rows = option_history_service._aggregate_rows(list(rows3), 30)
            except Exception:  # noqa: BLE001
                rows = []
            if len(rows) >= 3:
                return rows, "resampled_3m"
    rows = await load(interval, limit)
    if rows and _looks_like_snapshot_rows(rows):
        return rows, "snapshot_ltp"
    return rows, f"candles_{interval}"


async def _strike_series(
    root: str, exp_date: date, strike: float, *, interval: str, limit: int
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """({iso_ts: {"ce_close","ce_oi","pe_close","pe_oi","spot"}}, leg sources)."""
    (ce, ce_src), (pe, pe_src) = await asyncio.gather(
        _leg_candles(root, exp_date, strike, "CE", interval=interval, limit=limit),
        _leg_candles(root, exp_date, strike, "PE", interval=interval, limit=limit),
    )
    out: dict[str, dict[str, Any]] = {}
    for row in ce:
        out.setdefault(str(row.get("time")), {})["ce_close"] = row.get("close")
        out[str(row.get("time"))]["ce_oi"] = row.get("oi")
        if row.get("underlying_price") is not None:
            out[str(row.get("time"))]["spot"] = row.get("underlying_price")
    for row in pe:
        out.setdefault(str(row.get("time")), {})["pe_close"] = row.get("close")
        out[str(row.get("time"))]["pe_oi"] = row.get("oi")
        if row.get("underlying_price") is not None:
            out[str(row.get("time"))].setdefault("spot", row.get("underlying_price"))
    sources = {}
    if ce:
        sources["CE"] = ce_src
    if pe:
        sources["PE"] = pe_src
    return out, sources


async def fetch_gex_progression(
    underlying: str,
    expiry: str,
    *,
    band: int = 3,
    interval: str = "30minute",
    limit: int = 80,
    timeout: float = 2.0,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    now = now or datetime.now(IST).replace(tzinfo=None)
    exp_date = None
    try:
        exp_date = date.fromisoformat(str(expiry)[:10])
    except ValueError:
        return {"available": False, "underlying": underlying, "expiry": expiry, "reason": "bad_expiry"}

    try:
        app_symbol = to_app_symbol(underlying) or underlying
    except Exception:  # noqa: BLE001
        app_symbol = underlying

    # ATM + band strikes from the live cached chain.
    try:
        cached = await asyncio.wait_for(
            option_chain_service.get_cached(app_symbol, expiry), timeout=timeout
        )
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        cached = None
    if not cached or not cached.get("entries"):
        return {"available": False, "underlying": underlying, "expiry": expiry, "reason": "no_chain"}

    strikes = sorted({
        float(e["strike"]) for e in cached.get("entries") or [] if e.get("strike") is not None
    })
    if len(strikes) < 3:
        return {"available": False, "underlying": underlying, "expiry": expiry, "reason": "thin_chain"}
    spot = float(cached.get("spot_price") or 0.0)
    atm = min(strikes, key=lambda k: abs(k - spot)) if spot else strikes[len(strikes) // 2]
    idx = strikes.index(atm)
    band_strikes = strikes[max(0, idx - band): idx + band + 1]

    root = underlying.upper()
    results = await asyncio.gather(
        *(_strike_series(root, exp_date, k, interval=interval, limit=limit) for k in band_strikes)
    )
    per_strike: dict[float, dict[str, dict[str, Any]]] = {}
    source_counts: dict[str, int] = {}
    for k, (s, leg_sources) in zip(band_strikes, results):
        per_strike[k] = s
        for src in leg_sources.values():
            source_counts[src] = source_counts.get(src, 0) + 1

    # Common time grid = union of all timestamps that carry data, last `limit` buckets.
    all_ts: set[str] = set()
    for s in per_strike.values():
        all_ts.update(s.keys())
    parsed = sorted(
        ((_parse_dt(ts), ts) for ts in all_ts if _parse_dt(ts) is not None),
        key=lambda kv: kv[0],
    )
    # RTH-only (09:15–15:30 IST): the snapshot fallback writes rows from 05:40
    # pre-market through 18:05 post-close; off-hours buckets repeat a frozen
    # LTP, flattening the GEX tail and distorting the bucket spacing
    # (2026-06-10 audit).
    parsed = [(dt, ts) for dt, ts in parsed if dtime(9, 15) <= dt.time() <= dtime(15, 30)]
    parsed = parsed[-limit:]
    if len(parsed) < 2:
        return {"available": False, "underlying": underlying, "expiry": expiry, "reason": "thin_history"}

    grid_dts = [dt for dt, _ in parsed]
    grid_keys = [ts for _, ts in parsed]
    multiday = grid_dts[0].date() != grid_dts[-1].date()
    times = [_label(dt, multiday) for dt in grid_dts]

    # underlying price per bucket (prefer ATM strike's spot, else any strike's).
    underlying_px: list[Optional[float]] = []
    for k in grid_keys:
        px = None
        atm_row = per_strike.get(atm, {}).get(k)
        if atm_row and atm_row.get("spot") is not None:
            px = atm_row["spot"]
        else:
            for s in per_strike.values():
                row = s.get(k)
                if row and row.get("spot") is not None:
                    px = row["spot"]
                    break
        underlying_px.append(px)

    # Fallback: fill missing per-bucket spot from underlying_spot_candles
    # (option candles frequently lack underlying_price -> would null the GEX).
    if any(px is None for px in underlying_px):
        spot_map = await _load_spot_series(root, ref_spot=spot)
        if spot_map:
            keys = sorted(spot_map)
            for i, dt in enumerate(grid_dts):
                if underlying_px[i] is None:
                    underlying_px[i] = _nearest_spot(spot_map, keys, dt)

    exp_instant = datetime.combine(exp_date, _EXPIRY_CLOSE)
    T_by_bucket = [max((exp_instant - dt).total_seconds() / _YEAR_SECONDS, 1e-6) for dt in grid_dts]

    series_by_strike: dict[float, dict[str, list]] = {}
    for k in band_strikes:
        s = per_strike.get(k, {})
        series_by_strike[k] = {
            "ce_close": [(s.get(key, {}) or {}).get("ce_close") for key in grid_keys],
            "ce_oi": [(s.get(key, {}) or {}).get("ce_oi") for key in grid_keys],
            "pe_close": [(s.get(key, {}) or {}).get("pe_close") for key in grid_keys],
            "pe_oi": [(s.get(key, {}) or {}).get("pe_oi") for key in grid_keys],
        }

    prog = compute_progression(series_by_strike, times, underlying_px, T_by_bucket)
    has_gex = any(g is not None for g in prog.get("gex", []))
    snapshot_legs = source_counts.get("snapshot_ltp", 0)
    total_legs = sum(source_counts.values())
    return {
        "available": bool(has_gex),
        "underlying": underlying,
        "expiry": str(expiry),
        "interval": interval,
        "atm": atm,
        # Per-leg candle provenance (resampled_3m / candles_30minute /
        # snapshot_ltp) — the spec expects real 30-min history; "degraded"
        # tells the client most of this grid is LTP pseudo-candles.
        "data_sources": source_counts,
        "degraded": bool(total_legs and snapshot_legs / total_legs > 0.5),
        "progression": prog,
    }
