from __future__ import annotations

from math import floor

from auction_intelligence.agents.base import StrategyAgent
from auction_intelligence.schemas import AgentContext, AgentDecision


class SwingAgent(StrategyAgent):
    def __init__(self, config: dict):
        super().__init__("swing", config)

    def evaluate(self, context: AgentContext) -> AgentDecision:
        regime = context.regime
        current = context.current_profile
        flow = context.order_flow

        min_confidence = float(self.config.get("min_confidence", 0.62))
        sleeve_fraction = float(self.config.get("sleeve_fraction", 0.35))
        lot_size = int(self.config.get("lot_size", 25))
        risk_multiple = float(self.config.get("risk_multiple", 2.0))
        max_notional = context.portfolio.net_liquidation * sleeve_fraction

        action = "FLAT"
        rationale = list(regime.reasons)
        entry = current.close_price
        stop = None
        target = None

        if regime.label in {"breakout_acceptance", "trend_continuation", "trend_day"} and "LONG" in regime.allowed_directions:
            if current.close_price >= current.vah and flow.delta >= 0:
                action = "LONG"
                stop = max(current.poc, current.initial_balance_low)
                rationale.append("Swing agent aligns with higher value acceptance and non-negative delta.")
        elif regime.label in {"breakout_acceptance", "trend_continuation", "trend_day"} and "SHORT" in regime.allowed_directions:
            if current.close_price <= current.val and flow.delta <= 0:
                action = "SHORT"
                stop = min(current.poc, current.initial_balance_high)
                rationale.append("Swing agent aligns with lower value acceptance and non-positive delta.")
        elif regime.label in {"failed_auction", "breakout_rejection", "reversal"} and "SHORT" in regime.allowed_directions:
            action = "SHORT"
            stop = current.high_price
            rationale.append("Swing agent fades rejected upside auction.")
        elif regime.label in {"failed_auction", "breakout_rejection", "reversal"} and "LONG" in regime.allowed_directions:
            action = "LONG"
            stop = current.low_price
            rationale.append("Swing agent buys rejected downside auction.")

        confidence = round((0.6 * regime.confidence) + (0.4 * flow.timing_confidence), 4)
        if action == "FLAT" or confidence < min_confidence or stop is None or stop == entry:
            rationale.append("Swing thresholds were not met.")
            return self._flat(rationale)

        per_unit_risk = abs(entry - stop)
        target = entry + (risk_multiple * per_unit_risk) if action == "LONG" else entry - (risk_multiple * per_unit_risk)
        quantity = floor(max_notional / max(entry * lot_size, 1.0)) * lot_size
        quantity = max(quantity, lot_size if max_notional >= entry * lot_size else 0)

        if quantity <= 0:
            rationale.append("Portfolio sleeve is too small for the configured lot size.")
            return self._flat(rationale)

        return AgentDecision(
            agent_name=self.name,
            action=action,
            confidence=confidence,
            entry_price=round(entry, 4),
            stop_price=round(stop, 4),
            target_price=round(target, 4),
            quantity=quantity,
            sleeve_fraction=sleeve_fraction,
            rationale=rationale,
            metadata={"regime": regime.label},
        )
