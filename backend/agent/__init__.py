"""Lightweight exports for the agent package.

Keep imports lazy so modules that only need signal/config helpers do not
pull in the heavier LLM trading agent at import time.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.rules_engine import RulesEngine
    from agent.trading_agent import TradeProposal, TradingAgent, trading_agent


__all__ = ["RulesEngine", "TradeProposal", "TradingAgent", "trading_agent"]


def __getattr__(name: str):
    if name == "RulesEngine":
        from agent.rules_engine import RulesEngine

        return RulesEngine
    if name in {"TradeProposal", "TradingAgent", "trading_agent"}:
        from agent.trading_agent import TradeProposal, TradingAgent, trading_agent

        exports = {
            "TradeProposal": TradeProposal,
            "TradingAgent": TradingAgent,
            "trading_agent": trading_agent,
        }
        return exports[name]
    raise AttributeError(name)
