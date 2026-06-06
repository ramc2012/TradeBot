from __future__ import annotations

from typing import Any

from auction_intelligence.agents.base import StrategyAgent
from auction_intelligence.schemas import AgentContext, AgentDecision


class SwingAgent(StrategyAgent):
    def __init__(self, config: dict):
        super().__init__("swing", config)

    def evaluate(self, context: AgentContext) -> AgentDecision:
        regime = context.regime
        current = context.current_profile
        prior = context.prior_profile
        flow = context.order_flow

        base_min_confidence = float(self.config.get("min_confidence", 0.62))
        min_confidence = base_min_confidence
        sleeve_fraction = float(self.config.get("sleeve_fraction", 0.35))
        risk_multiple = float(self.config.get("risk_multiple", 2.0))
        enable_eighty_percent_rule = bool(self.config.get("enable_eighty_percent_rule", False))
        enable_ib_failure = bool(self.config.get("enable_ib_failure", False))
        enable_auction_rejection_long = bool(self.config.get("enable_auction_rejection_long", False))
        enable_auction_rejection_short = bool(self.config.get("enable_auction_rejection_short", True))
        enable_acceptance_continuation_long = bool(self.config.get("enable_acceptance_continuation_long", False))
        enable_acceptance_continuation_short = bool(self.config.get("enable_acceptance_continuation_short", True))
        enable_spike_acceptance = bool(self.config.get("enable_spike_acceptance", True))
        enable_balance_area_breakout = bool(self.config.get("enable_balance_area_breakout", False))
        enable_trend_pullback = bool(self.config.get("enable_trend_pullback", False))

        _rl_action_idx: int | None = None
        _rl_state = None
        try:
            from auction_intelligence.rl.policy import rl_policy
            from auction_intelligence.rl.state import extract_state

            if regime.allowed_directions:
                _rl_direction = regime.allowed_directions[0]
                _rl_state = extract_state(regime.label, current, _rl_direction, order_flow=flow)
                if rl_policy._cache_loaded:
                    _rl_params = rl_policy.select_action_sync(_rl_state)
                    _rl_action_idx = _rl_params.action_idx
                    min_confidence = min(
                        _rl_params.min_confidence,
                        float(self.config.get("rl_max_min_confidence", base_min_confidence)),
                    )
                    sleeve_fraction = _rl_params.sleeve_fraction
                    risk_multiple = _rl_params.risk_multiple
        except Exception:
            pass

        value_width = max(float(current.vah) - float(current.val), float(current.tick_size))
        entry_tolerance = self._bounded_tolerance(
            reference_range=value_width,
            fraction=float(self.config.get("value_entry_tolerance_fraction", 0.25)),
            minimum=float(self.config.get("value_entry_tolerance_min_points", 10.0)),
            maximum=float(self.config.get("value_entry_tolerance_max_points", 50.0)),
            price=float(current.close_price),
        )
        prior_value_width = value_width if prior is None else max(float(prior.vah) - float(prior.val), float(current.tick_size))
        prior_reentry_tolerance = self._bounded_tolerance(
            reference_range=prior_value_width,
            fraction=float(self.config.get("prior_value_reentry_tolerance_fraction", 0.15)),
            minimum=float(self.config.get("prior_value_reentry_tolerance_min_points", 8.0)),
            maximum=float(self.config.get("prior_value_reentry_tolerance_max_points", 35.0)),
            price=float(current.close_price),
        )
        ib_break_tolerance = self._bounded_tolerance(
            reference_range=max(float(current.initial_balance_range), float(current.tick_size)),
            fraction=float(self.config.get("ib_break_tolerance_fraction", 0.12)),
            minimum=float(self.config.get("ib_break_tolerance_min_points", 8.0)),
            maximum=float(self.config.get("ib_break_tolerance_max_points", 35.0)),
            price=float(current.close_price),
        )
        trend_pullback_tolerance = self._bounded_tolerance(
            reference_range=max(value_width, float(current.initial_balance_range), float(current.tick_size)),
            fraction=float(self.config.get("trend_pullback_tolerance_fraction", 0.20)),
            minimum=float(self.config.get("trend_pullback_tolerance_min_points", 8.0)),
            maximum=float(self.config.get("trend_pullback_tolerance_max_points", 35.0)),
            price=float(current.close_price),
        )
        trend_pullback_timing_floor = float(self.config.get("trend_pullback_timing_floor", 0.38))
        delta_strength = min(abs(float(flow.delta or 0.0)) / 10.0, 1.0)
        book_unavailable = abs(flow.book_pressure) < 0.02 and abs(flow.order_flow_imbalance) < 0.02

        positive_response = flow.toxicity_score <= 0.84 and (
            (
                flow.book_pressure >= 0.03
                and flow.trade_imbalance >= -0.12
                and flow.order_flow_imbalance >= -0.12
            )
            or (
                book_unavailable
                and flow.delta > 0
                and flow.trade_imbalance >= 0.10
                and flow.timing_confidence >= 0.44
            )
        )
        negative_response = flow.toxicity_score <= 0.84 and (
            (
                flow.book_pressure <= -0.03
                and flow.trade_imbalance <= 0.12
                and flow.order_flow_imbalance <= 0.12
            )
            or (
                book_unavailable
                and flow.delta < 0
                and flow.trade_imbalance <= -0.10
                and flow.timing_confidence >= 0.44
            )
        )
        positive_initiative = (
            flow.timing_confidence >= max(0.55, min_confidence - 0.08)
            and flow.toxicity_score <= 0.82
            and (
                (
                    flow.delta >= 0
                    and flow.book_pressure >= 0.08
                    and flow.trade_imbalance >= -0.05
                    and flow.order_flow_imbalance >= -0.05
                )
                or (
                    book_unavailable
                    and flow.delta > 0
                    and flow.trade_imbalance >= 0.20
                    and delta_strength >= 0.3
                )
            )
        )
        negative_initiative = (
            flow.timing_confidence >= max(0.55, min_confidence - 0.08)
            and flow.toxicity_score <= 0.82
            and (
                (
                    flow.delta <= 0
                    and flow.book_pressure <= -0.08
                    and flow.trade_imbalance <= 0.05
                    and flow.order_flow_imbalance <= 0.05
                )
                or (
                    book_unavailable
                    and flow.delta < 0
                    and flow.trade_imbalance <= -0.20
                    and delta_strength >= 0.3
                )
            )
        )

        action = "FLAT"
        rationale = list(regime.reasons)
        entry = float(current.close_price)
        stop = None
        target = None
        setup_name = "no_setup"
        blockers: list[str] = []
        flat_reason = "regime_not_actionable"
        candidate_action = "FLAT"

        if (
            enable_eighty_percent_rule
            and
            prior is not None
            and current.open_price < (prior.val - prior_reentry_tolerance)
            and current.close_price >= (prior.val + prior_reentry_tolerance)
            and current.close_price <= (prior.poc + entry_tolerance)
        ):
            candidate_action = "LONG"
            if positive_response:
                action = "LONG"
                stop = min(current.low_price, current.initial_balance_low)
                target = max(prior.vah, current.vah)
                setup_name = "eighty_percent_rule_long"
                rationale.append("Auction re-entered prior value from below and order flow supports the 80% traverse higher.")
            else:
                flat_reason = "entry_filter_failed"
                blockers.append("positive_response_missing")
        elif (
            enable_eighty_percent_rule
            and
            prior is not None
            and current.open_price > (prior.vah + prior_reentry_tolerance)
            and current.close_price <= (prior.vah - prior_reentry_tolerance)
            and current.close_price >= (prior.poc - entry_tolerance)
        ):
            candidate_action = "SHORT"
            if negative_response:
                action = "SHORT"
                stop = max(current.high_price, current.initial_balance_high)
                target = min(prior.val, current.val)
                setup_name = "eighty_percent_rule_short"
                rationale.append("Auction re-entered prior value from above and order flow supports the 80% traverse lower.")
            else:
                flat_reason = "entry_filter_failed"
                blockers.append("negative_response_missing")
        elif (
            enable_ib_failure
            and
            current.high_price > (current.initial_balance_high + ib_break_tolerance)
            and current.close_price < (current.initial_balance_high - ib_break_tolerance)
        ):
            candidate_action = "SHORT"
            if negative_response:
                action = "SHORT"
                stop = current.high_price
                target = current.poc
                setup_name = "ib_failure_short"
                rationale.append("Initial-balance breakout failed and order flow is auctioning back toward value.")
            else:
                flat_reason = "entry_filter_failed"
                blockers.append("negative_response_missing")
        elif (
            enable_ib_failure
            and
            current.low_price < (current.initial_balance_low - ib_break_tolerance)
            and current.close_price > (current.initial_balance_low + ib_break_tolerance)
        ):
            candidate_action = "LONG"
            if positive_response:
                action = "LONG"
                stop = current.low_price
                target = current.poc
                setup_name = "ib_failure_long"
                rationale.append("Initial-balance downside break failed and order flow is auctioning back toward value.")
            else:
                flat_reason = "entry_filter_failed"
                blockers.append("positive_response_missing")
        elif enable_auction_rejection_short and regime.label in {"failed_auction", "breakout_rejection", "reversal"} and "SHORT" in regime.allowed_directions:
            candidate_action = "SHORT"
            action = "SHORT"
            stop = current.high_price
            setup_name = "auction_rejection_short"
            rationale.append("Swing agent fades rejected upside auction.")
        elif enable_auction_rejection_long and regime.label in {"failed_auction", "breakout_rejection", "reversal"} and "LONG" in regime.allowed_directions:
            candidate_action = "LONG"
            action = "LONG"
            stop = current.low_price
            setup_name = "auction_rejection_long"
            rationale.append("Swing agent buys rejected downside auction.")
        elif enable_spike_acceptance and current.spike_direction == "up" and current.close_price > current.vah:
            candidate_action = "LONG"
            if positive_initiative:
                action = "LONG"
                stop = max(current.vah, current.poc)
                setup_name = "spike_acceptance_long"
                rationale.append("Late-session upside spike is being accepted with supportive order flow.")
            else:
                flat_reason = "entry_filter_failed"
                blockers.append("positive_initiative_missing")
        elif enable_spike_acceptance and current.spike_direction == "down" and current.close_price < current.val:
            candidate_action = "SHORT"
            if negative_initiative:
                action = "SHORT"
                stop = min(current.val, current.poc)
                setup_name = "spike_acceptance_short"
                rationale.append("Late-session downside spike is being accepted with supportive order flow.")
            else:
                flat_reason = "entry_filter_failed"
                blockers.append("negative_initiative_missing")
        elif (
            enable_balance_area_breakout
            and
            regime.label in {"balance", "developing_balance", "rotational_day"}
            and current.close_price > max(current.vah, current.initial_balance_high) + ib_break_tolerance
        ):
            candidate_action = "LONG"
            if positive_initiative:
                action = "LONG"
                stop = max(current.poc, current.initial_balance_high)
                setup_name = "balance_area_breakout_long"
                rationale.append("Balanced auction is breaking higher with initiative order flow.")
            else:
                flat_reason = "entry_filter_failed"
                blockers.append("positive_initiative_missing")
        elif (
            enable_balance_area_breakout
            and
            regime.label in {"balance", "developing_balance", "rotational_day"}
            and current.close_price < min(current.val, current.initial_balance_low) - ib_break_tolerance
        ):
            candidate_action = "SHORT"
            if negative_initiative:
                action = "SHORT"
                stop = min(current.poc, current.initial_balance_low)
                setup_name = "balance_area_breakout_short"
                rationale.append("Balanced auction is breaking lower with initiative order flow.")
            else:
                flat_reason = "entry_filter_failed"
                blockers.append("negative_initiative_missing")
        elif enable_trend_pullback and regime.label in {"breakout_acceptance", "trend_continuation", "trend_day"} and "LONG" in regime.allowed_directions:
            candidate_action = "LONG"
            if (
                current.close_price >= (current.poc - trend_pullback_tolerance)
                and current.close_price >= (current.val - trend_pullback_tolerance)
                and positive_response
                and flow.timing_confidence >= trend_pullback_timing_floor
            ):
                action = "LONG"
                stop = min(current.val, current.initial_balance_low)
                target = max(current.high_price, current.vah + entry_tolerance)
                setup_name = "trend_pullback_long"
                rationale.append("Trend continuation is pulling back toward value and responsive buyers are holding the auction above lower value.")
            else:
                flat_reason = "entry_filter_failed"
                if current.close_price < (current.poc - trend_pullback_tolerance):
                    blockers.append("price_below_pullback_zone")
                if current.close_price < (current.val - trend_pullback_tolerance):
                    blockers.append("price_below_value_support")
                if not positive_response:
                    blockers.append("positive_response_missing")
                if flow.timing_confidence < trend_pullback_timing_floor:
                    blockers.append("trend_pullback_timing_missing")
        elif enable_trend_pullback and regime.label in {"breakout_acceptance", "trend_continuation", "trend_day"} and "SHORT" in regime.allowed_directions:
            candidate_action = "SHORT"
            if (
                current.close_price <= (current.poc + trend_pullback_tolerance)
                and current.close_price <= (current.vah + trend_pullback_tolerance)
                and negative_response
                and flow.timing_confidence >= trend_pullback_timing_floor
            ):
                action = "SHORT"
                stop = max(current.vah, current.initial_balance_high)
                target = min(current.low_price, current.val - entry_tolerance)
                setup_name = "trend_pullback_short"
                rationale.append("Trend continuation is pulling back toward value and responsive sellers are holding the auction below upper value.")
            else:
                flat_reason = "entry_filter_failed"
                if current.close_price > (current.poc + trend_pullback_tolerance):
                    blockers.append("price_above_pullback_zone")
                if current.close_price > (current.vah + trend_pullback_tolerance):
                    blockers.append("price_above_value_resistance")
                if not negative_response:
                    blockers.append("negative_response_missing")
                if flow.timing_confidence < trend_pullback_timing_floor:
                    blockers.append("trend_pullback_timing_missing")
        elif enable_acceptance_continuation_long and regime.label in {"breakout_acceptance", "trend_continuation", "trend_day"} and "LONG" in regime.allowed_directions:
            candidate_action = "LONG"
            if current.close_price >= (current.vah - entry_tolerance) and positive_initiative:
                action = "LONG"
                stop = max(current.poc, current.initial_balance_low)
                setup_name = "acceptance_continuation_long"
                rationale.append("Swing agent aligns with higher value acceptance and supportive order flow.")
            else:
                flat_reason = "entry_filter_failed"
                if current.close_price < (current.vah - entry_tolerance):
                    blockers.append("price_below_vah")
                if not positive_initiative:
                    blockers.append("positive_initiative_missing")
        elif enable_acceptance_continuation_short and regime.label in {"breakout_acceptance", "trend_continuation", "trend_day"} and "SHORT" in regime.allowed_directions:
            candidate_action = "SHORT"
            if current.close_price <= (current.val + entry_tolerance) and negative_initiative:
                action = "SHORT"
                stop = min(current.poc, current.initial_balance_high)
                setup_name = "acceptance_continuation_short"
                rationale.append("Swing agent aligns with lower value acceptance and supportive order flow.")
            else:
                flat_reason = "entry_filter_failed"
                if current.close_price > (current.val + entry_tolerance):
                    blockers.append("price_above_val")
                if not negative_initiative:
                    blockers.append("negative_initiative_missing")
        else:
            blockers.append("regime_not_supported")

        flow_signal = max(
            abs(flow.book_pressure),
            abs(flow.order_flow_imbalance),
            abs(flow.trade_imbalance),
            delta_strength * 0.7,
        )
        confidence = round(
            min(
                1.0,
                (0.48 * regime.confidence)
                + (0.32 * flow.timing_confidence)
                + (0.20 * flow_signal),
            ),
            4,
        )
        if setup_name in {"trend_pullback_long", "trend_pullback_short"}:
            confidence = round(min(1.0, confidence + 0.08), 4)

        metadata = self._decision_metadata(
            regime_label=regime.label,
            setup_name=setup_name,
            flat_reason=flat_reason,
            blockers=blockers,
            candidate_action=candidate_action,
            confidence=confidence,
            min_confidence=min_confidence,
            sleeve_fraction=sleeve_fraction,
            risk_multiple=risk_multiple,
            entry_tolerance=entry_tolerance,
            prior_reentry_tolerance=prior_reentry_tolerance,
            ib_break_tolerance=ib_break_tolerance,
            trend_pullback_tolerance=trend_pullback_tolerance,
            current=current,
            flow=flow,
            rl_state=_rl_state,
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
                sleeve_fraction=sleeve_fraction,
                risk_multiple=risk_multiple,
                entry_tolerance=entry_tolerance,
                prior_reentry_tolerance=prior_reentry_tolerance,
                ib_break_tolerance=ib_break_tolerance,
                trend_pullback_tolerance=trend_pullback_tolerance,
                current=current,
                flow=flow,
                rl_state=_rl_state,
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
                sleeve_fraction=sleeve_fraction,
                risk_multiple=risk_multiple,
                entry_tolerance=entry_tolerance,
                prior_reentry_tolerance=prior_reentry_tolerance,
                ib_break_tolerance=ib_break_tolerance,
                trend_pullback_tolerance=trend_pullback_tolerance,
                current=current,
                flow=flow,
                rl_state=_rl_state,
            )
            return self._flat(rationale, confidence=confidence, metadata=metadata)

        if target is None:
            target = self._risk_target(
                action=action,
                entry_price=entry,
                stop_price=stop,
                risk_multiple=risk_multiple,
            )

        quantity, _lot_size, margin_fraction_per_lot = self._size_quantity(
            context,
            entry_price=entry,
            sleeve_fraction=sleeve_fraction,
        )
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
                sleeve_fraction=sleeve_fraction,
                risk_multiple=risk_multiple,
                entry_tolerance=entry_tolerance,
                prior_reentry_tolerance=prior_reentry_tolerance,
                ib_break_tolerance=ib_break_tolerance,
                trend_pullback_tolerance=trend_pullback_tolerance,
                current=current,
                flow=flow,
                rl_state=_rl_state,
            )
            metadata["margin_fraction_per_lot"] = round(margin_fraction_per_lot, 4)
            return self._flat(rationale, confidence=confidence, metadata=metadata)

        final_meta = self._decision_metadata(
            regime_label=regime.label,
            setup_name=setup_name,
            flat_reason=None,
            blockers=[],
            candidate_action=action,
            confidence=confidence,
            min_confidence=min_confidence,
            sleeve_fraction=sleeve_fraction,
            risk_multiple=risk_multiple,
            entry_tolerance=entry_tolerance,
            prior_reentry_tolerance=prior_reentry_tolerance,
            ib_break_tolerance=ib_break_tolerance,
            trend_pullback_tolerance=trend_pullback_tolerance,
            current=current,
            flow=flow,
            rl_state=_rl_state,
        )
        final_meta["margin_fraction_per_lot"] = round(margin_fraction_per_lot, 4)
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
        sleeve_fraction: float,
        risk_multiple: float,
        entry_tolerance: float,
        prior_reentry_tolerance: float,
        ib_break_tolerance: float,
        trend_pullback_tolerance: float,
        current,
        flow,
        rl_state,
    ) -> dict[str, Any]:
        metadata = {
            "regime": regime_label,
            "setup_name": setup_name,
            "flat_reason": flat_reason,
            "blocking_reasons": blockers,
            "candidate_action": candidate_action,
            "computed_confidence": round(confidence, 4),
            "min_confidence": round(min_confidence, 4),
            "sleeve_fraction": round(sleeve_fraction, 4),
            "risk_multiple": round(risk_multiple, 4),
            "entry_tolerance": round(entry_tolerance, 4),
            "prior_reentry_tolerance": round(prior_reentry_tolerance, 4),
            "ib_break_tolerance": round(ib_break_tolerance, 4),
            "trend_pullback_tolerance": round(trend_pullback_tolerance, 4),
            "diagnostics": {
                "close_price": round(current.close_price, 4),
                "open_price": round(current.open_price, 4),
                "vah": round(current.vah, 4),
                "val": round(current.val, 4),
                "poc": round(current.poc, 4),
                "initial_balance_high": round(current.initial_balance_high, 4),
                "initial_balance_low": round(current.initial_balance_low, 4),
                "delta": round(flow.delta, 4),
                "trade_imbalance": round(flow.trade_imbalance, 4),
                "order_flow_imbalance": round(flow.order_flow_imbalance, 4),
                "book_pressure": round(flow.book_pressure, 4),
                "micro_price_offset_bps": round(flow.micro_price_offset_bps, 4),
                "adverse_selection_risk": round(flow.adverse_selection_risk, 4),
                "toxicity_score": round(flow.toxicity_score, 4),
                "timing_confidence": round(flow.timing_confidence, 4),
            },
        }
        if rl_state is not None:
            metadata["buyer_fail_bin"] = int(rl_state.buyer_fail_bin)
            metadata["seller_fail_bin"] = int(rl_state.seller_fail_bin)
            metadata["ib_size_bin"] = int(rl_state.ib_size_bin)
            metadata["rl_state_key"] = rl_state.to_key()
            metadata["rl_state_label"] = rl_state.label
        return metadata
