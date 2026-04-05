from __future__ import annotations

from typing import Any

from auction_intelligence.schemas import AgentDecision, PortfolioSnapshot, RiskDecision, SessionContext


class RiskGovernor:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.max_daily_loss = float(config.get("max_daily_loss", 75_000.0))
        self.max_agent_drawdown = float(config.get("max_agent_drawdown", 0.08))
        self.max_symbol_exposure = float(config.get("max_symbol_exposure", 0.35))
        self.max_correlated_exposure = float(config.get("max_correlated_exposure", 0.55))
        self.max_concurrent_positions = int(config.get("max_concurrent_positions", 3))
        self.session_close_buffer_minutes = int(config.get("session_close_buffer_minutes", 15))
        self.stale_data_seconds = int(config.get("stale_data_seconds", 10))
        self.min_model_confidence = float(config.get("min_model_confidence", 0.55))

    def evaluate(
        self,
        session: SessionContext,
        portfolio: PortfolioSnapshot,
        decisions: list[AgentDecision],
    ) -> RiskDecision:
        reasons: list[str] = []

        if not session.broker_connected:
            reasons.append("Broker connectivity unavailable.")
        if session.stale_data_seconds > self.stale_data_seconds:
            reasons.append("Market data is stale.")
        if portfolio.daily_realized_pnl <= -abs(self.max_daily_loss):
            reasons.append("Daily loss limit breached.")
        if portfolio.open_positions >= self.max_concurrent_positions and any(
            decision.action != "FLAT" for decision in decisions
        ):
            reasons.append("Max concurrent positions reached.")
        if session.minutes_to_close <= self.session_close_buffer_minutes and any(
            decision.action != "FLAT" for decision in decisions
        ):
            reasons.append("Too close to session close for new entries.")
        if portfolio.correlated_exposure >= self.max_correlated_exposure:
            reasons.append("Correlated exposure cap reached.")

        for decision in decisions:
            if decision.action == "FLAT":
                continue
            if decision.confidence < self.min_model_confidence:
                reasons.append(f"{decision.agent_name} confidence below threshold.")
            if portfolio.agent_drawdowns.get(decision.agent_name, 0.0) >= self.max_agent_drawdown:
                reasons.append(f"{decision.agent_name} drawdown cap reached.")
            symbol_exposure = portfolio.symbol_exposure.get(session.symbol, 0.0)
            if symbol_exposure >= self.max_symbol_exposure:
                reasons.append(f"{session.symbol} exposure cap reached.")

        if reasons:
            return RiskDecision(
                allowed=False,
                kill_switch=any(
                    reason in {
                        "Broker connectivity unavailable.",
                        "Market data is stale.",
                        "Daily loss limit breached.",
                    }
                    for reason in reasons
                ),
                max_size_multiplier=0.0,
                reasons=reasons,
            )

        return RiskDecision(allowed=True, kill_switch=False, max_size_multiplier=1.0, reasons=["Risk checks passed."])
