"""S1 strike selection + 3m-resampled 30m MACD.

Why this exists
---------------
The S1 lane's signal source (`atm_option_watchlist_snapshots`) collapses to ~5
names through the midday session (full-universe at 09:xx → ~5 by 11:00–13:00), so
S1 is blind to most crosses outside the open (e.g. POWERGRID's 11:45 cross on
2026-06-08 — last snapshot 10:08). Separately, the raw ATM strike is often thin /
illiquid with too little history to compute a 35-bar MACD.

Fix (uses the full chain history now available in `option_premium_candles`):
  1. Pick the nearest-to-spot **well-traded** strike (OI/volume floor) that has
     **enough history** (≥ MACD_MIN_BARS 30-min bars' worth of 3-min candles).
  2. Compute that strike's 30-min MACD by resampling the **dense 3-min feed**
     (which stays fresh through midday where the snapshot feed dies).

Pure helpers are unit-tested; the async path is validated against prod data.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional  # noqa: F401
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import text

from agent.strategy_config import MACD_FAST, MACD_MIN_BARS, MACD_SIGNAL, MACD_SLOW
from db.database import AsyncSessionLocal

IST = ZoneInfo("Asia/Kolkata")

# 35 30-min bars ≈ 350 3-min bars; ask for a little extra warmup headroom.
MIN_3M_BARS = MACD_MIN_BARS * 10
DEFAULT_MIN_OI = 0          # 0 = liquidity is a tiebreak, not a hard gate
DEFAULT_MAX_STEPS = 6       # how many strikes either side of ATM to consider


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------- #
def resample_closes_30m(rows: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    """Resample (ist_dt, close) 3-min rows to 30-min bars (close = last in bucket).

    Buckets align to floor(minute/30)*30 in IST — the same bucketing the live
    lane's SQL uses and the validation script confirmed.
    """
    buckets: dict[datetime, float] = {}
    for t, c in sorted(rows, key=lambda kv: kv[0]):
        key = t.replace(minute=(t.minute // 30) * 30, second=0, microsecond=0)
        buckets[key] = c  # time-ordered → last close in the bucket wins
    return [(k, buckets[k]) for k in sorted(buckets)]


def pick_strike(
    candidates: list[dict[str, Any]],
    spot: float,
    *,
    min_bars: int = MIN_3M_BARS,
    min_oi: float = DEFAULT_MIN_OI,
    fresh_within_days: float = 1.5,
) -> Optional[dict[str, Any]]:
    """Nearest-to-spot WELL-TRADED strike that has enough, still-FRESH history.

    candidates: [{strike, bars, oi, volume, last}] where `last` is the strike's
    most recent bar time. "Well-traded" = currently active (a stale strike with a
    big historical bar count is excluded) and liquid (OI tiebreak). Steps:
      1. require ≥min_bars of history,
      2. keep only strikes whose last bar is within fresh_within_days of the
         freshest strike (drops abandoned strikes — e.g. ALKEM 5400 stuck at 06-05
         while the active 5300 prints to 15:24),
      3. nearest-to-spot, OI as the tiebreak; prefer those clearing min_oi.
    """
    if not candidates or spot <= 0:
        return None
    historied = [c for c in candidates if c.get("bars", 0) >= min_bars]
    if not historied:
        return None
    lasts = [c["last"] for c in historied if c.get("last") is not None]
    fresh = historied
    if lasts:
        cutoff = max(lasts) - timedelta(days=fresh_within_days)
        fresh = [c for c in historied if c.get("last") is not None and c["last"] >= cutoff] or historied
    fresh.sort(key=lambda c: (abs(float(c["strike"]) - spot), -(c.get("oi") or 0)))
    for c in fresh:
        if (c.get("oi") or 0) >= min_oi:
            return c
    return fresh[0]


def _macd_zero_cross(closes: list[float], option_type: str, symbol: str, last_bar_time: Optional[str]):
    """(fresh_cross, current_macd, previous_macd). Lazy-imports the lane's MACD so
    signals match production exactly; falls back to a standard EMA MACD if the
    heavy import isn't available (e.g. isolated unit context)."""
    if len(closes) < MACD_MIN_BARS:
        return (False, None, None)
    try:
        from paper_engine.strategy_agent import _strategy_macd  # lazy, prod-consistent
        macd_line, _, _ = _strategy_macd(closes, symbol=symbol, timeframe="30minute", last_bar_time=last_bar_time)
    except Exception:  # noqa: BLE001
        macd_line = _ema_macd(closes)
    if len(macd_line) < 2 or macd_line[-1] is None or macd_line[-2] is None:
        return (False, None, None)
    cur, prev = float(macd_line[-1]), float(macd_line[-2])
    if option_type == "CE":
        fresh = prev <= 0 < cur
    else:
        fresh = prev >= 0 > cur
    return (fresh, cur, prev)


def _ema_macd(closes: list[float], fast: int = MACD_FAST, slow: int = MACD_SLOW) -> list[Optional[float]]:
    def ema(vals: list[float], n: int) -> list[Optional[float]]:
        k = 2 / (n + 1)
        out: list[Optional[float]] = []
        e: Optional[float] = None
        for i, v in enumerate(vals):
            if i + 1 < n:
                out.append(None)
                continue
            e = sum(vals[i - n + 1:i + 1]) / n if e is None else v * k + e * (1 - k)
            out.append(e)
        return out
    ef, es = ema(closes, fast), ema(closes, slow)
    return [(a - b) if (a is not None and b is not None) else None for a, b in zip(ef, es)]


