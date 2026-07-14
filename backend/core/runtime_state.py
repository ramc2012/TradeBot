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


def _rollback_quietly(conn) -> None:
    if conn is None:
        return
    try:
        conn.rollback()
    except Exception as exc:
        logger.debug(f"Runtime-state rollback skipped: {exc}")


def _return_connection(pool: ThreadedConnectionPool, conn) -> None:
    if conn is None:
        return
    try:
        if hasattr(conn, "autocommit"):
            conn.autocommit = False
    except Exception:
        pass
    try:
        pool.putconn(conn, close=bool(getattr(conn, "closed", 0)))
    except Exception as exc:
        logger.debug(f"Runtime-state pool return skipped: {exc}")


_trade_book_table_ready = False


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> Optional[float]:
    try:
        f = float(value)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _ensure_trade_book_table(cur) -> None:
    """Append-only durable paper-trade ledger. Separate from the mutable
    app_runtime_state blob (which a paper-account reset archives/clears), so the
    booked trade history + timestamps survive resets and restarts."""
    global _trade_book_table_ready
    if _trade_book_table_ready:
        return
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_trade_book (
            id BIGSERIAL PRIMARY KEY,
            market TEXT NOT NULL,
            strategy_key TEXT,
            session_id TEXT,
            symbol TEXT NOT NULL,
            underlying TEXT,
            instrument_type TEXT,
            action TEXT,
            qty INTEGER,
            lots INTEGER,
            entry_price DOUBLE PRECISION,
            exit_price DOUBLE PRECISION,
            pnl DOUBLE PRECISION,
            entry_time TIMESTAMPTZ,
            exit_time TIMESTAMPTZ,
            setup_type TEXT,
            regime TEXT,
            exit_reason TEXT,
            signal_id TEXT,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_paper_trade_book_market_time "
        "ON paper_trade_book (market, COALESCE(exit_time, recorded_at) DESC)"
    )
    _trade_book_table_ready = True


def record_paper_trade(
    *,
    market: str,
    symbol: str,
    action: Optional[str] = None,
    qty: Any = None,
    entry_price: Any = None,
    exit_price: Any = None,
    pnl: Any = None,
    entry_time: Any = None,
    exit_time: Any = None,
    strategy_key: Optional[str] = None,
    session_id: Optional[str] = None,
    underlying: Optional[str] = None,
    instrument_type: Optional[str] = None,
    lots: Any = None,
    setup_type: Optional[str] = None,
    regime: Optional[str] = None,
    exit_reason: Optional[str] = None,
    signal_id: Optional[str] = None,
) -> bool:
    """Append one closed trade to the durable DB ledger. Best-effort: returns
    False (and logs) on failure — never raises into the trading path."""
    pool = _connection_pool()
    if pool is None:
        return False
    conn = None
    try:
        conn = pool.getconn()
        conn.autocommit = False
        with conn.cursor() as cur:
            _ensure_trade_book_table(cur)
            cur.execute(
                """
                INSERT INTO paper_trade_book
                  (market, strategy_key, session_id, symbol, underlying, instrument_type,
                   action, qty, lots, entry_price, exit_price, pnl, entry_time, exit_time,
                   setup_type, regime, exit_reason, signal_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    market, strategy_key, session_id, symbol, underlying, instrument_type,
                    action, _to_int(qty), _to_int(lots), _to_float(entry_price),
                    _to_float(exit_price), _to_float(pnl), entry_time, exit_time,
                    setup_type, regime, exit_reason, signal_id,
                ),
            )
        conn.commit()
        # Invalidate the read cache for this market so the new trade shows immediately.
        with _runtime_state_cache_lock:
            for k in [k for k in _runtime_state_cache if k.startswith(f"__trade_book__:{market}:")]:
                _runtime_state_cache.pop(k, None)
        return True
    except Exception as exc:
        if conn is not None:
            _rollback_quietly(conn)
        logger.warning(f"Could not record paper trade ({market} {symbol}): {exc}")
        return False
    finally:
        if conn is not None:
            _return_connection(pool, conn)


def load_paper_trade_book(
    *, market: Optional[str] = None, strategy_key: Optional[str] = None, limit: int = 500
) -> list[dict[str, Any]]:
    """Read the durable trade ledger (recent-first). Cached briefly so frequent
    status calls don't hammer the pool. Datetimes returned as ISO strings."""
    cache_key = f"__trade_book__:{market}:{strategy_key}:{limit}"
    cached = _get_cached_runtime_state(cache_key)
    if cached is not None:
        return list(cached[0] or [])
    pool = _connection_pool()
    if pool is None:
        return []
    conn = None
    try:
        conn = pool.getconn()
        with conn.cursor() as cur:
            _ensure_trade_book_table(cur)
            clauses: list[str] = []
            params: list[Any] = []
            if market:
                clauses.append("market = %s")
                params.append(market)
            if strategy_key:
                clauses.append("strategy_key = %s")
                params.append(strategy_key)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            params.append(int(limit))
            cur.execute(
                f"""
                SELECT market, strategy_key, session_id, symbol, underlying, instrument_type,
                       action, qty, lots, entry_price, exit_price, pnl, entry_time, exit_time,
                       setup_type, regime, exit_reason, signal_id, recorded_at
                FROM paper_trade_book
                {where}
                ORDER BY COALESCE(exit_time, recorded_at) DESC
                LIMIT %s
                """,
                tuple(params),
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        conn.commit()
        for r in rows:
            for key in ("entry_time", "exit_time", "recorded_at"):
                if r.get(key) is not None:
                    r[key] = r[key].isoformat()
        _cache_runtime_state(cache_key, rows, None)
        return rows
    except Exception as exc:
        if conn is not None:
            _rollback_quietly(conn)
        logger.warning(f"Could not load paper trade book: {exc}")
        return []
    finally:
        if conn is not None:
            _return_connection(pool, conn)


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
            _rollback_quietly(conn)
        cached = _get_cached_runtime_state(state_key, allow_stale=True)
        if cached is not None:
            logger.warning(f"Could not load runtime state {state_key}; serving stale cached value: {exc}")
            return cached
        logger.warning(f"Could not load runtime state {state_key}: {exc}")
        return None, None
    finally:
        if conn is not None:
            _return_connection(pool, conn)


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
            _rollback_quietly(conn)
        logger.warning(f"Could not persist runtime state {state_key}: {exc}")
        return None
    finally:
        if conn is not None:
            _return_connection(pool, conn)
