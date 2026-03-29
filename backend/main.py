"""Nomad Curie — FastAPI application entry point."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from core.config import settings
from db.redis_client import get_redis, close_redis
from api.routers import auth, trading, market, analytics, agent, backtester as backtester_router
from api.routers import fo_data as fo_data_router
from api.routers import analysis as analysis_router
from api.websockets.ticks import ws_ticks, ws_positions, ws_proposals
from market_data import data_router as market_data_router
from market_data.symbols import LIVE_INDEX_APP_SYMBOLS
from paper_engine.strategy_agent import paper_strategy_agent


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    logger.info("═══ Nomad Curie starting up ═══")

    # Ensure Redis is reachable
    try:
        redis = await get_redis()
        await redis.ping()
        logger.info("✓ Redis connected")
    except Exception as e:
        logger.warning(f"Redis not available: {e} — running without cache")

    # Restore broker sessions from saved credentials (e.g. Upstox JWT token)
    try:
        from api.routers.auth import auto_restore_sessions, get_active_adapter
        await auto_restore_sessions()
        adapter = get_active_adapter("fyers") or get_active_adapter("upstox") or get_active_adapter()
    except Exception as e:
        logger.warning(f"Session auto-restore failed: {e}")
        adapter = None

    # Wire market profile builder to data router
    from market_data.market_profile import market_profile_builder
    for symbol in LIVE_INDEX_APP_SYMBOLS:
        market_data_router.register_callback(symbol, market_profile_builder.on_tick)

    # Prefer the real broker feed when a session exists; fall back to mock only otherwise.
    if adapter:
        market_data_router.set_broker(adapter)
        await market_data_router.subscribe(list(LIVE_INDEX_APP_SYMBOLS))
    else:
        asyncio.create_task(
            market_data_router.start_mock_feed(
                list(LIVE_INDEX_APP_SYMBOLS),
                interval_secs=1.0,
            )
        )

    await paper_strategy_agent.start()

    yield

    # Shutdown
    await paper_strategy_agent.stop()
    await market_data_router.stop_mock_feed()
    await close_redis()
    await market_data_router.unsubscribe()
    logger.info("═══ Nomad Curie shut down ═══")


app = FastAPI(
    title="Nomad Curie",
    description="NSE F&O Algorithmic Trading Platform",
    version="1.0.0",
    lifespan=lifespan,
)

# ── CORS ─────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── REST Routers ─────────────────────────────────────────────────────────────
app.include_router(auth.router)
app.include_router(trading.router)
app.include_router(market.router)
app.include_router(analytics.router)
app.include_router(agent.router)
app.include_router(backtester_router.router)
app.include_router(fo_data_router.router)
app.include_router(analysis_router.router)


# ── WebSocket Endpoints ───────────────────────────────────────────────────────
@app.websocket("/ws/ticks/{symbol:path}")
async def websocket_ticks(websocket: WebSocket, symbol: str):
    await ws_ticks(websocket, symbol)


@app.websocket("/ws/positions")
async def websocket_positions(websocket: WebSocket):
    await ws_positions(websocket)


@app.websocket("/ws/proposals")
async def websocket_proposals(websocket: WebSocket):
    await ws_proposals(websocket)


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


@app.get("/")
async def root():
    return {
        "name": "Nomad Curie",
        "description": "NSE F&O Algorithmic Trading Platform",
        "docs": "/docs",
    }
