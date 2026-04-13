from __future__ import annotations

from abc import ABC, abstractmethod
from math import floor
from typing import Any

from auction_intelligence.schemas import AgentContext, AgentDecision


class StrategyAgent(ABC):
    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.config = config

    @abstractmethod
    def evaluate(self, context: AgentContext) -> AgentDecision:
        raise NotImplementedError

    def _normalized_symbol(self, symbol: str) -> str:
        return str(symbol or "").upper().replace(" INDEX", "").replace(" FUT", "").strip()

    def _contract_spec(self, context: AgentContext) -> dict[str, Any]:
        symbol_key = self._normalized_symbol(context.session.symbol)
        return context.config.get("contract_specs", {}).get(symbol_key, {})

    def _lot_size(self, context: AgentContext) -> int:
        contract_spec = self._contract_spec(context)
        return int(contract_spec.get("lot_size", self.config.get("lot_size", 25)))

    def _margin_fraction_per_lot(self, context: AgentContext) -> float:
        contract_spec = self._contract_spec(context)
        return float(contract_spec.get("margin_fraction_per_lot", self.config.get("margin_fraction_per_lot", 1.0)))

    def _size_quantity(
        self,
        context: AgentContext,
        *,
        entry_price: float,
        sleeve_fraction: float,
    ) -> tuple[int, int, float]:
        lot_size = self._lot_size(context)
        margin_fraction_per_lot = self._margin_fraction_per_lot(context)
        max_notional = context.portfolio.net_liquidation * sleeve_fraction
        margin_per_lot = max(entry_price * lot_size * margin_fraction_per_lot, 1.0)
        quantity = floor(max_notional / margin_per_lot) * lot_size
        return quantity, lot_size, margin_fraction_per_lot

    def _risk_target(
        self,
        *,
        action: str,
        entry_price: float,
        stop_price: float,
        risk_multiple: float,
    ) -> float:
        per_unit_risk = abs(entry_price - stop_price)
        if action == "LONG":
            return entry_price + (risk_multiple * per_unit_risk)
        return entry_price - (risk_multiple * per_unit_risk)

    def _bounded_tolerance(
        self,
        *,
        reference_range: float,
        fraction: float,
        minimum: float,
        maximum: float,
    ) -> float:
        tolerance = max(minimum, reference_range * fraction)
        if maximum > 0:
            tolerance = min(tolerance, maximum)
        return round(max(tolerance, 0.0), 4)

    def _flat(
        self,
        rationale: list[str],
        *,
        confidence: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> AgentDecision:
        return AgentDecision(
            agent_name=self.name,
            action="FLAT",
            confidence=confidence,
            entry_price=None,
            stop_price=None,
            target_price=None,
            quantity=0,
            sleeve_fraction=float(self.config.get("sleeve_fraction", 0.0)),
            rationale=rationale,
            metadata=metadata or {},
        )
