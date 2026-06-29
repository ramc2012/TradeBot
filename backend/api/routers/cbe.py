"""Compression-Before-Expansion scanner API."""
from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from cbe_scanner.features import CBEConfig
from cbe_scanner.paper import cbe_paper_book
from cbe_scanner.project_provider import load_project_universe
from cbe_scanner.repository import load_latest_scan_payload
from cbe_scanner.service import build_config, load_project_instrument_analytics, run_scan


router = APIRouter(prefix="/api/cbe", tags=["cbe"])


class CBEScanRequest(BaseModel):
    # The alpha engine is now the default scan source. Legacy project_timescale
    # remains for research / regression queries — it bypasses the L1..L7 pipeline
    # and runs the original vol-compression composite score.
    source: Literal["alpha_engine", "project_timescale"] = "alpha_engine"
    universe: list[str] | None = Field(default=None, description="Legacy: symbols to scan in project_timescale mode.")
    scan_date: date | None = None
    lookback_days: int = Field(default=300, ge=60, le=1000)
    watchlist_min_score: float | None = Field(default=None, ge=0.0, le=10.0)
    watchlist_max_size: int | None = Field(default=None, ge=1, le=100)
    # Alpha-engine-specific knobs. Ignored when source=project_timescale.
    timeframe: str | None = Field(default=None, description="Alpha: 'weekly', 'daily', etc.")
    sectors_to_keep: int | None = Field(default=None, ge=1, le=13)
    stocks_per_sector: int | None = Field(default=None, ge=1, le=20)
    composite_gate: float | None = Field(default=None, ge=0.0, le=100.0, deprecated=True)
    top_n_watchlist: int | None = Field(default=None, ge=1, le=50)
    low_conviction_floor: float | None = Field(default=None, ge=0.0, le=100.0)


class CBEResetRequest(BaseModel):
    confirm: str = Field(..., description="Must equal 'RESET' to proceed (destructive).")
    actor: str | None = None


@router.get("/config")
async def get_cbe_config() -> dict:
    from cbe_scanner.alpha_engine import AlphaEngineConfig

    alpha_cfg = AlphaEngineConfig()
    legacy_cfg = CBEConfig()
    return {
        "default_source": "alpha_engine",
        "engine_version": "alpha_engine_v4_direction_aware",
        "timeframe": alpha_cfg.timeframe,
        "top_n_watchlist": alpha_cfg.top_n_watchlist,
        "minimum_opportunity_score": alpha_cfg.low_conviction_floor,
        "feature_weights": {
            "asset_rotation": alpha_cfg.weights.asset,
            "sector_relative_strength": alpha_cfg.weights.sector,
            "stock_relative_strength": alpha_cfg.weights.stock,
            "directional_macd": alpha_cfg.weights.macd,
            "directional_rsi": alpha_cfg.weights.rsi,
        },
        "legacy_project_timescale": {
            "watchlist_min_score": legacy_cfg.watchlist_min_score,
            "watchlist_max_size": legacy_cfg.watchlist_max_size,
        },
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
    alpha_cfg = None
    if body.source == "alpha_engine":
        from cbe_scanner.alpha_engine import AlphaEngineConfig
        alpha_cfg = AlphaEngineConfig()
        if body.timeframe:
            alpha_cfg.timeframe = body.timeframe
        if body.sectors_to_keep:
            alpha_cfg.sectors_to_keep = body.sectors_to_keep
        if body.stocks_per_sector is not None:
            raise HTTPException(
                status_code=400,
                detail="stocks_per_sector is no longer supported; use top_n_watchlist",
            )
        # composite_gate is now legacy — accept it for back-compat but it
        # has no effect. The watchlist is purely top-N by ranking.
        if body.top_n_watchlist is not None:
            alpha_cfg.top_n_watchlist = body.top_n_watchlist
        if body.low_conviction_floor is not None:
            alpha_cfg.low_conviction_floor = body.low_conviction_floor
    return await run_scan(
        source=body.source,
        universe=body.universe,
        scan_date=body.scan_date,
        lookback_days=body.lookback_days,
        cfg=cfg,
        alpha_config=alpha_cfg,
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


@router.get("/paper-summary")
async def cbe_paper_summary() -> dict:
    """Capital + P&L snapshot for the CBE cash-equity paper book."""
    return await cbe_paper_book.capital_status()


@router.get("/paper-positions")
async def cbe_paper_positions(
    status: Literal["all", "open", "closed"] = Query("all"),
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    return await cbe_paper_book.list_positions(status=status, limit=limit)


@router.get("/paper-journal")
async def cbe_paper_journal(
    instrument: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    return await cbe_paper_book.list_journal(instrument=instrument, limit=limit)


@router.post("/reset-paper")
async def cbe_reset_paper(body: CBEResetRequest) -> dict:
    if (body.confirm or "").strip().upper() != "RESET":
        raise HTTPException(
            status_code=400,
            detail=(
                "Paper reset is destructive. POST `{\"confirm\": \"RESET\"}` "
                "to confirm."
            ),
        )
    return await cbe_paper_book.reset_account(actor=body.actor)
