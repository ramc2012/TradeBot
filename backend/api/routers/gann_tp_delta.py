"""API surface for the Gann TP Delta harmonic module."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query

from gann_tp_delta.service import gann_tp_delta_service


router = APIRouter(prefix="/api/gann-tp-delta", tags=["gann-tp-delta"])


@router.get("/summary")
async def summary() -> dict:
    return await asyncio.to_thread(gann_tp_delta_service.summary)


@router.get("/workspace")
async def workspace(
    underlying: str = Query("NIFTY"),
    timeframe: str = Query("15minute"),
    lookback_sessions: int = Query(60, ge=4, le=180),
    anchor_mode: str = Query("auto_pivot"),
    h_mode: str = Query("median_tpd"),
    manual_h: float | None = Query(None),
) -> dict:
    return await asyncio.to_thread(
        gann_tp_delta_service.workspace,
        underlying.upper(),
        timeframe,
        lookback_sessions,
        anchor_mode,
        h_mode,
        manual_h,
    )


@router.get("/live-snapshot")
async def live_snapshot(
    underlying: str = Query("NIFTY"),
    timeframe: str = Query("15minute"),
    lookback_sessions: int = Query(60, ge=4, le=180),
    anchor_mode: str = Query("auto_pivot"),
    h_mode: str = Query("median_tpd"),
    manual_h: float | None = Query(None),
) -> dict:
    return await gann_tp_delta_service.live_snapshot(
        underlying.upper(),
        timeframe,
        lookback_sessions,
        anchor_mode,
        h_mode,
        manual_h,
    )


@router.get("/backtest")
async def backtest(
    underlying: str = Query("NIFTY"),
    timeframe: str = Query("15minute"),
    lookback_sessions: int = Query(60, ge=4, le=180),
    anchor_mode: str = Query("auto_pivot"),
    h_mode: str = Query("median_tpd"),
) -> dict:
    return await asyncio.to_thread(
        gann_tp_delta_service.backtest,
        underlying.upper(),
        timeframe,
        lookback_sessions,
        anchor_mode,
        h_mode,
    )


@router.post("/paper-proposal")
async def paper_proposal(
    underlying: str = Query("NIFTY"),
    timeframe: str = Query("15minute"),
    lookback_sessions: int = Query(60, ge=4, le=180),
    anchor_mode: str = Query("auto_pivot"),
    h_mode: str = Query("median_tpd"),
) -> dict:
    return await gann_tp_delta_service.record_paper_snapshot(
        underlying.upper(),
        timeframe,
        lookback_sessions,
        anchor_mode,
        h_mode,
    )


@router.get("/paper-journal")
async def paper_journal(
    symbol: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    return await asyncio.to_thread(gann_tp_delta_service.paper_journal, symbol, limit)


@router.get("/paper-agent/status")
async def paper_agent_status(
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    return await asyncio.to_thread(gann_tp_delta_service.paper_agent_status, limit)


@router.post("/paper-agent/run-once")
async def paper_agent_run_once(
    timeframe: str = Query("15minute"),
    lookback_sessions: int = Query(60, ge=4, le=180),
    anchor_mode: str = Query("auto_pivot"),
    h_mode: str = Query("median_tpd"),
    live_refresh: bool = Query(False),
    max_underlyings: int = Query(0, ge=0, le=500),
) -> dict:
    return await gann_tp_delta_service.run_paper_agent_once(
        timeframe=timeframe,
        lookback_sessions=lookback_sessions,
        anchor_mode=anchor_mode,
        h_mode=h_mode,
        live_refresh=live_refresh,
        max_underlyings=max_underlyings or None,
    )
