from __future__ import annotations

from typing import Any

from loguru import logger

from auction_intelligence.schemas import AgentDecision, PortfolioSnapshot, RiskDecision, SessionContext


class RiskGovernor:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.contract_specs = config.get("contract_specs", {})
        self.scope = config.get("mvp_scope", {})
        self.max_daily_loss = float(config.get("max_daily_loss", 75_000.0))
        self.max_agent_drawdown = float(config.get("max_agent_drawdown", 0.08))
        self.max_symbol_exposure = float(config.get("max_symbol_exposure", 0.35))
        self.max_correlated_exposure = float(config.get("max_correlated_exposure", 0.55))
        self.max_concurrent_positions = int(config.get("max_concurrent_positions", 3))
        self.session_close_buffer_minutes = int(config.get("session_close_buffer_minutes", 15))
        self.stale_data_seconds = int(config.get("stale_data_seconds", 10))
        self.min_model_confidence = float(config.get("min_model_confidence", 0.55))
        self.paper_mode = bool(config.get("paper_mode", False))

    def evaluate(
        self,
        session: SessionContext,
        portfolio: PortfolioSnapshot,
        decisions: list[AgentDecision],
    ) -> RiskDecision:
        reasons: list[str] = []
        gate_log: dict[str, Any] = {
            "symbol": session.symbol,
            "broker_connected": session.broker_connected,
            "stale_data_seconds": session.stale_data_seconds,
            "minutes_to_close": session.minutes_to_close,
            "open_positions": portfolio.open_positions,
            "daily_realized_pnl": portfolio.daily_realized_pnl,
            "decisions": [(d.agent_name, d.action, d.confidence) for d in decisions],
            "paper_mode": self.paper_mode,
        }

        if not session.broker_connected and not self.paper_mode:
            reasons.append("Broker connectivity unavailable.")
        if session.stale_data_seconds > self.stale_data_seconds and not self.paper_mode:
            reasons.append("Market data is stale.")
        # Daily-loss circuit breaker runs in PAPER mode too (it is the lane-local
        # kill-switch). The broker-connectivity and stale-data checks keep their
        # paper bypass (no broker / tight 10s tick window would false-halt the
        # paper desk), but a real drawdown on the paper book must still halt new
        # entries. Requires the paper book's realized P&L to be wired into
        # PortfolioSnapshot.daily_realized_pnl (done in automation.py).
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
        correlated_exposure = self._normalize_aggregate_exposure(
            portfolio.correlated_exposure,
            portfolio.net_liquidation,
        )
        if correlated_exposure >= self.max_correlated_exposure:
            reasons.append("Correlated exposure cap reached.")

        for decision in decisions:
            if decision.action == "FLAT":
                continue
            if decision.confidence < self.min_model_confidence:
                reasons.append(f"{decision.agent_name} confidence below threshold.")
            if portfolio.agent_drawdowns.get(decision.agent_name, 0.0) >= self.max_agent_drawdown:
                reasons.append(f"{decision.agent_name} drawdown cap reached.")
            symbol_exposure = self._normalize_symbol_exposure(
                portfolio.symbol_exposure.get(session.symbol, 0.0),
                session.symbol,
                portfolio.net_liquidation,
            )
            proposed_exposure = self._decision_exposure_ratio(
                decision,
                session.symbol,
                portfolio.net_liquidation,
            )
            if symbol_exposure >= self.max_symbol_exposure:
                reasons.append(f"{session.symbol} exposure cap reached.")
            elif symbol_exposure + proposed_exposure > self.max_symbol_exposure:
                reasons.append(f"{session.symbol} projected margin exposure would exceed cap.")
            if correlated_exposure + proposed_exposure > self.max_correlated_exposure:
                reasons.append("Projected correlated exposure would exceed cap.")

        if reasons:
            logger.info(
                "auction.risk.blocked symbol={symbol} reasons={reasons} gate_log={gate_log}",
                symbol=session.symbol,
                reasons=reasons,
                gate_log=gate_log,
            )
            return RiskDecision(
                allowed=False,
                kill_switch=(
                    # Daily-loss breach trips the kill-switch in paper AND live
                    # (lane-local circuit breaker); broker/stale only in live.
                    "Daily loss limit breached." in reasons
                    or (
                        not self.paper_mode
                        and any(
                            reason in {
                                "Broker connectivity unavailable.",
                                "Market data is stale.",
                            }
                            for reason in reasons
                        )
                    )
                ),
                max_size_multiplier=0.0,
                reasons=reasons,
            )

        logger.debug(
            "auction.risk.allowed symbol={symbol} gate_log={gate_log}",
            symbol=session.symbol,
            gate_log=gate_log,
        )
        return RiskDecision(allowed=True, kill_switch=False, max_size_multiplier=1.0, reasons=["Risk checks passed."])

    def _decision_exposure_ratio(
        self,
        decision: AgentDecision,
        session_symbol: str,
        net_liquidation: float,
    ) -> float:
        if decision.entry_price is None or decision.quantity <= 0:
            return 0.0
        margin_fraction = self._margin_fraction(session_symbol)
        notional = float(decision.entry_price) * float(decision.quantity)
        return round((notional * margin_fraction) / max(net_liquidation, 1.0), 4)

    def _normalize_symbol_exposure(
        self,
        value: float,
        symbol: str,
        net_liquidation: float,
    ) -> float:
        numeric = float(value or 0.0)
        if numeric <= 1.5:
            return numeric
        margin_fraction = self._margin_fraction(symbol)
        return round((numeric * margin_fraction) / max(net_liquidation, 1.0), 4)

    def _normalize_aggregate_exposure(self, value: float, net_liquidation: float) -> float:
        numeric = float(value or 0.0)
        if numeric <= 1.5:
            return numeric
        return round(numeric / max(net_liquidation, 1.0), 4)

    def _margin_fraction(self, symbol: str) -> float:
        # This governor evaluates ATM option-BUY decisions — capital at risk is the PREMIUM OUTGO,
        # not the full index notional. Use the option-buy fraction (a NIFTY ATM premium ≈ 2% of the
        # index notional) and DEFAULT to it for any symbol without an explicit per-contract margin.
        # (Previously defaulted to 1.0 = full notional, which sized one NIFTY lot at ~₹1.86M and
        # tripped the 0.35 symbol-exposure cap on every entry, blocking all NSE option opens.)
        premium_fraction = float(self.scope.get("option_buy_price_fraction", 0.02))
        if str(self.scope.get("instrument_type") or "").lower() == "options_buy":
            return premium_fraction
        normalized_symbol = str(symbol or "").upper().replace(" INDEX", "").replace(" FUT", "").strip()
        return float(
            self.contract_specs.get(normalized_symbol, {}).get("margin_fraction_per_lot", premium_fraction)
        )
