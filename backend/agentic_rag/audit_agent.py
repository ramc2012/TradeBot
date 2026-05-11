"""Cross-agent audit event sink.

Every meaningful transition in the trading app should land here:
  - Kill-switch trips and releases (per desk)
  - Bucket transitions on watchlist rows (favourable→drifting, →ready, etc.)
  - Manual operator interventions (config changes, kill-switch toggles)
  - Broker session state changes
  - Risk-block events at entry time

The user asked for traceability data they can audit and learn from; this
table is the canonical source for that. Writes are async, fire-and-forget,
and never raise out of the agent thread — audit failure must never block a
trading agent's main loop.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Optional

from loguru import logger
from sqlalchemy import text

from db.database import AsyncSessionLocal

_VALID_SEVERITY = {"info", "success", "warning", "error", "trade"}


async def record_audit_event(
    *,
    market: str,
    event_type: str,
    strategy_key: Optional[str] = None,
    actor: str = "system",
    symbol: Optional[str] = None,
    underlying: Optional[str] = None,
    severity: str = "info",
    message: Optional[str] = None,
    previous_state: Optional[str] = None,
    new_state: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    """Append a single audit event. Swallow exceptions — never block callers."""
    if severity not in _VALID_SEVERITY:
        severity = "info"
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO agent_audit_events
                        (market, strategy_key, event_type, actor, symbol,
                         underlying, severity, message, previous_state,
                         new_state, payload)
                    VALUES
                        (:market, :strategy_key, :event_type, :actor, :symbol,
                         :underlying, :severity, :message, :previous_state,
                         :new_state, CAST(:payload AS JSONB))
                    """
                ),
                {
                    "market": market,
                    "strategy_key": strategy_key,
                    "event_type": event_type,
                    "actor": actor,
                    "symbol": symbol,
                    "underlying": underlying,
                    "severity": severity,
                    "message": message,
                    "previous_state": previous_state,
                    "new_state": new_state,
                    "payload": json.dumps(payload or {}, default=str),
                },
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.opt(exception=True).warning(
            f"[AuditAgent] failed to record event {event_type} for {market}/{strategy_key}: {exc}"
        )


def record_audit_event_sync(**kwargs: Any) -> None:
    """Fire-and-forget wrapper. Schedules the coroutine on the current loop or
    discards it cleanly if no loop is running (e.g. unit tests).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(record_audit_event(**kwargs))


async def fetch_recent_events(
    *,
    market: Optional[str] = None,
    strategy_key: Optional[str] = None,
    event_type: Optional[str] = None,
    symbol: Optional[str] = None,
    since: Optional[datetime] = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Read back recent audit events for the audit endpoint."""
    where: list[str] = []
    params: dict[str, Any] = {"limit": min(max(int(limit), 1), 2000)}
    if market:
        where.append("market = :market")
        params["market"] = market
    if strategy_key:
        where.append("strategy_key = :strategy_key")
        params["strategy_key"] = strategy_key
    if event_type:
        where.append("event_type = :event_type")
        params["event_type"] = event_type
    if symbol:
        where.append("symbol = :symbol")
        params["symbol"] = symbol
    if since:
        where.append("created_at >= :since")
        params["since"] = since
    where_clause = (" WHERE " + " AND ".join(where)) if where else ""
    sql = (
        "SELECT id, created_at, market, strategy_key, event_type, actor, "
        "symbol, underlying, severity, message, previous_state, new_state, "
        "payload FROM agent_audit_events" + where_clause +
        " ORDER BY created_at DESC LIMIT :limit"
    )
    rows: list[dict[str, Any]] = []
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(text(sql), params)
            for record in result.mappings():
                rows.append(dict(record))
    except Exception as exc:  # noqa: BLE001
        logger.opt(exception=True).warning(f"[AuditAgent] read failure: {exc}")
    return rows
