"""WebSocket endpoint for real-time tick streaming via Redis pub/sub."""
from __future__ import annotations
import asyncio
import json

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger

from db.redis_client import get_redis


async def ws_ticks(websocket: WebSocket, symbol: str):
    """Stream real-time ticks for a symbol via Redis pub/sub."""
    await websocket.accept()
    logger.info(f"[WS] Client subscribed to ticks:{symbol}")
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


async def ws_positions(websocket: WebSocket):
    """Stream real-time position P&L updates."""
    await websocket.accept()
    logger.info("[WS] Client subscribed to positions")
    try:
        while True:
            from api.routers.trading import _get_or_create_paper_session
            _, portfolio = _get_or_create_paper_session()
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


async def ws_proposals(websocket: WebSocket):
    """Stream real-time agent proposals via Redis pub/sub."""
    await websocket.accept()
    logger.info("[WS] Client subscribed to proposals")
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
