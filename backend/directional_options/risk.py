"""Risk approval and position sizing for long-premium trades."""
from __future__ import annotations

import math
from typing import Any

from directional_options.schemas import ContractCandidate, DirectionalSignal, RiskDecision


class DirectionalOptionsRiskEngine:
    """Keep long-option sizing small and only approve clear expectancy."""

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def approve(
        self,
        *,
        candidate: ContractCandidate,
        signal: DirectionalSignal,
        equity: float,
        daily_realized: float = 0.0,
        weekly_realized: float = 0.0,
    ) -> RiskDecision:
        risk_budget = equity * float(self.config["risk_pct"])
        premium_cap = equity * float(self.config["premium_cap_pct"])
        planned_stop_pct = float(self.config["planned_stop_pct"])
        min_expected_edge_pct = float(self.config["min_expected_edge_pct"])
        fee_per_unit = 0.45

        stop_loss_per_unit = candidate.option_price * planned_stop_pct
        lot_premium = candidate.option_price * candidate.lot_size
        lot_risk = max(1.0, (stop_loss_per_unit + fee_per_unit) * candidate.lot_size)
        max_lots_by_risk = math.floor(risk_budget / lot_risk)
        max_lots_by_premium = math.floor(premium_cap / max(lot_premium, 1.0))
        qty_lots = max(0, min(max_lots_by_risk, max_lots_by_premium))

        reasons: list[str] = []
        if candidate.expected_pnl <= candidate.option_price * min_expected_edge_pct:
            reasons.append("Expected edge does not clear the long-premium hurdle.")
        if candidate.rejection_reasons:
            reasons.extend(f"Optimizer rejected candidate: {reason}." for reason in candidate.rejection_reasons)
        if daily_realized <= -(risk_budget * float(self.config["daily_loss_cap_r"])):
            reasons.append("Daily loss cap is already breached.")
        if weekly_realized <= -(risk_budget * float(self.config["weekly_loss_cap_r"])):
            reasons.append("Weekly loss cap is already breached.")
        if qty_lots < 1:
            reasons.append("Sizing rules do not permit even one lot.")

        return RiskDecision(
            approved=not reasons,
            quantity_lots=max(qty_lots, 0),
            quantity_units=max(qty_lots, 0) * candidate.lot_size,
            premium_at_risk=round(max(qty_lots, 0) * lot_premium, 2),
            max_loss=round(max(qty_lots, 0) * lot_risk, 2),
            risk_budget=round(risk_budget, 2),
            premium_cap=round(premium_cap, 2),
            reasons=reasons,
        )
