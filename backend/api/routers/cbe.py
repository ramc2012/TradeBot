"""Compression-Before-Expansion scanner API."""
from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from cbe_scanner.features import CBEConfig
from cbe_scanner.project_provider import load_project_universe
from cbe_scanner.repository import load_latest_scan_payload
from cbe_scanner.service import build_config, load_project_instrument_analytics, run_scan


router = APIRouter(prefix="/api/cbe", tags=["cbe"])


class CBEScanRequest(BaseModel):
    source: Literal["project_timescale"] = "project_timescale"
    universe: list[str] | None = Field(default=None, description="Optional symbols to scan.")
    scan_date: date | None = None
    lookback_days: int = Field(default=300, ge=60, le=1000)
    watchlist_min_score: float | None = Field(default=None, ge=0.0, le=10.0)
    watchlist_max_size: int | None = Field(default=None, ge=1, le=100)


@router.get("/config")
async def get_cbe_config() -> dict:
    cfg = CBEConfig()
    return {
        "feature_weights": {
            "volatility_compression": cfg.w_vc,
            "option_market_positioning": cfg.w_omp,
            "cross_sectional_divergence": cfg.w_csmd,
            "catalyst_proximity": cfg.w_cp,
            "microstructure_pressure": cfg.w_mp,
        },
        "watchlist_min_score": cfg.watchlist_min_score,
        "watchlist_max_size": cfg.watchlist_max_size,
        "default_source": "project_timescale",
    }


@router.get("/universe")
async def get_cbe_universe(limit: int = Query(default=500, ge=1, le=500)) -> dict:
    symbols = await load_project_universe(limit=limit)
    return {"count": len(symbols), "symbols": symbols}


@router.post("/scan")
async def scan_cbe(body: CBEScanRequest) -> dict:
    cfg = build_config(
        watchlist_min_score=body.watchlist_min_score,
        watchlist_max_size=body.watchlist_max_size,
    )
    return await run_scan(
        source=body.source,
        universe=body.universe,
        scan_date=body.scan_date,
        lookback_days=body.lookback_days,
        cfg=cfg,
    )


@router.get("/latest")
async def get_latest_cbe_scan(source: Literal["project_timescale"] | None = None) -> dict:
    payload = await load_latest_scan_payload(source=source)
    return payload or {"results": [], "watchlist": [], "source": source, "scan_date": None}


@router.get("/instruments/{symbol}/analytics")
async def get_cbe_instrument_analytics(
    symbol: str,
    lookback_days: int = Query(default=300, ge=60, le=1000),
    scan_date: date | None = None,
) -> dict:
    return await load_project_instrument_analytics(
        symbol,
        scan_date=scan_date,
        lookback_days=lookback_days,
    )
