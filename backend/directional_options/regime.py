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
        # Fast intraday tape = 3-minute (new FAST-lane default) or 5-minute.
        # Exact-match set: the previous startswith(("3", "5")) also caught
        # "30minute", silently applying fast-tape breakout/micro-trend
        # hurdles to the slow tape.
        is_fast = tf in {"3minute", "5minute", "3min", "5min", "3m", "5m"}

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

        # "Trend" — both ADX and EMA-spread argue for direction. ema_spread
        # threshold was 0.14% which is unrealistically tight on a 5/15-min
        # tape — BANKNIFTY at ADX 31 with EMA spread 0.015% (genuinely
        # trending micro) used to fall to chop. 0.05% is the new minimum:
        # roughly 25 NIFTY-points / 35 BANKNIFTY-points / 50 SENSEX-points
        # of EMA(8)–EMA(21) separation, which is a real directional bias.
        if adx >= 18.0 and abs(ema_spread) >= 0.0005:
            reasons.append("multi-bar trend bias is aligned")
            reasons.append("trend strength remains above chop threshold")
            return RegimeSnapshot(
                label="trend",
                trade_allowed=True,
                confidence=min(MAX_REGIME_CONFIDENCE, 0.48 + adx / 100.0 + abs(ema_spread) * 30.0),
                reasons=reasons,
                # Intraday 5/15-min directional — always prefer weekly. The
                # previous monthly fallback on high-rv days assumed multi-day
                # holds; that's not the strategy.
                preferred_expiry_kind="weekly",
                delta_target_min=0.35,
                delta_target_max=0.60,
                exit_profile="balanced",
            )

        if is_fast and adx >= 12.0 and abs(ema_spread) >= 0.0003:
            # Fast (3/5-minute) tape can support tradable micro-trends even
            # when the higher-timeframe ADX is suppressed. Threshold lowered
            # so a genuinely-moving instrument on fast bars isn't mislabelled
            # chop just because the 14-bar ADX hasn't crossed 14 yet.
            reasons.append("micro trend bias on fast intraday timeframe")
            reasons.append("adx/ema spread cleared fast-tape hurdle")
            return RegimeSnapshot(
                label="micro_trend",
                trade_allowed=True,
                confidence=min(MAX_REGIME_CONFIDENCE, 0.46 + adx / 120.0 + abs(ema_spread) * 25.0),
                reasons=reasons,
                preferred_expiry_kind="weekly",
                delta_target_min=0.35,
                delta_target_max=0.60,
                exit_profile="balanced",
            )

        # "Exploration" — neither hurdle is cleanly met, but ADX and the
        # DI bias still hint at a side. We take a *small* exploratory bet
        # (low confidence ⇒ the risk-allocation curve sizes it at the
        # 0.5× floor) so the case base actually accumulates evidence
        # instead of staying empty. Without recorded trades there is
        # nothing for RAG to learn from.
        plus_di = float(row.get("plus_di", 0.0))
        minus_di = float(row.get("minus_di", 0.0))
        di_bias = plus_di - minus_di
        if adx >= 10.0 and abs(di_bias) >= 2.0 and abs(ema_spread) >= 0.00008:
            reasons.append("low-conviction directional hint from ADX/DI bias")
            reasons.append("exploration trade to build learning case base")
            confidence = min(
                MAX_REGIME_CONFIDENCE,
                0.40 + adx / 200.0 + min(abs(di_bias), 25.0) / 80.0 + abs(ema_spread) * 18.0,
            )
            return RegimeSnapshot(
                label="exploration",
                trade_allowed=True,
                confidence=confidence,
                reasons=reasons,
                preferred_expiry_kind="weekly",
                # Tighter delta band so exploration buys ATM-ish options
                # whose theta drag is small and convexity is good if the
                # micro thesis plays out within the 3-bar horizon.
                delta_target_min=0.40,
                delta_target_max=0.55,
                exit_profile="defensive",
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
