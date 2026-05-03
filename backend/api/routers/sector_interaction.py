"""Sector interaction and alternative-data planning routes."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from agentic_rag import RAGDocument, rag_service
from sector_interaction import sector_interaction_service
from sector_interaction.india_live import india_live_sector_service
from sector_interaction.nse_constituents import nse_constituent_service
from sector_interaction.real_history import real_sector_history_service


router = APIRouter(prefix="/api/sector-interaction", tags=["sector-interaction"])


@router.get("/overview")
async def overview() -> dict[str, Any]:
    payload = sector_interaction_service.overview()
    payload["default_country"] = "IN"
    payload["live_india_endpoint"] = "/api/sector-interaction/india/overview"
    return payload


@router.get("/india/overview")
async def india_overview() -> dict[str, Any]:
    return await india_live_sector_service.overview()


@router.get("/nse-constituents/status")
async def nse_constituents_status() -> dict[str, Any]:
    return await asyncio.to_thread(nse_constituent_service.status)


@router.post("/nse-constituents/sync")
async def sync_nse_constituents(
    timeout_seconds: float = Query(8.0, ge=2.0, le=20.0),
) -> dict[str, Any]:
    return await nse_constituent_service.sync(timeout_seconds=timeout_seconds)


@router.get("/sectors/{sector_key}")
async def india_sector_detail(sector_key: str) -> dict[str, Any]:
    payload = await india_live_sector_service.sector_detail(sector_key)
    if payload.get("summary") is None:
        raise HTTPException(status_code=404, detail=f"No live India sector found for {sector_key}")
    return payload


@router.get("/market-intelligence")
async def market_intelligence() -> dict[str, Any]:
    return await india_live_sector_service.market_intelligence_payload()


@router.get("/india/real-model")
async def india_real_model(
    periods: int = Query(160, ge=48, le=500),
    max_lag: int = Query(2, ge=1, le=6),
    alpha: float = Query(0.05, gt=0.0, le=0.25),
    timeframe: str = Query("daily", pattern="^(daily|weekly|monthly)$"),
) -> dict[str, Any]:
    return await real_sector_history_service.india_model(
        periods=periods,
        max_lag=max_lag,
        alpha=alpha,
        timeframe=timeframe,
    )


@router.get("/model")
async def model(
    country: str = Query("US", description="US or IN"),
    periods: int = Query(160, ge=48, le=500),
    max_lag: int = Query(2, ge=1, le=6),
    alpha: float = Query(0.05, gt=0.0, le=0.25),
) -> dict[str, Any]:
    try:
        if str(country or "").upper() in {"IN", "INDIA", "NSE"}:
            return await real_sector_history_service.india_model(
                periods=periods,
                max_lag=max_lag,
                alpha=alpha,
                timeframe="daily",
            )
        return await asyncio.to_thread(sector_interaction_service.model, country, periods, max_lag, alpha)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/source-map")
async def source_map(country: str = Query("US", description="US or IN")) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(sector_interaction_service.source_map, country)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/signals")
async def signals(
    country: str = Query("US", description="US or IN"),
    periods: int = Query(160, ge=48, le=500),
) -> dict[str, Any]:
    try:
        if str(country or "").upper() in {"IN", "INDIA", "NSE"}:
            return await india_live_sector_service.signals_payload()
        return await asyncio.to_thread(sector_interaction_service.signals, country, periods)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/extended-network")
async def extended_network(
    country: str = Query("US", description="US or IN"),
    periods: int = Query(160, ge=48, le=500),
    max_lag: int = Query(2, ge=1, le=6),
    alpha: float = Query(0.05, gt=0.0, le=0.25),
) -> dict[str, Any]:
    try:
        if str(country or "").upper() in {"IN", "INDIA", "NSE"}:
            return await india_live_sector_service.extended_network_payload(
                periods=periods,
                max_lag=max_lag,
                alpha=alpha,
            )
        return await asyncio.to_thread(sector_interaction_service.extended_network, country, periods, max_lag, alpha)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/validation-backtest")
async def validation_backtest(
    country: str = Query("US", description="US or IN"),
    periods: int = Query(160, ge=60, le=500),
) -> dict[str, Any]:
    try:
        if str(country or "").upper() in {"IN", "INDIA", "NSE"}:
            return await india_live_sector_service.validation_payload()
        return await asyncio.to_thread(sector_interaction_service.validation_backtest, country, periods)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/pipeline-status")
async def pipeline_status(country: str = Query("US", description="US or IN")) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(sector_interaction_service.pipeline_status, country)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/ingestion-status")
async def ingestion_status(country: str = Query("US", description="US or IN")) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(sector_interaction_service.ingestion_status, country)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/run-ingestion")
async def run_ingestion(
    country: str = Query("US", description="US or IN"),
    dry_run: bool = Query(True, description="Preview collector output without writing observations"),
    include_prototype: bool = Query(False, description="Allow prototype connectors in addition to approved open-data connectors"),
) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            sector_interaction_service.run_ingestion,
            country,
            dry_run=dry_run,
            include_prototype=include_prototype,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/india/run-live-market-ingestion")
async def run_india_live_market_ingestion(
    dry_run: bool = Query(True, description="Preview live India sector-market observations without writing them"),
) -> dict[str, Any]:
    return await sector_interaction_service.run_india_live_market_ingestion(dry_run=dry_run)


@router.get("/report")
async def sector_report(
    country: str = Query("US", description="US or IN"),
    periods: int = Query(160, ge=60, le=500),
) -> dict[str, Any]:
    try:
        if str(country or "").upper() in {"IN", "INDIA", "NSE"}:
            return await india_live_sector_service.report_payload()
        return await asyncio.to_thread(sector_interaction_service.sector_report, country, periods)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/acquisition-plan")
async def acquisition_plan() -> dict[str, Any]:
    return sector_interaction_service.acquisition_plan()


@router.post("/seed-rag")
async def seed_rag_documents() -> dict[str, Any]:
    documents = [
        RAGDocument.model_validate(document)
        for document in sector_interaction_service.rag_documents()
    ]
    existing_ids = {
        document.id
        for document in await asyncio.to_thread(rag_service.store.load_documents)
    }
    missing = [document for document in documents if document.id not in existing_ids]
    stored = await asyncio.to_thread(rag_service.store.append_documents, missing)
    return {
        "stored": stored,
        "document_ids": [document.id for document in missing],
        "already_present": [document.id for document in documents if document.id in existing_ids],
    }
