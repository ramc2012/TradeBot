"""Task D (2026-07-18): default DB statement-timeout classes.

statement_timeout was 0 (unbounded) globally. The main app engine now applies
SET statement_timeout = DB_STATEMENT_TIMEOUT_SECONDS on every NEW connection,
with:
  * long_query_session() — per-transaction SET LOCAL override for legitimate
    long-runners (never poisons pooled connections);
  * disable_default_statement_timeout() — process-level opt-out used by the
    standalone research_sync container;
  * DB_STATEMENT_TIMEOUT_SECONDS = 0 — config-level disable.
No live Postgres needed: the connect hook and the session event listener are
exercised against fakes.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from core.config import settings
from db import database


@pytest.fixture(autouse=True)
def _reset_process_disable(monkeypatch: pytest.MonkeyPatch):
    """Each test starts with the process-level opt-out cleared."""
    monkeypatch.setattr(database, "_default_timeout_disabled", False)
    monkeypatch.setattr(database, "_default_timeout_disabled_reason", None)
    yield


# ---------------------------------------------------------------------------
# Effective-timeout resolution
# ---------------------------------------------------------------------------

def test_default_timeout_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DB_STATEMENT_TIMEOUT_SECONDS", 60, raising=False)
    assert database.default_statement_timeout_ms() == 60_000


def test_zero_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DB_STATEMENT_TIMEOUT_SECONDS", 0, raising=False)
    assert database.default_statement_timeout_ms() == 0


def test_negative_treated_as_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DB_STATEMENT_TIMEOUT_SECONDS", -5, raising=False)
    assert database.default_statement_timeout_ms() == 0


def test_process_level_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DB_STATEMENT_TIMEOUT_SECONDS", 60, raising=False)
    database.disable_default_statement_timeout("unit test")
    assert database.default_statement_timeout_ms() == 0
    status = database.statement_timeout_status()
    assert status["process_disabled"] is True
    assert status["process_disabled_reason"] == "unit test"
    assert status["effective_ms"] == 0


# ---------------------------------------------------------------------------
# Connect-time hook
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, executed: list[str]):
        self._executed = executed
        self.closed = False

    def execute(self, sql: str) -> None:
        self._executed.append(sql)

    def close(self) -> None:
        self.closed = True


class _FakeDbapiConnection:
    def __init__(self):
        self.executed: list[str] = []
        self.cursors: list[_FakeCursor] = []

    def cursor(self) -> _FakeCursor:
        cursor = _FakeCursor(self.executed)
        self.cursors.append(cursor)
        return cursor


def test_connect_hook_applies_default_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DB_STATEMENT_TIMEOUT_SECONDS", 60, raising=False)
    conn = _FakeDbapiConnection()
    database._apply_default_statement_timeout(conn, None)
    assert conn.executed == ["SET statement_timeout = 60000"]
    assert all(cursor.closed for cursor in conn.cursors)


def test_connect_hook_noop_when_config_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DB_STATEMENT_TIMEOUT_SECONDS", 0, raising=False)
    conn = _FakeDbapiConnection()
    database._apply_default_statement_timeout(conn, None)
    assert conn.executed == []


def test_connect_hook_noop_when_process_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DB_STATEMENT_TIMEOUT_SECONDS", 60, raising=False)
    database.disable_default_statement_timeout("research_sync standalone")
    conn = _FakeDbapiConnection()
    database._apply_default_statement_timeout(conn, None)
    assert conn.executed == []


def test_connect_hook_registered_on_postgres_engine() -> None:
    # The real engine is postgresql+asyncpg — the hook must be attached.
    assert database.engine.dialect.name == "postgresql"
    assert event.contains(
        database.engine.sync_engine, "connect", database._apply_default_statement_timeout
    )


# ---------------------------------------------------------------------------
# long_query_session escape hatch
# ---------------------------------------------------------------------------

class _FakeAsyncSession:
    """Async context manager exposing a REAL (unbound) ORM Session so the
    SQLAlchemy event machinery in long_query_session is exercised for real."""

    def __init__(self):
        self.sync_session = Session()

    async def __aenter__(self) -> "_FakeAsyncSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeSyncConnection:
    def __init__(self):
        self.executed: list[str] = []

    def exec_driver_sql(self, sql: str) -> None:
        self.executed.append(sql)


def _fire_after_begin(sync_session: Session, connection: _FakeSyncConnection, *, nested: bool = False) -> None:
    sync_session.dispatch.after_begin(
        sync_session, SimpleNamespace(nested=nested), connection
    )


@pytest.mark.asyncio
async def test_long_query_session_sets_local_unbounded(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_session = _FakeAsyncSession()
    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: fake_session)
    monkeypatch.setattr(
        database, "engine", SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    )
    conn = _FakeSyncConnection()
    async with database.long_query_session() as session:
        assert session is fake_session
        # Every new (non-nested) transaction gets the override.
        _fire_after_begin(fake_session.sync_session, conn)
        _fire_after_begin(fake_session.sync_session, conn)
    assert conn.executed == [
        "SET LOCAL statement_timeout = 0",
        "SET LOCAL statement_timeout = 0",
    ]
    # Listener removed on exit — later transactions are untouched.
    after_exit = _FakeSyncConnection()
    _fire_after_begin(fake_session.sync_session, after_exit)
    assert after_exit.executed == []


@pytest.mark.asyncio
async def test_long_query_session_custom_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_session = _FakeAsyncSession()
    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: fake_session)
    monkeypatch.setattr(
        database, "engine", SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    )
    conn = _FakeSyncConnection()
    async with database.long_query_session(timeout_seconds=600):
        _fire_after_begin(fake_session.sync_session, conn)
    assert conn.executed == ["SET LOCAL statement_timeout = 600000"]


@pytest.mark.asyncio
async def test_long_query_session_skips_nested_transactions(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_session = _FakeAsyncSession()
    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: fake_session)
    monkeypatch.setattr(
        database, "engine", SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    )
    conn = _FakeSyncConnection()
    async with database.long_query_session():
        _fire_after_begin(fake_session.sync_session, conn, nested=True)
    assert conn.executed == []  # savepoints inherit the outer setting


@pytest.mark.asyncio
async def test_long_query_session_noop_on_non_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_session = _FakeAsyncSession()
    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: fake_session)
    monkeypatch.setattr(
        database, "engine", SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    )
    conn = _FakeSyncConnection()
    async with database.long_query_session() as session:
        assert session is fake_session
        _fire_after_begin(fake_session.sync_session, conn)
    assert conn.executed == []  # SET LOCAL is PG-only; other dialects untouched


@pytest.mark.asyncio
async def test_long_query_session_removes_listener_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_session = _FakeAsyncSession()
    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: fake_session)
    monkeypatch.setattr(
        database, "engine", SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    )
    with pytest.raises(RuntimeError):
        async with database.long_query_session():
            raise RuntimeError("boom")
    conn = _FakeSyncConnection()
    _fire_after_begin(fake_session.sync_session, conn)
    assert conn.executed == []


# ---------------------------------------------------------------------------
# research_sync standalone exemption
# ---------------------------------------------------------------------------

def test_research_sync_standalone_entry_disables_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The standalone container's entry (_run) must opt the process out BEFORE
    running; the embedded path (run_daemon_from_env) must NOT."""
    from data import run_upstox_research_sync as entry

    calls: list[str] = []
    monkeypatch.setattr(
        database,
        "disable_default_statement_timeout",
        lambda reason="": calls.append(reason),
    )
    monkeypatch.setattr(entry, "_configure_logging", lambda: None)
    monkeypatch.setattr(entry, "_parse_args", lambda: SimpleNamespace())

    async def _fake_run_with_args(args) -> int:  # noqa: ANN001
        return 0

    monkeypatch.setattr(entry, "_run_with_args", _fake_run_with_args)
    assert asyncio.run(entry._run()) == 0
    assert calls == ["research_sync standalone container"]

    # Embedded mode goes through run_daemon_from_env and must not disable.
    calls.clear()
    monkeypatch.setattr(entry, "_build_daemon_args_from_env", lambda: SimpleNamespace())
    asyncio.run(entry.run_daemon_from_env())
    assert calls == []
