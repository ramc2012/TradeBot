"""API surface for the MACD Refined lane."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from macd_refined.service import macd_refined_service

router = APIRouter(prefix="/api/macd-refined", tags=["macd-refined"])
_service = macd_refined_service


class MacdRefinedResetRequest(BaseModel):
    confirm: str = Field(..., description="Must equal 'RESET' to proceed (destructive).")
    actor: str | None = None


def _parse_underlyings(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    items = [u.strip().upper() for u in raw.split(",") if u.strip()]
    return items or None


@router.get("/summary")
async def summary() -> dict[str, object]:
    return await asyncio.to_thread(_service.summary)


@router.get("/backtest")
async def backtest(
    source: str = Query("research", pattern="^(research|engine)$"),
    underlyings: str | None = Query(None, description="Comma-separated symbols; omit for full universe."),
    expiry_count: int = Query(8, ge=1, le=13),
) -> dict[str, object]:
    return await asyncio.to_thread(
        _service.backtest, source=source, underlyings=_parse_underlyings(underlyings), expiry_count=expiry_count
    )


@router.get("/backtest-compare")
async def backtest_compare(
    underlyings: str | None = Query(None),
    expiry_count: int = Query(8, ge=1, le=13),
) -> dict[str, object]:
    return await asyncio.to_thread(
        _service.backtest_compare, underlyings=_parse_underlyings(underlyings), expiry_count=expiry_count
    )


@router.get("/positioning")
async def positioning() -> dict[str, object]:
    """Current + next monthly expiry resolution and volume-tracking coverage."""
    return await asyncio.to_thread(_service.positioning)


@router.post("/run-live-cycle")
async def run_live_cycle(allow_entries: bool = Query(True)) -> dict[str, object]:
    """Fetch current+next expiry chains, persist volume/turnover, sync the
    paper book. Degrades cleanly (broker_ready=false) without a live broker."""
    return await _service.run_live_cycle(allow_entries=allow_entries)


@router.post("/data-audit")
async def data_audit(
    max_names: int | None = Query(None, ge=1, le=500),
    underlyings: str | None = Query(
        None,
        description="Optional comma-separated priority symbols, such as current open-position names.",
    ),
    wait: bool = Query(False, description="True = run inline and return the report (small sweeps); False = run in background, poll /data-audit-report."),
) -> dict[str, object]:
    """Sweep the full F&O universe: resolve current+next expiry, fetch chains,
    persist volume/turnover (backfill), and report which names have sufficient
    ATM CE+PE 30-min history for the premium-MACD. Broker-intensive (~1300 calls
    for the full universe) — default runs in the background and writes the report
    to runtime/macd_refined/data_audit_latest.json (read via /data-audit-report)."""
    selected = _parse_underlyings(underlyings)
    if wait:
        return await _service.data_audit(max_names=max_names, underlyings=selected)
    asyncio.create_task(_service.data_audit(max_names=max_names, underlyings=selected))
    return {"started": True, "background": True, "poll": "/api/macd-refined/data-audit-report"}


@router.get("/data-audit-report")
async def data_audit_report() -> dict[str, object]:
    """Latest persisted data-sufficiency report (or status if none yet)."""
    return await asyncio.to_thread(_service.data_audit_report)


@router.get("/paper-positions")
async def paper_positions(
    symbol: str | None = Query(None),
    status: str = Query("all"),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, object]:
    return await asyncio.to_thread(_service.paper_positions, symbol, status, limit)


@router.get("/signals")
async def signals(
    underlying: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, object]:
    """Recorded premium-MACD signals (every generated cross + gate verdicts)."""
    return await asyncio.to_thread(_service.signals, limit=limit, underlying=underlying)


@router.get("/paper-journal")
async def paper_journal(
    symbol: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, object]:
    return await asyncio.to_thread(_service.paper_journal, symbol, limit)


@router.get("/paper-summary")
async def paper_summary() -> dict[str, object]:
    return await asyncio.to_thread(_service.paper_summary)


@router.post("/reset-paper")
async def reset_paper(body: MacdRefinedResetRequest) -> dict[str, object]:
    if (body.confirm or "").strip().upper() != "RESET":
        raise HTTPException(
            status_code=400,
            detail="Paper reset is destructive. POST `{\"confirm\": \"RESET\"}` to confirm.",
        )
    return await asyncio.to_thread(_service.reset_paper, actor=body.actor)
