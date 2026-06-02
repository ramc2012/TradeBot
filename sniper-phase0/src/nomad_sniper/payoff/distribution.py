"""Payoff-distribution v1.

Uses the multi-head model predictions to estimate expected R, target probability, tail risk, and
payoff asymmetry. This is deterministic calibration scaffolding until enough OOS trades exist to
fit a proper distributional model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np


@dataclass(frozen=True)
class PayoffEstimate:
    p_target_before_stop: float
    expected_r: float
    expected_mfe_r: float
    expected_mae_r: float
    expected_time_to_target: float
    expected_time_to_stop: float
    tail_loss_probability: float
    payoff_asymmetry_score: float


def estimate_payoff(prediction: Mapping[str, object], *, cost_r: float = 0.05) -> PayoffEstimate:
    p_up = _num(prediction, "p_up")
    p_down = _num(prediction, "p_down")
    p_none = _num(prediction, "p_none")
    p_move = _num(prediction, "p_is_move", max(p_up, p_down))
    mfe = max(0.0, _num(prediction, "pred_magnitude_atr"))
    mae = max(0.01, _num(prediction, "pred_mae_atr", 0.4))
    t_target = max(1.0, _num(prediction, "pred_time_to_target", 60.0))

    p_target = float(np.clip(p_move * max(p_up, p_down) / max(1e-6, 1 - p_none), 0, 1))
    expected_r = p_target * mfe - (1 - p_target) * mae - cost_r
    asymmetry = mfe / max(mae, 1e-6)
    tail_loss = float(np.clip((1 - p_target) * min(1.0, mae), 0, 1))
    return PayoffEstimate(
        p_target_before_stop=p_target,
        expected_r=float(expected_r),
        expected_mfe_r=float(mfe),
        expected_mae_r=float(mae),
        expected_time_to_target=float(t_target),
        expected_time_to_stop=float(t_target * (1.0 + tail_loss)),
        tail_loss_probability=tail_loss,
        payoff_asymmetry_score=float(np.clip(asymmetry / 3.0, 0, 1)),
    )


def _num(row: Mapping[str, object], key: str, default: float = 0.0) -> float:
    value = row.get(key, default)
    if value is None:
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if np.isfinite(out) else default
