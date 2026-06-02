"""Regime intelligence layer.

This v1 classifier is deterministic and feature-contract aware. It can be replaced by a trained
model later, but it already exposes the regime fields required by the full alpha-machine spec:
trend/balance/event/expiry/liquidity context and a sizing-compatible regime score.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np


@dataclass(frozen=True)
class RegimeState:
    regime: str
    trend_strength: float
    volatility_regime: str
    auction_regime: str
    liquidity_regime: str
    expiry_regime: str
    regime_score: float
    reasons: list[str]


def classify_regime(features: Mapping[str, object]) -> RegimeState:
    """Classify the current auction regime from normalized feature values."""
    bull = _num(features, "h_bullish_confluence")
    bear = _num(features, "h_bearish_confluence")
    conflict = _num(features, "h_timeframe_conflict")
    range_expansion = _num(features, "u_range_expansion_score")
    trend_day = _num(features, "u_trend_day_score")
    balanced_day = _num(features, "u_balanced_day_score")
    atr_pct = _num(features, "c_atr_percentile")
    volume_z = _num(features, "u_volume_z")
    dte = _num(features, "c_days_to_weekly_expiry")

    directional_bias = bull - bear
    trend_strength = float(np.clip(abs(directional_bias) + trend_day + range_expansion * 0.5, 0, 1))
    if trend_strength >= 0.65 and directional_bias > 0:
        regime = "trend_up"
    elif trend_strength >= 0.65 and directional_bias < 0:
        regime = "trend_down"
    elif balanced_day >= 0.55 or conflict >= 0.5:
        regime = "balance_rotation"
    elif range_expansion >= 0.6:
        regime = "breakout_expansion"
    else:
        regime = "mixed"

    volatility_regime = "high_vol" if atr_pct >= 75 else "low_vol" if atr_pct <= 25 else "normal_vol"
    liquidity_regime = "thin" if volume_z < -1.5 else "active" if volume_z > 1.5 else "normal"
    expiry_regime = "expiry_day" if dte == 0 else "pre_expiry" if dte == 1 else "normal"
    auction_regime = "imbalanced" if trend_strength >= 0.65 else "balanced" if balanced_day >= 0.55 else "mixed"

    score = 0.55
    reasons: list[str] = []
    if regime in {"trend_up", "trend_down", "breakout_expansion"}:
        score += 0.18
        reasons.append(f"{regime} supports directional risk")
    if regime == "balance_rotation":
        score -= 0.08
        reasons.append("balanced auction favors selectivity")
    if conflict >= 0.5:
        score -= 0.15
        reasons.append("higher-timeframe conflict")
    if volatility_regime == "high_vol":
        score -= 0.06
        reasons.append("high volatility regime")
    if liquidity_regime == "thin":
        score -= 0.12
        reasons.append("thin liquidity")
    if expiry_regime != "normal":
        score -= 0.05
        reasons.append(expiry_regime)

    return RegimeState(
        regime=regime,
        trend_strength=trend_strength,
        volatility_regime=volatility_regime,
        auction_regime=auction_regime,
        liquidity_regime=liquidity_regime,
        expiry_regime=expiry_regime,
        regime_score=float(np.clip(score, 0, 1)),
        reasons=reasons,
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
