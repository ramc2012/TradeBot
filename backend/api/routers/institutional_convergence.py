from __future__ import annotations

from fastapi import APIRouter

from institutional_convergence.service import institutional_convergence_service


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
