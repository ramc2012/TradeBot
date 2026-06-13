"""
Guarded candle loader for backtests/sweeps (docs/STRATEGY_TESTING_PLAN.md §2.2).

underlying_spot_candles carries known cross-symbol CONTAMINATION (e.g. NIFTY rows
with a ~54k BANKNIFTY high) and ~duplicate timestamps. Every backtest/sweep MUST
read through these guards (the same dedup + RTH + outlier logic the nomad_sniper
sidecar uses) so a garbage print can't blow up the TPO ladder or skew ATR.

Pure pandas + optional asyncpg. No app/broker bootstrap — safe to run anywhere.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd

IST = timezone(timedelta(hours=5, minutes=30))
SESSION_OPEN = (9, 15)
SESSION_CLOSE = (15, 30)
CLEAN_SOURCES = ("timescaledb_spot_1minute",)  # allow-list (per plan §2.2)


def guard_ohlc(
    df: pd.DataFrame,
    *,
    band: float = 0.20,
    rth: bool = True,
    tz_aware_utc: bool = True,
) -> pd.DataFrame:
    """Dedup (keep last), optionally restrict to NSE RTH, and drop rows whose
    O/H/L/C deviate > `band` from the per-session median close (contamination)."""
    if df is None or df.empty:
        return df
    out = df.copy()
    out["time"] = pd.to_datetime(out["time"], utc=tz_aware_utc)
    out = out.sort_values("time").drop_duplicates("time", keep="last").reset_index(drop=True)

    ist = out["time"].dt.tz_convert(IST) if tz_aware_utc else out["time"]
    if rth:
        t = ist.dt.time
        from datetime import time as _t
        mask = (t >= _t(*SESSION_OPEN)) & (t <= _t(*SESSION_CLOSE))
        out = out[mask].reset_index(drop=True)
        ist = ist[mask].reset_index(drop=True)
    if out.empty:
        return out

    out["_session"] = ist.dt.date
    keep = np.ones(len(out), dtype=bool)
    for _, idx in out.groupby("_session").groups.items():
        sub = out.loc[idx]
        med = float(sub["close"].replace(0, np.nan).median())
        if not np.isfinite(med) or med <= 0:
            continue
        lo, hi = med * (1 - band), med * (1 + band)
        bad = (
            (sub["close"] <= 0)
            | (sub["close"] < lo) | (sub["close"] > hi)
            | (sub["high"] > hi) | ((sub["low"] > 0) & (sub["low"] < lo))
            | (sub["open"] > 0) & ((sub["open"] < lo) | (sub["open"] > hi))
        )
        keep[out.index.get_indexer(sub.index[bad.values])] = False
    out = out[keep].drop(columns=["_session"]).reset_index(drop=True)
    return out


_CANDLE_SQL = """
    SELECT time, open, high, low, close, COALESCE(volume, 0) AS volume
    FROM underlying_spot_candles
    WHERE underlying = $1 AND interval = $2 AND time >= $3
      AND ($4::text IS NULL OR source = $4)
    ORDER BY time ASC
"""


async def load_guarded_candles(
    conn,
    underlying: str,
    *,
    interval: str = "1minute",
    days: int = 180,
    source: Optional[str] = None,
    band: float = 0.20,
    rth: bool = True,
) -> pd.DataFrame:
    """Load OHLC from TimescaleDB via an asyncpg connection, guarded. `source`
    None = any; pass 'timescaledb_spot_1minute' to use the clean source only."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = await conn.fetch(_CANDLE_SQL, underlying.upper(), interval, since, source)
    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame([dict(r) for r in rows])
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return guard_ohlc(df, band=band, rth=rth)


# ── option premium candles: NO-ARBITRAGE guards ─────────────────────────────
# option_premium_candles carries Fyers zombie/post-corporate-action strikes with
# impossible prices (a PE quoted above its strike, a CE above spot, premia many x
# the underlying). The S1 INDIANB +Rs19L phantom (2026-06-11) came from exactly
# these rows. ~15,792 intrinsic-violating CE rows + 685 premium>1.5x-spot rows +
# duplicate timestamps exist for NIFTY alone — none must reach a backtest.

def guard_option_ohlc(
    df: pd.DataFrame,
    *,
    rth: bool = True,
    tz_aware_utc: bool = True,
    put_strike_tol: float = 1.02,   # PE ltp must be <= strike (x tolerance)
    call_spot_tol: float = 1.05,    # CE ltp must be <= underlying (x tolerance)
    max_premium_to_spot: float = 1.5,
) -> pd.DataFrame:
    """Dedup (keep last) + optional RTH + drop no-arbitrage-violating option rows.

    Requires columns: time, open, high, low, close, strike, option_type
    (and optionally underlying_price for the CE<=spot / premium<=spot checks).
    A row is dropped if {close,high} violates the bound for its type.
    """
    if df is None or df.empty:
        return df
    out = df.copy()
    out["time"] = pd.to_datetime(out["time"], utc=tz_aware_utc)
    out = out.sort_values("time").drop_duplicates("time", keep="last").reset_index(drop=True)

    if rth:
        ist = out["time"].dt.tz_convert(IST) if tz_aware_utc else out["time"]
        from datetime import time as _t
        t = ist.dt.time
        out = out[(t >= _t(*SESSION_OPEN)) & (t <= _t(*SESSION_CLOSE))].reset_index(drop=True)
    if out.empty:
        return out

    for c in ("open", "high", "low", "close", "strike", "underlying_price"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")

    otype = out["option_type"].astype(str).str.upper()
    strike = out["strike"] if "strike" in out.columns else None
    spot = out["underlying_price"] if "underlying_price" in out.columns else None
    px_hi = out[["close", "high"]].max(axis=1)

    bad = (out["close"] <= 0) | (~np.isfinite(out["close"]))
    if strike is not None:
        is_pe = otype.str.startswith("P")
        bad = bad | (is_pe & (strike > 0) & (px_hi > strike * put_strike_tol))
    if spot is not None:
        is_ce = otype.str.startswith("C")
        bad = bad | (is_ce & (spot > 0) & (px_hi > spot * call_spot_tol))
        bad = bad | ((spot > 0) & (px_hi > spot * max_premium_to_spot))
    return out[~bad.fillna(False)].reset_index(drop=True)


_OPTION_SQL = """
    SELECT time, open, high, low, close, COALESCE(volume,0) AS volume,
           COALESCE(oi,0) AS oi, strike, option_type, underlying_price
    FROM option_premium_candles
    WHERE underlying = $1 AND interval = $2 AND time >= $3
      AND ($4::numeric IS NULL OR strike = $4)
      AND ($5::text   IS NULL OR option_type = $5)
      AND ($6::date   IS NULL OR expiry = $6)
    ORDER BY time ASC
"""


async def load_guarded_option_candles(
    conn,
    underlying: str,
    *,
    interval: str = "30minute",
    days: int = 540,
    strike: Optional[float] = None,
    option_type: Optional[str] = None,
    expiry=None,
    rth: bool = True,
) -> pd.DataFrame:
    """Load option_premium_candles via asyncpg, no-arb guarded. strike/option_type/
    expiry None = any. Returns time/ohlc/volume/oi/strike/option_type/underlying_price."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = await conn.fetch(
        _OPTION_SQL, underlying.upper(), interval, since, strike, option_type, expiry
    )
    cols = ["time", "open", "high", "low", "close", "volume", "oi", "strike", "option_type", "underlying_price"]
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame([dict(r) for r in rows])
    for c in ("open", "high", "low", "close", "volume", "oi", "strike", "underlying_price"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return guard_option_ohlc(df, rth=rth)
