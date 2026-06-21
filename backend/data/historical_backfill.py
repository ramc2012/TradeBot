"""Automatic historical-data backfill.

Detects, per (instrument, interval), the gap between the desired coverage
(``data.backfill_config.DEFAULT_TARGETS``) and what is already stored, then pulls
only the missing windows and upserts them. Idempotent and resumable: every run is
bounded by a per-call budget and the underlying upserts are ON CONFLICT no-ops, so
the coordinator can be invoked repeatedly (on startup and on a poll loop) until
coverage is complete.

Sources (in priority order):
  - Upstox V3 historical-candle (active + expired-instruments), chunked to the
    V3 per-request limits, intraday clamped to 2022-01-01.
  - Fyers /history for the pre-2022 intraday slice (best-effort, if connected).
  - Daily candles to fill the remaining pre-2022 slice of a 5Y intraday target.

Storage:
  - spot / commodity → underlying_spot_candles
  - options          → option_premium_candles
all keyed (instrument_key, interval, time), matching the existing research-sync.
"""
from __future__ import annotations

import asyncio
import logging
import urllib.parse
from datetime import date, datetime, time, timedelta, timezone

import httpx
from sqlalchemy import text

from db.database import AsyncSessionLocal
from data.backfill_config import (
    DEFAULT_TARGETS,
    OPTIONS_INDEX_UNDERLYINGS,
    OPTIONS_STRIKE_BAND,
    UPSTOX_INTRADAY_FLOOR,
    CoverageTarget,
    targets_for,
)

logger = logging.getLogger(__name__)

UPSTOX_V3_BASE = "https://api.upstox.com/v3"
# Expired-instruments historical candles are ONLY served on V2 — the V3
# expired-instruments/historical-candle path returns an empty candle set
# (verified live 2026-06-20). Active instruments use V3; expired use V2.
UPSTOX_V2_BASE = "https://api.upstox.com/v2"
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

# Concurrent option contract fetch+store per batch. Kept modest: the live app +
# research-sync also hit Upstox, and over-concurrency causes ReadTimeouts (not 429s)
# that defer whole indices. 3 balances throughput vs. contention; fetch_upstox_candles
# retries on 429.
_OPTION_CONCURRENCY = 3

# Candle table per data class.
_SPOT_TABLE = "underlying_spot_candles"
_OPTION_TABLE = "option_premium_candles"


# ─────────────────────────────────────────────────────────────────────────────
# Interval / chunking helpers (Upstox V3)
# ─────────────────────────────────────────────────────────────────────────────
def _interval_to_v3(interval: str) -> tuple[str, int]:
    """Map an app interval to a Upstox V3 (unit, interval) pair."""
    mapping = {
        "1minute": ("minutes", 1),
        "5minute": ("minutes", 5),
        "15minute": ("minutes", 15),
        "30minute": ("minutes", 30),
        "day": ("days", 1),
    }
    if interval not in mapping:
        raise ValueError(f"Unsupported interval for backfill: {interval}")
    return mapping[interval]


def _max_request_days(unit: str, n: int) -> int:
    """Max date span Upstox V3 accepts in a single request for a unit/interval."""
    if unit == "minutes":
        return 28 if n <= 15 else 90       # 1 month for <=15min, 1 quarter for >15min
    if unit == "hours":
        return 90
    if unit == "days":
        return 3650                         # 1 decade
    return 90


def _fyers_resolution(interval: str) -> str:
    return {"1minute": "1", "5minute": "5", "15minute": "15",
            "30minute": "30", "day": "D"}.get(interval, "30")


def _chunks(start: date, end: date, span_days: int) -> list[tuple[date, date]]:
    out: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=span_days - 1), end)
        out.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return out


