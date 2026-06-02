"""Mixture-of-experts setup scoring.

These scores act as the gating inputs for specialist models. They are not trade rules; they
quantify which auction archetype is most relevant now.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np


@dataclass(frozen=True)
class SetupExpertScores:
    scores: dict[str, float]
    selected_expert: str
    confidence: float


def score_setup_experts(features: Mapping[str, object]) -> SetupExpertScores:
    scores = {
        "value_acceptance": _clip(_num(features, "u_acceptance_above_value_score") + _num(features, "u_acceptance_below_value_score")),
        "value_rejection": _clip(_num(features, "u_rejection_from_value_score")),
        "failed_auction": _clip(max(_num(features, "u_prev_poor_high"), _num(features, "u_prev_poor_low")) + max(0, -_num(features, "u_price_delta_divergence"))),
        "ib_breakout": _clip(max(_num(features, "u_price_above_ib"), _num(features, "u_price_below_ib")) + _num(features, "u_range_expansion_score")),
        "poc_rotation": _clip(1.0 - abs(_num(features, "u_dist_dev_poc_atr"))),
        "lvn_rejection": _clip(1.0 - abs(_num(features, "u_dist_nearest_lvn_atr", 2.0))),
        "trend_day_continuation": _clip(_num(features, "u_trend_day_score") + _num(features, "h_bullish_confluence") + _num(features, "h_bearish_confluence")),
        "expiry_day_trap": _clip(_num(features, "c_is_expiry_day") + max(0, _num(features, "h_timeframe_conflict"))),
    }
    selected = max(scores, key=scores.get)
    ordered = sorted(scores.values(), reverse=True)
    confidence = ordered[0] - ordered[1] if len(ordered) > 1 else ordered[0]
    return SetupExpertScores(scores=scores, selected_expert=selected, confidence=float(_clip(confidence)))


def _num(row: Mapping[str, object], key: str, default: float = 0.0) -> float:
    value = row.get(key, default)
    if value is None:
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if np.isfinite(out) else default


def _clip(x: float) -> float:
    return float(np.clip(x, 0, 1))