# --------------------------------------------------------------------------- #
# Async data path (validated against prod)
# --------------------------------------------------------------------------- #
async def _strike_stats(
    session, underlying: str, expiry: date, option_type: str, lookback_days: int
) -> list[dict[str, Any]]:
    """Per-strike {strike, bars, oi, volume} from the 3-min chain history."""
    result = await session.execute(
        text(
            """
            SELECT strike,
                   count(*) AS bars,
                   max(oi) AS oi,
                   coalesce(sum(volume), 0) AS volume,
                   max(time) AS last_time,
                   (array_agg(instrument_key ORDER BY time DESC)
                       FILTER (WHERE instrument_key IS NOT NULL))[1] AS instrument_key
            FROM option_premium_candles
            WHERE underlying = :u AND expiry = :e AND option_type = :o
              AND interval = '3minute'
              AND time > now() - make_interval(days => :lb)
            GROUP BY strike
            """
        ),
        {"u": underlying, "e": expiry, "o": option_type, "lb": int(lookback_days)},
    )
    out = []
    for row in result.fetchall():
        last = row.last_time
        if last is not None and last.tzinfo is not None:
            last = last.astimezone(IST).replace(tzinfo=None)
        out.append({
            "strike": float(row.strike),
            "bars": int(row.bars or 0),
            "oi": float(row.oi or 0.0),
            "volume": float(row.volume or 0.0),
            "last": last,
            "instrument_key": getattr(row, "instrument_key", None),
        })
    return out


async def _strike_3m_closes(
    session, underlying: str, expiry: date, strike: float, option_type: str, lookback_days: int
) -> list[tuple[datetime, float]]:
    result = await session.execute(
        text(
            """
            SELECT DISTINCT ON (time) time, close
            FROM option_premium_candles
            WHERE underlying = :u AND expiry = :e AND strike = :s AND option_type = :o
              AND interval = '3minute' AND close IS NOT NULL
              AND time > now() - make_interval(days => :lb)
            ORDER BY time,
                CASE source WHEN 'fyers_chain' THEN 0 WHEN 'fyers' THEN 1 WHEN 'upstox' THEN 2 ELSE 3 END,
                synced_at DESC NULLS LAST
            """
        ),
        {"u": underlying, "e": expiry, "s": strike, "o": option_type, "lb": int(lookback_days)},
    )
    rows: list[tuple[datetime, float]] = []
    for row in result.fetchall():
        t = row.time
        if t is None or row.close is None:
            continue
        if t.tzinfo is not None:
            t = t.astimezone(IST).replace(tzinfo=None)
        rows.append((t, float(row.close)))
    return rows


async def compute_s1_signal(
    underlying: str,
    expiry: date | str,
    option_type: str,
    spot: float,
    *,
    min_oi: float = DEFAULT_MIN_OI,
    lookback_days: int = 8,
    as_of: Optional[datetime] = None,
) -> dict[str, Any]:
    """Pick the nearest well-traded strike with history and return its fresh 30-min
    MACD zero-cross signal — sourced from the dense 3-min feed, not the
    midday-collapsing snapshot feed.

    Returns {available, underlying, option_type, strike, bars_30m, oi, volume,
    fresh_cross, macd, macd_prev, last_bar_time} (available=False + reason on miss).
    """
    exp = expiry if isinstance(expiry, date) else date.fromisoformat(str(expiry)[:10])
    option_type = str(option_type).upper()
    try:
        async with AsyncSessionLocal() as session:
            stats = await _strike_stats(session, underlying, exp, option_type, lookback_days)
            chosen = pick_strike(stats, spot, min_oi=min_oi)
            if not chosen:
                return {"available": False, "underlying": underlying, "option_type": option_type,
                        "reason": "no_strike_with_history", "strikes_seen": len(stats)}
            rows = await _strike_3m_closes(session, underlying, exp, chosen["strike"], option_type, lookback_days)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[s1-strike] {underlying} {option_type}: {exc}")
        return {"available": False, "underlying": underlying, "option_type": option_type, "reason": f"error:{exc}"}

    if as_of is not None:
        rows = [(t, c) for (t, c) in rows if t <= as_of]
    bars30 = resample_closes_30m(rows)
    closes = [c for _, c in bars30]
    if len(closes) < MACD_MIN_BARS:
        return {"available": False, "underlying": underlying, "option_type": option_type,
                "reason": "insufficient_30m_bars", "strike": chosen["strike"], "bars_30m": len(closes)}

    last_bar_time = bars30[-1][0].isoformat() if bars30 else None
    symbol = f"{underlying}:{int(chosen['strike'])}:{option_type}"
    fresh, cur, prev = _macd_zero_cross(closes, option_type, symbol, last_bar_time)
    return {
        "available": True,
        "underlying": underlying,
        "option_type": option_type,
        "strike": chosen["strike"],
        "instrument_key": chosen.get("instrument_key"),
        "oi": chosen["oi"],
        "volume": chosen["volume"],
        "bars_30m": len(closes),
        "fresh_cross": fresh,
        "macd": round(cur, 4) if cur is not None else None,
        "macd_prev": round(prev, 4) if prev is not None else None,
        "latest_close": round(closes[-1], 2) if closes else None,
        "last_bar_time": last_bar_time,
    }
