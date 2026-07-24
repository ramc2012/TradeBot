"""Durable self-heal for the ``agent_positions`` journal.

``agent_positions`` is upserted ``ON CONFLICT (symbol)`` — one row per option
symbol whose status reflects only the last write. Positions that leave the live
book without the close-side persist running (fill rejected after the entry was
journaled, expiry roll, post-restart prune) used to strand the row at
``status='open'`` forever — "zombies" that polluted the sticky-strike pin map,
the lane-auditor open-count, and the owner's "~100 open positions" impression.

``_reconcile_agent_positions_journal`` closes any open/partial journal row whose
symbol is no longer in the authoritative in-memory book. These tests lock in the
behaviour AND the safety guards (never wipe on a pre-restore empty book; never
touch a freshly-entered position).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import pytest

import paper_engine.strategy_agent as strategy_agent_module
from paper_engine.strategy_agent import PaperStrategyAgent, _now_ist


@dataclass
class _StubPos:
    symbol: str


class _FakeResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _FakeSession:
    """Records every execute()/commit() so the test can inspect the SQL."""

    def __init__(self, recorder: list, rowcount: int) -> None:
        self._rec = recorder
        self._rowcount = rowcount

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_a) -> bool:
        return False

    async def execute(self, stmt, params=None):
        self._rec.append(("EXEC", str(stmt), params))
        return _FakeResult(self._rowcount)

    async def commit(self) -> None:
        self._rec.append(("COMMIT", None, None))


def _patch_session(monkeypatch, recorder: list, rowcount: int = 1) -> None:
    monkeypatch.setattr(
        strategy_agent_module,
        "AsyncSessionLocal",
        lambda: _FakeSession(recorder, rowcount),
    )


@pytest.mark.asyncio
async def test_reconcile_closes_symbols_absent_from_live_book(monkeypatch) -> None:
    agent = PaperStrategyAgent()
    recorder: list = []
    _patch_session(monkeypatch, recorder)

    # S1 has scanned and holds two live positions; S2 has NOT scanned yet.
    agent._strategy1.last_scan_at = _now_ist().isoformat()
    agent._strategy1.positions = {
        "OPT:PNB:2026-08-25:110:PE": _StubPos("OPT:PNB:2026-08-25:110:PE"),
        "OPT:WIPRO:2026-08-25:175:CE": _StubPos("OPT:WIPRO:2026-08-25:175:CE"),
    }
    agent._strategy2.last_scan_at = None
    agent._strategy2.positions = {}

    await agent._reconcile_agent_positions_journal()

    execs = [r for r in recorder if r[0] == "EXEC"]
    # Only the scanned runtime issues an UPDATE (S2 is skipped: pre-scan guard).
    assert len(execs) == 1
    _, sql, params = execs[0]
    assert "UPDATE agent_positions" in sql
    assert "status IN ('open', 'partial_exit')" in sql
    assert "symbol <> ALL(CAST(:live_symbols AS text[]))" in sql
    # The jsonb timestamp bind is explicitly cast so asyncpg can infer its type
    # (without the cast the live UPDATE raised IndeterminateDatatypeError).
    assert "CAST(:now_ist_text AS text)" in sql
    assert params["strategy_key"] == "macd_strategy"
    # The authoritative live book is passed through verbatim — only symbols NOT
    # in this list get closed.
    assert set(params["live_symbols"]) == {
        "OPT:PNB:2026-08-25:110:PE",
        "OPT:WIPRO:2026-08-25:175:CE",
    }
    # Staleness cutoff is ~10 minutes in the past, protecting fresh entries.
    assert params["cutoff"] <= _now_ist() - timedelta(minutes=9)
    # A row was closed (fake rowcount=1) so the batch commits.
    assert any(r[0] == "COMMIT" for r in recorder)


@pytest.mark.asyncio
async def test_reconcile_skips_when_no_runtime_has_scanned(monkeypatch) -> None:
    """A pre-restore empty book must never wipe the journal."""
    agent = PaperStrategyAgent()
    recorder: list = []
    _patch_session(monkeypatch, recorder)

    agent._strategy1.last_scan_at = None
    agent._strategy1.positions = {}
    agent._strategy2.last_scan_at = None
    agent._strategy2.positions = {}

    await agent._reconcile_agent_positions_journal()

    assert not [r for r in recorder if r[0] == "EXEC"]
    assert not [r for r in recorder if r[0] == "COMMIT"]


@pytest.mark.asyncio
async def test_reconcile_empty_book_after_scan_still_reconciles(monkeypatch) -> None:
    """A scanned runtime with a genuinely empty book closes stale opens
    (``symbol <> ALL(empty array)`` matches every stale row)."""
    agent = PaperStrategyAgent()
    recorder: list = []
    _patch_session(monkeypatch, recorder)

    agent._strategy1.last_scan_at = _now_ist().isoformat()
    agent._strategy1.positions = {}
    agent._strategy2.last_scan_at = None
    agent._strategy2.positions = {}

    await agent._reconcile_agent_positions_journal()

    execs = [r for r in recorder if r[0] == "EXEC"]
    assert len(execs) == 1
    assert execs[0][2]["live_symbols"] == []
