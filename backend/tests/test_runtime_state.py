from __future__ import annotations

from datetime import datetime, timezone

import psycopg2

from core import runtime_state


class _BrokenCursor:
    def __init__(self, conn: "_BrokenConnection") -> None:
        self._conn = conn

    def __enter__(self) -> "_BrokenCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, _query, _params=None) -> None:
        self._conn.closed = 2
        raise psycopg2.OperationalError("server closed the connection unexpectedly")


class _BrokenConnection:
    def __init__(self) -> None:
        self.closed = 0
        self.autocommit = False
        self.rollback_calls = 0

    def cursor(self) -> _BrokenCursor:
        return _BrokenCursor(self)

    def rollback(self) -> None:
        self.rollback_calls += 1
        raise psycopg2.InterfaceError("connection already closed")


class _FakePool:
    def __init__(self, conn: _BrokenConnection) -> None:
        self._conn = conn
        self.putconn_calls: list[tuple[_BrokenConnection, bool]] = []

    def getconn(self) -> _BrokenConnection:
        return self._conn

    def putconn(self, conn: _BrokenConnection, close: bool = False) -> None:
        self.putconn_calls.append((conn, close))


def test_load_runtime_state_serves_stale_cache_when_connection_is_closed(monkeypatch) -> None:
    state_key = "commodity_strategy_state"
    cached_payload = {"watchlist": ["GOLD"]}
    cached_updated_at = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    broken_conn = _BrokenConnection()
    pool = _FakePool(broken_conn)

    with runtime_state._runtime_state_cache_lock:
        runtime_state._runtime_state_cache[state_key] = (cached_payload, cached_updated_at, 0.0)

    monkeypatch.setattr(runtime_state, "_connection_pool", lambda: pool)
    monkeypatch.setattr(runtime_state, "_ensure_runtime_state_table", lambda _cur: None)

    payload, updated_at = runtime_state.load_runtime_state(state_key)

    assert payload == cached_payload
    assert updated_at == cached_updated_at
    assert broken_conn.rollback_calls == 1
    assert pool.putconn_calls == [(broken_conn, True)]
