"""Execution alpha layer: order style, pullback/wait decisions, and slippage estimate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

import numpy as np

ExecutionAction = Literal["market_order", "passive_limit_order", "wait_for_pullback", "scale_in", "cancel_signal"]


@dataclass(frozen=True)
class ExecutionInstruction:
    action: ExecutionAction
    slippage_estimate_atr: float
    adverse_selection_score: float
    fill_probability: float
    reasons: list[str]


def choose_execution(
    features: Mapping[str, object],
    *,
    meta_action: str = "take",
    urgency: float = 0.5,
) -> ExecutionInstruction:
    spread_z = _num(features, "x_spread_zscore")
    book_imbalance = abs(_num(features, "u_book_imbalance_pct"))
    sweep = max(_num(features, "u_buy_sweep_intensity"), _num(features, "u_sell_sweep_intensity"))
    volume_z = _num(features, "u_volume_z")
    range_consumed = _num(features, "c_range_consumed_pct")

    adverse = float(np.clip(0.25 * max(0, spread_z) + 0.25 * sweep + 0.2 * book_imbalance + 0.3 * (range_consumed / 100), 0, 1))
    fill_prob = float(np.clip(0.7 + 0.1 * volume_z - 0.35 * max(0, spread_z) - 0.2 * book_imbalance, 0.05, 0.98))
    slippage = float(np.clip(0.02 + 0.04 * max(0, spread_z) + 0.05 * sweep + 0.04 * book_imbalance, 0.01, 0.5))
    reasons: list[str] = []
    if meta_action in {"skip", "wait"}:
        return ExecutionInstruction("cancel_signal", slippage, adverse, fill_prob, [f"meta action is {meta_action}"])
    if meta_action == "pullback_only" or adverse > 0.65 or range_consumed > 90:
        reasons.append("adverse selection or late move risk")
        return ExecutionInstruction("wait_for_pullback", slippage, adverse, fill_prob, reasons)
    if urgency >= 0.75 and spread_z < 1.0 and adverse < 0.55:
        return ExecutionInstruction("market_order", slippage, adverse, fill_prob, ["urgent signal with acceptable spread"])
    if fill_prob >= 0.55:
        return ExecutionInstruction("passive_limit_order", slippage * 0.6, adverse, fill_prob, ["passive fill likely"])
    return ExecutionInstruction("scale_in", slippage, adverse, fill_prob, ["uncertain fill quality"])


def _num(row: Mapping[str, object], key: str, default: float = 0.0) -> float:
    value = row.get(key, default)
    if value is None:
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if np.isfinite(out) else default
