"""Directional signal generation on the underlying spot series.

Confidence is hard-capped at MAX_SIGNAL_CONFIDENCE so a single overheated
bar can't dominate the policy's value posterior. There is no longer a
hard min_confidence cutoff — the RL policy in `directional_options.policy`
learns its own trade/skip threshold from realised R-multiples. Only an
empty / zero-direction-score signal is filtered here, because feeding
those into the policy would just teach it that flat tape doesn't pay
(wasting model capacity).
"""
from __future__ import annotations

from typing import Any, Optional

from directional_options.features import timeframe_minutes
from directional_options.schemas import DirectionalSignal, RegimeSnapshot


# Trading-grade ceiling. Matches regime.MAX_REGIME_CONFIDENCE so the two
# engines agree on the upper bound; the risk allocator uses this value as
# its 100% point on the confidence-to-size curve.
MAX_SIGNAL_CONFIDENCE = 0.85


class DirectionalSignalEngine:
    """Generate directional expected-move forecasts on the underlying."""

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def predict(self, row, regime: RegimeSnapshot, timeframe: str) -> Optional[DirectionalSignal]:
        if not regime.trade_allowed:
            return None

        ema_spread = float(row.get("ema_spread_pct", 0.0))
        breakout_up = max(float(row.get("breakout_up", 0.0)), 0.0)
        breakout_down = max(float(row.get("breakout_down", 0.0)), 0.0)
        di_bias = (float(row.get("plus_di", 0.0)) - float(row.get("minus_di", 0.0))) / 100.0
        momentum_3 = float(row.get("momentum_3", 0.0))
        momentum_8 = float(row.get("momentum_8", 0.0))
        atr = max(float(row.get("atr", 0.0)), 0.01)
        close = float(row.get("close", 0.0))
        range_expansion = float(row.get("range_expansion", 1.0))
        rv_pct = float(row.get("rv_percentile", 0.0))

        bull_score = (ema_spread * 180.0) + breakout_up + max(di_bias, 0.0) + max(momentum_3, 0.0) * 12.0 + max(momentum_8, 0.0) * 8.0
        bear_score = (-ema_spread * 180.0) + breakout_down + max(-di_bias, 0.0) + max(-momentum_3, 0.0) * 12.0 + max(-momentum_8, 0.0) * 8.0

        direction = "CE" if bull_score >= bear_score else "PE"
        direction_score = bull_score if direction == "CE" else bear_score
        # Tiny direction-score floor — empty / zero-momentum bars are
        # filtered so the policy doesn't waste capacity learning that flat
        # tape doesn't pay. Regime-aware: looser for the lower-conviction
        # regimes (exploration / micro_trend) since the policy is meant to
        # learn from those small bets.
        min_direction_score = float(
            self.config.get("min_direction_score_other", 0.02)
            if regime.label in {"exploration", "micro_trend"}
            else self.config.get("min_direction_score_trend", 0.04)
        )
        if direction_score <= min_direction_score:
            return None

        confidence = min(
            MAX_SIGNAL_CONFIDENCE,
            0.42
            + (direction_score * 0.18)
            + regime.confidence * 0.28
            + (self.config["breakout_confidence_bonus"] if regime.label == "breakout" else 0.0),
        )
        # NOTE: no hard min_confidence cutoff anymore. Low-confidence
        # signals pass through to the policy, which decides act/skip from
        # the learned R-multiple posterior.

        if regime.label == "breakout":
            horizon_bars = int(self.config["short_horizon_bars"])
            sleeve = "intraday_breakout"
            iv_change = 0.012 if rv_pct < 0.75 else -0.002
        elif regime.label == "trend":
            horizon_bars = int(self.config["medium_horizon_bars"] if rv_pct < 0.8 else self.config["long_horizon_bars"])
            sleeve = "swing_trend"
            iv_change = 0.004 if rv_pct < 0.55 else -0.004
        elif regime.label == "micro_trend":
            # 5-minute micro-trends — keep the horizon tight (short) so the
            # trade resolves inside the regime engine's intended timescale.
            # IV drift on micro_trend is essentially flat; we pay the spread,
            # not the vega, so don't try to monetise IV here.
            horizon_bars = int(self.config["short_horizon_bars"])
            sleeve = "intraday_micro_trend"
            iv_change = 0.0
        elif regime.label == "exploration":
            # Low-conviction exploratory bet so the agent learns. Smallest
            # horizon, neutral IV expectation, paired with the risk
            # allocator's 0.5× floor for minimum sizing.
            horizon_bars = int(self.config["short_horizon_bars"])
            sleeve = "intraday_exploration"
            iv_change = 0.0
        else:
            horizon_bars = int(self.config["short_horizon_bars"])
            sleeve = "no_trade"
            iv_change = -0.006

        expected_move = max(
            atr * (float(self.config["expected_move_atr_multiplier"]) + confidence * 0.3),
            close * abs(ema_spread) * (float(self.config["expected_move_trend_multiplier"]) + range_expansion * 0.2),
        )
        expected_move_pct = expected_move / max(close, 1.0)
        p_up = confidence if direction == "CE" else 1.0 - confidence
        jump_score = min(
            1.0,
            max(breakout_up, breakout_down, 0.0) * 0.45
            + max(range_expansion - 1.0, 0.0) * 0.28
            + max(rv_pct - 0.55, 0.0) * 0.32,
        )
        timing_precision = min(
            1.0,
            0.35
            + regime.confidence * 0.34
            + (
                0.18 if regime.label == "breakout"
                else 0.08 if regime.label == "trend"
                else 0.06 if regime.label == "micro_trend"
                else 0.04 if regime.label == "exploration"
                else 0.0
            )
            + max(range_expansion - 1.0, 0.0) * 0.12,
        )
        p_move_gt_1sigma = min(0.95, max(0.0, 0.18 + confidence * 0.32 + jump_score * 0.22))
        p_move_gt_2sigma = min(0.65, max(0.0, 0.04 + jump_score * 0.22 + max(confidence - 0.65, 0.0) * 0.35))
        tail_probability = p_move_gt_2sigma if jump_score >= 0.45 else p_move_gt_1sigma
        model_uncertainty = max(0.03, min(0.45, (1.0 - confidence) * 0.55 + max(rv_pct - 0.7, 0.0) * 0.18))

        hours = (horizon_bars * timeframe_minutes(timeframe)) / 60.0
        thesis = (
            f"{sleeve.replace('_', ' ')} setup with {direction} bias, "
            f"{confidence:.0%} confidence, and {expected_move:.1f} expected points."
        )

        return DirectionalSignal(
            direction=direction,
            confidence=round(confidence, 4),
            expected_move=round(expected_move, 2),
            expected_horizon_bars=horizon_bars,
            expected_horizon_hours=round(hours, 2),
            direction_score=round(direction_score, 4),
            expected_iv_change=round(iv_change, 4),
            sleeve=sleeve,
            thesis=thesis,
            regime=regime.label,
            expected_move_pct=round(expected_move_pct, 5),
            p_up=round(p_up, 4),
            p_move_gt_1sigma=round(p_move_gt_1sigma, 4),
            p_move_gt_2sigma=round(p_move_gt_2sigma, 4),
            jump_score=round(jump_score, 4),
            timing_precision=round(timing_precision, 4),
            tail_probability=round(tail_probability, 4),
            model_uncertainty=round(model_uncertainty, 4),
        )
