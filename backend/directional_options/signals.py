"""Directional signal generation on the underlying spot series.

Confidence is hard-capped at MAX_SIGNAL_CONFIDENCE so a single overheated
bar can't dominate the policy's value posterior. There is no longer a
hard min_confidence cutoff — the RL policy in `directional_options.policy`
learns its own trade/skip threshold from realised R-multiples. Only an
empty / zero-direction-score signal is filtered here, because feeding
those into the policy would just teach it that flat tape doesn't pay
(wasting model capacity).

2026-07-17 NOTE: a same-day experiment re-introduced structural fail-closed
gates here (allowed_regimes / min_confidence / raised direction-score
floor). REVERSED same day by owner directive ("uncap signals, no hard
gate") — regimes are FEATURES the policy sees as a one-hot, never
barriers, and IV state SIZES positions via compute_iv_sizing_factor()
instead of vetoing them. Cadence discipline lives in the EXECUTION layer
(paper.py: re-entry cooldowns, flip confirmation, min-hold), not here.
"""
from __future__ import annotations

from typing import Any, Optional

from core.config import settings
from directional_options.config import INDEX_UNIVERSE
from directional_options.features import timeframe_minutes
from directional_options.schemas import DirectionalSignal, RegimeSnapshot


# Trading-grade ceiling. Matches regime.MAX_REGIME_CONFIDENCE so the two
# engines agree on the upper bound; the risk allocator uses this value as
# its 100% point on the confidence-to-size curve.
MAX_SIGNAL_CONFIDENCE = 0.85

# ── IV sizing curve (2026-07-17) ─────────────────────────────────────────────
# OWNER DIRECTIVE: "position has to be sized as per IV it cannot prevent a
# trade." The former positional vol gate (vol_ok = d_atm_iv >= 0, hard veto)
# is replaced by a monotone sizing factor in [IV_SIZING_FLOOR, 1.0] applied to
# the BASE risk budget in risk.approve().
#
# RESEARCH GROUNDING (2026-06-28 vol-state, measured): IV-percentile LOW
# favors long premium — fwd ATM straddle IC −0.305 (t=−13.2): HIGH IV level
# LOSES for long premium. So: low/falling IV ⇒ full size (1.0); high/rising
# IV ⇒ shrink toward the floor. LEVEL (percentile of the ATM IV level within
# the positioning panel's trailing window) is the primary conditioner, a
# rising d_atm_iv (day-over-day ATM IV change, IV points) the secondary one.
IV_SIZING_FLOOR = 0.25
# d_atm_iv is NULL when the feed could not invert an ATM IV for today or
# yesterday (missing chain premium, series start). We still trade — the veto
# is gone — but SMALL: without an IV trend we cannot claim the favorable
# (low/falling) state that justifies full size, so take the conservative
# midpoint of the sizing range instead.
IV_SIZING_NEUTRAL = 0.5
# Rising-IV shrink saturates at +3 IV points/day (observed d_atm_iv magnitudes
# run ≈ −1.5…+0.3; +3 in a day is a violent vol spike — floor-size territory).
IV_RISE_SATURATION_POINTS = 3.0
# ATM-IV-level percentile above which the level component starts shrinking
# (median and below = full size; the measured harm concentrates in HIGH IV).
IV_PCTILE_SHRINK_START = 0.5


def compute_iv_sizing_factor(
    d_atm_iv: float | None,
    atm_iv_pctile: float | None = None,
    *,
    floor: float = IV_SIZING_FLOOR,
) -> float:
    """IV-state position-size factor in [floor, 1.0] — NEVER a trade veto.

    * ``d_atm_iv`` (IV points/day): falling/flat ⇒ trend component 1.0;
      rising shrinks linearly, saturating at IV_RISE_SATURATION_POINTS.
    * ``atm_iv_pctile`` (0..1 rank of today's ATM IV level in the positioning
      panel's trailing window; None when the feed lacks history): ≤ median ⇒
      level component 1.0; above the median it shrinks linearly to the floor
      at the 100th percentile. LEVEL is the primary researched conditioner.
    * ``d_atm_iv is None`` ⇒ conservative neutral IV_SIZING_NEUTRAL (feed
      could not compute an ATM IV trend — trade, but small).

    Monotone: non-increasing in both d_atm_iv and atm_iv_pctile.
    """
    if d_atm_iv is None:
        return IV_SIZING_NEUTRAL
    rise = max(float(d_atm_iv), 0.0)
    trend_component = 1.0 - (1.0 - floor) * min(rise / IV_RISE_SATURATION_POINTS, 1.0)
    level_component = 1.0
    if atm_iv_pctile is not None:
        pctile = min(max(float(atm_iv_pctile), 0.0), 1.0)
        if pctile > IV_PCTILE_SHRINK_START:
            level_component = 1.0 - (1.0 - floor) * (
                (pctile - IV_PCTILE_SHRINK_START) / (1.0 - IV_PCTILE_SHRINK_START)
            )
    return max(floor, min(1.0, trend_component * level_component))


