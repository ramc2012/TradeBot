from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from auction_intelligence.schemas import AgentContext, AgentDecision


class StrategyAgent(ABC):
    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.config = config

    @abstractmethod
    def evaluate(self, context: AgentContext) -> AgentDecision:
        raise NotImplementedError

    def _flat(self, rationale: list[str]) -> AgentDecision:
        return AgentDecision(
            agent_name=self.name,
            action="FLAT",
            confidence=0.0,
            entry_price=None,
            stop_price=None,
            target_price=None,
            quantity=0,
            sleeve_fraction=float(self.config.get("sleeve_fraction", 0.0)),
            rationale=rationale,
            metadata={},
        )
