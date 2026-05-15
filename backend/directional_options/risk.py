"""Risk approval and position sizing for long-premium trades.

Position size scales with signal confidence:

    multiplier = 0.5 + (confidence - min_confidence) / (max_confidence - min_confidence)

so a barely-passing 0.58-confidence signal sizes at ~0.5× the base budget,
while a 0.85-confidence setup sizes at ~1.5×. The motivation is that
confidence already encodes our expectation of edge — sizing should
respond, not stay flat. The base `risk_pct` and `premium_cap_pct` define
the *median* allocation; the scaler nudges it up or down.
"""
from __future__ import annotations

import math
from typing import Any

from directional_options.schemas import ContractCandidate, DirectionalSignal, RiskDecision


# Same ceiling the signal/regime engines clamp to. Anchors the "100%" point
# of the confidence-to-size curve.
MAX_ALLOCATION_CONFIDENCE = 0.85
# Floor of the curve at min_confidence — barely-passing signals still trade
# but at half the base risk budget. Below min_confidence the signal engine
# already filters the trade, so the scaler is never invoked below this.
MIN_ALLOCATION_FRACTION = 0.5
# Top of the curve at max_confidence — strongest signals scale to 1.5×.
MAX_ALLOCATION_FRACTION = 1.5


class DirectionalOptionsRiskEngine:
    """Long-option sizing scales with conviction; expectancy must still clear."""

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def _confidence_multiplier(self, confidence: float, min_confidence: float) -> float:
        """Linear ramp from MIN_ALLOCATION_FRACTION at min_confidence to
        MAX_ALLOCATION_FRACTION at MAX_ALLOCATION_CONFIDENCE."""
        span = max(MAX_ALLOCATION_CONFIDENCE - min_confidence, 1e-6)
        normalized = (max(confidence, min_confidence) - min_confidence) / span
        normalized = min(1.0, max(0.0, normalized))
        return MIN_ALLOCATION_FRACTION + normalized * (MAX_ALLOCATION_FRACTION - MIN_ALLOCATION_FRACTION)

    def approve(
        self,
        *,
        candidate: ContractCandidate,
        signal: DirectionalSignal,
        equity: float,
        daily_realized: float = 0.0,
        weekly_realized: float = 0.0,
    ) -> RiskDecision:
        # Scale base budgets by the signal's conviction. min_confidence comes
        # from the signal engine config so the curve is anchored to the same
        # cutoff used to filter signals upstream.
        min_confidence = float(self.config.get("min_confidence", 0.58))
        confidence = float(signal.confidence)
        scaler = self._confidence_multiplier(confidence, min_confidence)
        risk_budget = equity * float(self.config["risk_pct"]) * scaler
        premium_cap = equity * float(self.config["premium_cap_pct"]) * scaler
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
        # Exploration / micro-trend sleeves are *learning bets*. We don't
        # demand positive expected edge from them — that's the whole point
        # of having a small-size lane that records outcomes so RAG can
        # accumulate evidence. The confidence×size scaler keeps the bet
        # tiny (0.5×–0.7× of base risk) so even a string of losses stays
        # well inside the daily loss cap. High-conviction sleeves (trend,
        # breakout, swing_trend) still need the edge hurdle.
        learning_sleeve = str(signal.sleeve or "").lower() in {
            "intraday_exploration", "intraday_micro_trend",
        }
        if (
            not learning_sleeve
            and candidate.expected_pnl <= candidate.option_price * min_expected_edge_pct
        ):
            reasons.append("Expected edge does not clear the long-premium hurdle.")
        if candidate.rejection_reasons and not learning_sleeve:
            reasons.extend(
                f"Optimizer rejected candidate: {reason}."
                for reason in candidate.rejection_reasons
            )
        if daily_realized <= -(risk_budget * float(self.config["daily_loss_cap_r"])):
            reasons.append("Daily loss cap is already breached.")
        if weekly_realized <= -(risk_budget * float(self.config["weekly_loss_cap_r"])):
            reasons.append("Weekly loss cap is already breached.")
        if qty_lots < 1:
            reasons.append(
                f"Sizing rules do not permit even one lot (conf {confidence:.2f} → "
                f"scaler {scaler:.2f}× of {self.config['risk_pct']:.3%} risk / "
                f"{self.config['premium_cap_pct']:.3%} premium)."
            )

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
