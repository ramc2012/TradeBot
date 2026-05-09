"""Regime classification for the directional long-options engine."""
from __future__ import annotations

from directional_options.schemas import RegimeSnapshot


class RegimeClassifier:
    """Classify bars into trend, breakout, chop, or abnormal-vol states."""

    def classify(self, row) -> RegimeSnapshot:
        adx = float(row.get("adx", 0.0))
        breakout_up = float(row.get("breakout_up", 0.0))
        breakout_down = float(row.get("breakout_down", 0.0))
        rv_pct = float(row.get("rv_percentile", 0.0))
        range_expansion = float(row.get("range_expansion", 1.0))
        ema_spread = float(row.get("ema_spread_pct", 0.0))

        reasons: list[str] = []
        if rv_pct >= 0.9 and range_expansion >= 1.9:
            reasons.append("abnormal realized-vol expansion")
            return RegimeSnapshot(
                label="risk_off",
                trade_allowed=False,
                confidence=min(0.98, 0.55 + rv_pct / 2.0),
                reasons=reasons,
                preferred_expiry_kind="monthly",
                delta_target_min=0.45,
                delta_target_max=0.65,
                exit_profile="defensive",
            )

        if max(breakout_up, breakout_down) >= 0.5 and adx >= 22.0:
            reasons.append("range expansion cleared breakout hurdle")
            reasons.append("trend strength confirmed by ADX")
            return RegimeSnapshot(
                label="breakout",
                trade_allowed=True,
                confidence=min(0.96, 0.52 + max(breakout_up, breakout_down) * 0.18 + adx / 100.0),
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
                confidence=min(0.94, 0.48 + adx / 100.0 + abs(ema_spread) * 18.0),
                reasons=reasons,
                preferred_expiry_kind="monthly" if rv_pct > 0.7 else "weekly",
                delta_target_min=0.35,
                delta_target_max=0.60,
                exit_profile="balanced",
            )

        reasons.append("directional structure is conflicted or too compressed")
        return RegimeSnapshot(
            label="chop",
            trade_allowed=False,
            confidence=min(0.9, 0.45 + max(0.0, 18.0 - adx) / 40.0),
            reasons=reasons,
            preferred_expiry_kind="monthly",
            delta_target_min=0.45,
            delta_target_max=0.55,
            exit_profile="defensive",
        )
