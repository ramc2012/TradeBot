"""Isolated API for the Market Profile + order-flow strategy module."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from auction_intelligence import AuctionIntelligenceService
from auction_intelligence.config import clone_default_config
from auction_intelligence.schemas import (
    DepthLevel,
    DepthSnapshot,
    MarketBar,
    PortfolioSnapshot,
    QuoteSnapshot,
    SessionContext,
    TradePrint,
)


router = APIRouter(prefix="/api/auction-intelligence", tags=["auction-intelligence"])


class BarPayload(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class TradePayload(BaseModel):
    timestamp: datetime
    price: float
    quantity: float
    aggressor_side: str = "unknown"


class QuotePayload(BaseModel):
    timestamp: datetime
    bid: float
    ask: float
    bid_size: float = 0.0
    ask_size: float = 0.0


class DepthLevelPayload(BaseModel):
    price: float
    quantity: float


class DepthPayload(BaseModel):
    timestamp: datetime
    bids: list[DepthLevelPayload] = Field(default_factory=list)
    asks: list[DepthLevelPayload] = Field(default_factory=list)


class SessionPayload(BaseModel):
    symbol: str = "NIFTY FUT"
    session_date: date = Field(default_factory=date.today)
    last_price: float
    stale_data_seconds: float = 0.0
    minutes_to_close: int = 120
    broker_connected: bool = True


class PortfolioPayload(BaseModel):
    net_liquidation: float = 1_000_000.0
    daily_realized_pnl: float = 0.0
    open_positions: int = 0
    symbol_exposure: dict[str, float] = Field(default_factory=dict)
    agent_drawdowns: dict[str, float] = Field(default_factory=dict)
    correlated_exposure: float = 0.0


class AnalysisRequest(BaseModel):
    session: SessionPayload
    quote: QuotePayload
    bars: list[BarPayload]
    prior_bars: list[BarPayload] = Field(default_factory=list)
    trades: list[TradePayload] = Field(default_factory=list)
    depth: Optional[DepthPayload] = None
    portfolio: PortfolioPayload = Field(default_factory=PortfolioPayload)


def _service() -> AuctionIntelligenceService:
    return AuctionIntelligenceService()


def _bars(payload: list[BarPayload]) -> list[MarketBar]:
    return [MarketBar(**item.model_dump()) for item in payload]


def _trades(payload: list[TradePayload]) -> list[TradePrint]:
    return [TradePrint(**item.model_dump()) for item in payload]


def _depth(payload: Optional[DepthPayload]) -> Optional[DepthSnapshot]:
    if payload is None:
        return None
    return DepthSnapshot(
        timestamp=payload.timestamp,
        bids=[DepthLevel(**item.model_dump()) for item in payload.bids],
        asks=[DepthLevel(**item.model_dump()) for item in payload.asks],
    )


def _serialize(value: object) -> dict:
    return jsonable_encoder(asdict(value))


@router.get("/summary")
async def summary() -> dict:
    config = clone_default_config()
    return {
        "module": "auction_intelligence",
        "description": "Separate Market Profile + order-flow strategy stack",
        "auto_started": False,
        "mvp_scope": config["mvp_scope"],
        "deployable_first_sleeve": "swing",
        "endpoints": [
            "/api/auction-intelligence/summary",
            "/api/auction-intelligence/default-config",
            "/api/auction-intelligence/analyze",
            "/api/auction-intelligence/paper-proposal",
        ],
    }


@router.get("/default-config")
async def default_config() -> dict:
    return clone_default_config()


@router.post("/analyze")
async def analyze(request: AnalysisRequest) -> dict:
    service = _service()
    bundle = service.analyze(
        session=SessionContext(**request.session.model_dump()),
        bars=_bars(request.bars),
        quote=QuoteSnapshot(**request.quote.model_dump()),
        trades=_trades(request.trades),
        prior_bars=_bars(request.prior_bars),
        depth=_depth(request.depth),
        portfolio=PortfolioSnapshot(**request.portfolio.model_dump()),
    )
    return _serialize(bundle)


@router.post("/paper-proposal")
async def paper_proposal(request: AnalysisRequest) -> dict:
    service = _service()
    bundle, journal_paths = service.analyze_and_record_paper(
        session=SessionContext(**request.session.model_dump()),
        bars=_bars(request.bars),
        quote=QuoteSnapshot(**request.quote.model_dump()),
        trades=_trades(request.trades),
        prior_bars=_bars(request.prior_bars),
        depth=_depth(request.depth),
        portfolio=PortfolioSnapshot(**request.portfolio.model_dump()),
    )
    payload = _serialize(bundle)
    payload["journal_paths"] = journal_paths
    return payload
