"""Commodity MP + Order Flow — own-loop MCX futures paper lane.

Decision basis is market-profile + order-flow on MCX futures (not option greeks),
so greeks completeness is ``na``. Covers provenance precedence, completed-bar
completeness, input freshness, gate-funnel attribution (best-effort from the
shared agent_audit_events sink under strategy_key='commodity_futures') and
book/journal reconciliation (open positions from the live agent status).
Per-lane replay-parity is the deeper follow-up (S1-only today).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

from sqlalchemy import text

from audits.lanes._common import (
    BaseLaneAuditor,
    LaneAuditProfile,
    LaneAuditState,
    _safe_execute,
    collect_gate_and_signals_by_strategy_key,
)

PROFILE = LaneAuditProfile(
    key="commodity_mp_orderflow",
    label="Commodity MP + Order Flow",
    strategy_key="commodity_futures",
    expected_decision_source="upstox",  # commodity decision REST rides the default/Upstox plane
    cadence_seconds=60.0,
    greeks_applicable=False,  # MCX futures, not option greeks
    notes="Own-loop MCX futures agent; MP/order-flow decision basis.",
)


def _agent_status() -> dict[str, Any]:
    from paper_engine.commodity_strategy_agent import commodity_strategy_agent

    try:
        return commodity_strategy_agent.get_status(refresh=False) or {}
    except TypeError:
        return commodity_strategy_agent.get_status() or {}


def _open_positions_from(status: dict[str, Any]) -> int | None:
    v = status.get("open_positions")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v)
    for item in status.get("strategies") or status.get("strategy_agents") or []:
        if isinstance(item, dict) and item.get("key") == PROFILE.strategy_key:
            summary = item.get("summary") if isinstance(item.get("summary"), dict) else item
            ov = summary.get("open_positions")
            if isinstance(ov, (int, float)) and not isinstance(ov, bool):
                return int(ov)
    return None


@dataclass
class CommodityMpOrderFlowAuditor(BaseLaneAuditor):
    profile: ClassVar[LaneAuditProfile] = PROFILE

    async def collect_state(self, ws, we, audit_date, lookback_days) -> LaneAuditState:
        st = LaneAuditState.empty()
        st.notes["provenance"] = (
            "decision-source tag not yet persisted on the commodity journal; wire it to "
            "enable provenance precedence"
        )
        st.notes["completed_bar"] = "decision bar closed-vs-forming flag not persisted yet"
        st.notes["recon"] = "commodity agent status probe unavailable"

        # gate funnel from the shared audit-event sink (best-effort)
        await collect_gate_and_signals_by_strategy_key(
            self.session, self.profile.strategy_key, ws, we, st
        )

        # open positions from the live agent status (authoritative for this lane)
        try:
            status = await asyncio.to_thread(_agent_status)
            op = _open_positions_from(status)
            if op is not None:
                st.open_positions = op
                st.notes.pop("recon", None)
        except Exception as exc:  # noqa: BLE001 — best-effort, never fatal
            st.notes["recon"] = f"agent status unavailable: {type(exc).__name__}"

        # closed trades for edge persistence (best-effort; no recon error on miss)
        closed = await _safe_execute(
            self.session,
            text(
                """
                SELECT realized_pnl, entry_price, qty, closed_at
                FROM agent_positions
                WHERE strategy_key = :sk AND status = 'closed' AND closed_at >= :start
                """
            ),
            {"sk": self.profile.strategy_key, "start": audit_date - timedelta(days=365)},
        )
        if closed is not None:
            for r in closed.mappings().all():
                ep, qty = r["entry_price"], r["qty"]
                if not ep or not qty:
                    continue
                denom = float(ep) * float(qty)
                if denom == 0:
                    continue
                st.closed_trades.append({
                    "ret_pct": float(r["realized_pnl"] or 0) / denom * 100.0,
                    "closed_at": r["closed_at"],
                })
        return st
