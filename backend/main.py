"""Nomad Curie — FastAPI application entry point."""
from __future__ import annotations
import asyncio
import hmac
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

from core.config import settings
from core.market_hours_paper_supervisor import market_hours_paper_supervisor
from core.paper_bootstrap import bootstrap_paper_trading_runtime
from db.redis_client import get_redis, close_redis
from api.routers import auth, trading, market, analytics, agent, commodity, macro_research, rag, sector_interaction, cbe, backtester as backtester_router
from api.routers import fo_data as fo_data_router
from api.routers import analysis as analysis_router
from api.routers import strategy as strategy_router
from api.routers import auction_intelligence as auction_intelligence_router
from api.routers import directional_options as directional_options_router
from api.routers import gann_tp_delta as gann_tp_delta_router
from api.routers import fractal_market_profile as fractal_market_profile_router
from api.routers import orderflow as orderflow_router
from api.routers import charts as charts_router
from api.routers import system as system_router
from api.routers import audit as audit_router
from api.routers import data_quality as data_quality_router
from api.routers import notifications as notifications_router
from directional_options import mount_directional_options_dashboard
from directional_options.service import directional_options_service
from api.websockets.ticks import (
    ws_commodity_overview,
    ws_commodity_watchlist,
    ws_fractal_market_profile,
    ws_layout,
    ws_market_option_chain,
    ws_market_watchlist,
    ws_positions,
    ws_positions_overview,
    ws_proposals,
    ws_strategy_dashboard,
    ws_strategy_overview,
    ws_system_health,
    ws_system_overview,
    ws_ticks,
)
from market_data import data_router as market_data_router
from market_data.live_candle_store import live_candle_store
from market_data.symbols import LIVE_INDEX_APP_SYMBOLS
from auction_intelligence.rl.automation import rl_auto_trainer
from paper_engine.commodity_strategy_agent import commodity_strategy_agent
from paper_engine.strategy_agent import paper_strategy_agent


