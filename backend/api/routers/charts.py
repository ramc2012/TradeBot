"""OHLC chart module with trade-history overlays.

Returns spot/futures OHLC for any configured instrument plus the four
indicators traders verify strategies against (MACD, RSI, BB, EMA50) and
the trade entry/exit markers from S1 / S2 / Commodity / CBE / Directional.

Spot/futures is the cleanest series to render across the mixed F&O
universe (indices, MCX commodities, NSE stocks). Option-trade markers
carry their strike + premium + P&L in the tooltip so a trader can verify
a strategy fired where it was supposed to without reading two charts.
"""
from __future__ import annotations

import math
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from loguru import logger

from analysis.macd_engine import compute_macd
from analytics.technicals import compute_rsi
from db.database import AsyncSessionLocal
from market_data import index_band_guard


router = APIRouter(prefix="/api/charts", tags=["charts"])

# Secondary continuity net for the chart serve path. After the absolute /
# prior-session band pass (index_band_guard), a surviving bar whose worst leg
# deviates more than this fraction from the robust session center (median of
# surviving closes) is dropped. This catches the same-2x contamination family
# (e.g. a 48545 close on ~24000 NIFTY) even when the ±20% reference could not be
# seeded because of a DB blip — 48545 sits *inside* NIFTY's wide absolute band
# so the band alone would pass it. 0.30 is far outside any real intraday (or
# even circuit-halt) move for a broad index, so no valid bar is ever dropped.
_CHART_CONTINUITY_TOL = 0.30

IST = ZoneInfo("Asia/Kolkata")

# Server-side response cache. The chart endpoint pulls 17 days of 1-min
# bars (~7k rows) and aggregates + computes 8 indicator series — about
# 100-200ms per call. The frontend polls every 30s. Without a cache,
# multiple tabs open on the same instrument multiply that load on the
# backend's async loop for no benefit. 25s TTL is short enough that
# the user sees a fresh bar within one polling cycle.
_OHLC_CACHE: dict[tuple[str, str, int], tuple[float, dict[str, Any]]] = {}
_OHLC_CACHE_TTL = 25.0

# Strategy color palette matches what the frontend uses for trade markers.
STRATEGY_COLORS: dict[str, str] = {
    "s1": "#22d3ee",          # cyan
    "s2": "#3b82f6",          # blue
    "commodity": "#f59e0b",   # amber
    "cbe": "#a855f7",         # violet
    "directional": "#10b981", # emerald
}

SUPPORTED_TIMEFRAMES = ("15minute", "30minute", "60minute")

# Strategy-label classifier — matches the labels we already write to
# agent_signals.strategy_label and runtime/portfolio/events.jsonl.strategy.
def _classify_strategy(label: str | None) -> str | None:
    raw = str(label or "").lower()
    if "strategy 1" in raw or "s1" in raw or raw == "macd_strategy":
        return "s1"
    if "strategy 2" in raw or "s2" in raw or raw == "index_mp_strategy":
        return "s2"
    if "commodity" in raw or "mcx" in raw:
        return "commodity"
    if "cbe" in raw or "compression" in raw:
        return "cbe"
    if "directional" in raw:
        return "directional"
    return None


# ── Universe ────────────────────────────────────────────────────────────────

_INDEX_UNDERLYINGS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX", "NIFTYNXT50"}
_COMMODITY_UNDERLYINGS = {"CRUDEOIL", "GOLD", "SILVERM", "NATURALGAS"}


