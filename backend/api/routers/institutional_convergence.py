from __future__ import annotations

from fastapi import APIRouter

from institutional_convergence.service import institutional_convergence_service
from institutional_convergence.paper import convergence_paper_book


router = APIRouter(prefix="/api/institutional-convergence", tags=["institutional-convergence"])


@router.get("/status")
async def status() -> dict:
    return await institutional_convergence_service.status()


@router.get("/universe")
async def universe() -> dict:
    return await institutional_convergence_service.build_universe()


@router.post("/run-once")
async def run_once() -> dict:
    return await institutional_convergence_service.run_cycle()


@router.get("/paper")
async def paper() -> dict:
    return convergence_paper_book.summary()


@router.get("/orders")
async def orders(limit: int = 500) -> dict:
    """Paper order log — every open/partial_close/close action (instant fills)."""
    return convergence_paper_book.orders(limit=limit)


@router.get("/trades")
async def trades() -> dict:
    """Closed-trade book as flat CSV-able JSON rows."""
    return convergence_paper_book.trades()


@router.get("/statistics")
async def statistics() -> dict:
    """Win rate, avg R, profit factor, expectancy, drawdown, breakdowns, daily pnl."""
    return convergence_paper_book.statistics()


# ── Commodity (MCX) variant ────────────────────────────────────────────────
from institutional_convergence.commodity import (  # noqa: E402
    commodity_convergence_paper_book,
    commodity_convergence_service,
)


@router.get("/commodity/status")
async def commodity_status() -> dict:
    return await commodity_convergence_service.status()


@router.get("/commodity/universe")
async def commodity_universe() -> dict:
    return await commodity_convergence_service.build_universe()


@router.post("/commodity/run-once")
async def commodity_run_once() -> dict:
    return await commodity_convergence_service.run_cycle()


@router.get("/commodity/paper")
async def commodity_paper() -> dict:
    return commodity_convergence_paper_book.summary()


@router.get("/commodity/orders")
async def commodity_orders(limit: int = 500) -> dict:
    return commodity_convergence_paper_book.orders(limit=limit)


@router.get("/commodity/trades")
async def commodity_trades() -> dict:
    return commodity_convergence_paper_book.trades()


@router.get("/commodity/statistics")
async def commodity_statistics() -> dict:
    return commodity_convergence_paper_book.statistics()
