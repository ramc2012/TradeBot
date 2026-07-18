"""Auction Intelligence (NSE) — market-profile / order-flow auction paper lane.

Decision basis is market-profile + order-flow (not option greeks), so greeks
completeness is ``na``. Covers provenance precedence, completed-bar completeness,
input freshness, gate-funnel attribution and book/journal reconciliation
(best-effort from the lane's file-backed paper book). Per-lane replay-parity is
the deeper follow-up (S1-only today).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, ClassVar

from audits.lanes._common import BaseLaneAuditor, LaneAuditProfile, LaneAuditState

PROFILE = LaneAuditProfile(
    key="auction_intelligence",
    label="Auction Intelligence Paper Cycle",
    strategy_key=None,  # file-backed paper book
    expected_decision_source="fyers",  # fast lane: Fyers WS tick/order-flow tape
    cadence_seconds=180.0,
    greeks_applicable=False,  # MP + order-flow, not option greeks
    notes="File-backed paper book; MP/order-flow decision basis.",
)


def _open_positions_from(obj: Any) -> int | None:
    if isinstance(obj, dict):
        for key in ("open_positions", "open_position_count"):
            v = obj.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return int(v)
        for key in ("open_positions_list", "positions", "open"):
            v = obj.get(key)
            if isinstance(v, list):
                return len(v)
    return None


def _summary() -> dict[str, Any]:
    from auction_intelligence.paper.book import AuctionPaperBook  # noqa: F401

    try:
        from auction_intelligence.paper import service as paper_service

        book = getattr(paper_service, "auction_paper_book", None)
        if book is not None and hasattr(book, "summary"):
            return book.summary() or {}
    except Exception:  # noqa: BLE001
        pass
    return {}


@dataclass
class AuctionIntelligenceAuditor(BaseLaneAuditor):
    profile: ClassVar[LaneAuditProfile] = PROFILE

    async def collect_state(self, ws, we, audit_date, lookback_days) -> LaneAuditState:
        st = LaneAuditState.empty()
        st.notes["provenance"] = (
            "decision-source tag not yet persisted on the paper book; wire it to enable "
            "provenance precedence"
        )
        st.notes["completed_bar"] = "decision bar closed-vs-forming flag not persisted yet"
        st.notes["gate"] = (
            "auction gate/rejection reasons are transient (per-cycle blocked_reasons); "
            "persist them to a queryable sink to enable gate attribution"
        )
        st.notes["recon"] = "paper-book probe unavailable"

        try:
            summary = await asyncio.to_thread(_summary)
        except Exception as exc:  # noqa: BLE001
            st.notes["recon"] = f"paper book unavailable: {type(exc).__name__}"
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
