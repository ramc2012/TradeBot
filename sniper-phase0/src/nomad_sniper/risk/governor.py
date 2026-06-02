"""Risk governor with dynamic sizing and kill-switch checks."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class RiskState:
    daily_pnl_r: float = 0.0
    weekly_pnl_r: float = 0.0
    open_risk_r: float = 0.0
    loss_streak: int = 0
    model_drift_score: float = 0.0
    slippage_spike_score: float = 0.0
    volatility_shock_score: float = 0.0


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    size_multiplier: float
    kill_switch: bool
    reasons: list[str]


def apply_risk_governor(
    *,
    edge_score: float,
    liquidity_score: float,
    regime_score: float,
    meta_size_multiplier: float,
    state: RiskState | None = None,
    max_daily_loss_r: float = -3.0,
    max_weekly_loss_r: float = -7.0,
    max_open_risk_r: float = 3.0,
) -> RiskDecision:
    state = state or RiskState()
    reasons: list[str] = []
    kill = False
    if state.daily_pnl_r <= max_daily_loss_r:
        kill = True
        reasons.append("max daily loss breached")
    if state.weekly_pnl_r <= max_weekly_loss_r:
        kill = True
        reasons.append("max weekly loss breached")
    if state.open_risk_r >= max_open_risk_r:
        kill = True
        reasons.append("max open risk breached")
    if state.model_drift_score >= 0.8:
        kill = True
        reasons.append("model drift high")
    if state.slippage_spike_score >= 0.8:
        kill = True
        reasons.append("slippage spike")
    if state.volatility_shock_score >= 0.9:
        kill = True
        reasons.append("volatility shock")
    if kill:
        return RiskDecision(False, 0.0, True, reasons)

    drawdown_factor = 0.5 if state.loss_streak >= 3 else 0.75 if state.loss_streak >= 2 else 1.0
    drift_factor = 1.0 - 0.5 * np.clip(state.model_drift_score, 0, 1)
    size = edge_score * liquidity_score * regime_score * meta_size_multiplier * drawdown_factor * drift_factor
    size = float(np.clip(size, 0, 1.5))
    if size <= 0.05:
        return RiskDecision(False, 0.0, False, reasons + ["size below minimum"])
    return RiskDecision(True, size, False, reasons or ["risk limits clear"])
