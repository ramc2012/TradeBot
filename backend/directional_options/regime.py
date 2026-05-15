"""Regime classification for the directional long-options engine.

This engine targets *intraday* 5-minute and 15-minute setups — not multi-day
trend following. Expiry preference is therefore weekly (or current-day
weekly if available) across every actionable label; monthly is only used
for risk-off where we want extra time value as a cushion.

Confidence is hard-capped at MAX_REGIME_CONFIDENCE so the engine can never
claim near-certainty — no real-money setup deserves that. The cap also
keeps the downstream confidence×allocation scaler well-bounded.
"""
from __future__ import annotations

from directional_options.schemas import RegimeSnapshot


# Trading-grade confidence ceiling. 100% confidence is not possible; even
# the strongest setups warrant doubt. 0.85 leaves headroom for the signal
# engine's own ceiling (0.85) and the risk-allocation curve.
MAX_REGIME_CONFIDENCE = 0.85


class RegimeClassifier:
    """Classify bars into trend, breakout, chop, or abnormal-vol states."""

    def classify(self, row, *, timeframe: str | None = None) -> RegimeSnapshot:
        adx = float(row.get("adx", 0.0))
        breakout_up = float(row.get("breakout_up", 0.0))
        breakout_down = float(row.get("breakout_down", 0.0))
        rv_pct = float(row.get("rv_percentile", 0.0))
        range_expansion = float(row.get("range_expansion", 1.0))
        ema_spread = float(row.get("ema_spread_pct", 0.0))
        tf = str(timeframe or "").lower()
        is_fast = tf.startswith("5")

        reasons: list[str] = []
        if rv_pct >= 0.9 and range_expansion >= 1.9:
            reasons.append("abnormal realized-vol expansion")
            return RegimeSnapshot(
                label="risk_off",
                trade_allowed=False,
                confidence=min(MAX_REGIME_CONFIDENCE, 0.55 + rv_pct / 2.0),
                reasons=reasons,
                preferred_expiry_kind="monthly",
                delta_target_min=0.45,
                delta_target_max=0.65,
                exit_profile="defensive",
            )

        breakout_hurdle = 0.5
        breakout_adx = 18.0 if is_fast else 22.0
        if max(breakout_up, breakout_down) >= breakout_hurdle and adx >= breakout_adx:
            reasons.append("range expansion cleared breakout hurdle")
            reasons.append("trend strength confirmed by ADX")
            return RegimeSnapshot(
                label="breakout",
                trade_allowed=True,
                confidence=min(MAX_REGIME_CONFIDENCE, 0.52 + max(breakout_up, breakout_down) * 0.18 + adx / 100.0),
                reasons=reasons,
                preferred_expiry_kind="weekly",
                delta_target_min=0.30,
                delta_target_max=0.55,
                exit_profile="aggressive",
            )

        if adx >= 18.0 and abs(ema_spread) >= 0.0014:
            reasons.append("multi-bar trend bias is aligned")
            reasons.append("trend strength remains above chop threshold")
            return RegimeSnapshot(
                label="trend",
                trade_allowed=True,
                confidence=min(MAX_REGIME_CONFIDENCE, 0.48 + adx / 100.0 + abs(ema_spread) * 18.0),
                reasons=reasons,
                # Intraday 5/15-min directional — always prefer weekly. The
                # previous monthly fallback on high-rv days assumed multi-day
                # holds; that's not the strategy.
                preferred_expiry_kind="weekly",
                delta_target_min=0.35,
                delta_target_max=0.60,
                exit_profile="balanced",
            )

        if is_fast and adx >= 14.0 and abs(ema_spread) >= 0.0010:
            # 5-minute tape can support tradable micro-trends even when 15-minute
            # ADX remains suppressed. This keeps NIFTY tradable on short horizons
            # without mislabeling the higher timeframe as trending.
            reasons.append("micro trend bias on 5-minute timeframe")
            reasons.append("adx/ema spread cleared fast-tape hurdle")
            return RegimeSnapshot(
                label="micro_trend",
                trade_allowed=True,
                confidence=min(MAX_REGIME_CONFIDENCE, 0.46 + adx / 120.0 + abs(ema_spread) * 14.0),
                reasons=reasons,
                preferred_expiry_kind="weekly",
                delta_target_min=0.35,
                delta_target_max=0.60,
                exit_profile="balanced",
            )

        reasons.append("directional structure is conflicted or too compressed")
        return RegimeSnapshot(
            label="chop",
            trade_allowed=False,
            confidence=min(MAX_REGIME_CONFIDENCE, 0.45 + max(0.0, 18.0 - adx) / 40.0),
            reasons=reasons,
            preferred_expiry_kind="weekly",
            delta_target_min=0.45,
            delta_target_max=0.55,
            exit_profile="defensive",
        )
