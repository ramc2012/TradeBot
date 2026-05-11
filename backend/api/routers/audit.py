"""Audit-event read endpoint.

The AuditAgent (`agentic_rag.audit_agent`) appends every meaningful agent
transition to `agent_audit_events`. This router exposes a read-only feed for
the operator UI so the user can trace which agent did what when, and tune
strategies from real evidence.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Query

from agentic_rag.audit_agent import fetch_recent_events


router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("/events")
async def list_audit_events(
    market: Optional[str] = Query(default=None),
    strategy_key: Optional[str] = Query(default=None),
    event_type: Optional[str] = Query(default=None),
    symbol: Optional[str] = Query(default=None),
    since: Optional[datetime] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=2000),
) -> dict[str, Any]:
    rows = await fetch_recent_events(
        market=market,
        strategy_key=strategy_key,
        event_type=event_type,
        symbol=symbol,
        since=since,
        limit=limit,
    )
    return {
        "count": len(rows),
        "filters": {
            "market": market,
            "strategy_key": strategy_key,
            "event_type": event_type,
            "symbol": symbol,
            "since": since.isoformat() if since else None,
            "limit": limit,
        },
        "events": rows,
    }
