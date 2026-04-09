from __future__ import annotations

from math import floor
from typing import Any

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
        contract_spec = self._contract_spec(context)
        lot_size = int(contract_spec.get("lot_size", self.config.get("lot_size", 25)))
        margin_fraction_per_lot = float(
            contract_spec.get("margin_fraction_per_lot", self.config.get("margin_fraction_per_lot", 1.0))
        )
        risk_multiple = float(self.config.get("risk_multiple", 2.0))

        # ── RL parameter override ────────────────────────────────────────────
        # If the RL Q-table cache is loaded, replace min_confidence / sleeve_fraction
        # / risk_multiple with the learned optimal values for this MP state.
        # candidate_action must be determined first (below), so we use a tentative
        # direction from regime.allowed_directions for state extraction.
        _rl_action_idx: int | None = None
        try:
            from auction_intelligence.rl.policy import rl_policy
            from auction_intelligence.rl.state import extract_state
            if rl_policy._cache_loaded and regime.allowed_directions:
                _rl_direction = regime.allowed_directions[0]  # primary direction
                _rl_state = extract_state(regime.label, current, _rl_direction)
                _rl_params = rl_policy.select_action_sync(_rl_state)
                _rl_action_idx = _rl_params.action_idx
                min_confidence = _rl_params.min_confidence
                sleeve_fraction = _rl_params.sleeve_fraction
                risk_multiple = _rl_params.risk_multiple
        except Exception:
            pass  # RL module not yet available — use config defaults
        # ────────────────────────────────────────────────────────────────────

        max_notional = context.portfolio.net_liquidation * sleeve_fraction
        entry_tolerance = self._value_entry_tolerance(current)

        action = "FLAT"
        rationale = list(regime.reasons)
        entry = current.close_price
        stop = None
        target = None
        setup_name = "no_setup"
        blockers: list[str] = []
        flat_reason = "regime_not_actionable"
        candidate_action = "FLAT"

        if regime.label in {"breakout_acceptance", "trend_continuation", "trend_day"} and "LONG" in regime.allowed_directions:
            candidate_action = "LONG"
            if current.close_price >= (current.vah - entry_tolerance) and flow.delta >= 0:
                action = "LONG"
                stop = max(current.poc, current.initial_balance_low)
                setup_name = "acceptance_continuation_long"
                rationale.append("Swing agent aligns with higher value acceptance and non-negative delta.")
            else:
                flat_reason = "entry_filter_failed"
                if current.close_price < (current.vah - entry_tolerance):
                    blockers.append("price_below_vah")
                if flow.delta < 0:
                    blockers.append("negative_delta")
        elif regime.label in {"breakout_acceptance", "trend_continuation", "trend_day"} and "SHORT" in regime.allowed_directions:
            candidate_action = "SHORT"
            if current.close_price <= (current.val + entry_tolerance) and flow.delta <= 0:
                action = "SHORT"
                stop = min(current.poc, current.initial_balance_high)
                setup_name = "acceptance_continuation_short"
                rationale.append("Swing agent aligns with lower value acceptance and non-positive delta.")
            else:
                flat_reason = "entry_filter_failed"
                if current.close_price > (current.val + entry_tolerance):
                    blockers.append("price_above_val")
                if flow.delta > 0:
                    blockers.append("positive_delta")
        elif regime.label in {"failed_auction", "breakout_rejection", "reversal"} and "SHORT" in regime.allowed_directions:
            candidate_action = "SHORT"
            action = "SHORT"
            stop = current.high_price
            setup_name = "auction_rejection_short"
            rationale.append("Swing agent fades rejected upside auction.")
        elif regime.label in {"failed_auction", "breakout_rejection", "reversal"} and "LONG" in regime.allowed_directions:
            candidate_action = "LONG"
            action = "LONG"
            stop = current.low_price
            setup_name = "auction_rejection_long"
            rationale.append("Swing agent buys rejected downside auction.")
        else:
            blockers.append("regime_not_supported")

        confidence = round((0.6 * regime.confidence) + (0.4 * flow.timing_confidence), 4)
        metadata = self._decision_metadata(
            regime_label=regime.label,
            setup_name=setup_name,
            flat_reason=flat_reason,
            blockers=blockers,
            candidate_action=candidate_action,
            confidence=confidence,
            min_confidence=min_confidence,
            entry_tolerance=entry_tolerance,
            margin_fraction_per_lot=margin_fraction_per_lot,
            current=context.current_profile,
            flow=context.order_flow,
        )
        if action == "FLAT":
            rationale.append("Swing thresholds were not met.")
            return self._flat(rationale, confidence=confidence, metadata=metadata)
        if confidence < min_confidence:
            rationale.append("Swing confidence stayed below the configured threshold.")
            blockers = blockers + ["confidence_below_threshold"]
            metadata = self._decision_metadata(
                regime_label=regime.label,
                setup_name=setup_name,
                flat_reason="confidence_below_threshold",
                blockers=blockers,
                candidate_action=action,
                confidence=confidence,
                min_confidence=min_confidence,
                entry_tolerance=entry_tolerance,
                margin_fraction_per_lot=margin_fraction_per_lot,
                current=context.current_profile,
                flow=context.order_flow,
            )
            return self._flat(rationale, confidence=confidence, metadata=metadata)
        if stop is None or stop == entry:
            rationale.append("Swing stop placement was invalid for the candidate setup.")
            blockers = blockers + ["invalid_stop"]
            metadata = self._decision_metadata(
                regime_label=regime.label,
                setup_name=setup_name,
                flat_reason="invalid_stop",
                blockers=blockers,
                candidate_action=action,
                confidence=confidence,
                min_confidence=min_confidence,
                entry_tolerance=entry_tolerance,
                margin_fraction_per_lot=margin_fraction_per_lot,
                current=context.current_profile,
                flow=context.order_flow,
            )
            return self._flat(rationale, confidence=confidence, metadata=metadata)

        per_unit_risk = abs(entry - stop)
        target = entry + (risk_multiple * per_unit_risk) if action == "LONG" else entry - (risk_multiple * per_unit_risk)
        margin_per_lot = max(entry * lot_size * margin_fraction_per_lot, 1.0)
        quantity = floor(max_notional / margin_per_lot) * lot_size

        if quantity <= 0:
            rationale.append("Portfolio sleeve is too small for the configured lot size.")
            blockers = blockers + ["insufficient_notional"]
            metadata = self._decision_metadata(
                regime_label=regime.label,
                setup_name=setup_name,
                flat_reason="insufficient_notional",
                blockers=blockers,
                candidate_action=action,
                confidence=confidence,
                min_confidence=min_confidence,
                entry_tolerance=entry_tolerance,
                margin_fraction_per_lot=margin_fraction_per_lot,
                current=context.current_profile,
                flow=context.order_flow,
            )
            return self._flat(rationale, confidence=confidence, metadata=metadata)

        final_meta = self._decision_metadata(
            regime_label=regime.label,
            setup_name=setup_name,
            flat_reason=None,
            blockers=[],
            candidate_action=action,
            confidence=confidence,
            min_confidence=min_confidence,
            entry_tolerance=entry_tolerance,
            margin_fraction_per_lot=margin_fraction_per_lot,
            current=context.current_profile,
            flow=context.order_flow,
        )
        # Embed RL action index so the trainer can recover it for Q-table updates
        if _rl_action_idx is not None:
            final_meta["rl_action_idx"] = _rl_action_idx

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
            metadata=final_meta,
        )

    def _decision_metadata(
        self,
        *,
        regime_label: str,
        setup_name: str,
        flat_reason: str | None,
        blockers: list[str],
        candidate_action: str,
        confidence: float,
        min_confidence: float,
        entry_tolerance: float,
        margin_fraction_per_lot: float,
        current,
        flow,
    ) -> dict[str, Any]:
        return {
            "regime": regime_label,
            "setup_name": setup_name,
            "flat_reason": flat_reason,
            "blocking_reasons": blockers,
            "candidate_action": candidate_action,
            "computed_confidence": round(confidence, 4),
            "min_confidence": round(min_confidence, 4),
            "entry_tolerance": round(entry_tolerance, 4),
            "margin_fraction_per_lot": round(margin_fraction_per_lot, 4),
            "diagnostics": {
                "close_price": round(current.close_price, 4),
                "vah": round(current.vah, 4),
                "val": round(current.val, 4),
                "poc": round(current.poc, 4),
                "initial_balance_high": round(current.initial_balance_high, 4),
                "initial_balance_low": round(current.initial_balance_low, 4),
                "delta": round(flow.delta, 4),
                "timing_confidence": round(flow.timing_confidence, 4),
            },
        }

    def _contract_spec(self, context: AgentContext) -> dict[str, Any]:
        symbol = str(context.session.symbol or "").upper()
        symbol_key = symbol.replace(" INDEX", "").replace(" FUT", "").strip()
        return context.config.get("contract_specs", {}).get(symbol_key, {})

    def _value_entry_tolerance(self, current) -> float:
        value_width = max(float(current.vah) - float(current.val), 0.0)
        tolerance_fraction = float(self.config.get("value_entry_tolerance_fraction", 0.0))
        tolerance_min = float(self.config.get("value_entry_tolerance_min_points", 0.0))
        tolerance_max = float(self.config.get("value_entry_tolerance_max_points", tolerance_min or value_width or 0.0))
        tolerance = max(tolerance_min, value_width * tolerance_fraction)
        if tolerance_max > 0:
            tolerance = min(tolerance, tolerance_max)
        return round(max(tolerance, 0.0), 4)
