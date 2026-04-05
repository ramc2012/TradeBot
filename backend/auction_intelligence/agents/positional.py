from __future__ import annotations

from auction_intelligence.agents.base import StrategyAgent
from auction_intelligence.schemas import AgentContext, AgentDecision


class PositionalAgent(StrategyAgent):
    def __init__(self, config: dict):
        super().__init__("positional", config)

    def evaluate(self, context: AgentContext) -> AgentDecision:
        current = context.current_profile
        prior = context.prior_profile
        rationale = ["Positional sleeve is scaffolded for higher-timeframe bias only in MVP."]
        if prior is None:
            rationale.append("No prior composite profile supplied.")
            return self._flat(rationale)

        confidence = 0.0
        action = "FLAT"
        if (current.value_migration or 0.0) > 0 and current.close_price > current.poc:
            action = "LONG"
            confidence = 0.58
            rationale.append("Value migrated higher relative to the prior session.")
        elif (current.value_migration or 0.0) < 0 and current.close_price < current.poc:
            action = "SHORT"
            confidence = 0.58
            rationale.append("Value migrated lower relative to the prior session.")

        if confidence < float(self.config.get("min_confidence", 0.70)):
            rationale.append("Confidence below positional deployment threshold.")
            return self._flat(rationale)

        return AgentDecision(
            agent_name=self.name,
            action=action,
            confidence=confidence,
            entry_price=current.close_price,
            stop_price=current.val if action == "LONG" else current.vah,
            target_price=current.close_price + 2.5 * current.initial_balance_range if action == "LONG" else current.close_price - 2.5 * current.initial_balance_range,
            quantity=int(self.config.get("lot_size", 25)),
            sleeve_fraction=float(self.config.get("sleeve_fraction", 0.45)),
            rationale=rationale,
            metadata={"mode": "bias_only"},
        )
