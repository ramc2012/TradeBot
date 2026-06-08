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


async def _strike_series(
    root: str, exp_date: date, strike: float, *, interval: str, limit: int
) -> dict[str, dict[str, Any]]:
    """Return {iso_ts: {"ce_close","ce_oi","pe_close","pe_oi","spot"}} for one strike."""
    async def one(opt: str) -> list[dict[str, Any]]:
        try:
            return await option_history_service.load_candles(
                underlying=root, expiry=exp_date, strike=float(strike),
                option_type=opt, interval=interval, limit=limit,
                allow_broker_refresh=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[gex-prog] load_candles {root} {strike}{opt}: {exc}")
            return []

    ce, pe = await asyncio.gather(one("CE"), one("PE"))
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
    return out


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
    series = await asyncio.gather(
        *(_strike_series(root, exp_date, k, interval=interval, limit=limit) for k in band_strikes)
    )
    per_strike = dict(zip(band_strikes, series))

    # Common time grid = union of all timestamps that carry data, last `limit` buckets.
    all_ts: set[str] = set()
    for s in per_strike.values():
        all_ts.update(s.keys())
    parsed = sorted(
        ((_parse_dt(ts), ts) for ts in all_ts if _parse_dt(ts) is not None),
        key=lambda kv: kv[0],
    )
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
    return {
        "available": bool(has_gex),
        "underlying": underlying,
        "expiry": str(expiry),
        "interval": interval,
        "atm": atm,
        "progression": prog,
    }
