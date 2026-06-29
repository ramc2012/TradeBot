"""Position sizing, portfolio limits and the kill switch (spec §6, §7, §9)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class SizeDecision:
    accepted: bool
    qty_lots: int
    qty_units: int
    notional_rupees: float
    ticket_rupees: float
    reason: str = ""


def size_position(
    *,
    premium: float,
    lot_size: int,
    daily_turnover_rupees: float,
    equity: float,
    sizing_cfg: dict[str, Any],
    starter: bool = False,
) -> SizeDecision:
    """Liquidity-scaled sizing (spec §6).

    Per-trade rupee size = min of:
      - turnover_fraction × the contract's recent daily turnover,
      - equity_fraction × current portfolio equity,
      - per_name_cap.
    Skip if the resulting ticket < min economic ticket. Two-stage: a starter
    deploys only `starter_fraction` of the full ticket.
    """
    lot_size = max(int(lot_size or 1), 1)
    if premium <= 0:
        return SizeDecision(False, 0, 0, 0.0, 0.0, "non-positive premium")

    by_turnover = float(sizing_cfg["turnover_fraction"]) * max(float(daily_turnover_rupees), 0.0)
    by_equity = float(sizing_cfg["equity_fraction"]) * max(float(equity), 0.0)
    by_cap = float(sizing_cfg["per_name_cap_rupees"])
    ticket = min(by_turnover, by_equity, by_cap)
    if starter:
        ticket *= float(sizing_cfg.get("starter_fraction", 0.3334))

    min_ticket = float(sizing_cfg["min_ticket_rupees"])
    if ticket < min_ticket:
        return SizeDecision(
            False, 0, 0, 0.0, ticket,
            f"ticket ₹{ticket:,.0f} < min ₹{min_ticket:,.0f} "
            f"(turnover-cap ₹{by_turnover:,.0f}, equity-cap ₹{by_equity:,.0f})",
        )

    cost_per_lot = premium * lot_size
    qty_lots = int(math.floor(ticket / cost_per_lot)) if cost_per_lot > 0 else 0
    if qty_lots < 1:
        return SizeDecision(
            False, 0, 0, 0.0, ticket,
            f"ticket ₹{ticket:,.0f} < one lot cost ₹{cost_per_lot:,.0f}",
        )
    qty_units = qty_lots * lot_size
    notional = qty_units * premium
    return SizeDecision(True, qty_lots, qty_units, notional, ticket, "")


def kill_switch_state(
    closed_returns_pct: list[float],
    *,
    risk_cfg: dict[str, Any],
    current_drawdown_pct: float,
) -> tuple[bool, Optional[str]]:
    """Return (paused, reason). Pause NEW entries if the rolling win-rate
    drops below the floor or drawdown exceeds the cap (spec §9)."""
    window = int(risk_cfg.get("kill_switch_winrate_window", 20))
    min_wr = float(risk_cfg.get("kill_switch_min_winrate", 0.60))
    max_dd = float(risk_cfg.get("kill_switch_max_drawdown_pct", 0.25))

    if current_drawdown_pct >= max_dd:
        return True, f"drawdown {current_drawdown_pct:.1%} ≥ cap {max_dd:.0%}"
    if len(closed_returns_pct) >= window:
        recent = closed_returns_pct[-window:]
        wins = sum(1 for r in recent if r > 0)
        wr = wins / len(recent)
        if wr < min_wr:
            return True, f"rolling win-rate {wr:.0%} < floor {min_wr:.0%}"
    return False, None