def _parse_ts(value) -> datetime:
    """Upstox returns ISO strings; Fyers (via adapter) returns ISO too. Accept epoch ints."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(int(value), UTC)
    s = str(value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt


# ─────────────────────────────────────────────────────────────────────────────
# Fetch — Upstox (active → V3; expired → V2)
# ─────────────────────────────────────────────────────────────────────────────
async def fetch_upstox_candles(
    token: str,
    instrument_key: str,
    interval: str,
    start: date,
    end: date,
    *,
    expired: bool = False,
    gap_seconds: float = 0.4,
) -> list[dict]:
    """Fetch chunked candles → chronological list of normalized dicts.

    Active instruments use the V3 endpoint (unit/interval path, 2022 floor).
    Expired instruments use the V2 endpoint (interval-string path) — V3 expired
    returns an empty candle set, verified live."""
    unit, n = _interval_to_v3(interval)
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    if expired:
        # V2 expired: /v2/expired-instruments/historical-candle/{key}/{interval}/{to}/{from}
        # V2 is lenient on window size; chunk conservatively to stay safe.
        span = 25 if unit == "minutes" and n <= 15 else 90
    else:
        span = _max_request_days(unit, n)
    rows: list[dict] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for c_start, c_end in _chunks(start, end, span):
            key = urllib.parse.quote(instrument_key, safe="")
            if expired:
                url = (f"{UPSTOX_V2_BASE}/expired-instruments/historical-candle/{key}"
                       f"/{interval}/{c_end.isoformat()}/{c_start.isoformat()}")
            else:
                url = (f"{UPSTOX_V3_BASE}/historical-candle/{key}/{unit}/{n}"
                       f"/{c_end.isoformat()}/{c_start.isoformat()}")
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 429:
                    await asyncio.sleep(2.0)
                    resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    logger.debug("upstox %s %s: HTTP %s %s", instrument_key, interval,
                                 resp.status_code, resp.text[:120])
                    await asyncio.sleep(gap_seconds)
                    continue
                raw = resp.json().get("data", {}).get("candles", []) or []
                for r in reversed(raw):  # Upstox returns newest-first
                    try:
                        rows.append({
                            "time": _parse_ts(r[0]),
                            "open": float(r[1]), "high": float(r[2]),
                            "low": float(r[3]), "close": float(r[4]),
                            "volume": int(r[5] or 0),
                            "oi": int(r[6]) if len(r) > 6 and r[6] is not None else 0,
                        })
                    except Exception:
                        continue
            except Exception as exc:
                logger.debug("upstox fetch error %s %s: %s", instrument_key, interval, exc)
            await asyncio.sleep(gap_seconds)
    return rows


async def fetch_fyers_candles(adapter, fyers_symbol: str, interval: str,
                              start: date, end: date, *, cont_flag: int = 1) -> list[dict]:
    """Fyers /history candles. cont_flag=1 = continuous (futures); pass cont_flag=0
    for OPTIONS and single (non-continuous) instruments."""
    if adapter is None:
        return []
    resolution = _fyers_resolution(interval)
    span = 90 if interval != "day" else 360
    rows: list[dict] = []
    for c_start, c_end in _chunks(start, end, span):
        try:
            candles = await adapter.get_historical_candles(
                fyers_symbol, resolution,
                c_start.strftime("%Y-%m-%d"), c_end.strftime("%Y-%m-%d"),
                cont_flag=cont_flag,
            )
            for cdl in candles:
                rows.append({
                    "time": _parse_ts(cdl["time"]),
                    "open": float(cdl["open"]), "high": float(cdl["high"]),
                    "low": float(cdl["low"]), "close": float(cdl["close"]),
                    "volume": int(cdl.get("volume", 0) or 0), "oi": 0,
                })
        except Exception as exc:
            logger.debug("fyers fetch error %s %s: %s", fyers_symbol, interval, exc)
        await asyncio.sleep(0.3)
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Coverage / gap detection
# ─────────────────────────────────────────────────────────────────────────────
async def covered_range(table: str, instrument_key: str, interval: str
                        ) -> tuple[date | None, date | None]:
    async with AsyncSessionLocal() as session:
        res = await session.execute(
            text(f"SELECT MIN(time) AS lo, MAX(time) AS hi FROM {table} "
                 f"WHERE instrument_key = :k AND interval = :i"),
            {"k": instrument_key, "i": interval},
        )
        row = res.fetchone()
    if not row or row.lo is None:
        return None, None
    return row.lo.astimezone(IST).date(), row.hi.astimezone(IST).date()


def compute_gaps(start: date, end: date,
                 cmin: date | None, cmax: date | None) -> list[tuple[date, date]]:
    """Missing windows within [start, end] given covered [cmin, cmax]."""
    if cmin is None:
        return [(start, end)]
    gaps: list[tuple[date, date]] = []
    if start < cmin:
        gaps.append((start, cmin - timedelta(days=1)))
    if end > cmax:
        gaps.append((cmax + timedelta(days=1), end))
    return [(s, e) for (s, e) in gaps if s <= e]


async def detect_interior_gaps(table: str, instrument_key: str, interval: str,
                               cmin: date, cmax: date, exchange: str,
                               min_run: int = 3) -> list[tuple[date, date]]:
    """Find INTERIOR holes inside [cmin, cmax]: runs of >= min_run consecutive
    expected trading days that have NO candles (catches failed chunks).

    Uses a run-length threshold (default 3) so holiday clusters / illiquid days
    don't churn: NSE/MCX never close 3+ consecutive trading days, but the calendar
    lacks pre-2026 holiday exceptions, so 1-2 day holiday runs would otherwise be
    mis-flagged. Genuine chunk failures span many days and are still caught.
    Cheap: one DISTINCT-day query."""
    from core.trading_calendar import trading_calendar

    if cmin is None or cmax is None or cmin >= cmax:
        return []
    async with AsyncSessionLocal() as session:
        res = await session.execute(
            text(f"SELECT DISTINCT (timezone('Asia/Kolkata', time))::date AS d "
                 f"FROM {table} WHERE instrument_key = :k AND interval = :i "
                 f"  AND time >= :lo AND time < :hi"),
            {"k": instrument_key, "i": interval,
             "lo": datetime.combine(cmin, time(0, 0), tzinfo=IST),
             "hi": datetime.combine(cmax + timedelta(days=1), time(0, 0), tzinfo=IST)},
        )
        covered_days = {row.d for row in res.fetchall()}

    runs: list[tuple[date, date]] = []
    run_start: date | None = None
    prev_missing: date | None = None
    day = cmin
    while day <= cmax:
        is_trading = day.weekday() < 5 and trading_calendar.has_exchange_session(exchange, day)
        if is_trading and day not in covered_days:
            if run_start is None:
                run_start = day
            prev_missing = day
        else:
            if run_start is not None:
                runs.append((run_start, prev_missing))
                run_start = None
        day += timedelta(days=1)
    if run_start is not None:
        runs.append((run_start, prev_missing))

    # Keep only runs spanning >= min_run trading days.
    out: list[tuple[date, date]] = []
    for s, e in runs:
        trading_days = sum(
            1 for n in range((e - s).days + 1)
            if (s + timedelta(days=n)).weekday() < 5
            and trading_calendar.has_exchange_session(exchange, s + timedelta(days=n))
        )
        if trading_days >= min_run:
            out.append((s, e))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Store
# ─────────────────────────────────────────────────────────────────────────────
async def store_spot_rows(instrument_key: str, underlying: str, interval: str,
                          source: str, candles: list[dict]) -> int:
    if not candles:
        return 0
    payload = [{
        "time": c["time"], "instrument_key": instrument_key, "underlying": underlying,
        "interval": interval, "open": c["open"], "high": c["high"], "low": c["low"],
        "close": c["close"], "volume": c["volume"], "oi": c["oi"], "source": source,
    } for c in candles]
    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            INSERT INTO underlying_spot_candles (
                time, instrument_key, underlying, interval, open, high,
                low, close, volume, oi, source, synced_at)
            VALUES (:time, :instrument_key, :underlying, :interval, :open, :high,
                    :low, :close, :volume, :oi, :source, NOW())
            ON CONFLICT (instrument_key, interval, time) DO UPDATE
            SET open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                close=EXCLUDED.close, volume=EXCLUDED.volume, oi=EXCLUDED.oi,
                source=EXCLUDED.source, synced_at=NOW()
        """), payload)
        await session.commit()
    return len(payload)


