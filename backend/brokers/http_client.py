"""Shared long-lived httpx clients for broker data REST.

Previously every `_get_data_json` opened `async with httpx.AsyncClient(...)` per
call — a fresh TCP+TLS handshake and no connection pooling on ~30k+ Fyers/Upstox
calls/day. Reusing one keep-alive client per broker amortises the handshake and
bounds the connection pool.

Clients are keyed by (name, running-loop-id) so each asyncio loop gets its own
client — the test-suite spins up isolated loops, and an httpx.AsyncClient created
on one loop cannot be awaited on another. `aclose_all_shared_clients()` is called
on app shutdown.
"""
from __future__ import annotations

import asyncio

import httpx

_clients: dict[tuple[str, int], httpx.AsyncClient] = {}

# Modest pool — the shared rate limiters already cap real request concurrency, so
# these ceilings just prevent unbounded socket growth.
_LIMITS = httpx.Limits(max_keepalive_connections=20, max_connections=50)


def get_shared_async_client(name: str, *, default_timeout: float = 15.0) -> httpx.AsyncClient:
    """Return a cached keep-alive client for `name` on the current event loop,
    creating (or recreating, if closed) it lazily. Pass per-request timeouts to
    `client.get(..., timeout=...)` to override the default."""
    loop_id = id(asyncio.get_running_loop())
    key = (name, loop_id)
    client = _clients.get(key)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=default_timeout, limits=_LIMITS)
        _clients[key] = client
    return client


async def aclose_all_shared_clients() -> None:
    """Close every cached client (app shutdown). Best-effort."""
    for client in list(_clients.values()):
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass
    _clients.clear()
