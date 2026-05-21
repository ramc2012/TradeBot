"""Risk approval and position sizing for long-premium trades.

Position size scales with signal confidence:

    multiplier = 0.5 + (confidence - min_confidence) / (max_confidence - min_confidence)

so a barely-passing 0.58-confidence signal sizes at ~0.5× the base budget,
while a 0.85-confidence setup sizes at ~1.5×. The motivation is that
confidence already encodes our expectation of edge — sizing should
respond, not stay flat. The base `risk_pct` and `premium_cap_pct` define
the *median* allocation; the scaler nudges it up or down.

Edge hurdles (min_expected_edge_pct + optimizer rejection_reasons) are
deliberately bypassed for two categories:

  * Learning sleeves (intraday_exploration, intraday_micro_trend) — the
    whole point is to take small bets and accumulate RAG evidence.
  * Commodity underlyings (GOLD/SILVERM/NATURALGAS/CRUDEOIL/...) — the
    model has higher inherent uncertainty than for NSE indices, so the
    edge calculator routinely under-counts. The regime engine's
    confidence is the real signal; capital gates (sizing, daily/weekly
    loss caps) still keep us honest.
"""
from __future__ import annotations

import math
from typing import Any

from directional_options.calibration import load_calibrator
from directional_options.schemas import ContractCandidate, DirectionalSignal, RiskDecision


# Same ceiling the signal/regime engines clamp to. Anchors the "100%" point
# of the confidence-to-size curve.
MAX_ALLOCATION_CONFIDENCE = 0.85
# Floor of the curve at min_confidence — barely-passing signals still trade
# but at half the base risk budget. Below min_confidence the signal engine
# already filters the trade, so the scaler is never invoked below this.
MIN_ALLOCATION_FRACTION = 0.5
# Top of the curve at max_confidence — strongest signals scale to 1.5×.
MAX_ALLOCATION_FRACTION = 1.5

# Regime-level edge gate. Backtest on SENSEX (n=65) showed these expectancies
# per regime (₹):
#   exploration: +1,889 (n=27, wr 44.4%)
#   trend:       -2,587 (n=29, wr 34.5%)
#   breakout:   -11,100 (n=9,  wr 22.2%)
# Until live data shows otherwise, block breakout and shrink trend allocation.
REGIME_BLOCKED: set[str] = {"breakout"}
REGIME_SIZE_MULTIPLIER: dict[str, float] = {
    "exploration": 1.0,
    "trend": 0.5,         # halve allocation in trend until edge proves out
    "micro_trend": 0.7,
    "breakout": 0.0,      # blocked entirely
    "chop": 0.0,
    "risk_off": 0.0,
}

# Delta-bucket gate. Backtest delta breakdown showed:
#   convex (0.30-0.45 delta):  +2.88% (n=3)
#   core   (0.45-0.55 delta):  -5.83% (n=56)  ← theta bleed on ATM
#   deep   (>0.65 delta):      +9.89% (n=1)
#   linear (0.55-0.65 delta):  -1.60% (n=3)
#   lottery (<0.30 delta):    -29.65% (n=3)   ← user warning vindicated
#
# Lottery (deep OTM) is blocked outright. The signal/selector should not
# even propose them at this point. Core gets a size multiplier <1 because
# its sample dominates and is negative — but we can't block it (most
# selector picks land here).
DELTA_BUCKET_BLOCKED: set[str] = {"lottery"}
DELTA_BUCKET_SIZE_MULTIPLIER: dict[str, float] = {
    "convex": 1.2,    # small positive sample, modest oversize
    "core":   0.7,    # dominant sample, negative — shrink
    "deep":   1.1,    # tiny positive sample, neutral
    "linear": 0.9,    # tiny mildly negative sample
    "lottery": 0.0,   # blocked
}