RISK_FREE_RATE = 0.06

# Pass-scoped cache of {utc_iso_ts: close} per (underlying, interval). The spot
# series is large (1-min NIFTY ≈ 92k rows); reloading it on every option-contract
# store was the dominant cost. Cleared at the start of each backfill pass (spot is
# written before options, so within a pass the series is effectively stable).
_SPOT_MAP_CACHE: dict[tuple[str, str], dict[str, float]] = {}


def _clear_spot_map_cache() -> None:
    _SPOT_MAP_CACHE.clear()


async def _load_spot_map(underlying: str, interval: str) -> dict[str, float]:
    """{utc_iso_ts: close} of the underlying spot at the given interval — used to
    align spot to each option candle for greeks (mirrors research-sync). Cached."""
    ck = (underlying, interval)
    cached = _SPOT_MAP_CACHE.get(ck)
    if cached is not None:
        return cached
    async with AsyncSessionLocal() as session:
        res = await session.execute(text("""
            SELECT time, close FROM underlying_spot_candles
            WHERE underlying = :u AND interval = :i AND close IS NOT NULL
        """), {"u": underlying, "i": interval})
        mapping = {row.time.astimezone(UTC).isoformat(): float(row.close)
                   for row in res.fetchall()}
    _SPOT_MAP_CACHE[ck] = mapping
    return mapping


def _option_greek_fields(option_type: str, premium: float, spot: float | None,
                         strike: float, expiry: date, ts: datetime) -> dict:
    """Compute iv/greeks/underlying_price/tte for one option candle. NULLs when
    spot is unavailable or the option has no time value (reuses research-sync math)."""
    from data.upstox_research_sync import (
        SECONDS_PER_YEAR, _implied_volatility, _option_greeks)

    expiry_dt = datetime.combine(expiry, time(15, 30), tzinfo=IST)
    tte = max((expiry_dt - ts.astimezone(IST)).total_seconds() / SECONDS_PER_YEAR, 0.0)
    out = {"iv": None, "delta": None, "gamma": None, "theta": None, "vega": None,
           "underlying_price": float(spot) if spot is not None else None,
           "time_to_expiry_years": tte if tte > 0 else None}
    if spot and tte > 0:
        iv = _implied_volatility(option_type=option_type, premium=float(premium),
                                 spot=float(spot), strike=float(strike),
                                 tte_years=tte, rate=RISK_FREE_RATE)
        if iv:
            d, g, t, v = _option_greeks(option_type=option_type, spot=float(spot),
                                        strike=float(strike), tte_years=tte,
                                        rate=RISK_FREE_RATE, sigma=iv)
            out.update(iv=iv, delta=d, gamma=g, theta=t, vega=v)
    return out


