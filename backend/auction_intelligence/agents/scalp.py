from __future__ import annotations

from auction_intelligence.agents.base import StrategyAgent
from auction_intelligence.schemas import AgentContext, AgentDecision


class ScalpAgent(StrategyAgent):
    def __init__(self, config: dict):
        super().__init__("scalp", config)

    def evaluate(self, context: AgentContext) -> AgentDecision:
        flow = context.order_flow
        current = context.current_profile
        regime = context.regime
        rationale = ["Scalp sleeve now focuses on responsive extremes and initiative bursts."]

        min_confidence = float(self.config.get("min_confidence", 0.68))
        sleeve_fraction = float(self.config.get("sleeve_fraction", 0.2))
        risk_multiple = float(self.config.get("risk_multiple", 1.2))
        value_tolerance = self._bounded_tolerance(
            reference_range=max(float(current.vah) - float(current.val), float(current.tick_size)),
            fraction=float(self.config.get("value_tolerance_fraction", 0.12)),
            minimum=float(self.config.get("value_tolerance_min_points", 6.0)),
            maximum=float(self.config.get("value_tolerance_max_points", 20.0)),
        )

        if flow.execution_aggression == "WAIT" or flow.toxicity_score >= 0.82:
            rationale.append("Microstructure timing did not justify a scalp entry.")
            return self._flat(rationale, metadata={"setup_name": "no_setup", "flat_reason": "timing_not_tradeable"})

        balance_labels = {"balance", "developing_balance", "rotational_day", "neutral_extreme"}
        action = "FLAT"
        setup_name = "no_setup"
        stop = None
        target = None

        if (
            regime.label in balance_labels
            and current.close_price <= (current.val + value_tolerance)
            and flow.book_pressure >= 0.08
            and flow.trade_imbalance >= -0.05
            and flow.delta >= 0
        ):
            action = "LONG"
            setup_name = "responsive_buy_long"
            rationale.append("Responsive buying is lifting price from lower value.")
        elif (
            regime.label in balance_labels
            and current.close_price >= (current.vah - value_tolerance)
            and flow.book_pressure <= -0.08
            and flow.trade_imbalance <= 0.05
            and flow.delta <= 0
        ):
            action = "SHORT"
            setup_name = "responsive_sell_short"
            rationale.append("Responsive selling is leaning on upper value.")
        elif (
            regime.label in {"breakout_acceptance", "trend_continuation", "trend_day", "developing_balance"}
            and current.close_price > max(current.vah, current.initial_balance_high)
            and flow.book_pressure >= 0.14
            and flow.order_flow_imbalance >= 0.08
            and flow.trade_intensity_per_minute >= 2.0
        ):
            action = "LONG"
            setup_name = "initiative_burst_long"
            rationale.append("Initiative buyers are sustaining a micro-breakout above value.")
        elif (
            regime.label in {"breakout_acceptance", "trend_continuation", "trend_day", "developing_balance"}
            and current.close_price < min(current.val, current.initial_balance_low)
            and flow.book_pressure <= -0.14
            and flow.order_flow_imbalance <= -0.08
            and flow.trade_intensity_per_minute >= 2.0
        ):
            action = "SHORT"
            setup_name = "initiative_burst_short"
            rationale.append("Initiative sellers are sustaining a micro-breakout below value.")

        confidence = round(
            min(
                1.0,
                (0.55 * flow.timing_confidence)
                + (0.20 * abs(flow.book_pressure))
                + (0.15 * abs(flow.order_flow_imbalance))
                + (0.10 * abs(flow.trade_imbalance)),
            ),
            4,
        )
        metadata = {
            "setup_name": setup_name,
            "flat_reason": "no_scalp_alignment" if action == "FLAT" else None,
            "trade_imbalance": round(flow.trade_imbalance, 4),
            "order_flow_imbalance": round(flow.order_flow_imbalance, 4),
            "book_pressure": round(flow.book_pressure, 4),
            "timing_confidence": round(flow.timing_confidence, 4),
            "toxicity_score": round(flow.toxicity_score, 4),
            "value_tolerance": round(value_tolerance, 4),
        }
        if action == "FLAT":
            rationale.append("Order flow is not aligned with a defined scalp structure.")
            return self._flat(rationale, confidence=confidence, metadata=metadata)
        if confidence < min_confidence:
            rationale.append("Scalp confidence remained below the configured threshold.")
            metadata["flat_reason"] = "confidence_below_threshold"
            return self._flat(rationale, confidence=confidence, metadata=metadata)

        entry = float(context.session.last_price or current.close_price)
        stop = entry - flow.micro_stop_distance if action == "LONG" else entry + flow.micro_stop_distance
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
            metadata["flat_reason"] = "insufficient_notional"
            metadata["margin_fraction_per_lot"] = round(margin_fraction_per_lot, 4)
            return self._flat(rationale, confidence=confidence, metadata=metadata)

        metadata["margin_fraction_per_lot"] = round(margin_fraction_per_lot, 4)
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
            metadata=metadata,
        )