@router.get("/universe")
async def chart_universe() -> dict[str, Any]:
    """Return all instruments the chart module can render, with metadata.

    Indices + commodities ship in a fixed list (spot/futures series is
    always available). Stocks come from atm_option_watchlist_snapshots —
    only those we actually scan get a chart so the dropdown stays
    meaningful. Each row carries `traded_today` so the UI can show a
    chip when a strategy actually opened a position on it today.
    """
    async with AsyncSessionLocal() as session:
        stock_rows = await session.execute(
            text(
                """
                SELECT underlying
                FROM atm_option_watchlist_snapshots
                WHERE kind = 'STOCK'
                  AND time >= NOW() - INTERVAL '3 days'
                GROUP BY underlying
                ORDER BY underlying ASC
                """
            )
        )
        stock_symbols = [row[0] for row in stock_rows.fetchall() if row[0]]

        today_rows = await session.execute(
            text(
                """
                SELECT DISTINCT underlying
                FROM agent_signals
                WHERE created_at >= (CURRENT_DATE AT TIME ZONE 'Asia/Kolkata')
                  AND status IN ('open', 'closed', 'entered')
                """
            )
        )
        traded_today = {row[0] for row in today_rows.fetchall() if row[0]}

    items: list[dict[str, Any]] = []
    for sym in sorted(_INDEX_UNDERLYINGS):
        items.append(
            {"underlying": sym, "kind": "INDEX", "traded_today": sym in traded_today}
        )
    for sym in sorted(_COMMODITY_UNDERLYINGS):
        items.append(
            {"underlying": sym, "kind": "COMMODITY", "traded_today": sym in traded_today}
        )
    for sym in stock_symbols:
        if sym in _INDEX_UNDERLYINGS or sym in _COMMODITY_UNDERLYINGS:
            continue
        items.append(
            {"underlying": sym, "kind": "STOCK", "traded_today": sym in traded_today}
        )
    return {
        "instruments": items,
        "kinds": ["INDEX", "COMMODITY", "STOCK"],
        "strategy_colors": STRATEGY_COLORS,
        "supported_timeframes": list(SUPPORTED_TIMEFRAMES),
    }


# ── OHLC + indicators + trades ───────────────────────────────────────────────


def _compute_ema(values: list[float], period: int) -> list[float | None]:
    n = len(values)
    out: list[float | None] = [None] * n
    if n < period:
        return out
    sma = sum(values[:period]) / period
    out[period - 1] = sma
    k = 2.0 / (period + 1)
    prev = sma
    for i in range(period, n):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def _compute_bollinger(values: list[float], period: int = 20, num_std: float = 2.0) -> tuple[
    list[float | None], list[float | None], list[float | None]
]:
    n = len(values)
    upper: list[float | None] = [None] * n
    middle: list[float | None] = [None] * n
    lower: list[float | None] = [None] * n
    if n < period:
        return upper, middle, lower
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        std = math.sqrt(variance)
        middle[i] = mean
        upper[i] = mean + num_std * std
        lower[i] = mean - num_std * std
    return upper, middle, lower


def _compute_kama(
    values: list[float],
    er_period: int = 10,
    fast: int = 2,
    slow: int = 30,
) -> list[float | None]:
    """Kaufman's Adaptive Moving Average.

    KAMA hugs price in clean trends and flattens in chop by scaling its
    smoothing constant with the efficiency ratio — directional change over
    summed absolute change across ``er_period`` bars. Seeded with the SMA of
    the first ``er_period`` closes; values before the warm-up are None so the
    array stays index-aligned with the bars (same contract as the EMA / BB
    helpers above).
    """
    n = len(values)
    out: list[float | None] = [None] * n
    if n <= er_period:
        return out
    fast_sc = 2.0 / (fast + 1)
    slow_sc = 2.0 / (slow + 1)
    prev = sum(values[:er_period]) / er_period
    out[er_period - 1] = prev
    for i in range(er_period, n):
        change = abs(values[i] - values[i - er_period])
        volatility = sum(
            abs(values[j] - values[j - 1]) for j in range(i - er_period + 1, i + 1)
        )
        er = (change / volatility) if volatility > 0 else 0.0
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        prev = prev + sc * (values[i] - prev)
        out[i] = prev
    return out


def _is_in_market_session(ts_ist: datetime, market: str) -> bool:
    """Drop out-of-session bars before they make it into the chart.

    underlying_spot_candles can contain stray after-hours / pre-open 1-min
    rows (synthetic ticks, late broker prints, post-close session writes).
    Aggregating those into 30-min buckets produces degenerate degenerate
    O/H/L/C-all-equal bars that show up as visual gaps on the chart.
    Filter to the relevant exchange's regular session before bucketing.
    """
    # Weekends — exchanges closed.
    if ts_ist.weekday() >= 5:
        return False
    minute_of_day = ts_ist.hour * 60 + ts_ist.minute
    if market == "MCX":
        # MCX: 09:00 - 23:30 IST
        return 9 * 60 <= minute_of_day <= 23 * 60 + 30
    # Default: NSE/BSE equities + indices — 09:15 - 15:30 IST
    return 9 * 60 + 15 <= minute_of_day <= 15 * 60 + 30


