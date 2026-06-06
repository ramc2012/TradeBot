"""
Generic spot MACD-crossover backtester (instrument-agnostic, R-multiple output)
for validating the harness at depth on the 5y Alpaca dataset. Mirrors the NSE
Strategy-1 family (MACD on a higher timeframe) but on the underlying itself.

Long when MACD crosses above signal, short when below; exit on the opposite
cross OR an ATR stop / target. R is normalised to the ATR stop (stop = −1R), so
results are comparable across instruments. Costs applied on close.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from analysis.macd_engine import compute_ema


def _resample(frame: pd.DataFrame, tf: str) -> pd.DataFrame:
    f = frame.copy()
    f["time"] = pd.to_datetime(f["time"], utc=True)
    f = f.set_index("time").resample(tf).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    return f.reset_index()


def _atr(f: pd.DataFrame, period: int) -> pd.Series:
    pc = f["close"].shift(1)
    tr = pd.concat([f["high"] - f["low"], (f["high"] - pc).abs(), (f["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def macd_backtest(
    frame: pd.DataFrame,
    *,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    tf: str = "30min",
    atr_period: int = 14,
    sl_atr: float = 1.5,
    tp_atr: float = 3.0,
    cost_bps: float = 2.0,
) -> dict[str, Any]:
    """Returns {events:[{r_multiple, exit_time, ...}], summary:{...}} like the gann bt."""
    f = _resample(frame, tf)
    if len(f) < max(slow + signal + atr_period + 5, 60):
        return {"events": [], "summary": {"trades": 0}}
    closes = f["close"].tolist()
    ema_f = pd.Series(compute_ema(closes, fast), index=f.index, dtype="float64")
    ema_s = pd.Series(compute_ema(closes, slow), index=f.index, dtype="float64")
    macd = ema_f - ema_s
    # compute_ema seeds with an SMA of the first `period` values, so feeding it a
    # series with leading NaNs (macd has them from the slow EMA) poisons the whole
    # output to NaN. Compute the signal on the non-NaN tail, then re-align.
    valid = macd.dropna()
    sig = pd.Series(index=f.index, dtype="float64")
    if len(valid) > signal:
        sig.loc[valid.index] = pd.Series(compute_ema(valid.tolist(), signal), index=valid.index, dtype="float64")
    atr = _atr(f, atr_period)
    f = f.assign(macd=macd, sig=sig, atr=atr).dropna().reset_index(drop=True)
    if len(f) < 30:
        return {"events": [], "summary": {"trades": 0}}

    events: list[dict] = []
    pos = 0  # +1 long, -1 short
    entry_px = stop = target = 0.0
    entry_atr = 0.0
    cost = cost_bps / 10000.0

    cross_up = (f["macd"] > f["sig"]) & (f["macd"].shift(1) <= f["sig"].shift(1))
    cross_dn = (f["macd"] < f["sig"]) & (f["macd"].shift(1) >= f["sig"].shift(1))

    for i in range(1, len(f)):
        px = float(f["close"].iloc[i])
        hi = float(f["high"].iloc[i])
        lo = float(f["low"].iloc[i])
        t = f["time"].iloc[i]

        if pos != 0:
            exit_px = None
            reason = None
            if pos == 1:
                if lo <= stop:
                    exit_px, reason = stop, "stop"
                elif hi >= target:
                    exit_px, reason = target, "target"
                elif bool(cross_dn.iloc[i]):
                    exit_px, reason = px, "signal_flip"
            else:
                if hi >= stop:
                    exit_px, reason = stop, "stop"
                elif lo <= target:
                    exit_px, reason = target, "target"
                elif bool(cross_up.iloc[i]):
                    exit_px, reason = px, "signal_flip"
            if exit_px is not None:
                gross = (exit_px - entry_px) * pos
                net = gross - cost * (entry_px + exit_px)
                r = net / (sl_atr * entry_atr) if entry_atr > 0 else 0.0
                events.append({"exit_time": pd.Timestamp(t).isoformat(), "side": "long" if pos == 1 else "short",
                               "entry": round(entry_px, 2), "exit": round(exit_px, 2), "exit_reason": reason,
                               "r_multiple": round(float(r), 3)})
                pos = 0

        if pos == 0:
            a = float(f["atr"].iloc[i])
            if a <= 0:
                continue
            if bool(cross_up.iloc[i]):
                pos, entry_px, entry_atr = 1, px, a
                stop, target = px - sl_atr * a, px + tp_atr * a
            elif bool(cross_dn.iloc[i]):
                pos, entry_px, entry_atr = -1, px, a
                stop, target = px + sl_atr * a, px - tp_atr * a

    rs = [e["r_multiple"] for e in events]
    summary = {
        "trades": len(events),
        "win_rate_pct": round(100 * np.mean([r > 0 for r in rs]), 2) if rs else 0.0,
        "total_r": round(float(np.sum(rs)), 2) if rs else 0.0,
        "expectancy_r": round(float(np.mean(rs)), 3) if rs else 0.0,
    }
    return {"events": events, "summary": summary}