class DirectionalSignalEngine:
    """Generate directional expected-move forecasts on the underlying."""

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def predict(
        self,
        row,
        regime: RegimeSnapshot,
        timeframe: str,
        positioning: dict | None = None,
        underlying: str | None = None,
    ) -> Optional[DirectionalSignal]:
        # NOTE: the `regime.trade_allowed` gate was removed in the RL
        # refactor. Per the design directive, regimes are FEATURES (the
        # policy sees the label as a one-hot), not barriers. The bandit
        # will quickly learn that chop / risk_off trades bleed theta and
        # stop choosing them — but it needs to SEE them first. Without
        # this, the policy never gets called on any session whose
        # regime is currently chop / risk_off, and the UI shows "no
        # signal" indefinitely. (An allowed_regimes hard gate briefly
        # returned on 2026-07-17 and was reversed the same day by owner
        # directive.)
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

        # ── UNIVERSE SPLIT (2026-07-17, NIFTY-50 expansion) ───────────────
        # The positional-confirmation view and its fail-closed positioning
        # gate are INDEX-ONLY: the positioning feed (directional_positioning
        # _daily) is researched and populated for indices, and the researched
        # confirmation edge is index-specific (BANKNIFTY OI-build alignment).
        # STOCKS must not be silently killed by a missing index-only feed row
        # — they route through the standard signal engine below (regime +
        # momentum/fade features). `underlying=None` (older callers /
        # backtester) conservatively keeps the index-scope fail-closed
        # behaviour unchanged.
        index_scope = underlying is None or str(underlying).upper().strip() in INDEX_UNIVERSE

        if settings.DIRECTIONAL_POSITIONAL_OPTIONS_ENABLED and index_scope and positioning is None:
            # MISSING positioning row (feed never wrote this underlying, DB
            # error, table reset) fails CLOSED like the stale case — the legacy
            # momentum fallback below is the measured-catastrophic (PF~0.2)
            # entry this redesign replaced. No feed row -> no new entry; held
            # positions keep their protective exits in the paper book.
            return None

        positional_active = (
            settings.DIRECTIONAL_POSITIONAL_OPTIONS_ENABLED and index_scope and positioning is not None
        )
        iv_sizing = 1.0
        if positional_active:
            # POSITIONAL view (researched edge): HTF daily direction sets the side;
            # OPTION POSITIONING must CONFIRM it (call-OI building / low PCR for CE;
            # put-side for PE) — else no trade. Trend alone is anti-predictive;
            # the positioning is the edge.
            if positioning.get("is_stale"):
                # Stale positioning feed → take NO new positional entries. Do NOT
                # fall through to legacy intraday momentum (that is the churn the
                # redesign removes). Held positions keep their stop/target/DTE
                # exits in the paper book.
                return None
            htf_up = bool(positioning.get("htf_up"))
            oib = positioning.get("oi_build_bias")
            pcr = positioning.get("pcr_oi")
            daiv = positioning.get("d_atm_iv")
            direction = "CE" if htf_up else "PE"
            if direction == "CE":
                confirm = (oib is not None and float(oib) > 0.0) or (pcr is not None and float(pcr) < settings.DIRECTIONAL_POSITIONAL_PCR_LOW)
            else:
                confirm = (oib is not None and float(oib) < 0.0) or (pcr is not None and float(pcr) > settings.DIRECTIONAL_POSITIONAL_PCR_HIGH)
            if not confirm:
                return None
            # IV state SIZES the trade — it never vetoes it (2026-07-17 owner
            # directive: "position has to be sized as per IV it cannot prevent
            # a trade"). The former vol_ok hard gate is replaced by
            # compute_iv_sizing_factor(): low/falling IV ⇒ 1.0 (full base
            # budget), high/rising IV ⇒ monotone shrink toward IV_SIZING_FLOOR,
            # NULL d_atm_iv ⇒ conservative IV_SIZING_NEUTRAL. risk.approve()
            # scales the BASE risk budget by this factor.
            iv_sizing = compute_iv_sizing_factor(
                None if daiv is None else float(daiv),
                positioning.get("atm_iv_pctile"),
            )
            direction_score = 0.5
            confidence = min(MAX_SIGNAL_CONFIDENCE, 0.60)
        else:
            bull_score = (ema_spread * 180.0) + breakout_up + max(di_bias, 0.0) + max(momentum_3, 0.0) * 12.0 + max(momentum_8, 0.0) * 8.0
            bear_score = (-ema_spread * 180.0) + breakout_down + max(-di_bias, 0.0) + max(-momentum_3, 0.0) * 12.0 + max(-momentum_8, 0.0) * 8.0

            direction = "CE" if bull_score >= bear_score else "PE"
            direction_score = bull_score if direction == "CE" else bear_score
            # Floor lowered to ~0 so the policy sees ALL non-dead bars; the
            # bandit's regime one-hot captures context and Thompson-skips chop.
            min_direction_score = float(self.config.get("min_direction_score_floor", 0.001))
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
        # the learned R-multiple posterior. (A 0.60 cutoff briefly returned
        # on 2026-07-17; reversed same day by owner directive.)

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
            positional=positional_active,
            iv_sizing_factor=round(iv_sizing, 4),
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
