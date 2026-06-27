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

import math
from typing import Any, Optional

from core.config import settings
from directional_options.features import timeframe_minutes
from directional_options.schemas import DirectionalSignal, RegimeSnapshot


# Trading-grade ceiling. Matches regime.MAX_REGIME_CONFIDENCE so the two
# engines agree on the upper bound; the risk allocator uses this value as
# its 100% point on the confidence-to-size curve.
MAX_SIGNAL_CONFIDENCE = 0.85


def _sign(value: Any) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 1.0 if v > 0.0 else (-1.0 if v < 0.0 else 0.0)


def _safe(value: Any) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


class DirectionalSignalEngine:
    """Generate directional expected-move forecasts on the underlying."""

    def __init__(self, config: dict[str, Any], view_config: dict[str, Any] | None = None):
        self.config = config
        # Multi-factor-view weights/gates (DEFAULT_CONFIG['view']); consumed only
        # when settings.DIRECTIONAL_MULTIFACTOR_VIEW_ENABLED. Empty → inline defaults.
        self.view = dict(view_config or {})

    def _multifactor_view(self, *, row, regime: RegimeSnapshot, chain, close, atr):
        """Form (direction, conviction, confidence) from a sign-constrained
        confluence of orthogonal families. Each term is tanh-bounded so no single
        input dominates; chain terms are RAW (no causal normalization yet). GEX is
        a conviction damper, NOT a directional vote (its 1-2 day / NSE edge is
        unproven). Returns (None, 0, 0) is never used — direction is always set."""
        v = self.view
        atr_v = max(float(atr), 1e-9)
        atr_pct = max(float(row.get("atr_pct", 0.0)), 1e-6)
        ema_slow = float(row.get("ema_slow", close) or close)
        trend_tstat = float(row.get("trend_tstat", 0.0))
        macd_hist_pct = float(row.get("macd_hist_pct", 0.0))
        adx = float(row.get("adx", 0.0))
        htf_trend = float(row.get("htf_trend_pct", 0.0))

        # Price families (orthogonalized): vol-robust trend backbone, ATR-extension,
        # ATR-normalized acceleration (MACD histogram 2nd-derivative).
        trend_term = math.tanh(trend_tstat / 2.0)
        ext_term = math.tanh((float(close) - ema_slow) / atr_v)
        acc_term = math.tanh(macd_hist_pct / atr_pct)

        # Chain tilt (live, raw-bounded): 25Δ risk reversal — calls richer than puts
        # (RR>0) leans bullish; pick the cheaper wing downstream. Degrades to 0 if no
        # chain / no ATM IV.
        ch = chain or {}
        atm_iv = float(ch.get("atm_iv") or 0.0)
        skew_term = 0.0
        rr = ch.get("risk_reversal_25d")
        if rr is not None and atm_iv > 0.0:
            skew_term = math.tanh((float(rr) / atm_iv) * float(v.get("skew_scale", 8.0)))
        # Flow term wired but held at w_flow=0 by default: dex/OI sign convention is
        # unvalidated for NSE and the adversarial review flagged flow as mostly noise.
        flow_term = math.tanh(_sign(ch.get("dex_net")) * float(v.get("flow_scale", 1.0)))

        score = (
            float(v.get("w_trend", 1.0)) * trend_term
            + float(v.get("w_extension", 0.35)) * ext_term
            + float(v.get("w_acceleration", 0.30)) * acc_term
            + float(v.get("w_skew", 0.45)) * skew_term
            + float(v.get("w_flow", 0.0)) * flow_term
        )
        # Chop gate: attenuate when ADX is below the trend-strength floor.
        if adx < float(v.get("adx_floor", 25.0)):
            score *= float(v.get("adx_attenuation", 0.45))
        # HTF alignment: penalize a view that opposes the longer-window trend.
        if htf_trend != 0.0 and (score > 0.0) != (htf_trend > 0.0):
            score *= float(v.get("htf_align_penalty", 0.55))

        direction = "CE" if score >= 0.0 else "PE"
        conviction = abs(score)
        # Dealer-gamma regime damper on conviction (NOT direction): +GEX (pinning/
        # mean-revert) shrinks; -GEX (trending/amplifying) lifts. Prefer dealer GEX.
        gex = ch.get("dealer_gex_total")
        if gex is None:
            gex = ch.get("gex_total")
        if gex is not None:
            if float(gex) > 0.0:
                conviction *= 1.0 - float(v.get("gex_damp_max", 0.40))
            elif float(gex) < 0.0:
                conviction *= 1.0 + float(v.get("gex_amplify_max", 0.30))

        confidence = min(MAX_SIGNAL_CONFIDENCE, 0.42 + conviction * 0.45 + regime.confidence * 0.20)
        return direction, conviction, confidence

    def predict(
        self,
        row,
        regime: RegimeSnapshot,
        timeframe: str,
        chain: dict[str, Any] | None = None,
    ) -> Optional[DirectionalSignal]:
        # NOTE: the `regime.trade_allowed` gate was removed in the RL
        # refactor. Per the design directive, regimes are FEATURES (the
        # policy sees the label as a one-hot), not barriers. The bandit
        # will quickly learn that chop / risk_off trades bleed theta and
        # stop choosing them — but it needs to SEE them first. Without
        # this, the policy never gets called on any session whose
        # regime is currently chop / risk_off, and the UI shows "no
        # signal" indefinitely.
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

        min_direction_score = float(self.config.get("min_direction_score_floor", 0.001))

        if settings.DIRECTIONAL_MULTIFACTOR_VIEW_ENABLED:
            # MULTI-FACTOR VIEW: direction is formed from a regime-gated,
            # sign-constrained confluence of orthogonal families (trend backbone +
            # ATR-extension + acceleration + LIVE 25Δ skew tilt), attenuated by an
            # ADX/chop gate and HTF alignment, with conviction damped by the dealer
            # gamma regime. Unlike the legacy momentum sum, the chain tilt can FLIP
            # the side, not merely confirm it. Degrades to price-only if chain=None.
            direction, direction_score, confidence = self._multifactor_view(
                row=row, regime=regime, chain=chain, close=close, atr=atr
            )
            if direction is None or direction_score <= min_direction_score:
                return None
        else:
            # LEGACY collinear price-momentum sum. Floor ~0 so the policy sees ALL
            # non-dead bars; the bandit's regime one-hot captures context and the
            # value posterior Thompson-skips chop on its own. Only literal
            # zero-momentum bars are filtered.
            bull_score = (ema_spread * 180.0) + breakout_up + max(di_bias, 0.0) + max(momentum_3, 0.0) * 12.0 + max(momentum_8, 0.0) * 8.0
            bear_score = (-ema_spread * 180.0) + breakout_down + max(-di_bias, 0.0) + max(-momentum_3, 0.0) * 12.0 + max(-momentum_8, 0.0) * 8.0

            direction = "CE" if bull_score >= bear_score else "PE"
            direction_score = bull_score if direction == "CE" else bear_score
            if direction_score <= min_direction_score:
                return None

            confidence = min(
                MAX_SIGNAL_CONFIDENCE,
                0.42
                + (direction_score * 0.18)
                + regime.confidence * 0.28
                + (self.config["breakout_confidence_bonus"] if regime.label == "breakout" else 0.0),
            )
        # NOTE: no hard min_confidence cutoff in either path — low-confidence
        # signals pass through to the policy/meta-model, which decides act/skip.

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