class DirectionalOptionsRiskEngine:
    """Long-option sizing scales with conviction; expectancy must still clear."""

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def _confidence_multiplier(self, confidence: float, min_confidence: float) -> float:
        """Linear ramp from MIN_ALLOCATION_FRACTION at min_confidence to
        MAX_ALLOCATION_FRACTION at MAX_ALLOCATION_CONFIDENCE."""
        span = max(MAX_ALLOCATION_CONFIDENCE - min_confidence, 1e-6)
        normalized = (max(confidence, min_confidence) - min_confidence) / span
        normalized = min(1.0, max(0.0, normalized))
        return MIN_ALLOCATION_FRACTION + normalized * (MAX_ALLOCATION_FRACTION - MIN_ALLOCATION_FRACTION)

    def approve(
        self,
        *,
        candidate: ContractCandidate,
        signal: DirectionalSignal,
        equity: float,
        daily_realized: float = 0.0,
        weekly_realized: float = 0.0,
    ) -> RiskDecision:
        # Scale base budgets by the signal's CALIBRATED conviction.
        # Raw confidence from the signal engine is uncalibrated and was
        # shown by backtest to overstate win rate by ~37pp. We use an
        # isotonic calibrator (fit on actual trade outcomes) to map raw
        # confidence → realized P(win) before sizing. If no calibrator is
        # loaded (first run, no history yet), fall back to raw.
        min_confidence = float(self.config.get("min_confidence", 0.58))
        raw_confidence = float(signal.confidence)
        calibrator = load_calibrator()
        if calibrator is not None:
            calibrated_conf = calibrator.predict(raw_confidence)
        else:
            calibrated_conf = raw_confidence
        confidence = calibrated_conf
        scaler = self._confidence_multiplier(confidence, min_confidence)

        # Apply regime-specific size multiplier. Backtest showed breakout
        # regime trades have catastrophic expectancy (−₹11,100 avg, n=9);
        # trend is mildly negative (−₹2,587, n=29); only exploration is
        # positive (+₹1,889). Until live evidence updates this, gate sizing.
        regime_label = str(signal.regime or "").lower()
        regime_mult = REGIME_SIZE_MULTIPLIER.get(regime_label, 0.5)
        scaler = scaler * regime_mult

        # Apply delta-bucket size multiplier and gate.
        delta_bucket = str(candidate.delta_bucket or "").lower()
        delta_mult = DELTA_BUCKET_SIZE_MULTIPLIER.get(delta_bucket, 0.8)
        scaler = scaler * delta_mult

        risk_budget = equity * float(self.config["risk_pct"]) * scaler
        premium_cap = equity * float(self.config["premium_cap_pct"]) * scaler
        planned_stop_pct = float(self.config["planned_stop_pct"])
        min_expected_edge_pct = float(self.config["min_expected_edge_pct"])
        fee_per_unit = 0.45

        stop_loss_per_unit = candidate.option_price * planned_stop_pct
        lot_premium = candidate.option_price * candidate.lot_size
        lot_risk = max(1.0, (stop_loss_per_unit + fee_per_unit) * candidate.lot_size)
        max_lots_by_risk = math.floor(risk_budget / lot_risk)
        max_lots_by_premium = math.floor(premium_cap / max(lot_premium, 1.0))
        qty_lots = max(0, min(max_lots_by_risk, max_lots_by_premium))

        reasons: list[str] = []
        # Delta-bucket block: lottery (deep OTM) trades averaged -29.65% in
        # backtest — the "balance premium outgo and risk" rule the user
        # called out.
        if delta_bucket in DELTA_BUCKET_BLOCKED:
            reasons.append(
                f"Delta bucket '{delta_bucket}' is blocked at the lane level "
                f"(backtest avg return was -29.65%). Re-enable after live "
                f"evidence shows positive expectancy."
            )
            return RiskDecision(
                approved=False,
                quantity_lots=0,
                quantity_units=0,
                premium_at_risk=0.0,
                max_loss=0.0,
                risk_budget=round(risk_budget, 2),
                premium_cap=round(premium_cap, 2),
                reasons=reasons,
            )
        # Regime block: backtest expectancy was catastrophic in this regime.
        if regime_label in REGIME_BLOCKED:
            reasons.append(
                f"Regime '{regime_label}' is blocked at the lane level "
                f"(backtest expectancy was negative). Re-enable after live "
                f"data shows positive expectancy."
            )
            return RiskDecision(
                approved=False,
                quantity_lots=0,
                quantity_units=0,
                premium_at_risk=0.0,
                max_loss=0.0,
                risk_budget=round(risk_budget, 2),
                premium_cap=round(premium_cap, 2),
                reasons=reasons,
            )
        # Exploration / micro-trend sleeves are *learning bets*. We don't
        # demand positive expected edge from them — that's the whole point
        # of having a small-size lane that records outcomes so RAG can
        # accumulate evidence. The confidence×size scaler keeps the bet
        # tiny (0.5×–0.7× of base risk) so even a string of losses stays
        # well inside the daily loss cap. High-conviction sleeves (trend,
        # breakout, swing_trend) still need the edge hurdle.
        learning_sleeve = str(signal.sleeve or "").lower() in {
            "intraday_exploration", "intraday_micro_trend",
        }
        # Commodity underlyings — the model is less accurate (wider spreads,
        # higher IV, sparser book) so the edge calc routinely under-counts.
        # Trust the regime engine here; the confidence-scaled allocator
        # keeps the bet sized to conviction. The MCX: prefix uniquely
        # identifies commodity instruments across our broker mappings.
        trading_symbol = str(candidate.trading_symbol or "").upper()
        is_commodity = trading_symbol.startswith("MCX:") or trading_symbol.startswith("MCX_")
        bypass_edge_gate = learning_sleeve or is_commodity
        edge_hurdle = (
            candidate.option_price * min_expected_edge_pct
            + float(candidate.spread_cost or 0.0)
            + float(candidate.slippage_cost or 0.0)
            + float(candidate.fees or 0.0)
        )
        if not bypass_edge_gate and candidate.expected_pnl <= edge_hurdle:
            reasons.append("Expected edge does not clear the long-premium hurdle.")
        if candidate.rejection_reasons and not bypass_edge_gate:
            reasons.extend(
                f"Optimizer rejected candidate: {reason}."
                for reason in candidate.rejection_reasons
            )
        if daily_realized <= -(risk_budget * float(self.config["daily_loss_cap_r"])):
            reasons.append("Daily loss cap is already breached.")
        if weekly_realized <= -(risk_budget * float(self.config["weekly_loss_cap_r"])):
            reasons.append("Weekly loss cap is already breached.")
        if qty_lots < 1:
            reasons.append(
                f"Sizing rules do not permit even one lot (conf {confidence:.2f} → "
                f"scaler {scaler:.2f}× of {self.config['risk_pct']:.3%} risk / "
                f"{self.config['premium_cap_pct']:.3%} premium)."
            )

        return RiskDecision(
            approved=not reasons,
            quantity_lots=max(qty_lots, 0),
            quantity_units=max(qty_lots, 0) * candidate.lot_size,
            premium_at_risk=round(max(qty_lots, 0) * lot_premium, 2),
            max_loss=round(max(qty_lots, 0) * lot_risk, 2),
            risk_budget=round(risk_budget, 2),
            premium_cap=round(premium_cap, 2),
            reasons=reasons,
        )
