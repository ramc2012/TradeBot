"""API surface for the directional long-options module."""
from __future__ import annotations

from fastapi import APIRouter, Query

from directional_options.service import DirectionalOptionsService


router = APIRouter(prefix="/api/directional-options", tags=["directional-options"])
_service = DirectionalOptionsService()


@router.get("/summary")
async def summary() -> dict[str, object]:
    return _service.summary()


@router.get("/workspace")
async def workspace(
    underlying: str = Query("NIFTY"),
    timeframe: str = Query("5minute"),
    lookback_sessions: int = Query(16, ge=4, le=90),
) -> dict[str, object]:
    return _service.workspace(underlying, timeframe, lookback_sessions)


@router.get("/backtest")
async def backtest(
    underlying: str = Query("NIFTY"),
    timeframe: str = Query("5minute"),
    lookback_sessions: int = Query(16, ge=4, le=90),
) -> dict[str, object]:
    return _service.workspace(underlying, timeframe, lookback_sessions)["backtest"]
