"""WebSocket endpoint for real-time tick streaming via Redis pub/sub."""
from __future__ import annotations
import asyncio
import json

from fastapi.encoders import jsonable_encoder
from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger

from api.routers.auth import authenticate_websocket_client
from db.redis_client import get_redis


async def _accept_authenticated_socket(websocket: WebSocket, channel: str) -> dict:
    claims = authenticate_websocket_client(websocket)
    await websocket.accept()
    logger.info(f"[WS] Authenticated client subscribed to {channel}")
    return claims


async def ws_ticks(websocket: WebSocket, symbol: str):
    """Stream real-time ticks for a symbol via Redis pub/sub."""
    await _accept_authenticated_socket(websocket, f"ticks:{symbol}")
    redis = await get_redis()
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"ticks:{symbol}")
    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                data = message["data"]
                await websocket.send_text(data if isinstance(data, str) else data.decode())
    except WebSocketDisconnect:
        logger.info(f"[WS] Client disconnected from ticks:{symbol}")
    except Exception as e:
        logger.error(f"[WS] Error in ticks handler: {e}")
    finally:
        await pubsub.unsubscribe(f"ticks:{symbol}")


async def _stream_snapshot(
    websocket: WebSocket,
    *,
    channel: str,
    interval_seconds: float,
    payload_factory,
):
    """Push snapshot payloads only when the encoded value changes."""
    await _accept_authenticated_socket(websocket, channel)
    last_payload: str | None = None

    try:
        while True:
            payload = await payload_factory()
            encoded = json.dumps(jsonable_encoder(payload), separators=(",", ":"), sort_keys=True)
            if encoded != last_payload:
                await websocket.send_text(encoded)
                last_payload = encoded
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
        from api.routers.trading import portfolio_summary

        return {
            "broker_status": await broker_status(),
            "portfolio_summary": await portfolio_summary(),
        }

    await _stream_snapshot(
        websocket,
        channel="layout",
        interval_seconds=2.0,
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
        interval_seconds=5.0,
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
        interval_seconds=5.0,
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
        }

    await _stream_snapshot(
        websocket,
        channel="strategy_overview",
        interval_seconds=3.0,
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
        from api.routers.trading import get_positions, strategy_agent_status

        return {
            "manual": await get_positions(),
            "strategy": await strategy_agent_status(),
            "commodity": await commodity_strategy_status(),
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
        }

    await _stream_snapshot(
        websocket,
        channel="commodity_overview",
        interval_seconds=2.0,
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
