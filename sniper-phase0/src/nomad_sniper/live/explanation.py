"""Human-readable explanation builder for final alpha signals."""

from __future__ import annotations

from nomad_sniper.execution.models import ExecutionInstruction
from nomad_sniper.meta.labeler import MetaDecision
from nomad_sniper.options.selector import OptionExpression
from nomad_sniper.payoff.distribution import PayoffEstimate
from nomad_sniper.regime.classifier import RegimeState
from nomad_sniper.risk.governor import RiskDecision
from nomad_sniper.setups.experts import SetupExpertScores


def build_explanation(
    *,
    direction: str,
    regime: RegimeState,
    meta: MetaDecision,
    payoff: PayoffEstimate,
    execution: ExecutionInstruction,
    option: OptionExpression,
    risk: RiskDecision,
    experts: SetupExpertScores,
) -> str:
    parts = [
        f"Signal direction: {direction}.",
        f"Regime: {regime.regime} ({regime.auction_regime}); score {regime.regime_score:.2f}.",
        f"Dominant setup: {experts.selected_expert} ({experts.confidence:.2f} confidence).",
        f"Meta action: {meta.action}; score {meta.score:.2f}.",
        f"Expected R {payoff.expected_r:.2f}, MFE {payoff.expected_mfe_r:.2f}R, MAE {payoff.expected_mae_r:.2f}R.",
        f"Options: {option.action} {option.side} {option.moneyness} {option.expiry}.",
        f"Execution: {execution.action}; adverse selection {execution.adverse_selection_score:.2f}.",
        f"Risk: {'allowed' if risk.allowed else 'blocked'} at {risk.size_multiplier:.2f}x size.",
    ]
    reasons = regime.reasons + meta.reasons + execution.reasons + option.reasons + risk.reasons
    if reasons:
        parts.append("Reasons: " + "; ".join(dict.fromkeys(reasons)) + ".")
    return " ".join(parts)
