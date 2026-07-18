"""GAP 1 — macd_refined seconds-cadence protective-exit heartbeat.

Owner directive 2026-07-17: the 30-minute macd_refined DECISION cycle sets the
entry side, but held-position updates must be in SECONDS. These tests prove the
new ``refresh_paper_marks`` pass:

  * catches a breached hard stop off the REAL-TIME plane WITHOUT waiting for the
    30m cycle, and reads that plane (data_router.get_live_mark) rather than a
    broker REST / route_order decision fetch;
  * falls back to the shared ``oc:`` option-chain cache when no WS tick exists;
  * never fires a PRICE exit when no fresh real-time mark is available (the 30m
    cycle stays the backstop);
  * rejects a cross-wired (index-magnitude) tick via the ratio guard;
  * never opens a position (allow_entries=False).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from macd_refined.config import clone_default_config
from macd_refined.live import MacdRefinedLiveEngine
from macd_refined.paper import MacdRefinedPaperStore


class _StoreStub:
    def lot_size_for(self, _underlying: str) -> int:
        return 50


def _engine(tmp_path: Path) -> MacdRefinedLiveEngine:
    config = clone_default_config()
    config["paper_trading"]["journal_root"] = str(tmp_path / "paper")
    config["live"]["volume_store_root"] = str(tmp_path / "volume_tracking")
    paper = MacdRefinedPaperStore(config["paper_trading"]["journal_root"], config=config)
    return MacdRefinedLiveEngine(_StoreStub(), paper, config)


def _open_position(engine: MacdRefinedLiveEngine, *, entry: float = 100.0) -> str:
    # Far-out expiry so the time-based window_end never fires — only the
    # price-based stop is under test.
    proposal = {
        "underlying": "RELIANCE",
        "option_type": "CE",
        "trading_symbol": "RELIANCE 3000 CE",
        "instrument_key": "NSE:RELIANCE27JUL3000CE",
        "expiry": "2027-07-28",
        "expiry_window_end": "2027-07-21",
        "strike": 3000.0,
        "spot": 3010.0,
        "lot_size": 50,
        "quantity_lots": 1,
        "quantity_units": 50,
        "entry_premium": entry,
        "iv": 0.20,
        "selection_reason": "test-open",
    }
    engine.paper.sync_cycle(
        proposals=[proposal], marks={}, now="2026-07-17T09:20:00+00:00", allow_entries=True
    )
    opens = engine.paper.list_positions(status="open")["open_positions"]
    assert opens, "position failed to open"
    return str(opens[0]["position_id"])


def _patch_live_mark(monkeypatch, price_by_symbol) -> list[str]:
    """Patch data_router.get_live_mark; return the list it records calls into."""
    from market_data.data_router import data_router

    calls: list[str] = []

    async def _fake_get_live_mark(symbol, *, max_age_seconds=30.0):
        calls.append(str(symbol))
        if callable(price_by_symbol):
            return price_by_symbol(str(symbol))
        return price_by_symbol.get(str(symbol))

    monkeypatch.setattr(data_router, "get_live_mark", _fake_get_live_mark)
    return calls


def _patch_chain_cache(monkeypatch, payload) -> None:
    from market_data.option_chain import option_chain_service

    async def _fake_get_cached(_symbol, _expiry):
        return payload

    monkeypatch.setattr(option_chain_service, "get_cached", _fake_get_cached)


def test_fast_pass_catches_breached_stop_from_realtime_plane(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path)
    pid = _open_position(engine, entry=100.0)

    # Any WS symbol resolves to 60 — below the 30% hard stop (entry×0.70 = 70).
    calls = _patch_live_mark(monkeypatch, lambda _sym: 60.0)
    # A broker fetch would be a bug — refresh_paper_marks must never touch it.
    async def _boom():
        raise AssertionError("refresh_paper_marks must not call the broker adapter")

    monkeypatch.setattr(engine, "_adapter", _boom)

    result = asyncio.run(engine.refresh_paper_marks())

    # The position closed on the fast pass, not the 30m cycle.
    closed = engine.paper.list_positions(status="closed")["closed_positions"]
    assert len(closed) == 1
    assert closed[0]["close_reason"] == "stop_loss"
    assert engine.paper.list_positions(status="open")["open_positions"] == []
    assert result["exits"] == 1
    assert result["refreshed"] == 1
    # It read the real-time plane (the option leg symbol was queried).
    assert any("RELIANCE27JUL3000CE" in c or "RELIANCE 3000 CE" in c for c in calls)


def test_fast_pass_uses_oc_chain_cache_when_ws_tick_absent(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path)
    _open_position(engine, entry=100.0)

    # No WS tick anywhere → fall through to the shared oc: chain cache overlay.
    _patch_live_mark(monkeypatch, lambda _sym: None)
    _patch_chain_cache(
        monkeypatch,
        {
            "spot_price": 2900.0,
            "entries": [
                {"strike": 3000.0, "option_type": "PE", "ltp": 999.0},
                {"strike": 3000.0, "option_type": "CE", "ltp": 60.0},  # breached
            ],
        },
    )

    result = asyncio.run(engine.refresh_paper_marks())

    closed = engine.paper.list_positions(status="closed")["closed_positions"]
    assert len(closed) == 1
    assert closed[0]["close_reason"] == "stop_loss"
    assert result["exits"] == 1


def test_fast_pass_skips_price_exit_when_no_fresh_mark(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path)
    _open_position(engine, entry=100.0)

    # No WS tick AND no chain-cache entry → fresh=False → price exits skipped.
    _patch_live_mark(monkeypatch, lambda _sym: None)
    _patch_chain_cache(monkeypatch, None)

    result = asyncio.run(engine.refresh_paper_marks())

    assert engine.paper.list_positions(status="open")["open_positions"]  # still open
    assert result["exits"] == 0
    assert result["refreshed"] == 0


def test_fast_pass_rejects_cross_wired_tick(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path)
    _open_position(engine, entry=100.0)

    # An index-magnitude value mis-attributed to the option (7x the premium):
    # the ratio guard must reject it, marking the leg not-fresh so no exit fires.
    _patch_live_mark(monkeypatch, lambda _sym: 700.0)
    _patch_chain_cache(monkeypatch, None)

    result = asyncio.run(engine.refresh_paper_marks())

    assert engine.paper.list_positions(status="open")["open_positions"]  # still open
    assert result["refreshed"] == 0


def test_fast_pass_never_opens_positions(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path)
    # Flat book; a WS mark exists but there is nothing to manage.
    _patch_live_mark(monkeypatch, lambda _sym: 123.0)

    result = asyncio.run(engine.refresh_paper_marks())

    assert result["positions"] == 0
    assert result["exits"] == 0
    assert engine.paper.list_positions(status="open")["open_positions"] == []
