from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import desc, select

from db.database import AsyncSessionLocal
from db.models import ShadowObservation


class ShadowPersistenceService:
    async def record_records(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        if not records:
            return {"persisted": False, "error": "no_records"}

        try:
            async with AsyncSessionLocal() as session:
                rows: list[ShadowObservation] = []
                for record in records:
                    session_date = record["session_date"]
                    if isinstance(session_date, str):
                        session_date = date.fromisoformat(session_date)
                    signal_id = str(record.get("signal_id") or uuid.uuid4())
                    existing = await session.execute(
                        select(ShadowObservation).where(ShadowObservation.signal_id == signal_id).limit(1)
                    )
                    row = existing.scalar_one_or_none()
                    if row is None:
                        row = ShadowObservation(signal_id=signal_id, session_date=session_date, symbol=str(record["symbol"]), agent_name=str(record.get("agent_name") or "unknown"), action=str(record.get("action") or "FLAT"))
                        session.add(row)
                    row.session_date = session_date
                    row.symbol = str(record["symbol"])
                    row.source = record.get("source")
                    row.snapshot_mode = record.get("snapshot_mode")
                    row.agent_name = str(record.get("agent_name") or "unknown")
                    row.action = str(record.get("action") or "FLAT")
                    row.regime_label = record.get("regime_label")
                    row.setup_name = record.get("setup_name")
                    row.confidence = float(record.get("confidence") or 0.0)
                    row.quantity = int(record.get("quantity") or 0)
                    row.entry_price = record.get("entry_price")
                    row.stop_price = record.get("stop_price")
                    row.target_price = record.get("target_price")
                    row.tick_size = float(record.get("tick_size") or 0.5)
                    row.risk_allowed = bool(record.get("risk_allowed", False))
                    row.kill_switch_active = bool(record.get("kill_switch_active", False))
                    row.simulated_fill_price = record.get("simulated_fill_price")
                    row.observed_touch_price = record.get("observed_touch_price")
                    row.observed_fill_price = record.get("observed_fill_price")
                    row.fill_drift_ticks = record.get("fill_drift_ticks")
                    row.stale_signal = bool(record.get("stale_signal", False))
                    row.reconciliation_status = str(record.get("reconciliation_status") or "matched")
                    row.mismatch_duration_seconds = float(record.get("mismatch_duration_seconds") or 0.0)
                    row.kill_switch_tested = bool(record.get("kill_switch_tested", False))
                    row.kill_switch_passed = bool(record.get("kill_switch_passed", False))
                    row.dashboard_checked = bool(record.get("dashboard_checked", False))
                    row.alerts_checked = bool(record.get("alerts_checked", False))
                    row.manual_override_tested = bool(record.get("manual_override_tested", False))
                    row.details = record.get("metadata") or {}
                    rows.append(row)
                await session.flush()
                await session.commit()
                return {
                    "persisted": True,
                    "record_count": len(rows),
                    "record_ids": [str(row.id) for row in rows],
                }
        except Exception as exc:
            return {"persisted": False, "error": str(exc)}

    async def list_records(
        self,
        *,
        symbol: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        try:
            async with AsyncSessionLocal() as session:
                stmt = select(ShadowObservation).order_by(
                    desc(ShadowObservation.session_date),
                    desc(ShadowObservation.recorded_at),
                )
                if symbol:
                    stmt = stmt.where(ShadowObservation.symbol == symbol)
                stmt = stmt.limit(limit)
                result = await session.execute(stmt)
                rows = result.scalars().all()
                return [
                    {
                        "record_id": str(row.id),
                        "signal_id": row.signal_id,
                        "recorded_at": row.recorded_at.isoformat() if row.recorded_at else None,
                        "session_date": row.session_date.isoformat() if row.session_date else None,
                        "symbol": row.symbol,
                        "source": row.source,
                        "snapshot_mode": row.snapshot_mode,
                        "agent_name": row.agent_name,
                        "action": row.action,
                        "regime_label": row.regime_label,
                        "setup_name": row.setup_name,
                        "confidence": row.confidence,
                        "quantity": row.quantity,
                        "entry_price": row.entry_price,
                        "stop_price": row.stop_price,
                        "target_price": row.target_price,
                        "tick_size": row.tick_size,
                        "risk_allowed": row.risk_allowed,
                        "kill_switch_active": row.kill_switch_active,
                        "simulated_fill_price": row.simulated_fill_price,
                        "observed_touch_price": row.observed_touch_price,
                        "observed_fill_price": row.observed_fill_price,
                        "fill_drift_ticks": row.fill_drift_ticks,
                        "stale_signal": row.stale_signal,
                        "reconciliation_status": row.reconciliation_status,
                        "mismatch_duration_seconds": row.mismatch_duration_seconds,
                        "kill_switch_tested": row.kill_switch_tested,
                        "kill_switch_passed": row.kill_switch_passed,
                        "dashboard_checked": row.dashboard_checked,
                        "alerts_checked": row.alerts_checked,
                        "manual_override_tested": row.manual_override_tested,
                        "metadata": row.details or {},
                    }
                    for row in rows
                ]
        except Exception:
            return []
