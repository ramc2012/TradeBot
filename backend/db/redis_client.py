"""Redis async client singleton."""
from __future__ import annotations
import redis.asyncio as aioredis
from core.config import settings

_redis: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        # Bound the pool (was unbounded → grew to Redis maxclients under tick-cache
        # SET bursts and broke pub/sub). Idle connections are reused; this is headroom.
        _redis = await aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            max_connections=settings.REDIS_MAX_CONNECTIONS,
        )
    return _redis


async def close_redis():
    global _redis
    if _redis:
        await _redis.close()
        _redis = None
