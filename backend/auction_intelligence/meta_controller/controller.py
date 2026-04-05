from __future__ import annotations

from typing import Any

from auction_intelligence.schemas import AgentDecision, RegimeAssessment


class MetaController:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.hierarchy = list(config.get("hierarchy", ["positional", "swing", "scalp"]))
        self.allow_countertrend_scalp_in_balance = bool(
            config.get("allow_countertrend_scalp_in_balance", True)
        )

    def coordinate(
        self,
        decisions: list[AgentDecision],
        regime: RegimeAssessment,
    ) -> list[AgentDecision]:
        hierarchy_rank = {name: index for index, name in enumerate(self.hierarchy)}
        ordered = sorted(decisions, key=lambda item: hierarchy_rank.get(item.agent_name, 99))
        surviving: list[AgentDecision] = []
        dominant_direction = None

        for decision in ordered:
            if decision.action == "FLAT":
                surviving.append(decision)
                continue

            if dominant_direction is None and decision.action in {"LONG", "SHORT"}:
                dominant_direction = decision.action
                surviving.append(decision)
                continue

            if decision.action == dominant_direction:
                surviving.append(decision)
                continue

            if (
                decision.agent_name == "scalp"
                and regime.label in {"balance", "developing_balance", "rotational_day"}
                and self.allow_countertrend_scalp_in_balance
            ):
                surviving.append(decision)
                continue

            surviving.append(
                AgentDecision(
                    agent_name=decision.agent_name,
                    action="FLAT",
                    confidence=0.0,
                    entry_price=None,
                    stop_price=None,
                    target_price=None,
                    quantity=0,
                    sleeve_fraction=decision.sleeve_fraction,
                    rationale=decision.rationale + ["Meta-controller suppressed conflicting direction."],
                    metadata={**decision.metadata, "suppressed": True},
                )
            )

        return surviving
