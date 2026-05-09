"""Macro research and sector discovery routes."""
from __future__ import annotations

from fastapi import APIRouter, Query

from macro_research import macro_research_service

router = APIRouter(prefix="/api/macro-research", tags=["macro-research"])


@router.get("/overview")
async def get_macro_research_overview(refresh: bool = Query(False)):
    return await macro_research_service.overview(refresh=refresh)


@router.get("/sectors")
async def get_macro_research_sectors(refresh: bool = Query(False)):
    return await macro_research_service.sector_map(refresh=refresh)


@router.get("/sectors/{sector_code}")
async def get_macro_research_sector(sector_code: str, refresh: bool = Query(False)):
    return await macro_research_service.sector_detail(sector_code, refresh=refresh)


@router.get("/budding-sectors")
async def get_macro_research_budding_sectors(refresh: bool = Query(False)):
    return await macro_research_service.budding_sectors(refresh=refresh)


@router.get("/sources")
async def get_macro_research_sources():
    return macro_research_service.source_map()


@router.get("/search")
async def search_macro_research(
    q: str = Query("", max_length=180),
    sector: str | None = Query(None, max_length=80),
    limit: int = Query(12, ge=1, le=50),
    refresh: bool = Query(False),
):
    return await macro_research_service.search(q, sector_code=sector, limit=limit, refresh=refresh)
