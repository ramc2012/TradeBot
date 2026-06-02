"""Final alpha signal composition."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal, Mapping

from nomad_sniper.execution.models import ExecutionInstruction, choose_execution
from nomad_sniper.live.explanation import build_explanation
from nomad_sniper.meta.labeler import MetaDecision, decide_meta_action
from nomad_sniper.options.selector import OptionExpression, select_option_expression
from nomad_sniper.payoff.distribution import PayoffEstimate, estimate_payoff
from nomad_sniper.regime.classifier import RegimeState, classify_regime
from nomad_sniper.risk.governor import RiskDecision, RiskState, apply_risk_governor
from nomad_sniper.setups.experts import SetupExpertScores, score_setup_experts

SignalAction = Literal["long", "short", "no_trade", "wait"]


@dataclass(frozen=True)
class AlphaSignal:
    action: SignalAction
    direction: str
    trade_quality: float
    confidence: float
    expected_r: float
    expected_mfe_r: float
    expected_mae_r: float
    time_to_target: float
    invalidation_risk: float
    position_size_multiplier: float
    execution: ExecutionInstruction
    option: OptionExpression
    regime: RegimeState
    meta: MetaDecision
    payoff: PayoffEstimate
    risk: RiskDecision
    experts: SetupExpertScores
    explanation: str

    def to_dict(self) -> dict:
        return asdict(self)


def build_alpha_signal(
    prediction: Mapping[str, object],
    features: Mapping[str, object],
    *,
    risk_state: RiskState | None = None,
) -> AlphaSignal:
    direction = str(prediction.get("pred_direction", "none"))
    regime = classify_regime(features)
    meta = decide_meta_action(prediction, features, regime)
    payoff = estimate_payoff(prediction)
    experts = score_setup_experts(features)
    option = select_option_expression(direction, features, edge_score=max(meta.score, payoff.p_target_before_stop))
    execution = choose_execution(features, meta_action=meta.action, urgency=max(meta.score, payoff.p_target_before_stop))
    edge_score = max(0.0, min(1.0, (payoff.expected_r + 1.0) / 2.0))
    risk = apply_risk_governor(
        edge_score=edge_score,
        liquidity_score=option.liquidity_score,
        regime_score=regime.regime_score,
        meta_size_multiplier=meta.size_multiplier,
        state=risk_state,
    )

    if not risk.allowed or meta.action == "skip" or direction == "none" or option.action == "avoid":
        action: SignalAction = "no_trade"
    elif meta.action in {"wait", "pullback_only"} or execution.action in {"wait_for_pullback", "cancel_signal"}:
        action = "wait"
    elif direction == "up":
        action = "long"
    elif direction == "down":
        action = "short"
    else:
        action = "no_trade"

    quality = max(0.0, min(1.0, 0.35 * meta.score + 0.25 * regime.regime_score + 0.25 * payoff.p_target_before_stop + 0.15 * option.liquidity_score))
    confidence = max(float(prediction.get("p_up", 0.0) or 0.0), float(prediction.get("p_down", 0.0) or 0.0), float(prediction.get("p_none", 0.0) or 0.0))
    explanation = build_explanation(
        direction=direction,
        regime=regime,
        meta=meta,
        payoff=payoff,
        execution=execution,
        option=option,
        risk=risk,
        experts=experts,
    )
    return AlphaSignal(
        action=action,
        direction=direction,
        trade_quality=quality * 100.0,
        confidence=confidence,
        expected_r=payoff.expected_r,
        expected_mfe_r=payoff.expected_mfe_r,
        expected_mae_r=payoff.expected_mae_r,
        time_to_target=payoff.expected_time_to_target,
        invalidation_risk=payoff.tail_loss_probability,
        position_size_multiplier=risk.size_multiplier,
        execution=execution,
        option=option,
        regime=regime,
        meta=meta,
        payoff=payoff,
        risk=risk,
        experts=experts,
        explanation=explanation,
    )
