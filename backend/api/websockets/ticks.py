"""WebSocket endpoint for real-time tick streaming via Redis pub/sub."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic

from fastapi.encoders import jsonable_encoder
from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger
from starlette.websockets import WebSocketState

from api.routers.auth import authenticate_websocket_client
from db.redis_client import get_redis


@dataclass
class _SnapshotCacheEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    payload: object | None = None
    encoded: str | None = None
    expires_at: float = 0.0


_snapshot_cache_lock = asyncio.Lock()
_snapshot_cache: dict[str, _SnapshotCacheEntry] = {}


def _is_socket_closed_error(exc: Exception) -> bool:
    return isinstance(exc, RuntimeError) and "Cannot call \"send\" once a close message has been sent" in str(exc)


def _socket_is_connected(websocket: WebSocket) -> bool:
    return (
        websocket.client_state == WebSocketState.CONNECTED
        and websocket.application_state == WebSocketState.CONNECTED
    )


async def _get_snapshot_cache_entry(channel: str) -> _SnapshotCacheEntry:
    async with _snapshot_cache_lock:
        entry = _snapshot_cache.get(channel)
        if entry is None:
            entry = _SnapshotCacheEntry()
            _snapshot_cache[channel] = entry
        return entry


async def _get_snapshot_payload(
    *,
    channel: str,
    payload_factory,
    cache_ttl_seconds: float,
) -> tuple[object, str]:
    entry = await _get_snapshot_cache_entry(channel)
    now = monotonic()
    if entry.payload is not None and entry.encoded is not None and entry.expires_at > now:
        return entry.payload, entry.encoded

    async with entry.lock:
        now = monotonic()
        if entry.payload is not None and entry.encoded is not None and entry.expires_at > now:
            return entry.payload, entry.encoded

        try:
            payload = await payload_factory()
            encoded = json.dumps(jsonable_encoder(payload), separators=(",", ":"), sort_keys=True)
            entry.payload = payload
            entry.encoded = encoded
            entry.expires_at = now + max(cache_ttl_seconds, 0.5)
            return payload, encoded
        except Exception:
            if entry.payload is not None and entry.encoded is not None:
                entry.expires_at = now + 1.0
                logger.warning(f"[WS] Reusing stale snapshot for {channel} after refresh failure")
                return entry.payload, entry.encoded
            raise


async def _accept_authenticated_socket(websocket: WebSocket, channel: str) -> dict:
    claims = authenticate_websocket_client(websocket)
    await websocket.accept()
    logger.info(f"[WS] Authenticated client subscribed to {channel}")
    return claims


async def ws_ticks(websocket: WebSocket, symbol: str):
    """Stream real-time ticks for a symbol via Redis pub/sub."""
    await _accept_authenticated_socket(websocket, f"ticks:{symbol}")
    try:
        redis = await get_redis()
        pubsub = redis.pubsub()
        await pubsub.subscribe(f"ticks:{symbol}")
    except Exception as exc:
        logger.warning(f"[WS] Redis tick stream unavailable for {symbol}; using snapshot fallback: {exc}")
        await _stream_tick_snapshot_fallback(websocket, symbol)
        return

    last_payload: str | None = None
    try:
        while _socket_is_connected(websocket):
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message and message["type"] == "message":
                data = message["data"]
                payload = data if isinstance(data, str) else data.decode()
            else:
                payload = await _tick_snapshot_payload(symbol)
            if payload != last_payload:
                await websocket.send_text(payload)
                last_payload = payload
    except WebSocketDisconnect:
        logger.info(f"[WS] Client disconnected from ticks:{symbol}")
    except Exception as e:
        logger.error(f"[WS] Error in ticks handler: {e}")
    finally:
        try:
            await pubsub.unsubscribe(f"ticks:{symbol}")
        except Exception:
            pass


async def _tick_snapshot_payload(symbol: str) -> str:
    from api.routers.market import _latest_index_tick_snapshot
    from market_data import data_router

    snapshot = await _latest_index_tick_snapshot(
        symbol,
        data_router.get_ltp(symbol),
        source="data_router",
    )
    return json.dumps(
        {
            "symbol": symbol,
            "ltp": snapshot.ltp,
            "open": snapshot.open,
            "high": snapshot.high,
            "low": snapshot.low,
            "close": snapshot.close,
            "volume": snapshot.volume,
            "oi": snapshot.oi,
            "timestamp": snapshot.timestamp or datetime.now(timezone.utc).isoformat(),
            "source": snapshot.source,
            "stale": snapshot.stale,
            "stale_seconds": snapshot.stale_seconds,
        },
        separators=(",", ":"),
    )


async def _stream_tick_snapshot_fallback(websocket: WebSocket, symbol: str) -> None:
    """Keep ticker clients fed when Redis pub/sub is unavailable."""
    last_payload: str | None = None
    try:
        while True:
            payload = await _tick_snapshot_payload(symbol)
            if not _socket_is_connected(websocket):
                break
            if payload != last_payload:
                await websocket.send_text(payload)
                last_payload = payload
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        logger.info(f"[WS] Client disconnected from fallback ticks:{symbol}")
    except Exception as exc:
        if not _is_socket_closed_error(exc):
            logger.error(f"[WS] Fallback tick stream failed for {symbol}: {exc}")


async def _stream_snapshot(
    websocket: WebSocket,
    *,
    channel: str,
    interval_seconds: float,
    payload_factory,
    cache_ttl_seconds: float | None = None,
):
    """Push snapshot payloads only when the encoded value changes."""
    await _accept_authenticated_socket(websocket, channel)
    last_payload: str | None = None
    effective_cache_ttl = interval_seconds if cache_ttl_seconds is None else cache_ttl_seconds

    try:
        while True:
            try:
                _, encoded = await _get_snapshot_payload(
                    channel=channel,
                    payload_factory=payload_factory,
                    cache_ttl_seconds=effective_cache_ttl,
                )
                if encoded != last_payload:
                    if not _socket_is_connected(websocket):
                        break
                    await websocket.send_text(encoded)
                    last_payload = encoded
            except WebSocketDisconnect:
                logger.info(f"[WS] Client disconnected from {channel}")
                break
            except Exception as e:
                if _is_socket_closed_error(e):
                    logger.debug(f"[WS] Snapshot channel {channel} closed during send")
                    break
                logger.error(f"[WS] Error refreshing {channel}: {e}")
            await asyncio.sleep(interval_seconds)
    except WebSocketDisconnect:
        logger.info(f"[WS] Client disconnected from {channel}")
    except Exception as e:
        logger.error(f"[WS] Error in {channel}: {e}")


async def ws_positions(websocket: WebSocket):
    """Stream real-time position P&L updates."""
    await _accept_authenticated_socket(websocket, "positions")
    try:
        while True:
            from api.routers.trading import _get_or_create_paper_session
            _, _, portfolio = await _get_or_create_paper_session()
            data = json.dumps({
                "positions": portfolio.get_positions_list(),
                "summary": portfolio.get_summary(),
            })
            await websocket.send_text(data)
            await asyncio.sleep(1)  # push every second
    except WebSocketDisconnect:
        logger.info("[WS] Client disconnected from positions")
    except Exception as e:
        logger.error(f"[WS] Positions error: {e}")


async def ws_layout(websocket: WebSocket):
    """Stream broker status plus portfolio summary for the app shell."""

    async def payload_factory():
        from api.routers.auth import broker_status
        from api.routers.trading import get_mode, portfolio_summary

        return {
            "broker_status": await broker_status(),
            "portfolio_summary": await portfolio_summary(),
            "trading_mode": await get_mode(),
        }

    await _stream_snapshot(
        websocket,
        channel="layout",
        interval_seconds=5.0,
        payload_factory=payload_factory,
    )


async def ws_system_overview(websocket: WebSocket):
    """Stream homepage system overview snapshots."""

    async def payload_factory():
        from api.routers.system import system_overview

        return await system_overview()

    await _stream_snapshot(
        websocket,
        channel="system_overview",
        interval_seconds=10.0,
        payload_factory=payload_factory,
    )


async def ws_system_health(websocket: WebSocket):
    """Stream health page payloads."""

    async def payload_factory():
        from api.routers.system import system_health

        return await system_health()

    await _stream_snapshot(
        websocket,
        channel="system_health",
        interval_seconds=15.0,
        payload_factory=payload_factory,
    )


async def ws_strategy_overview(websocket: WebSocket):
    """Stream the strategy supervisor page snapshot."""

    async def payload_factory():
        from api.routers.auth import broker_status
        from api.routers.strategy import (
            get_agent_comments,
            get_data_status,
            get_open_signals,
            get_portfolio_stats,
        )
        from api.routers.trading import strategy_agent_status

        return {
            "agent_status": await strategy_agent_status(),
            "open_signals": await get_open_signals(),
            "comments": await get_agent_comments(),
            "brokers": await broker_status(),
            "pipeline": await get_data_status(),
            "live_portfolio": await get_portfolio_stats(),
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
        }

    await _stream_snapshot(
        websocket,
        channel="strategy_overview",
        interval_seconds=5.0,
        payload_factory=payload_factory,
    )


async def ws_strategy_dashboard(websocket: WebSocket):
    """Stream the NSE strategy execution desk snapshot."""

    async def payload_factory():
        from api.routers.trading import (
            get_kill_switch_state,
            get_orders,
            risk_status,
            strategy_agent_status,
            strategy_equity_history,
        )

        return {
            "agent_status": await strategy_agent_status(),
            "kill_switch_state": await get_kill_switch_state(),
            "orders": await get_orders(),
            "risk_status": await risk_status(),
            "equity_curves": await strategy_equity_history(),
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
        }

    await _stream_snapshot(
        websocket,
        channel="strategy_dashboard",
        interval_seconds=2.0,
        payload_factory=payload_factory,
    )


async def ws_positions_overview(websocket: WebSocket):
    """Stream the combined positions page snapshot."""

    async def payload_factory():
        from api.routers.commodity import commodity_strategy_status
        from api.routers.auction_intelligence import paper_positions as auction_paper_positions
        from api.routers.directional_options import paper_positions as directional_paper_positions
        from api.routers.fractal_market_profile import fractal_market_profile_paper_positions
        from api.routers.gann_tp_delta import paper_agent_status as gann_paper_agent_status
        from api.routers.trading import get_positions, strategy_agent_status

        errors: dict[str, str] = {}

        async def settle(key: str, task):
            try:
                return await task
            except Exception as exc:  # pragma: no cover - defensive stream isolation
                errors[key] = str(exc)
                logger.warning(f"[WS] positions overview source failed: {key}: {exc}")
                return None

        (
            manual_positions,
            nse_status,
            commodity_status,
            directional_positions,
            gann_status,
            auction_positions,
            fractal_positions,
        ) = await asyncio.gather(
            settle("manual", get_positions()),
            settle("nse", strategy_agent_status()),
            settle("commodity", commodity_strategy_status()),
            settle("directional", directional_paper_positions(symbol=None, status="all", limit=100)),
            settle("gann", gann_paper_agent_status(limit=100)),
            settle("auction", auction_paper_positions(symbol=None, status="all", limit=100)),
            settle("fractal", fractal_market_profile_paper_positions(symbol=None, status="all", limit=100)),
        )

        return {
            "manual": manual_positions,
            "nse": nse_status,
            "strategy": nse_status,
            "commodity": commodity_status,
            "directional": directional_positions,
            "gann": gann_status,
            "auction": auction_positions,
            "fractal": fractal_positions,
            "errors": errors,
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
        }

    await _stream_snapshot(
        websocket,
        channel="positions_overview",
        interval_seconds=2.0,
        payload_factory=payload_factory,
    )


async def ws_commodity_overview(websocket: WebSocket):
    """Stream the commodity execution desk snapshot."""

    async def payload_factory():
        from api.routers.commodity import (
            commodity_kill_switch_state,
            commodity_orders,
            commodity_positions,
            commodity_reports,
            commodity_strategy_status,
        )

        return {
            "status": await commodity_strategy_status(),
            "kill_switch_state": await commodity_kill_switch_state(),
            "orders": await commodity_orders(limit=40),
            "positions": await commodity_positions(),
            "reports": await commodity_reports(limit=24),
            # Heartbeat. _stream_snapshot only pushes when the encoded payload
            # changes; without a moving field, the channel goes silent between
            # the agent's 60s scans even though the client is healthy and
            # expects a live feed. The timestamp guarantees a push every
            # interval so the UI's "last update" clock keeps ticking and a
            # dropped socket is detectable. (Per-tick position marks land in
            # Stage 2 via the live-mark overlay.)
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
        }

    await _stream_snapshot(
        websocket,
        channel="commodity_overview",
        interval_seconds=2.0,
        payload_factory=payload_factory,
    )


async def ws_commodity_watchlist(websocket: WebSocket):
    """Stream the commodity watchlist setup snapshot."""

    async def payload_factory():
        from api.routers.commodity import (
            commodity_watchlist_snapshot,
        )

        # The commodity strategy agent already refreshes futures/options data
        # on its scan cadence. Do not make every websocket client force a live
        # option-chain refresh; that fans out into broker REST calls and trips
        # Fyers 429 limits, leaving rows stale for everyone.
        return await commodity_watchlist_snapshot(live_refresh=False)

    await _stream_snapshot(
        websocket,
        channel="commodity_watchlist",
        interval_seconds=8.0,
        payload_factory=payload_factory,
    )


async def ws_market_watchlist(websocket: WebSocket):
    """Stream the market ATM watchlist snapshot for a selected expiry."""

    requested_expiry = str(websocket.query_params.get("expiry") or "").strip() or None

    async def payload_factory():
        from api.routers.market import (
            get_atm_watchlist,
            get_atm_watchlist_expiries,
        )

        expiry_payload = await get_atm_watchlist_expiries(
            expiry=requested_expiry,
            live_refresh=True,
        )
        effective_expiry = requested_expiry or expiry_payload.get("default_expiry")
        return {
            "expiry_catalog": expiry_payload,
            "watchlist": await get_atm_watchlist(
                expiry=effective_expiry,
                symbols=None,
                live_refresh=True,
            ),
        }

    await _stream_snapshot(
        websocket,
        channel=f"market_watchlist:{requested_expiry or 'default'}",
        interval_seconds=8.0,
        payload_factory=payload_factory,
    )


async def ws_market_option_chain(websocket: WebSocket, symbol: str):
    """Stream the selected market option chain."""

    requested_expiry = str(websocket.query_params.get("expiry") or "").strip() or None

    async def payload_factory():
        from api.routers.market import get_option_chain

        return await get_option_chain(symbol=symbol, expiry=requested_expiry)

    await _stream_snapshot(
        websocket,
        channel=f"market_option_chain:{symbol}:{requested_expiry or 'default'}",
        interval_seconds=5.0,
        payload_factory=payload_factory,
    )


async def ws_fractal_market_profile(websocket: WebSocket, symbol: str):
    """Stream the Fractal Market Profile desk snapshot for one symbol."""

    async def payload_factory():
        from api.routers.fractal_market_profile import fractal_market_profile_live_snapshot

        return await fractal_market_profile_live_snapshot(symbol=symbol)

    await _stream_snapshot(
        websocket,
        channel=f"fractal_market_profile:{symbol}",
        interval_seconds=5.0,
        payload_factory=payload_factory,
    )


async def ws_proposals(websocket: WebSocket):
    """Stream real-time agent proposals via Redis pub/sub."""
    await _accept_authenticated_socket(websocket, "proposals")
    redis = await get_redis()
    pubsub = redis.pubsub()
    await pubsub.subscribe("proposals")
    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                data = message["data"]
                await websocket.send_text(data if isinstance(data, str) else data.decode())
    except WebSocketDisconnect:
        logger.info("[WS] Client disconnected from proposals")
    except Exception as e:
        logger.error(f"[WS] Proposals error: {e}")
    finally:
        await pubsub.unsubscribe("proposals")
