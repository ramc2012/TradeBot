from __future__ import annotations

from datetime import datetime
from threading import Lock
from time import monotonic
from typing import Any, Optional

from loguru import logger
import psycopg2
from psycopg2.extras import Json
from psycopg2.pool import ThreadedConnectionPool

from core.config import settings


_RUNTIME_STATE_CACHE_TTL_SECONDS = 2.0
_RUNTIME_STATE_POOL_RETRY_SECONDS = 5.0
_runtime_state_cache_lock = Lock()
_runtime_state_cache: dict[str, tuple[Any | None, datetime | None, float]] = {}
_runtime_state_pool_lock = Lock()
_runtime_state_pool: ThreadedConnectionPool | None = None
_runtime_state_pool_retry_at = 0.0
_runtime_state_table_ready = False


def _database_url() -> Optional[str]:
    raw = str(settings.DATABASE_URL or "").strip()
    if not raw:
        return None
    return (
        raw.replace("postgresql+asyncpg://", "postgresql://")
        .replace("postgresql+psycopg2://", "postgresql://")
    )


def _cache_runtime_state(state_key: str, payload: Any | None, updated_at: datetime | None) -> None:
    with _runtime_state_cache_lock:
        _runtime_state_cache[state_key] = (payload, updated_at, monotonic() + _RUNTIME_STATE_CACHE_TTL_SECONDS)


def _get_cached_runtime_state(state_key: str, *, allow_stale: bool = False) -> tuple[Any | None, datetime | None] | None:
    with _runtime_state_cache_lock:
        cached = _runtime_state_cache.get(state_key)
    if cached is None:
        return None
    payload, updated_at, expires_at = cached
    if allow_stale or expires_at > monotonic():
        return payload, updated_at
    return None


def _connection_pool() -> ThreadedConnectionPool | None:
    global _runtime_state_pool, _runtime_state_pool_retry_at
    db_url = _database_url()
    if not db_url:
        return None
    with _runtime_state_pool_lock:
        if _runtime_state_pool is None and _runtime_state_pool_retry_at > monotonic():
            return None
        if _runtime_state_pool is None:
            try:
                _runtime_state_pool = ThreadedConnectionPool(minconn=1, maxconn=2, dsn=db_url)
                _runtime_state_pool_retry_at = 0.0
            except Exception as exc:
                _runtime_state_pool_retry_at = monotonic() + _RUNTIME_STATE_POOL_RETRY_SECONDS
                logger.warning(f"Could not initialize runtime state pool: {exc}")
                return None
        return _runtime_state_pool


def _ensure_runtime_state_table(cur) -> None:
    global _runtime_state_table_ready
    if _runtime_state_table_ready:
        return
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS app_runtime_state (
            state_key TEXT PRIMARY KEY,
            payload JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    _runtime_state_table_ready = True


def load_runtime_state(state_key: str) -> tuple[Any | None, datetime | None]:
    cached = _get_cached_runtime_state(state_key)
    if cached is not None:
        return cached

    pool = _connection_pool()
    if pool is None:
        return None, None
    conn = None
    try:
        conn = pool.getconn()
        with conn.cursor() as cur:
            _ensure_runtime_state_table(cur)
            cur.execute(
                "SELECT payload, updated_at FROM app_runtime_state WHERE state_key = %s",
                (state_key,),
            )
            row = cur.fetchone()
        conn.commit()
        if not row:
            _cache_runtime_state(state_key, None, None)
            return None, None
        _cache_runtime_state(state_key, row[0], row[1])
        return row[0], row[1]
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        cached = _get_cached_runtime_state(state_key, allow_stale=True)
        if cached is not None:
            logger.warning(f"Could not load runtime state {state_key}; serving stale cached value: {exc}")
            return cached
        logger.warning(f"Could not load runtime state {state_key}: {exc}")
        return None, None
    finally:
        if conn is not None:
            pool.putconn(conn)


def save_runtime_state(state_key: str, payload: Any) -> datetime | None:
    pool = _connection_pool()
    if pool is None:
        return None

    conn = None
    try:
        conn = pool.getconn()
        conn.autocommit = False
        with conn.cursor() as cur:
            _ensure_runtime_state_table(cur)
            cur.execute(
                """
                INSERT INTO app_runtime_state (state_key, payload, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (state_key) DO UPDATE SET
                    payload = EXCLUDED.payload,
                    updated_at = NOW()
                RETURNING updated_at
                """,
                (state_key, Json(payload)),
            )
            row = cur.fetchone()
        conn.commit()
        updated_at = row[0] if row else None
        _cache_runtime_state(state_key, payload, updated_at)
        return updated_at
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        logger.warning(f"Could not persist runtime state {state_key}: {exc}")
        return None
    finally:
        if conn is not None:
            conn.autocommit = False
            pool.putconn(conn)
