"""Data Quality agent read endpoint.

`/api/data-quality/snapshot` returns the per-symbol freshness ledger so the
frontend and the strategy agents can short-circuit on stale data instead of
trading on it.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from market_data.data_quality_agent import data_quality_agent


router = APIRouter(prefix="/api/data-quality", tags=["data-quality"])


@router.get("/snapshot")
async def data_quality_snapshot() -> dict[str, Any]:
    return data_quality_agent.snapshot()
