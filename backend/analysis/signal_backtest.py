"""
Neutral underlying execution engine for validating a lane's SIGNAL edge at depth.

Many lanes (directional_options, the MP/auction swing agent, fractal) generate an
instrument-agnostic directional signal on underlying OHLC, then execute it through
an NSE-specific OPTION layer (contract selection, premium stops, lot sizing). The
alpha lives in the signal; the option layer is execution. To test the signal edge
on the deep 5y underlying data we strip the option layer and execute the signal
directly on the underlying with a neutral ATR stop/target model — identical across
lanes, so verdicts are comparable. R is normalised to the ATR stop (stop = −1R).

A lane adapter produces a frame with columns time/open/high/low/close/atr and an
integer `sig` column (+1 long, −1 short, 0 flat) per bar; simulate_underlying walks
it and emits {events:[{r_multiple, exit_time, ...}], summary:{...}} — the shape the
validate_strategy harness consumes.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def simulate_underlying(
    f: pd.DataFrame,
    *,
    sl_atr: float = 1.5,
    tp_atr: float = 3.0,
    cost_bps: float = 2.0,
    atr_col: str = "atr",
    signal_col: str = "sig",
    time_col: str = "time",
    max_hold: int | None = None,
    allow_flip: bool = True,
) -> dict[str, Any]:
    """Walk a signal-bearing frame and simulate underlying trades with ATR stops.

    Entry at the close of the bar whose signal is non-zero (and we are flat). Exit
    on ATR stop / target, an opposite signal (if allow_flip), or max_hold bars.
    """
    need = {time_col, "open", "high", "low", "close", atr_col, signal_col}
    if not need.issubset(f.columns) or len(f) < 5:
        return {"events": [], "summary": {"trades": 0}}
    f = f.reset_index(drop=True)
    cost = cost_bps / 10000.0

    events: list[dict] = []
    pos = 0
    entry_px = stop = target = entry_atr = 0.0
    entry_i = 0

    for i in range(1, len(f)):
        px = float(f["close"].iloc[i]); hi = float(f["high"].iloc[i]); lo = float(f["low"].iloc[i])
        sig = int(f[signal_col].iloc[i]) if not pd.isna(f[signal_col].iloc[i]) else 0
        t = f[time_col].iloc[i]

        if pos != 0:
            exit_px = None; reason = None
            if pos == 1:
                if lo <= stop: exit_px, reason = stop, "stop"
                elif hi >= target: exit_px, reason = target, "target"
                elif allow_flip and sig == -1: exit_px, reason = px, "flip"
            else:
                if hi >= stop: exit_px, reason = stop, "stop"
                elif lo <= target: exit_px, reason = target, "target"
                elif allow_flip and sig == 1: exit_px, reason = px, "flip"
            if exit_px is None and max_hold and (i - entry_i) >= max_hold:
                exit_px, reason = px, "time"
            if exit_px is not None:
                gross = (exit_px - entry_px) * pos
                net = gross - cost * (entry_px + exit_px)
                r = net / (sl_atr * entry_atr) if entry_atr > 0 else 0.0
                events.append({"exit_time": pd.Timestamp(t).isoformat(),
                               "side": "long" if pos == 1 else "short",
                               "entry": round(entry_px, 4), "exit": round(exit_px, 4),
                               "exit_reason": reason, "bars_held": i - entry_i,
                               "r_multiple": round(float(r), 4)})
                pos = 0

        if pos == 0:
            a = float(f[atr_col].iloc[i])
            if a <= 0 or sig == 0:
                continue
            pos = 1 if sig == 1 else -1
            entry_px, entry_atr, entry_i = px, a, i
            if pos == 1:
                stop, target = px - sl_atr * a, px + tp_atr * a
            else:
                stop, target = px + sl_atr * a, px - tp_atr * a

    rs = [e["r_multiple"] for e in events]
    summary = {
        "trades": len(events),
        "win_rate_pct": round(100 * float(np.mean([r > 0 for r in rs])), 2) if rs else 0.0,
        "total_r": round(float(np.sum(rs)), 2) if rs else 0.0,
        "expectancy_r": round(float(np.mean(rs)), 3) if rs else 0.0,
    }
    return {"events": events, "summary": summary}
