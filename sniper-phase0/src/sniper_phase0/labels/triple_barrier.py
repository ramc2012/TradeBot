"""Triple-barrier labeler.

For each candidate entry, walk forward through the actual underlying ticks
(strictly AFTER entry) until one of:
  - target barrier hit
  - stop barrier hit
  - max-hold timeout

Label outputs:
  outcome ∈ {'target', 'stop', 'timeout'}
  exit_ts, exit_price
  gross_R = signed return in stop-units
  net_R   = gross_R after costs (the model trains on this)
  mae, mfe (in stop-units)
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from sniper_phase0.labels.cost_model import net_pnl
from sniper_phase0.utils.settings import Costs


@dataclass
class LabelRow:
    trade_id: int
    outcome: str
    exit_ts: pd.Timestamp
    exit_price: float
    gross_R: float
    net_R: float
    mae: float
    mfe: float


def label_one(
    trade_id: int,
    entry_ts: pd.Timestamp,
    entry_price: float,
    side: str,
    qty: int,
    stop_price: float,
    target_price: float,
    max_hold_minutes: int,
    forward_ticks: pd.DataFrame,
    costs: Costs,
    slippage_multiplier: float = 1.0,
    is_event_day: bool = False,
) -> LabelRow:
    if side not in ("long", "short"):
        raise ValueError(f"side must be 'long' or 'short', got {side!r}")
    if (side == "long" and not (stop_price < entry_price < target_price)) or (
        side == "short" and not (target_price < entry_price < stop_price)
    ):
        raise ValueError(
            f"Invalid barriers for {side}: entry={entry_price}, "
            f"stop={stop_price}, target={target_price}"
        )

    stop_distance = abs(entry_price - stop_price)
    if stop_distance == 0:
        raise ValueError("stop_distance is zero — degenerate barriers")

    forward = forward_ticks[forward_ticks["ts"] > entry_ts]
    forward = forward[
        forward["ts"] <= entry_ts + pd.Timedelta(minutes=max_hold_minutes)
    ]

    outcome = "timeout"
    exit_ts = entry_ts + pd.Timedelta(minutes=max_hold_minutes)
    exit_price = entry_price
    mae_abs = 0.0
    mfe_abs = 0.0

    for ts, ltp in zip(forward["ts"].to_numpy(), forward["ltp"].to_numpy()):
        if side == "long":
            mae_abs = max(mae_abs, entry_price - ltp)
            mfe_abs = max(mfe_abs, ltp - entry_price)
            if ltp <= stop_price:
                outcome, exit_ts, exit_price = "stop", ts, stop_price
                break
            if ltp >= target_price:
                outcome, exit_ts, exit_price = "target", ts, target_price
                break
        else:
            mae_abs = max(mae_abs, ltp - entry_price)
            mfe_abs = max(mfe_abs, entry_price - ltp)
            if ltp >= stop_price:
                outcome, exit_ts, exit_price = "stop", ts, stop_price
                break
            if ltp <= target_price:
                outcome, exit_ts, exit_price = "target", ts, target_price
                break

    if outcome == "timeout" and len(forward):
        exit_price = float(forward["ltp"].iloc[-1])
        exit_ts = pd.Timestamp(forward["ts"].iloc[-1])

    gross, net, _tc = net_pnl(
        entry_price, exit_price, qty, side, costs, slippage_multiplier, is_event_day
    )
    gross_R = gross / (stop_distance * qty)
    net_R = net / (stop_distance * qty)

    return LabelRow(
        trade_id=trade_id,
        outcome=outcome,
        exit_ts=exit_ts,
        exit_price=exit_price,
        gross_R=gross_R,
        net_R=net_R,
        mae=mae_abs / stop_distance,
        mfe=mfe_abs / stop_distance,
    )
