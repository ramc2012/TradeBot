"""API surface for the US MACD Refined lane (Alpaca-backed, paper)."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from macd_refined.service import us_macd_refined_service

router = APIRouter(prefix="/api/us/macd-refined", tags=["us-macd-refined"])
_service = us_macd_refined_service


class ResetRequest(BaseModel):
    confirm: str = Field(..., description="Must equal 'RESET' to proceed (destructive).")
    actor: str | None = None


async def _alpaca_source_health() -> dict[str, object]:
    """Alpaca connectivity probe shared by /summary and /data-source-health.

    On this deployment ``brokers.alpaca`` does not exist (analysis/alpaca_data.py
    also points at a non-existent local parquet path), so this reports
    configured=False — the lane is honestly PARKED (audit 2026-07-18: the
    /summary previously presented a full ready lane payload while the data
    source reported configured=false)."""
    try:
        from brokers.alpaca import alpaca_adapter
        if not alpaca_adapter.has_credentials:
            return {"provider": "alpaca", "configured": False,
                    "note": "Set ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY in .env."}
        h = await alpaca_adapter.health()
        return {"provider": "alpaca", "configured": True, **h}
    except Exception as exc:  # noqa: BLE001
        return {"provider": "alpaca", "configured": False, "error": str(exc)[:160]}


@router.get("/summary")
async def summary() -> dict[str, object]:
    payload = dict(await asyncio.to_thread(_service.summary))
    health = await _alpaca_source_health()
    # Additive keys only — the UI (UsMacdDesk.tsx) reads params/automation/
    # timeframe/live_universe and must keep rendering; status/status_reason/
    # data_source are new, so unconfigured surfaces as "unavailable" instead
    # of a full ready lane (audit 2026-07-18).
    if not health.get("configured"):
        payload["status"] = "unavailable"
        payload["status_reason"] = str(
            health.get("error") or health.get("note") or "Alpaca data source not configured."
        )
    else:
        payload["status"] = "ready"
        payload["status_reason"] = None
    payload["data_source"] = health
    return payload


@router.get("/data-source-health")
async def data_source_health() -> dict[str, object]:
    """Alpaca connectivity for the US lane (keys present? SPY quote fetch?)."""
    health = await _alpaca_source_health()
    # Additive status key mirroring /summary (UI reads configured/ok directly).
    health["status"] = "ready" if health.get("configured") else "unavailable"
    return health


@router.get("/positioning")
async def positioning() -> dict[str, object]:
    return await asyncio.to_thread(_service.positioning)


@router.get("/signals")
async def signals(underlying: str | None = Query(None), limit: int = Query(100, ge=1, le=1000)) -> dict[str, object]:
    return await asyncio.to_thread(_service.signals, limit=limit, underlying=underlying)


@router.post("/run-live-cycle")
async def run_live_cycle(allow_entries: bool = Query(True)) -> dict[str, object]:
    return await _service.run_live_cycle(allow_entries=allow_entries)


@router.post("/data-audit")
async def data_audit(
    max_names: int | None = Query(None, ge=1, le=200),
    wait: bool = Query(False),
) -> dict[str, object]:
    if wait:
        return await _service.data_audit(max_names=max_names)
    asyncio.create_task(_service.data_audit(max_names=max_names))
    return {"started": True, "background": True, "poll": "/api/us/macd-refined/data-audit-report"}


@router.get("/data-audit-report")
async def data_audit_report() -> dict[str, object]:
    return await asyncio.to_thread(_service.data_audit_report)


@router.get("/paper-positions")
async def paper_positions(
    symbol: str | None = Query(None), status: str = Query("all"), limit: int = Query(50, ge=1, le=200),
) -> dict[str, object]:
    return await asyncio.to_thread(_service.paper_positions, symbol, status, limit)


@router.get("/paper-summary")
async def paper_summary() -> dict[str, object]:
    return await asyncio.to_thread(_service.paper_summary)


@router.post("/reset-paper")
async def reset_paper(body: ResetRequest) -> dict[str, object]:
    if (body.confirm or "").strip().upper() != "RESET":
        raise HTTPException(status_code=400, detail="POST {\"confirm\": \"RESET\"} to confirm.")
    return await asyncio.to_thread(_service.reset_paper, actor=body.actor)