def _aggregate_to_timeframe(
    rows: list[dict[str, Any]],
    minutes: int,
    *,
    market: str = "NSE",
) -> list[dict[str, Any]]:
    """Aggregate 1-minute bars to the requested timeframe boundary.

    Bucket key = bar-open floored to N-minute IST boundary inside the
    regular session. Within each bucket: open = first, high = max,
    low = min, close = last, volume = sum. Bars outside session hours
    (e.g. NIFTY 19:30 IST) are dropped before bucketing — see
    _is_in_market_session.
    """
    if minutes <= 1:
        # Filter even the 1-min passthrough so non-session ticks don't
        # paint phantom candles on a 1-min view.
        out: list[dict[str, Any]] = []
        for row in rows:
            ts = row.get("time")
            if ts is None:
                continue
            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            ts_ist = ts.astimezone(IST)
            if not _is_in_market_session(ts_ist, market):
                continue
            out.append(row)
        return out
    buckets: dict[datetime, dict[str, Any]] = {}
    order: list[datetime] = []
    for row in rows:
        ts = row.get("time")
        if ts is None:
            continue
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts_ist = ts.astimezone(IST)
        if not _is_in_market_session(ts_ist, market):
            continue
        bucket_minute = (ts_ist.minute // minutes) * minutes
        bucket_start = ts_ist.replace(minute=bucket_minute, second=0, microsecond=0)
        if bucket_start not in buckets:
            buckets[bucket_start] = {
                "time": bucket_start.astimezone(timezone.utc),
                "open": float(row.get("open") or row.get("close") or 0.0),
                "high": float(row.get("high") or row.get("close") or 0.0),
                "low": float(row.get("low") or row.get("close") or 0.0),
                "close": float(row.get("close") or 0.0),
                "volume": float(row.get("volume") or 0.0),
            }
            order.append(bucket_start)
        else:
            bucket = buckets[bucket_start]
            high = float(row.get("high") or row.get("close") or 0.0)
            low = float(row.get("low") or row.get("close") or 0.0)
            bucket["high"] = max(bucket["high"], high)
            bucket["low"] = min(bucket["low"], low)
            bucket["close"] = float(row.get("close") or bucket["close"])
            bucket["volume"] += float(row.get("volume") or 0.0)
    return [buckets[start] for start in sorted(order)]


def _guard_rows(
    app_symbol: str | None,
    rows: list[dict[str, Any]],
    *,
    underlying: str,
) -> list[dict[str, Any]]:
    """Drop cross-symbol-contaminated bars before they reach the chart axis.

    Non-guarded symbols (stocks/commodities/anything ``app_symbol`` is None for)
    pass through untouched — matching ``index_band_guard.is_guarded`` semantics.

    For a guarded index this runs two nets, log-then-drop only (never a repair /
    fabricate):

      1. The shipped absolute + ±REL_TOL band (``check_ohlc``), which tests every
         O/H/L/C leg. Requires the ±20% reference to have been seeded upstream to
         catch in-band contamination like a ``48545`` close.
      2. A continuity net keyed on the median of surviving closes, as a secondary
         guard for the 2x family in case the reference could not be seeded.

    Read-path only — no DB writes. Immediately collapses a poisoned 24k-57k axis
    back to a clean ~24k axis for every client.
    """
    if not app_symbol:
        return rows

    kept: list[dict[str, Any]] = []
    for row in rows:
        o, h, l, c = row.get("open"), row.get("high"), row.get("low"), row.get("close")
        if index_band_guard.check_ohlc(app_symbol, o, h, l, c):
            kept.append(row)
        else:
            logger.warning(
                "[charts] dropped out-of-band {} bar t={} o={} h={} l={} c={}",
                underlying,
                row.get("time"),
                o,
                h,
                l,
                c,
            )

    # Continuity net — robust center from the band-survivors' closes.
    closes = sorted(
        float(r["close"])
        for r in kept
        if r.get("close") not in (None, "") and float(r.get("close") or 0.0) > 0
    )
    if len(closes) >= 3:
        center = closes[len(closes) // 2]
        if center > 0:
            survivors: list[dict[str, Any]] = []
            for row in kept:
                legs = [
                    float(v)
                    for v in (row.get("open"), row.get("high"), row.get("low"), row.get("close"))
                    if v not in (None, "") and float(v or 0.0) > 0
                ]
                worst = max((abs(v - center) / center for v in legs), default=0.0)
                if worst > _CHART_CONTINUITY_TOL:
                    logger.warning(
                        "[charts] dropped discontinuous {} bar t={} legs={} center={:.1f}",
                        underlying,
                        row.get("time"),
                        legs,
                        center,
                    )
                    continue
                survivors.append(row)
            return survivors
    return kept


async def _load_underlying_spot(
    underlying: str,
    lookback_sessions: int,
    timeframe: str,
) -> list[dict[str, Any]]:
    """Pull underlying_spot_candles and aggregate to the requested timeframe."""
    minutes = {"15minute": 15, "30minute": 30, "60minute": 60}.get(timeframe, 30)
    # MCX commodities trade until 23:30 IST; everything else stays on the
    # NSE/BSE 09:15-15:30 window. Used to filter phantom out-of-session
    # 1-min rows so the chart doesn't show degenerate 19:30 / 08:00 bars.
    market = "MCX" if underlying in _COMMODITY_UNDERLYINGS else "NSE"
    # Pull enough 1-min history to cover the lookback in trading hours. ~7
    # hours/day = 420 bars/day; pad to cover weekends.
    days_back = max(lookback_sessions * 2 + 7, 14)
    # Seed the ±20% prior-session reference (TTL-gated, cheap) so the band guard
    # below can catch in-band cross-symbol contamination (e.g. a 48545 close on
    # ~24000 NIFTY, which sits inside the wide absolute band). None for stocks /
    # commodities — those pass through the guard untouched.
    app_symbol = index_band_guard.app_symbol_for_underlying(underlying)
    if app_symbol:
        await index_band_guard.maybe_refresh_reference_closes()
    async with AsyncSessionLocal() as session:
        # First try the native timeframe if it's already stored.
        result = await session.execute(
            text(
                """
                SELECT time, open, high, low, close, volume
                FROM underlying_spot_candles
                WHERE underlying = :underlying
                  AND interval = :interval
                  AND time >= NOW() - make_interval(days => :days)
                ORDER BY time ASC
                """
            ),
            {"underlying": underlying, "interval": timeframe, "days": days_back},
        )
        rows = _guard_rows(
            app_symbol,
            [dict(row._mapping) for row in result.fetchall()],
            underlying=underlying,
        )
        if len(rows) >= 30:
            # Even pre-aggregated rows can include after-hours synthetic
            # ticks — filter to session before returning.
            return _aggregate_to_timeframe(rows, 1, market=market)

        # Fall back to 1-minute and aggregate.
        result = await session.execute(
            text(
                """
                SELECT time, open, high, low, close, volume
                FROM underlying_spot_candles
                WHERE underlying = :underlying
                  AND interval = '1minute'
                  AND time >= NOW() - make_interval(days => :days)
                ORDER BY time ASC
                """
            ),
            {"underlying": underlying, "days": days_back},
        )
        one_min_rows = _guard_rows(
            app_symbol,
            [dict(row._mapping) for row in result.fetchall()],
            underlying=underlying,
        )
    return _aggregate_to_timeframe(one_min_rows, minutes, market=market)


async def _load_trade_markers(
    underlying: str,
    *,
    since: datetime,
    until: datetime,
) -> list[dict[str, Any]]:
    """Return entry + exit markers for this underlying within [since, until]."""
    markers: list[dict[str, Any]] = []
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT
                    underlying,
                    option_type,
                    strike,
                    entry_price,
                    entered_at,
                    closed_at,
                    status,
                    strategy_label,
                    signal_reason,
                    metadata
                FROM agent_signals
                WHERE underlying = :underlying
                  AND entered_at IS NOT NULL
                  AND entered_at BETWEEN :since AND :until
                ORDER BY entered_at ASC
                """
            ),
            {"underlying": underlying, "since": since, "until": until},
        )
        for row in result.fetchall():
            r = dict(row._mapping)
            strategy = _classify_strategy(r.get("strategy_label"))
            if strategy not in STRATEGY_COLORS:
                continue
            entered_at = r.get("entered_at")
            entry_price = r.get("entry_price")
            metadata = dict(r.get("metadata") or {})
            markers.append(
                {
                    "strategy": strategy,
                    "type": "entry",
                    "time": entered_at.isoformat() if entered_at else None,
                    "option_type": r.get("option_type"),
                    "strike": float(r.get("strike") or 0.0) or None,
                    "premium": float(entry_price or 0.0) or None,
                    "reason": r.get("signal_reason"),
                    "label": r.get("strategy_label"),
                }
            )
            closed_at = r.get("closed_at")
            if closed_at and r.get("status") in {"closed", "exited"}:
                exit_price = metadata.get("exit_price") or metadata.get("exit_premium")
                pnl = metadata.get("pnl") or metadata.get("realized_pnl")
                markers.append(
                    {
                        "strategy": strategy,
                        "type": "exit",
                        "time": closed_at.isoformat(),
                        "option_type": r.get("option_type"),
                        "strike": float(r.get("strike") or 0.0) or None,
                        "premium": float(exit_price or 0.0) or None,
                        "pnl": float(pnl) if pnl is not None else None,
                        "reason": metadata.get("exit_reason") or metadata.get("close_reason"),
                        "label": r.get("strategy_label"),
                    }
                )
    return markers


@router.get("/ohlc")
async def chart_ohlc(
    underlying: str = Query(..., description="Symbol — NIFTY, RELIANCE, CRUDEOIL, …"),
    timeframe: str = Query("30minute"),
    lookback_sessions: int = Query(5, ge=1, le=30),
) -> dict[str, Any]:
    timeframe = timeframe.lower()
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported timeframe: {timeframe}. Supported: {', '.join(SUPPORTED_TIMEFRAMES)}",
        )
    underlying = underlying.upper().strip()
    if not underlying:
        raise HTTPException(status_code=400, detail="underlying is required")

    cache_key = (underlying, timeframe, lookback_sessions)
    now_mono = time.monotonic()
    cached = _OHLC_CACHE.get(cache_key)
    if cached and (now_mono - cached[0]) < _OHLC_CACHE_TTL:
        return cached[1]

    bars_raw = await _load_underlying_spot(underlying, lookback_sessions, timeframe)
    if not bars_raw:
        return {
            "underlying": underlying,
            "timeframe": timeframe,
            "bars": [],
            "indicators": {},
            "trades": [],
            "detail": f"No spot/futures candle history available for {underlying}.",
        }

    closes = [float(b.get("close") or 0.0) for b in bars_raw]
    # Indicators
    macd_line, signal_line, hist = compute_macd(closes)
    rsi_values = compute_rsi(closes, period=14)
    upper, middle, lower = _compute_bollinger(closes, period=20, num_std=2.0)
    ema50 = _compute_ema(closes, period=50)

    bars: list[dict[str, Any]] = []
    for i, b in enumerate(bars_raw):
        t = b.get("time")
        if hasattr(t, "isoformat"):
            t_iso = t.isoformat()
        else:
            t_iso = str(t)
        bars.append(
            {
                "time": t_iso,
                "open": float(b.get("open") or 0.0),
                "high": float(b.get("high") or 0.0),
                "low": float(b.get("low") or 0.0),
                "close": float(b.get("close") or 0.0),
                "volume": float(b.get("volume") or 0.0),
            }
        )

    if bars:
        first_t = datetime.fromisoformat(bars[0]["time"].replace("Z", "+00:00"))
        last_t = datetime.fromisoformat(bars[-1]["time"].replace("Z", "+00:00"))
        # Pad ±1 day so entry/exit markers that landed slightly outside the
        # aggregated bar window still attach to the nearest bar visually.
        since = first_t - timedelta(days=1)
        until = last_t + timedelta(days=1)
    else:
        since = datetime.now(timezone.utc) - timedelta(days=lookback_sessions * 2 + 5)
        until = datetime.now(timezone.utc)
    trades = await _load_trade_markers(underlying, since=since, until=until)

    response = {
        "underlying": underlying,
        "timeframe": timeframe,
        "bar_count": len(bars),
        "bars": bars,
        "indicators": {
            "macd": macd_line,
            "macd_signal": signal_line,
            "macd_histogram": hist,
            "rsi": rsi_values,
            "bb_upper": upper,
            "bb_middle": middle,
            "bb_lower": lower,
            "ema50": ema50,
        },
        "trades": trades,
        "strategy_colors": STRATEGY_COLORS,
    }
    _OHLC_CACHE[cache_key] = (now_mono, response)
    # Lazy eviction: keep at most ~50 entries so the dict doesn't grow
    # unbounded as users browse the dropdown.
    if len(_OHLC_CACHE) > 50:
        oldest_key = min(_OHLC_CACHE.items(), key=lambda kv: kv[1][0])[0]
        _OHLC_CACHE.pop(oldest_key, None)
    return response


# ── Per-contract option-premium OHLC + indicators ────────────────────────────
# Powers the per-ATM-strike pop-up chart on the NSE signal desk. The /ohlc
# endpoint above renders the underlying spot/futures series; this one renders
# the OPTION PREMIUM series for one logical contract (underlying + expiry +
# strike + side) so a trader can verify the 30m ATM MACD lane against the same
# bars it traded on.

SUPPORTED_OPTION_TIMEFRAMES = ("5minute", "15minute", "30minute")

_OPTION_OHLC_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_OPTION_OHLC_CACHE_TTL = 20.0


@router.get("/option-ohlc")
async def option_chart_ohlc(
    underlying: str = Query(..., description="Underlying — SENSEX, NIFTY, RELIANCE, …"),
    expiry: str = Query(..., description="Contract expiry date (YYYY-MM-DD)"),
    strike: float = Query(..., description="Strike price"),
    option_type: str = Query(..., description="CE or PE"),
    interval: str = Query("30minute"),
    limit: int = Query(200, ge=20, le=500),
    instrument_key: str | None = Query(None),
) -> dict[str, Any]:
    """Option-premium OHLC + the four signal-desk indicators.

    Returns candles plus index-aligned MACD (12/26/9), RSI(14), Bollinger
    Bands (20, 2σ) and Kaufman's adaptive MA (10/2/30) for one option
    contract, read through OptionHistoryService (cross-broker deduped, with a
    broker top-up when the local series is short). The indicator arrays mirror
    the /ohlc shape so the frontend can reuse its rendering.
    """
    from market_data.option_history import option_history_service

    underlying = underlying.upper().strip()
    option_type = option_type.upper().strip()
    interval = interval.lower().strip()
    if option_type not in {"CE", "PE"}:
        raise HTTPException(status_code=400, detail="option_type must be CE or PE")
    if interval not in SUPPORTED_OPTION_TIMEFRAMES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported interval: {interval}. Supported: {', '.join(SUPPORTED_OPTION_TIMEFRAMES)}",
        )
    try:
        expiry_date = date.fromisoformat(expiry[:10])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid expiry: {expiry}") from exc

    contract = {
        "underlying": underlying,
        "expiry": expiry_date.isoformat(),
        "strike": float(strike),
        "option_type": option_type,
        "interval": interval,
    }

    cache_key = (underlying, expiry_date.isoformat(), float(strike), option_type, interval, limit)
    now_mono = time.monotonic()
    cached = _OPTION_OHLC_CACHE.get(cache_key)
    if cached and (now_mono - cached[0]) < _OPTION_OHLC_CACHE_TTL:
        return cached[1]

    try:
        candles = await option_history_service.load_candles(
            underlying=underlying,
            expiry=expiry_date,
            strike=float(strike),
            option_type=option_type,
            instrument_key=(instrument_key or None),
            interval=interval,
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean empty chart, never a 500
        return {
            **contract,
            "bar_count": 0,
            "bars": [],
            "indicators": {},
            "detail": f"Could not load premium history: {exc}",
        }

    bars: list[dict[str, Any]] = []
    for c in candles:
        close = c.get("close")
        if close is None:
            continue
        close_f = float(close)
        bars.append(
            {
                "time": str(c.get("time")),
                "open": float(c.get("open")) if c.get("open") is not None else close_f,
                "high": float(c.get("high")) if c.get("high") is not None else close_f,
                "low": float(c.get("low")) if c.get("low") is not None else close_f,
                "close": close_f,
                "volume": float(c.get("volume") or 0.0),
            }
        )

    if not bars:
        strike_label = int(strike) if float(strike).is_integer() else strike
        return {
            **contract,
            "bar_count": 0,
            "bars": [],
            "indicators": {},
            "detail": f"No premium candle history for {underlying} {strike_label} {option_type} ({expiry_date.isoformat()}).",
        }

    closes = [b["close"] for b in bars]
    macd_line, signal_line, hist = compute_macd(closes)
    rsi_values = compute_rsi(closes, period=14)
    upper, middle, lower = _compute_bollinger(closes, period=20, num_std=2.0)
    kama = _compute_kama(closes, er_period=10, fast=2, slow=30)

    response = {
        **contract,
        "bar_count": len(bars),
        "bars": bars,
        "indicators": {
            "macd": macd_line,
            "macd_signal": signal_line,
            "macd_histogram": hist,
            "rsi": rsi_values,
            "bb_upper": upper,
            "bb_middle": middle,
            "bb_lower": lower,
            "kama": kama,
        },
    }
    _OPTION_OHLC_CACHE[cache_key] = (now_mono, response)
    if len(_OPTION_OHLC_CACHE) > 80:
        oldest_key = min(_OPTION_OHLC_CACHE.items(), key=lambda kv: kv[1][0])[0]
        _OPTION_OHLC_CACHE.pop(oldest_key, None)
    return response
