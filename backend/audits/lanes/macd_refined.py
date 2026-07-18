"""MACD Refined — refined MACD paper lane (never trades before 09:45).

File-backed paper book (not the shared agent_* tables). This auditor covers the
data-quality / signal-correctness dimensions checkable without a replay engine.
MACD Refined DOES consume IV/greeks, so greeks completeness is applicable. Live
collection reads the lane's service summary best-effort (open positions +
freshness); the provenance / completed-bar / greeks live feeds are honestly
reported ``na`` with a wiring note until the recorder tags them onto the book.
Per-lane replay-parity is the deeper follow-up (S1-only today).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, ClassVar

from audits.lanes._common import BaseLaneAuditor, LaneAuditProfile, LaneAuditState

PROFILE = LaneAuditProfile(
    key="macd_refined",
    label="MACD Refined Paper Cycle",
    strategy_key=None,  # file-backed book, not agent_* tables
    expected_decision_source="upstox",  # slow lane: decision REST rides the SLOW (Upstox) profile
    cadence_seconds=300.0,
    greeks_applicable=True,
    notes="File-backed paper book; slow-profile decision feed; greeks-consuming.",
)


def _summary() -> dict[str, Any]:
    from macd_refined.service import macd_refined_service

    return macd_refined_service.summary() or {}


def _extract_open_positions(summary: dict[str, Any]) -> int | None:
    paper = summary.get("paper_summary") or summary.get("summary") or {}
    for src in (paper, summary):
        if isinstance(src, dict):
            v = src.get("open_positions")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return int(v)
            ops = src.get("open_positions_list") or src.get("positions")
            if isinstance(ops, list):
                return len(ops)
    return None


def _extract_updated_age(summary: dict[str, Any]) -> float | None:
    for key in ("last_updated", "updated_at", "as_of", "last_run_at"):
        raw = summary.get(key) or (summary.get("paper_summary") or {}).get(key)
        if isinstance(raw, str):
            try:
                ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            now = datetime.now(timezone.utc) if ts.tzinfo else datetime.utcnow()
            return (now - ts).total_seconds()
    return None


@dataclass
class MacdRefinedAuditor(BaseLaneAuditor):
    profile: ClassVar[LaneAuditProfile] = PROFILE

    async def collect_state(self, ws, we, audit_date, lookback_days) -> LaneAuditState:
        st = LaneAuditState.empty()
        st.notes["provenance"] = (
            "decision-source tag is not yet persisted on the file-backed book; "
            "record it to enable provenance precedence"
        )
        st.notes["completed_bar"] = (
            "decision bar closed-vs-forming flag is not persisted on the book yet"
        )
        st.notes["greeks"] = (
            "per-decision IV/greeks completeness is not yet persisted on the book"
        )
        st.notes["gate"] = (
            "rejection/gate attribution is not yet emitted to a queryable sink for this lane"
        )
        st.notes["recon"] = "book/journal probe unavailable"

        try:
            summary = await asyncio.to_thread(_summary)
        except Exception as exc:  # noqa: BLE001 — best-effort, never fatal
            st.notes["recon"] = f"service summary unavailable: {type(exc).__name__}"
            return st

        op = _extract_open_positions(summary)
        if op is not None:
            st.open_positions = op
            st.notes.pop("recon", None)
        age = _extract_updated_age(summary)
        if age is not None:
            st.input_age_seconds = age
            st.notes.pop("freshness", None)
        return st
