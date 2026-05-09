"""API surface for the directional long-options module."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query

from directional_options.service import directional_options_service


router = APIRouter(prefix="/api/directional-options", tags=["directional-options"])
_service = directional_options_service


@router.get("/summary")
async def summary() -> dict[str, object]:
    return await asyncio.to_thread(_service.summary)


@router.get("/workspace")
async def workspace(
    underlying: str = Query("NIFTY"),
    timeframe: str = Query("5minute"),
    lookback_sessions: int = Query(16, ge=4, le=90),
) -> dict[str, object]:
    return await asyncio.to_thread(_service.workspace, underlying, timeframe, lookback_sessions)


@router.get("/live-snapshot")
async def live_snapshot(
    underlying: str = Query("NIFTY"),
    timeframe: str = Query("5minute"),
    lookback_sessions: int = Query(16, ge=4, le=90),
) -> dict[str, object]:
    return await _service.live_snapshot(underlying, timeframe, lookback_sessions)


@router.post("/paper-proposal")
async def paper_proposal(
    underlying: str = Query("NIFTY"),
    timeframe: str = Query("5minute"),
    lookback_sessions: int = Query(16, ge=4, le=90),
) -> dict[str, object]:
    return await _service.record_paper_snapshot(underlying, timeframe, lookback_sessions)


@router.get("/paper-journal")
async def paper_journal(
    symbol: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, object]:
    return await _service.paper_journal(symbol=symbol, limit=limit)


@router.get("/paper-positions")
async def paper_positions(
    symbol: str | None = Query(None),
    status: str = Query("all"),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, object]:
    return await _service.paper_positions(symbol=symbol, status=status, limit=limit)


@router.get("/backtest")
async def backtest(
    underlying: str = Query("NIFTY"),
    timeframe: str = Query("5minute"),
    lookback_sessions: int = Query(16, ge=4, le=90),
) -> dict[str, object]:
    payload = await asyncio.to_thread(_service.workspace, underlying, timeframe, lookback_sessions)
    return payload["backtest"]
