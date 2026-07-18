"""Institutional Convergence (NSE) — shadow convergence paper lane.

Decision basis is cross-signal convergence + order-flow (not option greeks), so
greeks completeness is ``na``. Covers provenance precedence, completed-bar
completeness, input freshness, gate-funnel attribution (the engine already emits
``blocked_reasons`` per candidate) and book/journal reconciliation, best-effort
from ``convergence_paper_book``. Per-lane replay-parity is the deeper follow-up
(S1-only today).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, ClassVar

from audits.lanes._common import BaseLaneAuditor, LaneAuditProfile, LaneAuditState

PROFILE = LaneAuditProfile(
    key="institutional_convergence",
    label="Institutional Convergence Shadow Cycle",
    strategy_key=None,  # file-backed shadow book
    expected_decision_source="fyers",  # fast lane: Fyers WS tick/CVD tape
    cadence_seconds=180.0,
    greeks_applicable=False,  # convergence + order-flow, not option greeks
    notes="File-backed shadow book; convergence/order-flow decision basis.",
)


def _book_summary() -> dict[str, Any]:
    from institutional_convergence.service import convergence_paper_book

    return convergence_paper_book.summary() or {}


def _open_positions_from(summary: dict[str, Any]) -> int | None:
    for key in ("open_positions", "open_position_count", "open"):
        v = summary.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v)
    for key in ("open_positions_list", "positions"):
        v = summary.get(key)
        if isinstance(v, list):
            return len(v)
    return None


@dataclass
class InstitutionalConvergenceAuditor(BaseLaneAuditor):
    profile: ClassVar[LaneAuditProfile] = PROFILE

    async def collect_state(self, ws, we, audit_date, lookback_days) -> LaneAuditState:
        st = LaneAuditState.empty()
        st.notes["provenance"] = (
            "decision-source tag not yet persisted on the shadow book; wire it to enable "
            "provenance precedence"
        )
        st.notes["completed_bar"] = "decision bar closed-vs-forming flag not persisted yet"
        st.notes["gate"] = (
            "engine computes per-candidate blocked_reasons but they are not yet persisted "
            "to a queryable window; persist them to enable gate attribution"
        )
        st.notes["recon"] = "shadow-book probe unavailable"

        try:
            summary = await asyncio.to_thread(_book_summary)
        except Exception as exc:  # noqa: BLE001
            st.notes["recon"] = f"shadow book unavailable: {type(exc).__name__}"
            return st

        op = _open_positions_from(summary)
        if op is not None:
            st.open_positions = op
            st.notes.pop("recon", None)
        raw = summary.get("last_updated") or summary.get("updated_at")
        if isinstance(raw, str):
            try:
                ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc) if ts.tzinfo else datetime.utcnow()
                st.input_age_seconds = (now - ts).total_seconds()
            except ValueError:
                pass
        return st
