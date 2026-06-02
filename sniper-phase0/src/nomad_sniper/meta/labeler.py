"""Meta-labeling layer: take, skip, wait, reduce size, or require pullback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

import numpy as np

from nomad_sniper.regime.classifier import RegimeState

MetaAction = Literal["take", "skip", "wait", "reduce_size", "pullback_only"]


@dataclass(frozen=True)
class MetaDecision:
    action: MetaAction
    score: float
    size_multiplier: float
    reasons: list[str]


def decide_meta_action(
    prediction: Mapping[str, object],
    features: Mapping[str, object],
    regime: RegimeState,
    *,
    min_move_probability: float = 0.52,
    min_edge_atr: float = 0.15,
) -> MetaDecision:
    direction = str(prediction.get("pred_direction", "none"))
    p_move = _num(prediction, "p_is_move")
    p_dir = max(_num(prediction, "p_up"), _num(prediction, "p_down"))
    exp_mfe = _num(prediction, "pred_magnitude_atr")
    exp_mae = _num(prediction, "pred_mae_atr", 0.4)
    expected_edge = exp_mfe - exp_mae
    conflict = _num(features, "h_timeframe_conflict")
    range_consumed = _num(features, "c_range_consumed_pct")

    reasons: list[str] = []
    score = 0.5 * p_move + 0.3 * p_dir + 0.2 * regime.regime_score
    if direction == "none":
        return MetaDecision("skip", 0.0, 0.0, ["model selected no trade"])
    if p_move < min_move_probability:
        return MetaDecision("skip", float(score), 0.0, ["move probability below threshold"])
    if expected_edge < min_edge_atr:
        return MetaDecision("skip", float(score), 0.0, ["expected edge below threshold"])
    if conflict >= 0.65:
        reasons.append("higher-timeframe conflict")
        return MetaDecision("reduce_size", float(score), 0.5, reasons)
    if range_consumed >= 90:
        reasons.append("late move / range consumed")
        return MetaDecision("pullback_only", float(score), 0.6, reasons)
    if regime.liquidity_regime == "thin":
        reasons.append("thin liquidity")
        return MetaDecision("wait", float(score), 0.0, reasons)
    if regime.expiry_regime != "normal":
        reasons.append(regime.expiry_regime)
        return MetaDecision("reduce_size", float(score), 0.65, reasons)
    return MetaDecision("take", float(np.clip(score, 0, 1)), 1.0, reasons or ["edge and regime acceptable"])


def _num(row: Mapping[str, object], key: str, default: float = 0.0) -> float:
    value = row.get(key, default)
    if value is None:
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if np.isfinite(out) else default
