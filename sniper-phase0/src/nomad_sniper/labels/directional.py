"""Directional grid labeler.

For each decision-grid point, run symmetric ATR triple-barrier geometry on the underlying forward
path, then apply the option-economics gate. Output one row per grid point with the five heads from
the feature contract: direction, is_move, magnitude_atr, time_to_target, and mae_atr.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

import numpy as np
import pandas as pd
from tqdm import tqdm

from nomad_sniper.data.option_bars import ATMOptionSeries
from nomad_sniper.labels.profitability_gate import (
    GateContext,
    ProfitabilityGate,
    build_profitability_gate,
)
from nomad_sniper.utils.normalize import atr_reference
from nomad_sniper.utils.timeutil import ensure_ist

DIRECTION_TO_CLASS = {"none": 0, "up": 1, "down": 2}
CLASS_TO_DIRECTION = {v: k for k, v in DIRECTION_TO_CLASS.items()}


def label_directional_point(
    bars: pd.DataFrame,
    decision_time: datetime,
    *,
    atr_ref: float | None = None,
    horizon_minutes: int = 60,
    barrier_m: float = 1.0,
    gate: ProfitabilityGate | None = None,
    atm_series: ATMOptionSeries | None = None,
) -> dict | None:
    """Label one decision point from the underlying forward path."""
    decision_time = ensure_ist(decision_time)
    if atr_ref is None:
        atr_ref = atr_reference(bars, decision_time.date())
    if atr_ref is None or atr_ref <= 0:
        return None

    forward = bars[(bars.index > decision_time) & (bars.index <= decision_time + timedelta(minutes=horizon_minutes))]
    if forward.empty:
        return None

    entry_price = float(forward.iloc[0]["open"])
    up_barrier = entry_price + barrier_m * atr_ref
    down_barrier = entry_price - barrier_m * atr_ref
    mfe = 0.0
    mae = 0.0
    hit_direction = "none"
    exit_time = ensure_ist(forward.index[-1].to_pydatetime())
    exit_price = float(forward.iloc[-1]["close"])
    time_to_target = float(horizon_minutes)

    for ts, bar in forward.iterrows():
        high = float(bar["high"])
        low = float(bar["low"])
        mfe = max(mfe, high - entry_price, entry_price - low)
        up_adv = max(0.0, entry_price - low)
        down_adv = max(0.0, high - entry_price)

        up_hit = high >= up_barrier
        down_hit = low <= down_barrier
        if up_hit and down_hit:
            hit_direction = "none"
            exit_time = ensure_ist(ts.to_pydatetime())
            exit_price = float(bar["close"])
            time_to_target = (exit_time - decision_time).total_seconds() / 60.0
            mae = max(up_adv, down_adv)
            break
        if up_hit:
            hit_direction = "up"
            exit_time = ensure_ist(ts.to_pydatetime())
            exit_price = up_barrier
            time_to_target = (exit_time - decision_time).total_seconds() / 60.0
            mae = up_adv
            break
        if down_hit:
            hit_direction = "down"
            exit_time = ensure_ist(ts.to_pydatetime())
            exit_price = down_barrier
            time_to_target = (exit_time - decision_time).total_seconds() / 60.0
            mae = down_adv
            break
        mae = max(mae, min(up_adv, down_adv))

    magnitude_atr = mfe / atr_ref
    mae_atr = mae / atr_ref
    gate = gate or build_profitability_gate("atr_proxy")
    final_direction = gate.keep_direction(
        GateContext(
            candidate_direction=hit_direction,
            barrier_m=barrier_m,
            magnitude_atr=magnitude_atr,
            time_to_target_minutes=time_to_target,
            horizon_minutes=horizon_minutes,
            entry_price=entry_price,
            exit_price=exit_price,
            atm_series=atm_series,
            decision_time=decision_time,
            exit_time=exit_time,
        )
    )

    if final_direction == "none":
        is_move = 0
    else:
        is_move = 1
    return {
        "decision_time": decision_time,
        "label_end_time": decision_time + timedelta(minutes=horizon_minutes),
        "entry_price": entry_price,
        "exit_time": exit_time,
        "exit_price": exit_price,
        "candidate_direction": hit_direction,
        "direction": final_direction,
        "direction_class": DIRECTION_TO_CLASS[final_direction],
        "is_move": is_move,
        "magnitude_atr": float(magnitude_atr),
        "time_to_target": float(time_to_target),
        "mae_atr": float(mae_atr),
        "sample_weight": 1.0,
    }


def build_directional_labels_for_grid(
    grid_points: Iterable[tuple[str, datetime]],
    bars_by_underlying: dict[str, pd.DataFrame],
    *,
    atm_by_underlying: dict[tuple[str, object], ATMOptionSeries] | None = None,
    horizon_minutes: int = 60,
    barrier_m: float = 1.0,
    gate_mode: str = "atr_proxy",
    m_breakeven: float = 0.75,
) -> pd.DataFrame:
    gate = build_profitability_gate(gate_mode, m_breakeven=m_breakeven)
    rows = []
    for underlying, dt in tqdm(list(grid_points), desc="labels"):
        bars = bars_by_underlying.get(underlying)
        if bars is None:
            continue
        atm = None
        if atm_by_underlying is not None:
            atm = atm_by_underlying.get((underlying, dt.date())) or atm_by_underlying.get(underlying)
        row = label_directional_point(
            bars,
            dt,
            horizon_minutes=horizon_minutes,
            barrier_m=barrier_m,
            gate=gate,
            atm_series=atm,
        )
        if row is None:
            continue
        row["underlying"] = underlying
        row["row_id"] = f"{underlying}|{pd.Timestamp(row['decision_time']).isoformat()}"
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).set_index("row_id")
    return df.replace([np.inf, -np.inf], np.nan)