OAUTH_CALLBACK_PATHS = {"/api/auth/fyers/callback", "/api/auth/upstox/callback"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    logger.info("═══ Nomad Curie starting up ═══")
    research_sync_task: asyncio.Task | None = None

    # Ensure Redis is reachable
    try:
        redis = await get_redis()
        await redis.ping()
        logger.info("✓ Redis connected")
    except Exception as e:
        logger.warning(f"Redis not available: {e} — running without cache")

    # Restore broker sessions from saved credentials (e.g. Upstox JWT token)
    try:
        from api.routers.auth import auto_restore_sessions, get_active_adapter, load_persistent_credentials
        await asyncio.to_thread(load_persistent_credentials)
        await auto_restore_sessions()
        adapter = get_active_adapter("fyers") or get_active_adapter("upstox") or get_active_adapter()
    except Exception as e:
        logger.warning(f"Session auto-restore failed: {e}")
        adapter = None

    # Wire market profile builder to data router
    from market_data.market_profile import market_profile_builder
    for symbol in LIVE_INDEX_APP_SYMBOLS:
        market_data_router.register_callback(symbol, market_profile_builder.on_tick)
    market_data_router.register_global_callback(live_candle_store.on_tick)
    await live_candle_store.start()

    # Prefer the real broker feed when a session exists. Without a broker, keep
    # the shared header feed idle so the UI falls back to stored spot closes
    # instead of publishing synthetic 100-based index ticks.
    if adapter:
        from api.routers.auth import get_active_adapter
        from market_data.source_policy import choose_active_adapter, source_policy_snapshot

        active_adapters = {
            "fyers": get_active_adapter("fyers"),
            "upstox": get_active_adapter("upstox"),
        }
        active_brokers = [name for name, active_adapter in active_adapters.items() if active_adapter is not None]
        selected_adapter, source, decisions = choose_active_adapter("live_ticks", active_adapters)
        if selected_adapter is not None:
            adapter = selected_adapter
        market_data_router.set_source_policy(
            source_policy_snapshot(
                active_brokers=active_brokers,
                selected_live_source=source,
                route_decisions=decisions,
            )
        )
        market_data_router.set_broker(adapter)
        await market_data_router.subscribe(list(LIVE_INDEX_APP_SYMBOLS))
    else:
        await market_data_router.stop_mock_feed()
        await market_data_router.unsubscribe()

    await paper_strategy_agent.start()
    await commodity_strategy_agent.start()
    try:
        await bootstrap_paper_trading_runtime()
    except Exception as e:
        logger.warning(f"Paper bootstrap skipped: {e}")

    # Load RL Q-table cache into memory (non-fatal if table doesn't exist yet)
    try:
        from auction_intelligence.rl.policy import rl_policy
        await rl_policy.load_cache()
        logger.info("✓ RL Q-table cache loaded")
    except Exception as e:
        logger.warning(f"RL Q-table cache load skipped: {e}")
    try:
        await rl_auto_trainer.start()
        logger.info("✓ RL auto-trainer scheduled")
    except Exception as e:
        logger.warning(f"RL auto-trainer start skipped: {e}")
    try:
        await market_hours_paper_supervisor.start()
        logger.info("✓ Market-hours paper supervisor started")
    except Exception as e:
        logger.warning(f"Market-hours paper supervisor start skipped: {e}")

    if settings.RESEARCH_SYNC_EMBEDDED_ENABLED:
        async def _embedded_research_sync_worker() -> None:
            try:
                from data.run_upstox_research_sync import run_daemon_from_env

                await run_daemon_from_env()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(f"Embedded research sync daemon stopped: {exc}")

        research_sync_task = asyncio.create_task(
            _embedded_research_sync_worker(),
            name="embedded-research-sync",
        )
        logger.info("✓ Embedded research sync daemon started")

    yield

    # Shutdown
    if research_sync_task is not None:
        research_sync_task.cancel()
        try:
            await research_sync_task
        except asyncio.CancelledError:
            logger.info("Embedded research sync daemon stopped")
    await market_hours_paper_supervisor.stop()
    await rl_auto_trainer.stop()
    await paper_strategy_agent.stop()
    await commodity_strategy_agent.stop()
    await live_candle_store.stop()
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
mount_directional_options_dashboard(app, directional_options_service)


def _extract_write_token(request: Request) -> str:
    header_token = str(request.headers.get("x-nomad-write-token") or "").strip()
    if header_token:
        return header_token
    authorization = str(request.headers.get("authorization") or "").strip()
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return str(request.cookies.get("nomad_write_token") or "").strip()


def _api_guard_active() -> bool:
    return bool(settings.APP_TOKEN_AUTH_ENABLED)


@app.middleware("http")
async def require_api_token(request: Request, call_next):
    if not request.url.path.startswith("/api/"):
        return await call_next(request)
    if not _api_guard_active():
        return await call_next(request)
    if request.url.path in OAUTH_CALLBACK_PATHS:
        return await call_next(request)

    expected = settings.APP_WRITE_TOKEN.strip()
    if not expected:
        return JSONResponse(
            {"detail": "APP_WRITE_TOKEN must be configured before API access is enabled in production."},
            status_code=503,
        )
    supplied = _extract_write_token(request)
    if not supplied or not hmac.compare_digest(supplied, expected):
        return JSONResponse({"detail": "API token is required."}, status_code=403)
    return await call_next(request)

# ── CORS ─────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_origin_regex=settings.BACKEND_CORS_ORIGIN_REGEX,
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
app.include_router(rag.router)
app.include_router(sector_interaction.router)
app.include_router(cbe.router)
app.include_router(macro_research.router)
app.include_router(commodity.router)
app.include_router(backtester_router.router)
app.include_router(fo_data_router.router)
app.include_router(analysis_router.router)
app.include_router(strategy_router.router)
app.include_router(auction_intelligence_router.router)
app.include_router(directional_options_router.router)
app.include_router(gann_tp_delta_router.router)
app.include_router(fractal_market_profile_router.router)
app.include_router(orderflow_router.router)
app.include_router(charts_router.router)
app.include_router(system_router.router)
app.include_router(audit_router.router)
app.include_router(data_quality_router.router)
app.include_router(notifications_router.router)


# ── WebSocket Endpoints ───────────────────────────────────────────────────────
@app.websocket("/ws/ticks/{symbol:path}")
async def websocket_ticks(websocket: WebSocket, symbol: str):
    await ws_ticks(websocket, symbol)


@app.websocket("/ws/positions")
async def websocket_positions(websocket: WebSocket):
    await ws_positions(websocket)


@app.websocket("/ws/layout")
async def websocket_layout(websocket: WebSocket):
    await ws_layout(websocket)


@app.websocket("/ws/system-overview")
async def websocket_system_overview(websocket: WebSocket):
    await ws_system_overview(websocket)


@app.websocket("/ws/system-health")
async def websocket_system_health(websocket: WebSocket):
    await ws_system_health(websocket)


@app.websocket("/ws/strategy-overview")
async def websocket_strategy_overview(websocket: WebSocket):
    await ws_strategy_overview(websocket)


@app.websocket("/ws/strategy-dashboard")
async def websocket_strategy_dashboard(websocket: WebSocket):
    await ws_strategy_dashboard(websocket)


@app.websocket("/ws/positions-overview")
async def websocket_positions_overview(websocket: WebSocket):
    await ws_positions_overview(websocket)


@app.websocket("/ws/commodity-overview")
async def websocket_commodity_overview(websocket: WebSocket):
    await ws_commodity_overview(websocket)


@app.websocket("/ws/commodity-watchlist")
async def websocket_commodity_watchlist(websocket: WebSocket):
    await ws_commodity_watchlist(websocket)


@app.websocket("/ws/market-watchlist")
async def websocket_market_watchlist(websocket: WebSocket):
    await ws_market_watchlist(websocket)


@app.websocket("/ws/market-option-chain/{symbol:path}")
async def websocket_market_option_chain(websocket: WebSocket, symbol: str):
    await ws_market_option_chain(websocket, symbol)


@app.websocket("/ws/fractal-market-profile/{symbol}")
async def websocket_fractal_market_profile(websocket: WebSocket, symbol: str):
    await ws_fractal_market_profile(websocket, symbol)


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
