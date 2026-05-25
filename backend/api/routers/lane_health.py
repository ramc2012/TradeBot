"""Lane-health read endpoints — surfaces rows from `lane_audit`.

Pairs with the audit framework in `backend/audits/`. Audits are written
by the CLI (`python -m audits.lane_audit --lane s1`) or a scheduled
worker; this router only reads.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from audits.lanes import REGISTRY
from db.database import AsyncSessionLocal


router = APIRouter(prefix="/api/lane-health", tags=["lane-health"])


@router.get("/lanes")
async def list_lanes() -> dict[str, Any]:
    return {"lanes": sorted(REGISTRY.keys())}


@router.get("/{lane}/latest")
async def latest(lane: str) -> dict[str, Any]:
    if lane not in REGISTRY:
        raise HTTPException(status_code=404, detail=f"unknown lane: {lane}")
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT
                        lane, audit_date, window_start, window_end,
                        data_integrity_pass, replay_parity_pass,
                        gate_attribution_pass, backtest_parity_pass,
                        trade_recon_pass, edge_persistence_pass,
                        signals_emitted, signals_blocked_total, gate_block_breakdown,
                        replay_signals, live_signals, replay_match_count, replay_mismatches,
                        trades_booked, trade_recon_pass_count,
                        expectancy_60d, expectancy_baseline, drift_pct,
                        overall_status, report_path, metadata, created_at
                    FROM lane_audit
                    WHERE lane = :lane
                    ORDER BY audit_date DESC
                    LIMIT 1
                    """
                ),
                {"lane": lane},
            )
        ).mappings().first()
    if row is None:
        return {"lane": lane, "status": "no-audit-yet"}
    return _serialize(row)


@router.get("/{lane}/history")
async def history(lane: str, days: int = 30) -> dict[str, Any]:
    if lane not in REGISTRY:
        raise HTTPException(status_code=404, detail=f"unknown lane: {lane}")
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT audit_date, overall_status,
                           data_integrity_pass, replay_parity_pass,
                           gate_attribution_pass, backtest_parity_pass,
                           trade_recon_pass, edge_persistence_pass,
                           signals_emitted, expectancy_60d, drift_pct
                    FROM lane_audit
                    WHERE lane = :lane
                      AND audit_date >= (CURRENT_DATE - (:days || ' days')::interval)::date
                    ORDER BY audit_date DESC
                    """
                ),
                {"lane": lane, "days": days},
            )
        ).mappings().all()
    return {"lane": lane, "rows": [dict(r) for r in rows]}


def _serialize(row) -> dict[str, Any]:
    d = dict(row)
    for k, v in list(d.items()):
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    return d
