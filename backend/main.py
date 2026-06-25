"""Nomad Curie — FastAPI application entry point."""
from __future__ import annotations
import asyncio
import hmac
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

from core import metrics as app_metrics
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
from api.routers import macd_refined as macd_refined_router
from api.routers import gann_tp_delta as gann_tp_delta_router
from api.routers import fractal_market_profile as fractal_market_profile_router
from api.routers import orderflow as orderflow_router
from api.routers import charts as charts_router
from api.routers import system as system_router
from api.routers import audit as audit_router
from api.routers import data_quality as data_quality_router
from api.routers import lane_health as lane_health_router
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
    ws_depth,
    ws_proposals,
    ws_quotes,
    ws_strategy_dashboard,
    ws_strategy_overview,
    ws_strategy_snapshot,
    ws_system_health,
    ws_system_overview,
    ws_ticks,
)
from market_data import data_router as market_data_router
from market_data.live_candle_store import live_candle_store
from market_data.symbols import LIVE_INDEX_APP_SYMBOLS, SECTOR_INDEX_APP_SYMBOLS
from auction_intelligence.rl.automation import rl_auto_trainer
from paper_engine.commodity_strategy_agent import commodity_strategy_agent
from paper_engine.strategy_agent import paper_strategy_agent


OAUTH_CALLBACK_PATHS = {"/api/auth/fyers/callback", "/api/auth/upstox/callback"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    logger.info("═══ Nomad Curie starting up ═══")

    # ── Production security guardrails (gap-audit B1/H7) ─────────────────────────
    # During the PAPER-TRADING test phase auth is intentionally off (no real money
    # moves), so don't block boot — just warn loudly. Hard-fail ONLY the dangerous
    # misconfig: auth enabled but no token. Re-enable APP_TOKEN_AUTH_ENABLED before
    # going LIVE (the in-app unlock modal then handles the token).
    if settings.APP_ENV == "production":
        if not settings.APP_TOKEN_AUTH_ENABLED:
            logger.critical(
                "SECURITY: API auth is DISABLED in production — /api accepts UNAUTHENTICATED "
                "requests. OK for paper-trading testing; MUST set APP_TOKEN_AUTH_ENABLED=true "
                "before enabling LIVE trading."
            )
        elif not settings.APP_WRITE_TOKEN.strip():
            raise RuntimeError(
                "REFUSING TO BOOT: APP_TOKEN_AUTH_ENABLED is true but APP_WRITE_TOKEN is empty."
            )
        if "change-me" in settings.SECRET_KEY.lower() or settings.SECRET_KEY == "change-me-to-a-random-secret-key":
            # WARN (not fatal): a hard fail here would brick the live box until the
            # key is rotated WITH credential re-encryption (creds are Fernet-encrypted
            # under sha256(SECRET_KEY)). Rotate via the supervised migration instead.
            logger.critical(
                "SECURITY: SECRET_KEY is a weak/default value in production — broker "
                "credentials at rest are weakly encrypted. Rotate SECRET_KEY (with "
                "credential re-encryption) ASAP."
            )

    research_sync_task: asyncio.Task | None = None
    loop_lag_task: asyncio.Task | None = None

    # Event-loop lag monitor (WS-0.2): pure observation, started first so it
    # captures startup contention too. Cancelled on shutdown below.
    try:
        loop_lag_task = asyncio.create_task(
            app_metrics.run_loop_lag_monitor(),
            name="event-loop-lag-monitor",
        )
        logger.info("✓ Event-loop lag monitor started")
    except Exception as e:
        logger.warning(f"Event-loop lag monitor start skipped: {e}")

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

    # Terminal-grade low-latency quote fan-out: the quote_bus taps the same tick
    # feed and coalesces it into ~150ms multi-symbol frames on Redis 'quotes:bus'
    # for the event-driven /ws/quotes endpoint (no snapshot-timer latency floor).
    from market_data.quote_bus import quote_bus
    await quote_bus.start()

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
        # Sector-network coverage: stream the NSE sector indices so the quote_bus
        # tape (and terminal / sector views) carry them live. Trivial RAM (<10 MB),
        # ~7 extra symbols vs the 5000 WS cap. Pinned via add_subscriptions →
        # survives an auth/session resync.
        try:
            await market_data_router.add_subscriptions(list(SECTOR_INDEX_APP_SYMBOLS))
        except Exception as exc:
            logger.warning(f"Sector index subscription failed: {exc}")
        # Phase-2 (flag-gated, default OFF): the full ~206 F&O sector constituents.
        # Heavy on backend RSS (live_candle_store per-symbol OHLC) — only enable
        # after v1 is retired and RSS headroom is confirmed on the t3.medium.
        if getattr(settings, "STOCK_WS_SUBSCRIPTIONS_ENABLED", False):
            try:
                from analytics.sector import SECTOR_STOCKS
                stock_syms = [f"NSE:{s}-EQ" for s in sorted(SECTOR_STOCKS)]
                added = await market_data_router.add_subscriptions(stock_syms)
                logger.info(f"Stock WS subscriptions enabled: +{added} sector constituents")
            except Exception as exc:
                logger.warning(f"Stock WS subscription failed: {exc}")
    else:
        await market_data_router.stop_mock_feed()
        await market_data_router.unsubscribe()

    # WS-0.5c — bound these starts so a hang can't block the process from ever
    # becoming healthy. They normally just spawn background loops and return fast.
    try:
        await asyncio.wait_for(paper_strategy_agent.start(), timeout=60.0)
    except Exception as e:
        logger.warning(f"NSE paper strategy agent start degraded: {e}")
    try:
        await asyncio.wait_for(commodity_strategy_agent.start(), timeout=60.0)
    except Exception as e:
        logger.warning(f"Commodity strategy agent start degraded: {e}")
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

    # P2 streaming: option WS subscription manager. Computes the ATM
    # option symbol set every 5 min and reconciles against data_router's
    # current subscriptions. DRY-RUN by default — set
    # OPTION_WS_SUBSCRIPTIONS_ENABLED=true to actually subscribe.
    try:
        from market_data.option_subscription_manager import run_subscription_loop, _is_enabled
        option_ws_task = asyncio.create_task(
            run_subscription_loop(),
            name="option-ws-subscription-manager",
        )
        mode = "ACTIVE" if _is_enabled() else "DRY-RUN"
        logger.info(f"✓ Option WS subscription manager started ({mode})")
    except Exception as e:
        logger.warning(f"Option WS subscription manager start skipped: {e}")
        option_ws_task = None

    # Keep open-position option legs subscribed so the dashboard's P&L marks
    # stream per-tick instead of per-60s-scan. Registers each leg's feed
    # symbol with market_data.live_marks for the WS overlay.
    try:
        from market_data.option_subscription_manager import run_held_position_subscription_loop
        held_position_ws_task = asyncio.create_task(
            run_held_position_subscription_loop(),
            name="held-position-subscription-refresh",
        )
        logger.info("✓ Held-position subscription refresh started")
    except Exception as e:
        logger.warning(f"Held-position subscription refresh start skipped: {e}")
        held_position_ws_task = None

    # MCX futures have no WS feed; bridge a fast REST LTP poll into the tick
    # hot-cache so commodity position marks stream at ~12s instead of 60s.
    try:
        from market_data.option_subscription_manager import run_commodity_mark_refresh_loop
        commodity_mark_task = asyncio.create_task(
            run_commodity_mark_refresh_loop(),
            name="commodity-mark-refresh",
        )
        logger.info("✓ Commodity mark refresh started")
    except Exception as e:
        logger.warning(f"Commodity mark refresh start skipped: {e}")
        commodity_mark_task = None

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

    # Automatic historical-data backfill: detect missing coverage vs the desk's
    # targets and pull only the gaps. Idempotent + resumable; gated OFF by default.
    auto_backfill_task = None
    if settings.AUTO_BACKFILL_ENABLED:
        async def _auto_backfill_worker() -> None:
            try:
                from data.run_auto_backfill import run_daemon_from_env

                if not settings.AUTO_BACKFILL_ON_STARTUP:
                    await asyncio.sleep(settings.AUTO_BACKFILL_POLL_MINUTES * 60)
                await run_daemon_from_env()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(f"Auto-backfill daemon stopped: {exc}")

        auto_backfill_task = asyncio.create_task(
            _auto_backfill_worker(), name="auto-backfill")
        logger.info("✓ Auto historical backfill daemon started")

    # MACD diffusion: hourly CE/PE-above-zero breadth snapshot (market sentiment),
    # seeded from option_premium_candles at startup. Lightweight; gated ON.
    macd_diffusion_task = None
    if settings.MACD_DIFFUSION_ENABLED:
        async def _macd_diffusion_worker() -> None:
            try:
                from market_data.macd_diffusion import run_daemon

                await run_daemon(
                    poll_minutes=settings.MACD_DIFFUSION_POLL_MINUTES,
                    backfill_days=settings.MACD_DIFFUSION_BACKFILL_DAYS,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(f"MACD diffusion daemon stopped: {exc}")

        macd_diffusion_task = asyncio.create_task(
            _macd_diffusion_worker(), name="macd-diffusion")
        logger.info("✓ MACD diffusion daemon started")

    # MCX auto-rollover: keep the MP+OF agent's futures on their current
    # front-month so the watchlist never tracks an expired contract.
    mcx_rollover_task = None
    if settings.MCX_ROLLOVER_ENABLED:
        async def _mcx_rollover_worker() -> None:
            try:
                from market_data.mcx_rollover import run_daemon

                await run_daemon(poll_hours=settings.MCX_ROLLOVER_POLL_HOURS)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(f"MCX rollover daemon stopped: {exc}")

        mcx_rollover_task = asyncio.create_task(
            _mcx_rollover_worker(), name="mcx-rollover")
        logger.info("✓ MCX rollover daemon started")

    # F1 feed: full-universe option-chain → 3m CE+PE OHLC (S1's headline feed).
    # Gated OFF by default; the poll self-staggers through FYERS_DATA_LIMITER.
    if settings.CHAIN_CANDLE_BUILDER_ENABLED:
        try:
            from market_data.chain_candle_builder import chain_candle_builder
            await chain_candle_builder.start()
            logger.info("✓ Chain candle builder (F1 3m CE+PE) started")
        except Exception as e:
            logger.warning(f"Chain candle builder start skipped: {e}")

    yield

    # Shutdown
    if research_sync_task is not None:
        research_sync_task.cancel()
        try:
            await research_sync_task
        except asyncio.CancelledError:
            logger.info("Embedded research sync daemon stopped")
    if auto_backfill_task is not None:
        auto_backfill_task.cancel()
        try:
            await auto_backfill_task
        except asyncio.CancelledError:
            logger.info("Auto-backfill daemon stopped")
    if macd_diffusion_task is not None:
        macd_diffusion_task.cancel()
        try:
            await macd_diffusion_task
        except asyncio.CancelledError:
            logger.info("MACD diffusion daemon stopped")
    if mcx_rollover_task is not None:
        mcx_rollover_task.cancel()
        try:
            await mcx_rollover_task
        except asyncio.CancelledError:
            logger.info("MCX rollover daemon stopped")
    if loop_lag_task is not None:
        loop_lag_task.cancel()
        try:
            await loop_lag_task
        except asyncio.CancelledError:
            pass
    # B4 (gap-audit): cancel the market-data background loops too — previously they
    # kept running after `yield`, leaking DB/Redis connections into the next boot and
    # hanging restarts. All three re-raise CancelledError, so this is clean.
    for _name, _task in (
        ("option-ws", option_ws_task),
        ("held-position-ws", held_position_ws_task),
        ("commodity-mark", commodity_mark_task),
    ):
        if _task is not None:
            _task.cancel()
            try:
                await _task
            except asyncio.CancelledError:
                pass
            except Exception as _exc:  # noqa: BLE001
                logger.debug(f"shutdown: {_name} task stop error: {_exc}")
    if settings.CHAIN_CANDLE_BUILDER_ENABLED:
        try:
            from market_data.chain_candle_builder import chain_candle_builder
            await chain_candle_builder.stop()
        except Exception:
            pass
    await market_hours_paper_supervisor.stop()
    await rl_auto_trainer.stop()
    await paper_strategy_agent.stop()
    await commodity_strategy_agent.stop()
    await live_candle_store.stop()
    from market_data.quote_bus import quote_bus
    await quote_bus.stop()
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
app.include_router(macd_refined_router.router)
app.include_router(gann_tp_delta_router.router)
app.include_router(fractal_market_profile_router.router)
app.include_router(orderflow_router.router)
app.include_router(charts_router.router)
app.include_router(system_router.router)
app.include_router(audit_router.router)
app.include_router(data_quality_router.router)
app.include_router(lane_health_router.router)
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


@app.websocket("/ws/strategy-snapshot")
async def websocket_strategy_snapshot(websocket: WebSocket):
    await ws_strategy_snapshot(websocket)


@app.websocket("/ws/quotes")
async def websocket_quotes(websocket: WebSocket):
    await ws_quotes(websocket)


@app.websocket("/ws/depth/{symbol:path}")
async def websocket_depth(websocket: WebSocket, symbol: str):
    await ws_depth(websocket, symbol)


@app.websocket("/ws/proposals")
async def websocket_proposals(websocket: WebSocket):
    await ws_proposals(websocket)


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


# ── Metrics (WS-0.2) — Prometheus text exposition; not under /api so it is not
# gated by the write-token middleware. Lock down at the reverse proxy / SG. ─────
@app.get("/metrics")
async def metrics() -> Response:
    body, content_type = app_metrics.render()
    return Response(content=body, media_type=content_type)


@app.get("/")
async def root():
    return {
        "name": "Nomad Curie",
        "description": "NSE F&O Algorithmic Trading Platform",
        "docs": "/docs",
    }
