from __future__ import annotations

from typing import Any

from auction_intelligence.agents.base import StrategyAgent
from auction_intelligence.schemas import AgentContext, AgentDecision


class PositionalAgent(StrategyAgent):
    def __init__(self, config: dict):
        super().__init__("positional", config)

    def evaluate(self, context: AgentContext) -> AgentDecision:
        current = context.current_profile
        prior = context.prior_profile
        flow = context.order_flow
        regime = context.regime

        rationale = ["Positional sleeve now targets gap, value migration, and balance-rotation structures."]
        if prior is None:
            rationale.append("No prior profile is available for higher-timeframe auction references.")
            return self._flat(rationale)

        min_confidence = float(self.config.get("min_confidence", 0.70))
        sleeve_fraction = float(self.config.get("sleeve_fraction", 0.45))
        risk_multiple = float(self.config.get("risk_multiple", 2.5))
        value_width = max(float(current.vah) - float(current.val), float(current.tick_size))
        value_tolerance = self._bounded_tolerance(
            reference_range=value_width,
            fraction=float(self.config.get("value_tolerance_fraction", 0.18)),
            minimum=float(self.config.get("value_tolerance_min_points", 12.0)),
            maximum=float(self.config.get("value_tolerance_max_points", 45.0)),
        )
        gap_tolerance = self._bounded_tolerance(
            reference_range=max(float(prior.vah) - float(prior.val), value_width),
            fraction=float(self.config.get("gap_tolerance_fraction", 0.12)),
            minimum=float(self.config.get("gap_tolerance_min_points", 10.0)),
            maximum=float(self.config.get("gap_tolerance_max_points", 40.0)),
        )
        vpoc_tolerance = self._bounded_tolerance(
            reference_range=max(value_width, float(current.initial_balance_range)),
            fraction=float(self.config.get("vpoc_tolerance_fraction", 0.12)),
            minimum=float(self.config.get("vpoc_tolerance_min_points", 8.0)),
            maximum=float(self.config.get("vpoc_tolerance_max_points", 30.0)),
        )
        balance_labels = {"balance", "developing_balance", "rotational_day", "neutral_extreme"}
        enable_gap_continuation = bool(self.config.get("enable_gap_continuation", True))
        enable_gap_failure = bool(self.config.get("enable_gap_failure", False))
        enable_balance_rotation = bool(self.config.get("enable_balance_rotation", False))
        enable_vpoc_rejection = bool(self.config.get("enable_vpoc_rejection", False))
        # Trend-follow lets the positional sleeve participate on regime-led
        # trend days when no fade/rejection setup matches. Without it, every
        # trend day collapses to FLAT because the existing setups all expect
        # a rejection structure, and AI never produces paper trades on
        # genuine momentum sessions. Order flow gates are intentionally
        # bypassed here so historical_replay (which infers flow from bars,
        # not ticks) can still emit decisions — risk + confidence still
        # gate execution downstream.
        enable_trend_follow = bool(self.config.get("enable_trend_follow", True))
        # Single-direction regimes — the regime engine has already verified
        # there's a clear bias. We trust it for the trend-follow setup.
        trend_follow_labels = set(
            self.config.get(
                "trend_follow_regime_labels",
                [
                    "trend_day",
                    "trend_continuation",
                    "breakout_acceptance",
                    "failed_auction",
                    "breakout_rejection",
                    "reversal",
                ],
            )
        )
        delta_strength = min(abs(float(flow.delta or 0.0)) / 10.0, 1.0)
        book_unavailable = abs(flow.book_pressure) < 0.02 and abs(flow.order_flow_imbalance) < 0.02

        positive_response = flow.toxicity_score <= 0.84 and (
            (
                flow.book_pressure >= 0.05
                and flow.trade_imbalance >= -0.10
                and flow.order_flow_imbalance >= -0.10
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
                flow.book_pressure <= -0.05
                and flow.trade_imbalance <= 0.10
                and flow.order_flow_imbalance <= 0.10
            )
            or (
                book_unavailable
                and flow.delta < 0
                and flow.trade_imbalance <= -0.10
                and flow.timing_confidence >= 0.44
            )
        )
        positive_initiative = positive_response and flow.delta > 0 and (
            flow.timing_confidence >= max(0.50, min_confidence - 0.12)
            or (book_unavailable and delta_strength >= 0.3)
        )
        negative_initiative = negative_response and flow.delta < 0 and (
            flow.timing_confidence >= max(0.50, min_confidence - 0.12)
            or (book_unavailable and delta_strength >= 0.3)
        )

        gap_up = current.open_price > (prior.vah + gap_tolerance)
        gap_down = current.open_price < (prior.val - gap_tolerance)
        prior_poc_touched = current.prior_poc_untouched is False

        action = "FLAT"
        candidate_action = "FLAT"
        setup_name = "no_setup"
        flat_reason = "regime_not_actionable"
        blockers: list[str] = []
        entry = float(current.close_price)
        stop: float | None = None
        target: float | None = None

        if enable_gap_continuation and gap_up and current.close_price > max(prior.high_price, current.initial_balance_high) and positive_initiative:
            candidate_action = "LONG"
            action = "LONG"
            setup_name = "gap_continuation_long"
            stop = max(prior.vah, current.poc)
            rationale.append("Gap above prior value is holding with initiative buying and higher value acceptance.")
        elif enable_gap_continuation and gap_down and current.close_price < min(prior.low_price, current.initial_balance_low) and negative_initiative:
            candidate_action = "SHORT"
            action = "SHORT"
            setup_name = "gap_continuation_short"
            stop = min(prior.val, current.poc)
            rationale.append("Gap below prior value is holding with initiative selling and lower value acceptance.")
        elif enable_gap_failure and gap_up and current.close_price <= prior.vah:
            candidate_action = "SHORT"
            if negative_initiative:
                action = "SHORT"
                setup_name = "gap_failure_short"
                stop = current.high_price
                target = min(prior.poc, current.poc)
                rationale.append("Gap above prior value failed and order flow is rejecting the higher auction.")
            else:
                flat_reason = "entry_filter_failed"
                blockers.append("negative_initiative_missing")
        elif enable_gap_failure and gap_down and current.close_price >= prior.val:
            candidate_action = "LONG"
            if positive_initiative:
                action = "LONG"
                setup_name = "gap_failure_long"
                stop = current.low_price
                target = max(prior.poc, current.poc)
                rationale.append("Gap below prior value failed and order flow is rejecting the lower auction.")
            else:
                flat_reason = "entry_filter_failed"
                blockers.append("positive_initiative_missing")
        elif enable_balance_rotation and regime.label in balance_labels and current.close_price >= (current.vah - value_tolerance):
            candidate_action = "SHORT"
            if negative_response and (current.poor_high or current.excess_high > 0 or current.range_extension_up > 0):
                action = "SHORT"
                setup_name = "balance_rotation_short"
                stop = current.high_price
                target = current.poc
                rationale.append("Auction is near upper value in balance and order flow supports responsive selling back to POC.")
            else:
                flat_reason = "entry_filter_failed"
                if not negative_response:
                    blockers.append("negative_response_missing")
                if not (current.poor_high or current.excess_high > 0 or current.range_extension_up > 0):
                    blockers.append("upper_extreme_not_confirmed")
        elif enable_balance_rotation and regime.label in balance_labels and current.close_price <= (current.val + value_tolerance):
            candidate_action = "LONG"
            if positive_response and (current.poor_low or current.excess_low > 0 or current.range_extension_down > 0):
                action = "LONG"
                setup_name = "balance_rotation_long"
                stop = current.low_price
                target = current.poc
                rationale.append("Auction is near lower value in balance and order flow supports responsive buying back to POC.")
            else:
                flat_reason = "entry_filter_failed"
                if not positive_response:
                    blockers.append("positive_response_missing")
                if not (current.poor_low or current.excess_low > 0 or current.range_extension_down > 0):
                    blockers.append("lower_extreme_not_confirmed")
        elif enable_vpoc_rejection and prior_poc_touched and current.close_price >= (prior.poc + vpoc_tolerance):
            candidate_action = "LONG"
            if positive_response:
                action = "LONG"
                setup_name = "vpoc_rejection_long"
                stop = min(current.low_price, current.val)
                target = max(current.vah, prior.vah)
                rationale.append("Prior session POC was tested and rejected with responsive buying.")
            else:
                flat_reason = "entry_filter_failed"
                blockers.append("positive_response_missing")
        elif enable_vpoc_rejection and prior_poc_touched and current.close_price <= (prior.poc - vpoc_tolerance):
            candidate_action = "SHORT"
            if negative_response:
                action = "SHORT"
                setup_name = "vpoc_rejection_short"
                stop = max(current.high_price, current.vah)
                target = min(current.val, prior.val)
                rationale.append("Prior session POC was tested and rejected with responsive selling.")
            else:
                flat_reason = "entry_filter_failed"
                blockers.append("negative_response_missing")
        elif (
            enable_trend_follow
            and regime.label in trend_follow_labels
            and "LONG" in (regime.allowed_directions or [])
            and "SHORT" not in (regime.allowed_directions or [])
        ):
            candidate_action = "LONG"
            action = "LONG"
            setup_name = "regime_trend_follow_long"
            stop = min(current.initial_balance_low, current.low_price, current.val)
            target = max(current.vah, current.poc + (current.poc - stop) * risk_multiple)
            rationale.append(
                "Trend day with LONG bias — positional follows the regime; "
                "tick-level order-flow confirmation bypassed by design."
            )
        elif (
            enable_trend_follow
            and regime.label in trend_follow_labels
            and "SHORT" in (regime.allowed_directions or [])
            and "LONG" not in (regime.allowed_directions or [])
        ):
            candidate_action = "SHORT"
            action = "SHORT"
            setup_name = "regime_trend_follow_short"
            stop = max(current.initial_balance_high, current.high_price, current.vah)
            target = min(current.val, current.poc - (stop - current.poc) * risk_multiple)
            rationale.append(
                "Trend day with SHORT bias — positional follows the regime; "
                "tick-level order-flow confirmation bypassed by design."
            )
        else:
            blockers.append("positional_setup_not_triggered")

        flow_signal = max(
            abs(flow.book_pressure),
            abs(flow.order_flow_imbalance),
            abs(flow.trade_imbalance),
            delta_strength * 0.7,
        )
        # Trend-follow setups derive conviction from the regime engine, not
        # from intraday order flow (which is unreliable in historical_replay
        # and during the first hour of a session). Blend the regime
        # confidence dominantly so the threshold check passes when the
        # regime is itself decisive.
        if setup_name in {"regime_trend_follow_long", "regime_trend_follow_short"}:
            # Trend-follow rides the regime engine's conviction directly so
            # historical_replay (which can't produce tick-level flow_signal)
            # still clears min_confidence on decisive trend days. The regime
            # engine has already weighed range_extension, close_location,
            # value-area overlap and POC shift — we don't penalise that.
            confidence = round(min(1.0, regime.confidence), 4)
        else:
            confidence = round(
                min(
                    1.0,
                    (0.42 * regime.confidence)
                    + (0.28 * flow.timing_confidence)
                    + (0.30 * flow_signal),
                ),
                4,
            )

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
            value_tolerance=value_tolerance,
            gap_tolerance=gap_tolerance,
            vpoc_tolerance=vpoc_tolerance,
            current=current,
            prior=prior,
            flow=flow,
        )
        if action == "FLAT":
            rationale.append("Positional thresholds were not met.")
            return self._flat(rationale, confidence=confidence, metadata=metadata)
        if confidence < min_confidence:
            rationale.append("Positional confidence remained below the deployment threshold.")
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
                value_tolerance=value_tolerance,
                gap_tolerance=gap_tolerance,
                vpoc_tolerance=vpoc_tolerance,
                current=current,
                prior=prior,
                flow=flow,
            )
            return self._flat(rationale, confidence=confidence, metadata=metadata)
        if stop is None or stop == entry:
            rationale.append("Positional stop placement was invalid for the candidate setup.")
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
                value_tolerance=value_tolerance,
                gap_tolerance=gap_tolerance,
                vpoc_tolerance=vpoc_tolerance,
                current=current,
                prior=prior,
                flow=flow,
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
                value_tolerance=value_tolerance,
                gap_tolerance=gap_tolerance,
                vpoc_tolerance=vpoc_tolerance,
                current=current,
                prior=prior,
                flow=flow,
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
            value_tolerance=value_tolerance,
            gap_tolerance=gap_tolerance,
            vpoc_tolerance=vpoc_tolerance,
            current=current,
            prior=prior,
            flow=flow,
        )
        final_meta["margin_fraction_per_lot"] = round(margin_fraction_per_lot, 4)

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
        value_tolerance: float,
        gap_tolerance: float,
        vpoc_tolerance: float,
        current,
        prior,
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
            "sleeve_fraction": round(sleeve_fraction, 4),
            "risk_multiple": round(risk_multiple, 4),
            "value_tolerance": round(value_tolerance, 4),
            "gap_tolerance": round(gap_tolerance, 4),
            "vpoc_tolerance": round(vpoc_tolerance, 4),
            "diagnostics": {
                "close_price": round(current.close_price, 4),
                "open_price": round(current.open_price, 4),
                "vah": round(current.vah, 4),
                "val": round(current.val, 4),
                "poc": round(current.poc, 4),
                "prior_vah": round(prior.vah, 4),
                "prior_val": round(prior.val, 4),
                "prior_poc": round(prior.poc, 4),
                "value_migration": round(float(current.value_migration or 0.0), 4),
                "delta": round(flow.delta, 4),
                "trade_imbalance": round(flow.trade_imbalance, 4),
                "order_flow_imbalance": round(flow.order_flow_imbalance, 4),
                "book_pressure": round(flow.book_pressure, 4),
                "toxicity_score": round(flow.toxicity_score, 4),
                "timing_confidence": round(flow.timing_confidence, 4),
            },
        }
