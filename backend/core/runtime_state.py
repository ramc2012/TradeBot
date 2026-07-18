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
                # maxconn 2→8 (2026-07-15): with persists offloaded to worker
                # threads (S1 + commodity _apersist_state) plus trade-book
                # writes and API status reads sharing this pool, 2 connections
                # serialized every caller behind the slowest write.
                _runtime_state_pool = ThreadedConnectionPool(minconn=1, maxconn=8, dsn=db_url)
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
    # Phase-2 ITEM 3: optimistic-concurrency version for compare-and-set writes
    # on the control-state persistence path (live-safe nullable-with-default add;
    # existing rows backfill to 0). Callers that don't pass expected_version keep
    # the unconditional upsert semantics — strictly additive.
    cur.execute(
        "ALTER TABLE app_runtime_state "
        "ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 0"
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
                INSERT INTO app_runtime_state (state_key, payload, updated_at, version)
                VALUES (%s, %s, NOW(), 1)
                ON CONFLICT (state_key) DO UPDATE SET
                    payload = EXCLUDED.payload,
                    updated_at = NOW(),
                    version = app_runtime_state.version + 1
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


def load_runtime_state_versioned(
    state_key: str,
) -> tuple[Any | None, datetime | None, int | None]:
    """Like load_runtime_state but also returns the row's optimistic version.

    version is None when the row is absent or the version could not be read
    (older schema / DB down); callers must then fall back to an unconditional
    save. Never raises."""
    pool = _connection_pool()
    if pool is None:
        return None, None, None
    conn = None
    try:
        conn = pool.getconn()
        with conn.cursor() as cur:
            _ensure_runtime_state_table(cur)
            cur.execute(
                "SELECT payload, updated_at, version FROM app_runtime_state WHERE state_key = %s",
                (state_key,),
            )
            row = cur.fetchone()
        conn.commit()
        if not row:
            return None, None, None
        return row[0], row[1], row[2]
    except Exception as exc:  # noqa: BLE001
        if conn is not None:
            _rollback_quietly(conn)
        logger.warning(f"Could not load versioned runtime state {state_key}: {exc}")
        return None, None, None
    finally:
        if conn is not None:
            _return_connection(pool, conn)


def save_runtime_state_cas(
    state_key: str, payload: Any, expected_version: int | None
) -> tuple[bool, datetime | None, int | None]:
    """Compare-and-set write: only persist when the stored version still equals
    ``expected_version`` (a concurrent writer bumping it => conflict). Returns
    (committed, updated_at, new_version).

    ``expected_version is None`` means "row absent" — insert with version 1 and
    fail (committed=False) if a row already exists (another writer won the
    insert race)."""
    pool = _connection_pool()
    if pool is None:
        return False, None, None
    conn = None
    try:
        conn = pool.getconn()
        conn.autocommit = False
        with conn.cursor() as cur:
            _ensure_runtime_state_table(cur)
            if expected_version is None:
                cur.execute(
                    """
                    INSERT INTO app_runtime_state (state_key, payload, updated_at, version)
                    VALUES (%s, %s, NOW(), 1)
                    ON CONFLICT (state_key) DO NOTHING
                    RETURNING updated_at, version
                    """,
                    (state_key, Json(payload)),
                )
            else:
                cur.execute(
                    """
                    UPDATE app_runtime_state
                    SET payload = %s, updated_at = NOW(), version = version + 1
                    WHERE state_key = %s AND version = %s
                    RETURNING updated_at, version
                    """,
                    (Json(payload), state_key, int(expected_version)),
                )
            row = cur.fetchone()
        conn.commit()
        if not row:
            return False, None, None  # version moved under us → conflict
        updated_at, new_version = row[0], row[1]
        _cache_runtime_state(state_key, payload, updated_at)
        return True, updated_at, new_version
    except Exception as exc:  # noqa: BLE001
        if conn is not None:
            _rollback_quietly(conn)
        logger.warning(f"CAS persist failed for runtime state {state_key}: {exc}")
        return False, None, None
    finally:
        if conn is not None:
            _return_connection(pool, conn)


def _merge_control_state(
    stored: Any,
    payload: dict,
    *,
    owns_control_flags: bool,
    flag_keys: tuple[str, ...],
    heartbeat_key: str,
) -> dict:
    """Field-ownership merge that makes control writes and scan persists commute.

    Ownership rule:
      * control writers own ``flag_keys`` (kill/auto-run/manual-restart …);
      * the scan loop owns ``heartbeat_key`` (+ all runtime/strategy subtrees).

    A scan persist (``owns_control_flags=False``) keeps the STORED flag values so
    a concurrent operator toggle is never clobbered; a control write
    (``owns_control_flags=True``) keeps the STORED heartbeat (and every other
    stored subtree) so the scan's fresh loop state is never rolled back. Only the
    ``control`` subtree is reconciled; the owning writer's copy wins elsewhere."""
    import copy

    merged = copy.deepcopy(payload) if isinstance(payload, dict) else {}
    stored_dict = stored if isinstance(stored, dict) else {}
    stored_control = stored_dict.get("control") if isinstance(stored_dict.get("control"), dict) else {}
    merged_control = merged.get("control") if isinstance(merged.get("control"), dict) else {}

    if owns_control_flags:
        # Control write: preserve the scan-owned heartbeat (take the newer of the
        # two, string ISO compare — same-format IST/UTC stamps sort correctly).
        hb_stored = stored_control.get(heartbeat_key)
        hb_ours = merged_control.get(heartbeat_key)
        if hb_stored and (not hb_ours or str(hb_stored) > str(hb_ours)):
            merged_control[heartbeat_key] = hb_stored
    else:
        # Scan persist: preserve the control-owned flags from the store.
        for key in flag_keys:
            if key in stored_control:
                merged_control[key] = stored_control[key]

    if merged_control or "control" in merged:
        merged["control"] = merged_control
    return merged


def save_runtime_state_control_merged(
    state_key: str,
    payload: dict,
    *,
    owns_control_flags: bool,
    flag_keys: tuple[str, ...],
    heartbeat_key: str = "loop_heartbeat_at",
    max_retries: int = 3,
) -> datetime | None:
    """Persist ``payload`` under optimistic concurrency with a control-subtree
    merge (Phase-2 ITEM 3), so a control toggle and a concurrent scan persist can
    never clobber each other. Falls back to an unconditional
    :func:`save_runtime_state` when versioning is unavailable or the CAS retries
    are exhausted (favor never losing a control toggle over strict versioning)."""
    for _attempt in range(max(1, int(max_retries))):
        stored, _updated_at, version = load_runtime_state_versioned(state_key)
        merged = _merge_control_state(
            stored,
            payload,
            owns_control_flags=owns_control_flags,
            flag_keys=flag_keys,
            heartbeat_key=heartbeat_key,
        )
        committed, cas_updated_at, _new_version = save_runtime_state_cas(state_key, merged, version)
        if committed:
            return cas_updated_at
    # Retries exhausted (or CAS unsupported) — best-effort unconditional write.
    return save_runtime_state(state_key, payload)
