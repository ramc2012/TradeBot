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

import asyncio
import json
import math
import uuid

from loguru import logger
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


# ── Phase-2 ITEM 2: strategy-command RPC (core plane → strategy plane) ────────
# A LANESET=core API call (run-once / close-position / commodity start) can't
# touch the agents — they live in the strategy plane. Rather than 409, publish a
# command onto a Redis list; the strategy plane's consumer executes it in-process
# and RPUSHes the JSON result onto a per-id ack list the core plane BLPOPs with a
# bounded timeout. Redis LISTS (not pub/sub) so a single consumer reliably claims
# each request and there is no subscribe race. Collapses to nothing in single
# process (routers never take this path — see is_core_only gate at the call site).

_STRATEGY_ACK_PREFIX = "strat:cmd:ack:"
_STRATEGY_DONE_PREFIX = "strat:cmd:done:"

# Sentinels the core caller maps to honest HTTP errors instead of hanging.
PROXY_TIMEOUT = "__proxy_timeout__"
PROXY_ERROR = "__proxy_error__"


async def proxy_strategy_command(action: str, args: dict | None = None, *, timeout: float) -> dict:
    """Publish a strategy command and wait (bounded) for the strategy plane ack.

    Returns the strategy plane's result dict on success. On ack timeout returns
    ``{PROXY_TIMEOUT: True}`` (→ 504). Raises on a Redis publish failure so the
    caller can surface a 503 — a run-once / close must never be silently dropped.
    """
    request_key = str(getattr(settings, "STRATEGY_PROXY_REQUEST_KEY", "strat:cmd:req"))
    cmd_id = uuid.uuid4().hex
    req = {"id": cmd_id, "action": action, "args": dict(args or {})}
    redis = await get_redis()
    await redis.rpush(request_key, json.dumps(req))  # may raise → caller → 503
    ack_key = _STRATEGY_ACK_PREFIX + cmd_id
    # BLPOP timeout is whole seconds; ceil so a sub-second budget still waits ≥1s.
    res = await redis.blpop(ack_key, timeout=max(1, int(math.ceil(float(timeout)))))
    if not res:
        return {PROXY_TIMEOUT: True, "id": cmd_id, "action": action}
    try:
        ack = json.loads(res[1])
    except Exception as exc:  # noqa: BLE001
        return {PROXY_ERROR: f"malformed ack: {exc}", "id": cmd_id}
    return ack


async def consume_strategy_commands(dispatch, *, stop_event: "asyncio.Event | None" = None) -> None:
    """Strategy-plane consumer loop: BLPOP a command, dispatch it, RPUSH the ack.

    ``dispatch(action, args) -> awaitable[result]``. Runs until ``stop_event``
    is set (or forever). Every Redis hiccup is swallowed with a short backoff so
    the consumer self-heals — it must never crash the strategy plane."""
    request_key = str(getattr(settings, "STRATEGY_PROXY_REQUEST_KEY", "strat:cmd:req"))
    while stop_event is None or not stop_event.is_set():
        try:
            redis = await get_redis()
            item = await redis.blpop(request_key, timeout=5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[StrategyProxy] consumer BLPOP failed: {exc}")
            await asyncio.sleep(1.0)
            continue
        if not item:
            continue
        try:
            req = json.loads(item[1])
        except Exception:  # noqa: BLE001 — malformed request: drop
            continue
        cmd_id = str(req.get("id") or "")
        action = req.get("action")
        args = req.get("args") or {}
        if not cmd_id or not action:
            continue
        ack_key = _STRATEGY_ACK_PREFIX + cmd_id
        # Dedup: claim the id so a redelivery can never double-execute a trade.
        try:
            redis = await get_redis()
            claimed = await redis.set(_STRATEGY_DONE_PREFIX + cmd_id, "1", nx=True, ex=300)
            if not claimed:
                continue
        except Exception:  # noqa: BLE001 — dedup is best-effort; proceed
            pass
        try:
            result = await dispatch(action, args)
            ack = {"id": cmd_id, "result": result}
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — surface the error to the caller
            ack = {"id": cmd_id, "error": str(exc)}
        try:
            redis = await get_redis()
            await redis.rpush(ack_key, json.dumps(ack, default=str))
            await redis.expire(ack_key, 60)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[StrategyProxy] consumer ack push failed for {cmd_id}: {exc}")


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
