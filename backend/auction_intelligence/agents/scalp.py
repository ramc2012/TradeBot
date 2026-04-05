from __future__ import annotations

from auction_intelligence.agents.base import StrategyAgent
from auction_intelligence.schemas import AgentContext, AgentDecision


class ScalpAgent(StrategyAgent):
    def __init__(self, config: dict):
        super().__init__("scalp", config)

    def evaluate(self, context: AgentContext) -> AgentDecision:
        rationale = ["Scalp sleeve is present but intentionally conservative until live data replay is validated."]
        flow = context.order_flow
        if flow.execution_aggression == "WAIT" or flow.timing_confidence < float(self.config.get("min_confidence", 0.68)):
            rationale.append("Microstructure timing did not justify a scalp entry.")
            return self._flat(rationale)

        action = "LONG" if flow.delta > 0 and flow.top_imbalance >= 0 else "SHORT" if flow.delta < 0 and flow.top_imbalance <= 0 else "FLAT"
        if action == "FLAT":
            rationale.append("Order flow is not directionally aligned.")
            return self._flat(rationale)

        entry = context.session.last_price
        stop = entry - flow.micro_stop_distance if action == "LONG" else entry + flow.micro_stop_distance
        target = entry + (1.2 * flow.micro_stop_distance) if action == "LONG" else entry - (1.2 * flow.micro_stop_distance)
        rationale.append("Scalp sleeve follows aligned delta and queue pressure.")
        return AgentDecision(
            agent_name=self.name,
            action=action,
            confidence=flow.timing_confidence,
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            quantity=int(self.config.get("lot_size", 25)),
            sleeve_fraction=float(self.config.get("sleeve_fraction", 0.2)),
            rationale=rationale,
            metadata={"micro": True},
        )
