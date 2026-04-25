"""Agentic RAG routes — shared memory, context gate, and audit bundles."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query

from agentic_rag import ContextGateRequest, RAGDocument, RAGSearchRequest, TradeCaseRecord, rag_service


router = APIRouter(prefix="/api/rag", tags=["rag"])


@router.get("/health")
async def rag_health() -> dict[str, Any]:
    return await asyncio.to_thread(rag_service.health)


@router.post("/documents")
async def add_document(document: RAGDocument) -> dict[str, Any]:
    stored = await asyncio.to_thread(rag_service.add_document, document)
    return {"stored": True, "document": stored.model_dump()}


@router.post("/trade-cases")
async def add_trade_case(trade_case: TradeCaseRecord) -> dict[str, Any]:
    stored = await asyncio.to_thread(rag_service.add_trade_case, trade_case)
    return {"stored": True, "trade_case": stored.model_dump()}


@router.post("/search")
async def search(request: RAGSearchRequest) -> dict[str, Any]:
    hits = await asyncio.to_thread(rag_service.search, request)
    return {
        "query": request.query,
        "count": len(hits),
        "hits": [hit.model_dump() for hit in hits],
    }


@router.get("/search")
async def search_get(
    query: str = Query(..., min_length=2),
    top_k: int = Query(8, ge=1, le=50),
    underlying: str | None = None,
    strategy_key: str | None = None,
    collection: str | None = None,
) -> dict[str, Any]:
    filters = {
        key: value
        for key, value in {
            "underlying": underlying,
            "strategy_key": strategy_key,
            "collection": collection,
        }.items()
        if value
    }
    request = RAGSearchRequest(query=query, top_k=top_k, filters=filters)
    hits = await asyncio.to_thread(rag_service.search, request)
    return {
        "query": query,
        "filters": filters,
        "count": len(hits),
        "hits": [hit.model_dump() for hit in hits],
    }


@router.post("/context-gate")
async def context_gate(request: ContextGateRequest) -> dict[str, Any]:
    result = await asyncio.to_thread(rag_service.context_gate, request)
    return result.model_dump()
