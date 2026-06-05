"""Review fix: directional _summary realized P&L must come from the DB-wide SUM
over ALL closed positions — not the capped in-memory list (LIMIT 500 / [-250:]),
which silently understated realized/equity as history grew — with a graceful
fallback to the in-memory sum when the DB is unavailable.
"""
import asyncio

import directional_options.paper as dp
from directional_options.paper import DirectionalOptionsPaperStore


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeSession:
    """Minimal async-context-manager stand-in for AsyncSessionLocal()."""
    def __init__(self, scalar_value):
        self._scalar_value = scalar_value

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *a, **k):
        return _FakeResult(self._scalar_value)


def _store():
    return DirectionalOptionsPaperStore("/tmp/_dir_summary_test_unused")


def test_summary_realized_uses_db_wide_sum(monkeypatch):
    # DB lifetime SUM (9999) differs from the capped in-memory list (sums to 70).
    # The fix must report the DB SUM, not the truncated in-memory total.
    monkeypatch.setattr(dp, "AsyncSessionLocal", lambda: _FakeSession(9999.0))
    closed = [{"realized_pnl": 100.0}, {"realized_pnl": -30.0}]  # in-memory sum = 70
    summary = asyncio.run(_store()._summary([], closed))
    assert summary["realized_pnl"] == 9999.0


def test_summary_realized_falls_back_when_db_unavailable(monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(dp, "AsyncSessionLocal", _boom)
    closed = [{"realized_pnl": 100.0}, {"realized_pnl": -30.0}]
    summary = asyncio.run(_store()._summary([], closed))
    assert summary["realized_pnl"] == 70.0  # graceful fallback to in-memory sum