async def store_option_rows(meta: dict, interval: str, candles: list[dict],
                            source: str = "upstox_expired") -> int:
    if not candles:
        return 0
    # Align spot at the SAME interval so greeks are computable (spot must already
    # be backfilled). On-conflict refreshes greeks too, so a later pass fills NULLs
    # once spot lands.
    spot_map = await _load_spot_map(meta["underlying"], interval)
    payload = []
    for c in candles:
        ts = c["time"]
        spot = spot_map.get(ts.astimezone(UTC).isoformat())
        g = _option_greek_fields(meta["option_type"], c["close"], spot,
                                 float(meta["strike"]), meta["expiry"], ts)
        payload.append({
            "time": ts, "instrument_key": meta["instrument_key"],
            "trading_symbol": meta.get("trading_symbol"), "underlying": meta["underlying"],
            "market": meta.get("market", "NSE"), "expiry": meta["expiry"],
            "strike": meta["strike"], "option_type": meta["option_type"], "interval": interval,
            "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"],
            "volume": c["volume"], "oi": c["oi"], "source": source, **g,
        })
    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            INSERT INTO option_premium_candles (
                time, instrument_key, trading_symbol, underlying, market, expiry,
                strike, option_type, interval, open, high, low, close, volume, oi,
                iv, delta, gamma, theta, vega, underlying_price, source, synced_at,
                time_to_expiry_years)
            VALUES (:time, :instrument_key, :trading_symbol, :underlying, :market,
                    :expiry, :strike, :option_type, :interval, :open, :high, :low,
                    :close, :volume, :oi, :iv, :delta, :gamma, :theta, :vega,
                    :underlying_price, :source, NOW(), :time_to_expiry_years)
            ON CONFLICT (instrument_key, interval, time) DO UPDATE
            SET open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                close=EXCLUDED.close, volume=EXCLUDED.volume, oi=EXCLUDED.oi,
                iv=EXCLUDED.iv, delta=EXCLUDED.delta, gamma=EXCLUDED.gamma,
                theta=EXCLUDED.theta, vega=EXCLUDED.vega,
                underlying_price=EXCLUDED.underlying_price,
                time_to_expiry_years=EXCLUDED.time_to_expiry_years,
                synced_at=NOW()
        """), payload)
        await session.commit()
    return len(payload)


# ─────────────────────────────────────────────────────────────────────────────
# Backfill one spot/commodity instrument against one target
# ─────────────────────────────────────────────────────────────────────────────
async def _backfill_spotlike(token, fyers_adapter, *, instrument_key, underlying,
                             fyers_symbol, source, target: CoverageTarget,
                             today: date, exchange: str = "NSE") -> dict:
    desired_start, desired_end = target.window(today)
    cmin, cmax = await covered_range(_SPOT_TABLE, instrument_key, target.interval)
    stored = 0
    notes: list[str] = []

    gaps = compute_gaps(desired_start, desired_end, cmin, cmax)
    # Interior holes (failed chunks inside the covered range) — solid history needs these.
    if cmin is not None and cmax is not None:
        interior = await detect_interior_gaps(_SPOT_TABLE, instrument_key,
                                              target.interval, cmin, cmax, exchange)
        if interior:
            notes.append(f"interior:{len(interior)}")
            gaps = gaps + interior

    for g_start, g_end in gaps:
        # Upstox intraday slice (clamped to floor)
        ux_start = g_start if target.interval == "day" else max(g_start, UPSTOX_INTRADAY_FLOOR)
        if ux_start <= g_end:
            rows = await fetch_upstox_candles(token, instrument_key, target.interval,
                                              ux_start, g_end)
            stored += await store_spot_rows(instrument_key, underlying,
                                            target.interval, source, rows)
        # Pre-2022 intraday slice → Fyers, then daily fallback
        if target.interval != "day" and g_start < UPSTOX_INTRADAY_FLOOR:
            pre_end = min(g_end, UPSTOX_INTRADAY_FLOOR - timedelta(days=1))
            got_fyers = 0
            if target.extend_with_fyers and fyers_symbol and fyers_adapter is not None:
                frows = await fetch_fyers_candles(fyers_adapter, fyers_symbol,
                                                  target.interval, g_start, pre_end)
                got_fyers = await store_spot_rows(instrument_key, underlying,
                                                  target.interval, "fyers_spot", frows)
                stored += got_fyers
                if got_fyers:
                    notes.append(f"fyers:{got_fyers}")
            if target.extend_with_daily and got_fyers == 0:
                drows = await fetch_upstox_candles(token, instrument_key, "day",
                                                   g_start, pre_end)
                d = await store_spot_rows(instrument_key, underlying, "day",
                                          source, drows)
                if d:
                    notes.append(f"daily<2022:{d}")
                    stored += d
    return {"instrument_key": instrument_key, "interval": target.interval,
            "stored": stored, "notes": notes}


# ─────────────────────────────────────────────────────────────────────────────
# Instrument enumeration
# ─────────────────────────────────────────────────────────────────────────────
async def _index_spot_instruments() -> list[dict]:
    """Index underlyings with spot_instrument_key + fyers symbol."""
    from market_data.symbols import to_fyers_symbol
    from analysis.instruments import INDEX_INSTRUMENT_KEYS
    out: list[dict] = []
    async with AsyncSessionLocal() as session:
        res = await session.execute(text(
            "SELECT symbol, spot_instrument_key FROM fo_underlying_catalog "
            "WHERE kind = 'INDEX' AND spot_instrument_key IS NOT NULL"))
        db_rows = {r.symbol: r.spot_instrument_key for r in res.fetchall()}
    fyers_map = {"NIFTY": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
                 "FINNIFTY": "NSE:FINNIFTY-INDEX", "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
                 "SENSEX": "BSE:SENSEX-INDEX"}
    for sym in OPTIONS_INDEX_UNDERLYINGS:
        key = db_rows.get(sym) or INDEX_INSTRUMENT_KEYS.get(sym)
        if not key:
            continue
        out.append({"underlying": sym, "instrument_key": key,
                    "fyers_symbol": fyers_map.get(sym)})
    return out


async def _commodity_instruments() -> list[dict]:
    """MCX front-month futures resolved to Upstox instrument keys."""
    from market_data.commodity_runtime_history import DEFAULT_COMMODITY_FUTURES
    from market_data.upstox_commodity import resolve_upstox_mcx_future
    out: list[dict] = []
    for root, symbol in DEFAULT_COMMODITY_FUTURES.items():
        try:
            resolved = await resolve_upstox_mcx_future(symbol)
        except Exception as exc:
            logger.debug("commodity resolve failed %s: %s", symbol, exc)
            resolved = None
        if resolved and resolved.get("instrument_key"):
            out.append({"underlying": root,
                        "instrument_key": resolved["instrument_key"],
                        "fyers_symbol": None})
    return out


async def _expiry_option_count(underlying: str, expiry: date, interval: str) -> int:
    """Distinct option contracts already stored for (underlying, expiry, interval).
    Used to skip re-discovery of completed expiries — one cheap indexed query."""
    async with AsyncSessionLocal() as session:
        res = await session.execute(text(
            "SELECT COUNT(DISTINCT instrument_key) AS n FROM option_premium_candles "
            "WHERE underlying = :u AND expiry = :e AND interval = :i"),
            {"u": underlying, "e": expiry, "i": interval})
        row = res.fetchone()
    return int(row.n) if row and row.n is not None else 0


async def _db_spot_near(underlying: str, ref_date: date) -> float | None:
    """ATM-centering spot close near ref_date, read from the already-backfilled
    spot series in the DB — avoids slow/timeout-prone live Upstox spot calls."""
    async with AsyncSessionLocal() as session:
        res = await session.execute(text("""
            SELECT close FROM underlying_spot_candles
            WHERE underlying = :u AND interval IN ('30minute','day','1minute')
              AND close IS NOT NULL AND time >= :lo AND time < :hi
            ORDER BY time LIMIT 1
        """), {"u": underlying,
               "lo": datetime.combine(ref_date - timedelta(days=7), time(0, 0), tzinfo=IST),
               "hi": datetime.combine(ref_date + timedelta(days=14), time(0, 0), tzinfo=IST)})
        row = res.fetchone()
    return float(row.close) if row and row.close is not None else None


async def _index_option_contracts_for_expiry(bt, underlying: str, expiry: date,
                                             band: int, ref_date: date) -> list[dict]:
    """Discover ATM±band CE/PE contracts for one index expiry directly from Upstox
    expired-instruments (no dependency on research-sync). Returns option-meta dicts."""
    from analysis.instruments import STRIKE_STEPS, get_atm_strike

    # ATM center from the DB spot (fast); only fall back to the live API if absent.
    spot = await _db_spot_near(underlying, ref_date)
    if not spot:
        try:
            spot, _ = await asyncio.wait_for(bt._get_spot_reference(underlying, ref_date), timeout=10)
        except (asyncio.TimeoutError, Exception):
            spot = None
    if not spot:
        return []
    step = STRIKE_STEPS.get(underlying, 50)
    atm = get_atm_strike(float(spot), step)
    lo, hi = atm - band * step, atm + band * step

    # Hard timeout so a hung discovery call fails fast instead of stalling the pass.
    try:
        contracts = await asyncio.wait_for(
            bt._fetch_expired_contracts(underlying, expiry), timeout=20)
    except asyncio.TimeoutError:
        logger.debug("expired-contracts discovery timed out %s %s", underlying, expiry)
        return []
    if getattr(bt, "_upstox_plan_restricted", False):
        return []
    out: list[dict] = []
    for c in contracts:
        strike = c.get("strike_price")
        if strike is None:
            strike = c.get("strike")
        otype = c.get("instrument_type")
        key = c.get("instrument_key")
        if strike is None or not key or otype not in ("CE", "PE"):
            continue
        if not (lo <= float(strike) <= hi):
            continue
        out.append({
            "instrument_key": key,                       # use Upstox key VERBATIM
            "trading_symbol": c.get("trading_symbol"),
            "underlying": underlying,
            "market": c.get("exchange") or ("BSE" if underlying in ("SENSEX", "BANKEX") else "NSE"),
            "expiry": expiry, "strike": float(strike), "option_type": otype,
        })
    return out


async def _backfill_index_options(token: str, *, today: date, band: int,
                                  intervals: tuple[str, ...],
                                  max_contracts: int,
                                  max_expiries: int = 2) -> list[dict]:
    """Self-contained index-option backfill. Enumerates expiries + ATM band per
    index directly from Upstox expired-instruments, fetches expired candles (V2)
    per interval with gap detection, and stores greeks-enriched rows.

    Bounded per pass by max_contracts (newest expiries first) and resumable: gap
    detection skips contracts already stored, so the daemon completes over passes.
    Note: Upstox expired option history only reaches ~Oct 2024 (~1.7Y), so the 5Y
    30-min target is clamped to what the broker serves."""
    from analysis.backtest import MACDBacktester
    from analysis.instruments import (INDEX_INSTRUMENT_KEYS, get_first_trading_day_after,
                                      is_valid_index_expiry)

    bt = MACDBacktester(access_token=token)
    bt._spot_series_from = today - timedelta(days=5 * 365 + 7)
    bt._spot_series_to = today
    # Pre-seed underlying metadata from the known index keys so discovery never hits
    # MACDBacktester._search_instruments — that path ReadTimeouts ~30s for several
    # indices (BANKNIFTY/FINNIFTY/SENSEX) and the static fallback only runs AFTER the
    # (failing) search, so the timeout would otherwise defer those indices every pass.
    for u in OPTIONS_INDEX_UNDERLYINGS:
        key = INDEX_INSTRUMENT_KEYS.get(u)
        if key:
            bt._underlying_meta_cache[u.upper()] = {
                "spot_instrument_key": key, "underlying_key": key,
                "segment": "BSE_INDEX" if u in ("SENSEX", "BANKEX") else "NSE_INDEX",
                "display_name": u,
            }

    # Phase 1: gather expiries per underlying (fast now that keys are pre-seeded;
    # inline-retry handles transient expiries-API ReadTimeouts).
    floor_5y = today - timedelta(days=5 * 365)
    exp_data: dict[str, tuple] = {}
    for underlying in OPTIONS_INDEX_UNDERLYINGS:
        all_exp = []
        for attempt in range(3):
            try:
                bt._expiry_cache.pop(underlying.upper(), None)
                all_exp = await asyncio.wait_for(
                    bt._fetch_expiry_dates(underlying), timeout=12)
                if all_exp:
                    break
            except (asyncio.TimeoutError, Exception) as exc:
                logger.debug("expiry fetch attempt %d failed %s: %s", attempt, underlying, exc)
            await asyncio.sleep(1.0 * (attempt + 1))
        if not all_exp:
            continue
        monthly, prev_map = bt._select_monthly_expiries(all_exp, floor_5y, today)
        exp_data[underlying] = (all_exp, monthly, set(monthly), prev_map)

    results: list[dict] = []
    processed = 0
    # Phase 2: interval-OUTER so every index gets the lighter 30-min baseline before
    # the heavy 1-min pull (which alone is millions of rows for NIFTY) — otherwise a
    # single heavy index would consume the whole pass and starve the others.
    for interval in intervals:
        for underlying in OPTIONS_INDEX_UNDERLYINGS:
            if processed >= max_contracts:
                break
            if underlying not in exp_data:
                continue
            all_exp, monthly, monthly_set, prev_map = exp_data[underlying]
            if interval == "1minute":
                win_start = today - timedelta(days=365)
                exps = sorted([e for e in all_exp if win_start <= e <= today], reverse=True)
            else:  # 30minute: monthly expiries over the full available range
                exps = sorted(monthly, reverse=True)
            # "Current contract" scope: only the most recent N expiries per index
            # (newest first). Keeps options fast; deeper history is broker-bound.
            if max_expiries > 0:
                exps = exps[:max_expiries]

            for expiry in exps:
                if processed >= max_contracts:
                    break
                if not is_valid_index_expiry(underlying, expiry):
                    continue
                # Skip the expensive discovery API call for already-backfilled expiries.
                if await _expiry_option_count(underlying, expiry, interval) >= 2 * band:
                    continue
                prev = prev_map.get(expiry) if expiry in monthly_set else None
                ref_date = get_first_trading_day_after(prev or (expiry - timedelta(days=30)))
                try:
                    contracts = await _index_option_contracts_for_expiry(
                        bt, underlying, expiry, band, ref_date)
                except Exception as exc:
                    logger.debug("contract discovery failed %s %s: %s", underlying, expiry, exc)
                    continue

                c_start = expiry - timedelta(days=45)
                c_end = min(expiry, today)
                # Contracts that actually need work (cheap covered-range checks);
                # already-covered ones must NOT consume budget.
                pending: list[dict] = []
                for meta in contracts:
                    if processed + len(pending) >= max_contracts:
                        break
                    cmin, cmax = await covered_range(_OPTION_TABLE, meta["instrument_key"], interval)
                    gaps = compute_gaps(c_start, c_end, cmin, cmax)
                    if gaps:
                        pending.append({**meta, "_gaps": gaps})

                async def _fetch_store(meta: dict, interval=interval) -> tuple[dict, int]:
                    stored = 0
                    for g_start, g_end in meta["_gaps"]:
                        rows = await fetch_upstox_candles(
                            token, meta["instrument_key"], interval,
                            g_start, g_end, expired=True)
                        stored += await store_option_rows(meta, interval, rows)
                    return meta, stored

                for i in range(0, len(pending), _OPTION_CONCURRENCY):
                    batch = pending[i:i + _OPTION_CONCURRENCY]
                    for meta, stored in await asyncio.gather(*[_fetch_store(m) for m in batch]):
                        processed += 1
                        if stored:
                            results.append({"underlying": underlying, "expiry": expiry.isoformat(),
                                            "interval": interval, "strike": meta["strike"],
                                            "option_type": meta["option_type"], "stored": stored})
    return results


async def _backfill_commodity(token: str, fyers_adapter, *, today: date) -> list[dict]:
    """Commodity history. Upstox does NOT serve expired MCX futures (active-only
    master; expired-instruments unsupported for MCX, confirmed), so multi-year
    depth comes from Fyers CONTINUOUS futures (cont_flag=1) under a synthetic
    per-root rolling key. The current Upstox front-month is also pulled (its own
    lifetime) for high-fidelity recent data. Fyers path is best-effort and skips
    cleanly when Fyers is not connected."""
    from market_data.commodity_runtime_history import DEFAULT_COMMODITY_FUTURES
    from market_data.upstox_commodity import resolve_upstox_mcx_future

    results: list[dict] = []
    for root, symbol in DEFAULT_COMMODITY_FUTURES.items():
        # 1) Upstox active front-month (real key, recent depth only).
        try:
            resolved = await resolve_upstox_mcx_future(symbol)
        except Exception as exc:
            logger.debug("commodity resolve failed %s: %s", symbol, exc)
            resolved = None
        if resolved and resolved.get("instrument_key"):
            for target in targets_for("commodity"):
                r = await _backfill_spotlike(
                    token, None, instrument_key=resolved["instrument_key"],
                    underlying=root, fyers_symbol=None,
                    source="commodity_broker_history", target=target,
                    today=today, exchange="MCX")
                r["root"] = root
                r["track"] = "upstox_frontmonth"
                results.append(r)

        # 2) Fyers continuous multi-year (cont_flag=1) under a stable rolling key.
        if fyers_adapter is not None:
            cont_key = f"MCX_CONT|{root}"
            for target in targets_for("commodity"):
                d_start, d_end = target.window(today)
                cmin, cmax = await covered_range(_SPOT_TABLE, cont_key, target.interval)
                stored = 0
                for g_start, g_end in compute_gaps(d_start, d_end, cmin, cmax):
                    rows = await fetch_fyers_candles(
                        fyers_adapter, symbol, target.interval, g_start, g_end)
                    stored += await store_spot_rows(
                        cont_key, root, target.interval, "fyers_mcx_cont", rows)
                results.append({"instrument_key": cont_key, "interval": target.interval,
                                "stored": stored, "root": root, "track": "fyers_continuous"})
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Coordinator
# ─────────────────────────────────────────────────────────────────────────────
async def run_auto_backfill_once(
    token: str | None = None,
    *,
    today: date | None = None,
    do_spot: bool = True,
    do_commodity: bool = True,
    do_options: bool = True,
    max_option_contracts: int = 300,
) -> dict:
    """One bounded, resumable backfill pass. Returns a summary dict.

    Owns ALL three data classes directly (no dependency on the research-sync
    daemon): spot+index, index options (ATM band, expired-instruments, both
    intervals, greeks-enriched), and commodity. Each pass is bounded and
    idempotent — re-runs only fetch missing windows.
    """
    from api.routers.auth import get_active_adapter, get_broker_token

    today = today or datetime.now(IST).date()
    token = token or (get_broker_token("upstox") or "")
    if not token:
        return {"status": "no_token", "detail": "Upstox token unavailable"}
    fyers_adapter = get_active_adapter("fyers")
    _clear_spot_map_cache()  # fresh spot series per pass (spot is written first)

    summary: dict = {"status": "ok", "today": today.isoformat(),
                     "spot": [], "commodity": [], "options": []}

    # Order: spot first (greeks need it), then OPTIONS (research priority — must not
    # be starved behind the long commodity pull), then commodity last.
    if do_spot:
        instruments = await _index_spot_instruments()
        for inst in instruments:
            for target in targets_for("spot"):
                try:
                    r = await _backfill_spotlike(
                        token, fyers_adapter, instrument_key=inst["instrument_key"],
                        underlying=inst["underlying"], fyers_symbol=inst["fyers_symbol"],
                        source="upstox_spot", target=target, today=today, exchange="NSE")
                except Exception as exc:
                    logger.warning("spot backfill failed %s %s: %s",
                                   inst["underlying"], target.interval, exc)
                    r = {"instrument_key": inst["instrument_key"],
                         "interval": target.interval, "stored": 0, "error": str(exc)}
                summary["spot"].append(r)

    if do_options:
        from core.config import settings as _settings
        intervals = tuple(t.interval for t in targets_for("options"))
        try:
            summary["options"] = await _backfill_index_options(
                token, today=today, band=OPTIONS_STRIKE_BAND,
                intervals=intervals, max_contracts=max_option_contracts,
                max_expiries=_settings.AUTO_BACKFILL_OPTION_MAX_EXPIRIES)
        except Exception as exc:
            logger.warning("options backfill pass failed: %s", exc)
            summary["options_error"] = str(exc)

    if do_commodity:
        # Forward-capture: archive today's active MCX contracts so their keys are
        # available for future backfill before they drop off at expiry.
        try:
            from market_data.upstox_commodity import snapshot_mcx_active_contracts
            summary["mcx_snapshot"] = await snapshot_mcx_active_contracts()
        except Exception as exc:
            logger.debug("mcx snapshot skipped: %s", exc)
        try:
            summary["commodity"] = await _backfill_commodity(token, fyers_adapter, today=today)
        except Exception as exc:
            logger.warning("commodity backfill pass failed: %s", exc)
            summary["commodity_error"] = str(exc)

    summary["totals"] = {
        "spot": sum(r["stored"] for r in summary["spot"]),
        "commodity": sum(r.get("stored", 0) for r in summary["commodity"]),
        "options": sum(r["stored"] for r in summary["options"]),
    }
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# Coverage report (for the status endpoint / CLI)
# ─────────────────────────────────────────────────────────────────────────────
async def coverage_report(today: date | None = None) -> dict:
    """For each target × tracked instrument, report covered vs desired range."""
    today = today or datetime.now(IST).date()
    report: dict = {"today": today.isoformat(), "targets": []}
    spot_instr = await _index_spot_instruments()
    commo_instr = await _commodity_instruments()

    for target in DEFAULT_TARGETS:
        d_start, d_end = target.window(today)
        entry = {"data_class": target.data_class, "interval": target.interval,
                 "desired_start": d_start.isoformat(), "desired_end": d_end.isoformat(),
                 "instruments": []}
        if target.data_class in ("spot", "commodity"):
            insts = spot_instr if target.data_class == "spot" else commo_instr
            for inst in insts:
                cmin, cmax = await covered_range(_SPOT_TABLE, inst["instrument_key"],
                                                 target.interval)
                gaps = compute_gaps(
                    d_start if target.interval == "day" else max(d_start, UPSTOX_INTRADAY_FLOOR),
                    d_end, cmin, cmax)
                entry["instruments"].append({
                    "underlying": inst["underlying"],
                    "covered_start": cmin.isoformat() if cmin else None,
                    "covered_end": cmax.isoformat() if cmax else None,
                    "missing_windows": [[s.isoformat(), e.isoformat()] for s, e in gaps],
                })
            if target.data_class == "commodity":
                # Multi-year commodity depth lives under the synthetic continuous
                # key (Fyers cont_flag=1), NOT the front-month key — report it so the
                # desk sees real coverage instead of phantom 5Y front-month gaps.
                for inst in insts:
                    cont_key = f"MCX_CONT|{inst['underlying']}"
                    ck_min, ck_max = await covered_range(_SPOT_TABLE, cont_key, target.interval)
                    cont_gaps = compute_gaps(d_start, d_end, ck_min, ck_max)
                    entry["instruments"].append({
                        "underlying": inst["underlying"], "track": "fyers_continuous",
                        "covered_start": ck_min.isoformat() if ck_min else None,
                        "covered_end": ck_max.isoformat() if ck_max else None,
                        "missing_windows": [[s.isoformat(), e.isoformat()] for s, e in cont_gaps],
                    })
                entry["note"] = ("Two tracks: 'fyers_continuous' (MCX_CONT|<root>, "
                                 "cont_flag=1) carries multi-year depth; the front-month "
                                 "Upstox key covers recent only. Upstox serves NO expired "
                                 "MCX history, so front-month 5Y 'missing' is expected.")
        elif target.data_class == "options":
            # Per-underlying option coverage from the candle table directly.
            async with AsyncSessionLocal() as session:
                res = await session.execute(text("""
                    SELECT underlying,
                           COUNT(*) AS rows,
                           COUNT(DISTINCT instrument_key) AS contracts,
                           MIN(expiry) AS first_expiry, MAX(expiry) AS last_expiry,
                           SUM(CASE WHEN iv IS NOT NULL THEN 1 ELSE 0 END) AS greek_rows
                    FROM option_premium_candles
                    WHERE interval = :i AND underlying = ANY(:u)
                    GROUP BY underlying
                """), {"i": target.interval, "u": list(OPTIONS_INDEX_UNDERLYINGS)})
                for row in res.fetchall():
                    entry["instruments"].append({
                        "underlying": row.underlying, "rows": int(row.rows),
                        "contracts": int(row.contracts),
                        "first_expiry": row.first_expiry.isoformat() if row.first_expiry else None,
                        "last_expiry": row.last_expiry.isoformat() if row.last_expiry else None,
                        "greek_pct": round(100.0 * row.greek_rows / row.rows, 1) if row.rows else 0.0,
                    })
            entry["note"] = ("Index ATM±%d band, expired-instruments (V2). Upstox "
                             "expired option history reaches ~Oct 2024 (~1.7Y); the "
                             "5Y/30m target is clamped to what the broker serves."
                             % OPTIONS_STRIKE_BAND)
        report["targets"].append(entry)
    return report
