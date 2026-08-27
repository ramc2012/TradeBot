"""Nomad Curie — FastAPI application entry point."""
from __future__ import annotations
import asyncio
import faulthandler
import hmac
import signal
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

from core import metrics as app_metrics
from core.config import settings
from core.laneset import boots_core, boots_strategies, normalized_laneset
from core.market_hours_paper_supervisor import market_hours_paper_supervisor
from core.paper_bootstrap import bootstrap_paper_trading_runtime
from db.redis_client import get_redis, close_redis
from api.routers import auth, trading, market, analytics, agent, commodity, macro_research, rag, sector_interaction, cbe, backtester as backtester_router
from api.routers import fo_data as fo_data_router
from api.routers import analysis as analysis_router
from api.routers import strategy as strategy_router
from api.routers import auction_intelligence as auction_intelligence_router
from api.routers import institutional_convergence as institutional_convergence_router
from api.routers import directional_options as directional_options_router
from api.routers import gann_tp_delta as gann_tp_delta_router
from api.routers import fractal_market_profile as fractal_market_profile_router
from api.routers import orderflow as orderflow_router
from api.routers import charts as charts_router
from api.routers import system as system_router
from api.routers import audit as audit_router
from api.routers import data_quality as data_quality_router
from api.routers import lane_health as lane_health_router
from api.routers import notifications as notifications_router
from api.routers import macd_refined as macd_refined_router
from api.routers import vanguard as vanguard_router
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

try:
    faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)
except Exception:
    pass


