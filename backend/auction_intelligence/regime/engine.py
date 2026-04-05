from __future__ import annotations

from typing import Any, Optional

from auction_intelligence.schemas import MarketProfileSnapshot, OrderFlowSnapshot, RegimeAssessment


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class RegimeEngine:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.balance_overlap_min = float(config.get("balance_overlap_min", 0.70))
        self.developing_balance_overlap_min = float(config.get("developing_balance_overlap_min", 0.55))
        self.breakout_acceptance_overlap_max = float(config.get("breakout_acceptance_overlap_max", 0.45))
        self.trend_day_extension_min = float(config.get("trend_day_extension_min", 0.30))
        self.trend_close_extreme_min = float(config.get("trend_close_extreme_min", 0.80))
        self.no_trade_confidence_max = float(config.get("no_trade_confidence_max", 0.55))

    def classify(
        self,
        current: MarketProfileSnapshot,
        prior: Optional[MarketProfileSnapshot],
        order_flow: OrderFlowSnapshot,
    ) -> RegimeAssessment:
        range_fraction = max(current.range_extension_up, current.range_extension_down) / max(current.day_range, current.tick_size)
        close_location = (current.close_price - current.low_price) / max(current.day_range, current.tick_size)
        overlap = current.value_area_overlap or 0.0
        poc_shift = current.poc_shift or 0.0
        delta_bias = 1.0 if order_flow.delta > 0 else -1.0 if order_flow.delta < 0 else 0.0
        reasons: list[str] = []
        scorecard = {
            "range_extension_fraction": round(range_fraction, 4),
            "close_location": round(close_location, 4),
            "value_area_overlap": round(overlap, 4),
            "poc_shift": round(poc_shift, 4),
            "timing_confidence": round(order_flow.timing_confidence, 4),
        }

        if prior is not None:
            if current.high_price > prior.vah and current.close_price < prior.vah and overlap >= self.developing_balance_overlap_min:
                reasons.append("Auction probed above prior value and was rejected back into value.")
                return RegimeAssessment(
                    label="breakout_rejection",
                    confidence=0.73,
                    allowed_directions=["SHORT"],
                    reasons=reasons,
                    scorecard=scorecard,
                )
            if current.low_price < prior.val and current.close_price > prior.val and overlap >= self.developing_balance_overlap_min:
                reasons.append("Auction probed below prior value and was rejected back into value.")
                return RegimeAssessment(
                    label="breakout_rejection",
                    confidence=0.73,
                    allowed_directions=["LONG"],
                    reasons=reasons,
                    scorecard=scorecard,
                )
            if current.high_price > prior.high_price and prior.val <= current.close_price <= prior.vah:
                reasons.append("Range extension beyond prior high failed to hold.")
                return RegimeAssessment(
                    label="failed_auction",
                    confidence=0.76,
                    allowed_directions=["SHORT"],
                    reasons=reasons,
                    scorecard=scorecard,
                )
            if current.low_price < prior.low_price and prior.val <= current.close_price <= prior.vah:
                reasons.append("Range extension below prior low failed to hold.")
                return RegimeAssessment(
                    label="failed_auction",
                    confidence=0.76,
                    allowed_directions=["LONG"],
                    reasons=reasons,
                    scorecard=scorecard,
                )
            if current.close_price > prior.vah and overlap <= self.breakout_acceptance_overlap_max and poc_shift > 0:
                reasons.append("Value migrated higher after acceptance above prior value.")
                label = "trend_continuation" if delta_bias > 0 else "breakout_acceptance"
                return RegimeAssessment(
                    label=label,
                    confidence=0.8,
                    allowed_directions=["LONG"],
                    reasons=reasons,
                    scorecard=scorecard,
                )
            if current.close_price < prior.val and overlap <= self.breakout_acceptance_overlap_max and poc_shift < 0:
                reasons.append("Value migrated lower after acceptance below prior value.")
                label = "trend_continuation" if delta_bias < 0 else "breakout_acceptance"
                return RegimeAssessment(
                    label=label,
                    confidence=0.8,
                    allowed_directions=["SHORT"],
                    reasons=reasons,
                    scorecard=scorecard,
                )

        if range_fraction >= self.trend_day_extension_min:
            if close_location >= self.trend_close_extreme_min and poc_shift >= 0:
                reasons.append("Session closed near the high after meaningful range extension.")
                return RegimeAssessment(
                    label="trend_day",
                    confidence=0.78,
                    allowed_directions=["LONG"],
                    reasons=reasons,
                    scorecard=scorecard,
                )
            if close_location <= (1.0 - self.trend_close_extreme_min) and poc_shift <= 0:
                reasons.append("Session closed near the low after meaningful range extension.")
                return RegimeAssessment(
                    label="trend_day",
                    confidence=0.78,
                    allowed_directions=["SHORT"],
                    reasons=reasons,
                    scorecard=scorecard,
                )

        if overlap >= self.balance_overlap_min and range_fraction < self.trend_day_extension_min:
            reasons.append("Value overlap remains high and directional extension is muted.")
            return RegimeAssessment(
                label="balance",
                confidence=0.72,
                allowed_directions=["LONG", "SHORT"],
                reasons=reasons,
                scorecard=scorecard,
            )

        if overlap >= self.developing_balance_overlap_min:
            reasons.append("Value overlap is moderate; auction is balancing but not fully resolved.")
            return RegimeAssessment(
                label="developing_balance",
                confidence=0.64,
                allowed_directions=["LONG", "SHORT"],
                reasons=reasons,
                scorecard=scorecard,
            )

        if current.poor_high and delta_bias < 0:
            reasons.append("Poor high and negative order flow suggest reversal risk.")
            return RegimeAssessment(
                label="reversal",
                confidence=0.67,
                allowed_directions=["SHORT"],
                reasons=reasons,
                scorecard=scorecard,
            )
        if current.poor_low and delta_bias > 0:
            reasons.append("Poor low and positive order flow suggest reversal risk.")
            return RegimeAssessment(
                label="reversal",
                confidence=0.67,
                allowed_directions=["LONG"],
                reasons=reasons,
                scorecard=scorecard,
            )

        confidence = _clamp(order_flow.timing_confidence, 0.0, self.no_trade_confidence_max)
        reasons.append("No deterministic auction state reached conviction thresholds.")
        return RegimeAssessment(
            label="no_trade",
            confidence=confidence,
            allowed_directions=[],
            reasons=reasons,
            scorecard=scorecard,
        )
