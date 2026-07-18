"""Redis async client singletons — bounded command pool + isolated pub/sub pool.

2026-07-18 (Redis P0): the old singleton was ONE ``from_url(max_connections=1000)``
client serving BOTH commands and pub/sub. Under event-loop stalls, per-tick
publish tasks piled up and demanded hundreds of connections at once; the plain
``ConnectionPool`` *raises* "Too many connections" at the ceiling instead of
waiting (5662 errors/24h on 07-17), and pub/sub subscribers held connections
from the very same pool, so WS subscribers and tick publishes starved each other.

Fix:
  * Commands ride a bounded ``BlockingConnectionPool`` — at the cap, callers
    WAIT (up to ``REDIS_POOL_ACQUIRE_TIMEOUT_SECONDS``) for a free connection
    instead of erroring. Demand spikes now queue briefly rather than fail.
  * Pub/sub gets its OWN bounded ``BlockingConnectionPool`` via
    :func:`get_redis_pubsub`, so long-lived subscriber connections (one per WS
    client) can never starve command/publish traffic — and vice versa.
  * Both pools are instrumented (wait + timeout counters) and exposed through
    :func:`redis_pool_stats` for the /api/system/pools telemetry endpoint.
"""
from __future__ import annotations

import redis.asyncio as aioredis

from core.config import settings

_redis: aioredis.Redis | None = None
_redis_pubsub: aioredis.Redis | None = None


class InstrumentedBlockingPool(aioredis.BlockingConnectionPool):
    """BlockingConnectionPool that counts contention events.

    ``acquire_waits``    — acquisitions that found the pool fully checked out
                           and had to wait for a release.
    ``acquire_timeouts`` — acquisitions that waited the full timeout and raised
                           (the old hard-failure mode; should stay ~0).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.acquire_waits = 0
        self.acquire_timeouts = 0

    async def get_connection(self, command_name, *keys, **options):
        if not self.can_get_connection():
            self.acquire_waits += 1
        try:
            return await super().get_connection(command_name, *keys, **options)
        except aioredis.ConnectionError as exc:
            # The blocking pool signals acquisition timeout with this exact
            # message; real connect failures re-raise uncounted.
            if "No connection available" in str(exc):
                self.acquire_timeouts += 1
            raise

    def stats(self) -> dict:
        in_use = len(self._in_use_connections)
        return {
            "max_connections": self.max_connections,
            "in_use": in_use,
            "available": len(self._available_connections),
            "created": in_use + len(self._available_connections),
            "acquire_waits": self.acquire_waits,
            "acquire_timeouts": self.acquire_timeouts,
            "acquire_timeout_seconds": self.timeout,
        }


def _build_client(max_connections: int) -> aioredis.Redis:
    pool = InstrumentedBlockingPool.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        max_connections=max_connections,
        timeout=settings.REDIS_POOL_ACQUIRE_TIMEOUT_SECONDS,
    )
    return aioredis.Redis(connection_pool=pool)


async def get_redis() -> aioredis.Redis:
    """Shared client for regular commands (GET/SET/PUBLISH/pipelines)."""
    global _redis
    if _redis is None:
        _redis = _build_client(settings.REDIS_COMMAND_MAX_CONNECTIONS)
    return _redis


async def get_redis_pubsub() -> aioredis.Redis:
    """Shared client for pub/sub SUBSCRIBERS only.

    Each ``.pubsub()`` checkout holds a dedicated connection for the WS
    client's lifetime; isolating them here means a burst of UI reconnects can
    never exhaust the command pool (and a tick-publish storm can never block a
    subscribe).
    """
    global _redis_pubsub
    if _redis_pubsub is None:
        _redis_pubsub = _build_client(settings.REDIS_PUBSUB_MAX_CONNECTIONS)
    return _redis_pubsub


def redis_pool_stats() -> dict:
    """Point-in-time pool telemetry for /api/system/pools (never raises)."""
    out: dict = {}
    for name, client in (("command", _redis), ("pubsub", _redis_pubsub)):
        if client is None:
            out[name] = {"initialized": False}
            continue
        pool = client.connection_pool
        try:
            stats = pool.stats() if isinstance(pool, InstrumentedBlockingPool) else {
                "max_connections": getattr(pool, "max_connections", None),
            }
        except Exception:  # noqa: BLE001 — telemetry must never throw
            stats = {}
        out[name] = {"initialized": True, **stats}
    return out


async def close_redis():
    global _redis, _redis_pubsub
    for client in (_redis, _redis_pubsub):
        if client is not None:
            try:
                await client.close()
                await client.connection_pool.disconnect()
            except Exception:  # noqa: BLE001 — shutdown must not raise
                pass
    _redis = None
    _redis_pubsub = None