OAUTH_CALLBACK_PATHS = {"/api/auth/fyers/callback", "/api/auth/upstox/callback"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle.

    Phase-1 process split (2026-07-18): boot blocks are gated by LANESET via
    core.laneset.boots_core()/boots_strategies(). LANESET=all (the default)
    makes EVERY guard True — the executed call sequence is byte-identical to
    the pre-split single-process boot.
    """
    logger.info("═══ Nomad Curie starting up ═══")
    _laneset = normalized_laneset()
    if _laneset != "all":
        logger.info(
            f"LANESET={_laneset}: booting the "
            f"{'data/API (core)' if _laneset == 'core' else 'strategy'} plane only"
        )

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
    sdk_log_guard_task: asyncio.Task | None = None

    # Third-party SDK log cap (2026-07-27). fyers_apiv3 installs its own,
    # never-rotated FileHandler; with log_path="" it wrote into the /app bind
    # mount and reached 1.33 GB on a host volume that is 98% full. The guard
    # truncates in place (O_APPEND-verified) keeping the newest few MB.
    try:
        from core.sdk_log_guard import run_sdk_log_guard

        sdk_log_guard_task = asyncio.create_task(
            run_sdk_log_guard(), name="sdk-log-guard"
        )
        logger.info("✓ SDK log guard started")
    except Exception as e:
        logger.warning(f"SDK log guard start skipped: {e}")

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

    # ── CORE plane: tick construction + the ONE broker WS ────────────────────
    # (LANESET=all keeps every guard True — byte-identical single-process boot.)
    if boots_core():
        # Catalog integrity invariant: no two F&O underlyings may share a
        # spot_instrument_key. They key the same row in underlying_spot_candles
        # (PK = instrument_key/interval/time), so a collision makes the two names
        # silently overwrite each other bar-for-bar (the M&M/MARUTI ISIN bug).
        # Non-fatal by design — it ERRORs per collision so it cannot pass unseen,
        # but a bad catalog row must never take the whole backend dark.
        try:
            from db.database import AsyncSessionLocal as _CatalogSession
            from market_data.catalog_integrity import assert_unique_spot_keys
            async with _CatalogSession() as _session:
                await assert_unique_spot_keys(_session)
        except Exception as e:
            logger.warning(f"Catalog integrity check skipped: {e}")

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
    # Strategy plane: data_router.subscribe() is a laneset-gated no-op, and this
    # whole block is skipped — the core plane owns the ONE Fyers WS.
    if adapter and boots_core():
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
        # Directional NIFTY-50 universe (2026-07-17): stream the 50 stock spots
        # so live_candle_store writes their 1m/3m bars — the directional stock
        # readiness gate requires fresh underlying_spot_candles, and the 1m spot
        # collector otherwise only covers MI ATM-rotation names. 50 symbols on a
        # 5000-symbol WS cap, zero broker REST load. Pinned sticky via
        # add_subscriptions so an auth resync doesn't drop them.
        if getattr(settings, "DIRECTIONAL_INCLUDE_STOCK_UNIVERSE", False):
            try:
                from directional_options.config import DIRECTIONAL_STOCK_UNIVERSE
                directional_syms = [f"NSE:{s}-EQ" for s in DIRECTIONAL_STOCK_UNIVERSE]
                added = await market_data_router.add_subscriptions(directional_syms)
                logger.info(
                    f"Directional stock-universe WS subscriptions: +{added} NIFTY-50 spots"
                )
            except Exception as exc:
                logger.warning(f"Directional stock WS subscription failed: {exc}")
    else:
        await market_data_router.stop_mock_feed()
        await market_data_router.unsubscribe()

    # ── STRATEGY plane: own-loop agents + RL trainer ─────────────────────────
    if boots_strategies():
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

    # ── CORE plane: WS subscription managers + data-maintenance daemons ─────
    option_ws_task = None
    held_position_ws_task = None
    held_position_candle_task = None
    commodity_mark_task = None
    if boots_core():
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

        # Premium CANDLES for held legs. Subscribing a leg (above) only gives
        # it live ticks; its option_premium_candles series was still collected
        # by the S1 scan, i.e. only while the leg was the CURRENT ATM strike.
        # Once spot drifted the held leg stopped being collected entirely
        # (2026-08-03: 16 of 21 open positions had marks up to 6 days old and
        # ZERO candles for their expiry). Held positions are maintained for
        # candle data alongside the ATM options, for as long as they are open.
        try:
            from market_data.held_position_candles import run_held_position_candle_loop
            held_position_candle_task = asyncio.create_task(
                run_held_position_candle_loop(),
                name="held-position-candle-refresh",
            )
            logger.info("✓ Held-position candle refresh started")
        except Exception as e:
            logger.warning(f"Held-position candle refresh start skipped: {e}")
            held_position_candle_task = None

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

    if settings.RESEARCH_SYNC_EMBEDDED_ENABLED and boots_core():
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

    # MACD diffusion: hourly CE/PE-above-zero breadth snapshot (market sentiment),
    # seeded from option_premium_candles at startup. Lightweight; gated ON.
    macd_diffusion_task = None
    if settings.MACD_DIFFUSION_ENABLED and boots_core():
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

    # Greeks enrichment: stamp real broker greeks (from option_chain_snapshots)
    # onto greeks-null index option candles — restores what the dead 2026-06-23
    # Fyers greeks writer filled, at zero broker cost. Lightweight; gated ON.
    greeks_enrich_task = None
    if settings.GREEKS_ENRICHMENT_ENABLED and boots_core():
        async def _greeks_enrichment_worker() -> None:
            try:
                from market_data.greeks_enrichment import run_daemon as run_greeks_enrichment

                await run_greeks_enrichment(
                    poll_minutes=settings.GREEKS_ENRICHMENT_POLL_MINUTES,
                    lookback_days=settings.GREEKS_ENRICHMENT_LOOKBACK_DAYS,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(f"Greeks enrichment daemon stopped: {exc}")

        greeks_enrich_task = asyncio.create_task(
            _greeks_enrichment_worker(), name="greeks-enrichment")
        logger.info("✓ Greeks enrichment daemon started")

    # F1 feed: full-universe option-chain → 3m CE+PE OHLC (S1's headline feed).
    # Gated OFF by default; the poll self-staggers through FYERS_DATA_LIMITER.
    if settings.CHAIN_CANDLE_BUILDER_ENABLED and boots_core():
        try:
            from market_data.chain_candle_builder import chain_candle_builder
            await chain_candle_builder.start()
            logger.info("✓ Chain candle builder (F1 3m CE+PE) started")
        except Exception as e:
            logger.warning(f"Chain candle builder start skipped: {e}")

    # Upstox chain builder: the LIVE equity-iv feed. Replaces the dead
    # fyers_chain/upstox_expired paths (see the module docstring). Gated OFF by
    # default; the sweep self-staggers through the shared Upstox limiter.
    if settings.UPSTOX_CHAIN_BUILDER_ENABLED and boots_core():
        try:
            from market_data.upstox_chain_builder import upstox_chain_builder
            await upstox_chain_builder.start()
            logger.info("✓ Upstox chain builder (30m CE+PE + greeks) started")
        except Exception as e:
            logger.warning(f"Upstox chain builder start skipped: {e}")

    yield

    # Shutdown
    if research_sync_task is not None:
        research_sync_task.cancel()
        try:
            await research_sync_task
        except asyncio.CancelledError:
            logger.info("Embedded research sync daemon stopped")
    if macd_diffusion_task is not None:
        macd_diffusion_task.cancel()
        try:
            await macd_diffusion_task
        except asyncio.CancelledError:
            logger.info("MACD diffusion daemon stopped")
    if greeks_enrich_task is not None:
        greeks_enrich_task.cancel()
        try:
            await greeks_enrich_task
        except asyncio.CancelledError:
            logger.info("Greeks enrichment daemon stopped")
    if loop_lag_task is not None:
        loop_lag_task.cancel()
        try:
            await loop_lag_task
        except asyncio.CancelledError:
            pass
    if sdk_log_guard_task is not None:
        sdk_log_guard_task.cancel()
        try:
            await sdk_log_guard_task
        except asyncio.CancelledError:
            pass
    # B4 (gap-audit): cancel the market-data background loops too — previously they
    # kept running after `yield`, leaking DB/Redis connections into the next boot and
    # hanging restarts. All three re-raise CancelledError, so this is clean.
    for _name, _task in (
        ("option-ws", option_ws_task),
        ("held-position-ws", held_position_ws_task),
        ("held-position-candles", held_position_candle_task),
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
    if settings.CHAIN_CANDLE_BUILDER_ENABLED and boots_core():
        try:
            from market_data.chain_candle_builder import chain_candle_builder
            await chain_candle_builder.stop()
        except Exception:
            pass
    if settings.UPSTOX_CHAIN_BUILDER_ENABLED and boots_core():
        try:
            from market_data.upstox_chain_builder import upstox_chain_builder
            await upstox_chain_builder.stop()
        except Exception:
            pass
    await market_hours_paper_supervisor.stop()
    if boots_strategies():
        await rl_auto_trainer.stop()
        await paper_strategy_agent.stop()
        await commodity_strategy_agent.stop()
    if boots_core():
        await live_candle_store.stop()
        from market_data.quote_bus import quote_bus
        await quote_bus.stop()
        await market_data_router.stop_mock_feed()
    await close_redis()
    await market_data_router.unsubscribe()
    try:
        from brokers.http_client import aclose_all_shared_clients
        await aclose_all_shared_clients()
    except Exception:  # noqa: BLE001
        pass
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

# ── Response compression ─────────────────────────────────────────────────────
# The API serves large JSON status/book payloads (a books response measured
# 3.17 MB, automation-status 768 KB - 1.46 MB) and previously never negotiated
# compression: identical bytes and no content-encoding header even when the
# client sent `Accept-Encoding: gzip`. These bodies compress 6.6-13x.
#
# Starlette applies middleware in REVERSE registration order, so registering
# GZipMiddleware AFTER CORSMiddleware puts it OUTSIDE CORS: the CORS headers
# are added first and then the whole response is compressed, which is the
# correct nesting (compressing after CORS, never compressing the preflight
# handshake away). HTTP-only — WebSocket scopes pass through untouched, so
# /ws/* streams are unaffected. Only compresses when the client advertises
# gzip, so non-gzip clients see byte-identical responses to before.
app.add_middleware(GZipMiddleware, minimum_size=1024)

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
app.include_router(institutional_convergence_router.router)
app.include_router(directional_options_router.router)
app.include_router(gann_tp_delta_router.router)
app.include_router(fractal_market_profile_router.router)
app.include_router(orderflow_router.router)
app.include_router(charts_router.router)
app.include_router(system_router.router)
app.include_router(audit_router.router)
app.include_router(data_quality_router.router)
app.include_router(lane_health_router.router)
app.include_router(notifications_router.router)
app.include_router(macd_refined_router.router)
app.include_router(vanguard_router.router)


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
