from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from fractal_market_profile.service import fmp_service


router = APIRouter(prefix="/api/fractal-market-profile", tags=["fractal-market-profile"])


@router.get("/summary")
async def fractal_market_profile_summary() -> dict:
    return await fmp_service.summary()


def _resolve_symbol(symbol: str | None, symbol_code: str | None) -> str:
    return str(symbol_code or symbol or "NIFTY").upper().strip()


@router.get("/live-snapshot")
async def fractal_market_profile_live_snapshot(
    symbol: str | None = Query(None),
    symbol_code: Annotated[str | None, Query(alias="symbol_code")] = None,
) -> dict:
    try:
        return await fmp_service.live_snapshot(_resolve_symbol(symbol, symbol_code))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/paper-proposal")
async def fractal_market_profile_paper_proposal(
    symbol: str | None = Query(None),
    symbol_code: Annotated[str | None, Query(alias="symbol_code")] = None,
) -> dict:
    try:
        return await fmp_service.record_paper_snapshot(_resolve_symbol(symbol, symbol_code))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/paper-journal")
async def fractal_market_profile_paper_journal(
    symbol: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    return await fmp_service.paper_journal(symbol=symbol, limit=limit)


@router.get("/paper-positions")
async def fractal_market_profile_paper_positions(
    symbol: str | None = Query(None),
    status: str = Query("all"),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    return await fmp_service.paper_positions(symbol=symbol, status=status, limit=limit)


@router.get("/replay-report")
async def fractal_market_profile_replay_report(
    symbol: str | None = Query(None),
    symbol_code: Annotated[str | None, Query(alias="symbol_code")] = None,
    force: bool = Query(False),
) -> dict:
    try:
        return await fmp_service.replay_report(_resolve_symbol(symbol, symbol_code), force=force)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/replay-suite")
async def fractal_market_profile_replay_suite(force: bool = Query(False)) -> dict:
    return await fmp_service.replay_suite(force=force)
