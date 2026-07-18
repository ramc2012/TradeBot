from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from loguru import logger
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from core.config import settings


def _database_pool_config() -> dict[str, int | bool]:
    is_development = settings.APP_ENV == "development"
    # Production was sized at pool_size=3 / max_overflow=2 (5 connections max
    # per Cloud Run instance). That was exhausted by the new agents (audit
    # writes, paper bootstrap, data-quality, AI/FMP/DO paper cycles, NSE +
    # commodity scans) running together — observed
    # "QueuePool limit of size 6 overflow 4 reached, connection timed out"
    # on /api/commodity/overview and /api/system/health under load.
    # Cloud SQL standard tiers tolerate well above 25 connections; budget
    # 16 per Cloud Run instance and rely on Cloud Run scaling to bound the
    # global footprint. DATABASE_POOL_SIZE/DATABASE_MAX_OVERFLOW env vars
    # still override, so the cap can be tightened without a code change.
    pool_size = settings.DATABASE_POOL_SIZE or (8 if is_development else 8)
    max_overflow = settings.DATABASE_MAX_OVERFLOW or (8 if is_development else 8)
    return {
        "pool_pre_ping": True,
        "pool_size": max(pool_size, 1),
        "max_overflow": max(max_overflow, 0),
        "pool_timeout": max(int(settings.DATABASE_POOL_TIMEOUT_SECONDS), 1),
        "pool_recycle": max(int(settings.DATABASE_POOL_RECYCLE_SECONDS), 30),
        "pool_use_lifo": not is_development,
    }


engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.APP_ENV == "development",
    **_database_pool_config(),
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


# ---------------------------------------------------------------------------
# Default statement timeout (Task D, 2026-07-18)
#
# statement_timeout was 0 (unbounded) globally — one runaway aggregation could
# hold a connection for minutes and starve the 8+8 pool. Every NEW connection
# of this engine now gets SET statement_timeout = DB_STATEMENT_TIMEOUT_SECONDS
# (default 60s, 0 disables). Escape hatches, in order of preference:
#   * long_query_session(...) — per-TRANSACTION `SET LOCAL` override for
#     legitimate long-runners (backfills / aggregations). SET LOCAL can never
#     poison a pooled connection because it dies with the transaction.
#   * disable_default_statement_timeout() — process-wide opt-out, called at
#     entry by the standalone research_sync container (multi-hour contract
#     sweeps share this module but must NOT inherit the app's timeout).
#   * DB_STATEMENT_TIMEOUT_SECONDS=0 env — config-level disable.
# ---------------------------------------------------------------------------

_default_timeout_disabled: bool = False
_default_timeout_disabled_reason: str | None = None


def disable_default_statement_timeout(reason: str = "") -> None:
    """Opt this PROCESS out of the connect-time default statement timeout.

    Must be called before the first connection is checked out (connections are
    created lazily, so calling at process entry is safe). Existing pooled
    connections keep whatever timeout they were created with.
    """
    global _default_timeout_disabled, _default_timeout_disabled_reason
    _default_timeout_disabled = True
    _default_timeout_disabled_reason = reason or None
    logger.info(
        "Default DB statement_timeout disabled for this process"
        + (f" ({reason})" if reason else "")
    )


def default_statement_timeout_ms() -> int:
    """Effective connect-time timeout in ms; 0 means disabled."""
    if _default_timeout_disabled:
        return 0
    try:
        seconds = int(settings.DB_STATEMENT_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        return 0
    return max(seconds, 0) * 1000


def statement_timeout_status() -> dict[str, object]:
    """Telemetry for /api/system/pools."""
    return {
        "configured_seconds": int(getattr(settings, "DB_STATEMENT_TIMEOUT_SECONDS", 0) or 0),
        "effective_ms": default_statement_timeout_ms(),
        "process_disabled": _default_timeout_disabled,
        "process_disabled_reason": _default_timeout_disabled_reason,
    }


def _apply_default_statement_timeout(dbapi_connection, connection_record) -> None:  # noqa: ANN001
    ms = default_statement_timeout_ms()
    if not ms:
        return
    # Runs inside SQLAlchemy's greenlet during async connect, so the adapted
    # asyncpg connection's sync cursor facade is usable here.
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"SET statement_timeout = {int(ms)}")
    finally:
        cursor.close()


if engine.dialect.name == "postgresql":
    event.listen(engine.sync_engine, "connect", _apply_default_statement_timeout)


@asynccontextmanager
async def long_query_session(
    timeout_seconds: int | None = None,
    *,
    session_factory=None,
) -> AsyncIterator[AsyncSession]:
    """Session for legitimate long-runners (backfills, deep aggregations).

    Every transaction the session opens gets ``SET LOCAL statement_timeout``
    (default 0 = unbounded for this session), overriding the connect-time
    default. SET LOCAL is transaction-scoped, so it survives the session's own
    commit cadence and can never leak onto a pooled connection.

    ``session_factory`` lets a caller pass its own (test-patchable) factory;
    defaults to this module's AsyncSessionLocal. Fake sessions without a real
    ``sync_session`` (unit tests) skip the SET LOCAL wiring entirely.
    """
    ms = max(int(timeout_seconds or 0), 0) * 1000
    factory = session_factory or AsyncSessionLocal
    async with factory() as session:
        sync_session = getattr(session, "sync_session", None)
        if engine.dialect.name != "postgresql" or sync_session is None:
            yield session
            return

        def _set_local_timeout(sess, transaction, connection) -> None:  # noqa: ANN001
            # Nested/savepoint transactions inherit the outer setting.
            if transaction.nested:
                return
            connection.exec_driver_sql(f"SET LOCAL statement_timeout = {ms}")

        event.listen(sync_session, "after_begin", _set_local_timeout)
        try:
            yield session
        finally:
            event.remove(sync_session, "after_begin", _set_local_timeout)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
