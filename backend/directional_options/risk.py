"""Risk approval and position sizing for long-premium index-option trades.

The trade-vs-skip and size-multiplier decisions are owned by the RL policy
(`directional_options.policy.DirectionalPolicy`). This module is now thin:

  * compute the base risk budget from equity × risk_pct
  * apply the policy's chosen size multiplier
  * compute lot count from premium / stop budget
  * enforce capital-safety caps that are NOT trade-quality gates:
      - daily / weekly loss cap (R-multiple of base risk)
      - sane lot count (must be ≥ 1)

What was removed in the RL refactor:

  * REGIME_BLOCKED / DELTA_BUCKET_BLOCKED hard gates — the policy learns
    per-regime and per-delta-bucket value from outcomes.
  * `min_expected_edge_pct` hurdle and optimizer rejection_reasons — the
    policy already sees these scores as features.
  * commodity bypass — commodities are out of universe.
  * the "learning sleeve" bypass — every signal is a learning bet now.
  * premium_cap_pct — user directive: no size cap.

Capital safety remains: the daily/weekly R-multiple loss caps will block
new opens after the desk's loss budget is exhausted for the period. That
is not a strategy gate — it's a stop-trading-after-bleed-out rule.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from directional_options.schemas import ContractCandidate, DirectionalSignal, RiskDecision


class DirectionalOptionsRiskEngine:
    """Compute lot sizing given a policy-chosen multiplier, enforce loss caps."""

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def approve(
        self,
        *,
        candidate: ContractCandidate,
        signal: DirectionalSignal,
        equity: float,
        size_multiplier: float = 1.0,
        daily_realized: float = 0.0,
        weekly_realized: float = 0.0,
    ) -> RiskDecision:
        risk_pct = float(self.config["risk_pct"])
        base_risk_budget = equity * risk_pct
        risk_budget = base_risk_budget * float(size_multiplier)

        # Premium cap is intentionally unbounded per the "no size limit"
        # directive — only the per-lot risk budget gates quantity now.
        premium_cap_cfg = self.config.get("premium_cap_pct")
        premium_cap: Optional[float]
        if premium_cap_cfg is None:
            premium_cap = None
        else:
            premium_cap = equity * float(premium_cap_cfg) * float(size_multiplier)

        planned_stop_pct = float(self.config["planned_stop_pct"])
        fee_per_unit = 0.45

        stop_loss_per_unit = candidate.option_price * planned_stop_pct
        lot_premium = candidate.option_price * candidate.lot_size
        lot_risk = max(1.0, (stop_loss_per_unit + fee_per_unit) * candidate.lot_size)

        max_lots_by_risk = math.floor(risk_budget / lot_risk)
        if premium_cap is not None:
            max_lots_by_premium = math.floor(premium_cap / max(lot_premium, 1.0))
            qty_lots = max(0, min(max_lots_by_risk, max_lots_by_premium))
        else:
            qty_lots = max(0, max_lots_by_risk)

        reasons: list[str] = []

        # Capital-safety caps — NOT a strategy gate, a stop-trading rule.
        # Comparing against base_risk_budget (not the scaled one) means
        # the cap is in "R-multiples of typical trade risk" — consistent
        # across trades regardless of the policy's chosen multiplier.
        daily_cap_R = float(self.config.get("daily_loss_cap_r", 4.0))
        weekly_cap_R = float(self.config.get("weekly_loss_cap_r", 10.0))
        if daily_realized <= -(base_risk_budget * daily_cap_R):
            reasons.append(
                f"Daily loss cap breached (realized {daily_realized:.0f} ≤ "
                f"-{base_risk_budget * daily_cap_R:.0f}); trading paused for the session."
            )
        if weekly_realized <= -(base_risk_budget * weekly_cap_R):
            reasons.append(
                f"Weekly loss cap breached (realized {weekly_realized:.0f} ≤ "
                f"-{base_risk_budget * weekly_cap_R:.0f}); trading paused for the week."
            )
        if qty_lots < 1:
            reasons.append(
                f"Sizing produced 0 lots at multiplier {size_multiplier:.2f}× "
                f"(risk_budget ₹{risk_budget:.0f} / lot_risk ₹{lot_risk:.0f})."
            )

        return RiskDecision(
            approved=not reasons,
            quantity_lots=max(qty_lots, 0),
            quantity_units=max(qty_lots, 0) * candidate.lot_size,
            premium_at_risk=round(max(qty_lots, 0) * lot_premium, 2),
            max_loss=round(max(qty_lots, 0) * lot_risk, 2),
            risk_budget=round(risk_budget, 2),
            premium_cap=round(premium_cap, 2) if premium_cap is not None else None,
            reasons=reasons,
        )
