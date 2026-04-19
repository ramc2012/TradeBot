"""Directional signal generation on the underlying spot series."""
from __future__ import annotations

from typing import Any, Optional

from directional_options.features import timeframe_minutes
from directional_options.schemas import DirectionalSignal, RegimeSnapshot


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
        if direction_score <= 0.15:
            return None

        confidence = min(
            0.97,
            0.42
            + (direction_score * 0.18)
            + regime.confidence * 0.28
            + (self.config["breakout_confidence_bonus"] if regime.label == "breakout" else 0.0),
        )
        if confidence < float(self.config["min_confidence"]):
            return None

        if regime.label == "breakout":
            horizon_bars = int(self.config["short_horizon_bars"])
            sleeve = "intraday_breakout"
            iv_change = 0.012 if rv_pct < 0.75 else -0.002
        elif regime.label == "trend":
            horizon_bars = int(self.config["medium_horizon_bars"] if rv_pct < 0.8 else self.config["long_horizon_bars"])
            sleeve = "swing_trend"
            iv_change = 0.004 if rv_pct < 0.55 else -0.004
        else:
            horizon_bars = int(self.config["short_horizon_bars"])
            sleeve = "no_trade"
            iv_change = -0.006

        expected_move = max(
            atr * (float(self.config["expected_move_atr_multiplier"]) + confidence * 0.3),
            close * abs(ema_spread) * (float(self.config["expected_move_trend_multiplier"]) + range_expansion * 0.2),
        )

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
        )
