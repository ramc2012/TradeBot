"""Regression tests for the 2026-06-10 directional paper-book fixes.

The 6-session audit (06-03..06-10) found: (1) 91/112 closes carried a phantom
mark (exit_premium == entry_premium) because the close path never saw the
chain-cache mark; (2) the configured stop/target/trail/expiry exits existed
only in the backtester — the live book's exits were 100% signal-driven;
(3) 25/46 "signal_flip" closes were same-direction strike re-ranks that paid a
full round-trip charge stack to move one strike; (4) entries and flips had no
hysteresis — a single noisy ~60s cycle could open or reverse a position.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import directional_options.chain_analytics as chain_mod
from directional_options.paper import DirectionalOptionsPaperStore


def _payload(
    *,
    direction: str | None = "CE",
    option_price: float = 132.0,
    strike: float = 22500.0,
    trading_symbol: str = "NIFTY 22500 CE",
    instrument_key: str = "NSE_FO|NIFTY22500CE",
    as_of: str = "2026-04-21T09:45:00+00:00",
    approved: bool = True,
    execution_ready: bool = True,
) -> dict:
    signal = (
        {
            "direction": direction,
            "confidence": 0.7,
            "expected_move": 100.0,
            "expected_horizon_bars": 8,
        }
        if direction
        else None
    )
    contract = (
        {
            "trading_symbol": trading_symbol,
            "instrument_key": instrument_key,
            "option_type": direction,
            "expiry": "2026-04-30",
            "expiry_kind": "weekly",
            "strike": strike,
            "option_price": option_price,
            "expected_pnl": 18.0,
            "price_source": "local_watchlist",
        }
        if direction
        else None
    )
    return {
        "selection": {"underlying": "NIFTY", "timeframe": "5minute", "lookback_sessions": 16},
        "snapshot": {
            "as_of": as_of,
            "underlying": "NIFTY",
            "timeframe": "5minute",
            "spot_price": 22512.5,
            "signal": signal,
            "regime": {"label": "trend"},
            "selected_contract": contract,
            "risk": {"approved": approved, "quantity_lots": 1, "quantity_units": 75},
            "selection_reason": "test",
            "data_status": {"execution_ready": execution_ready},
        },
    }


def _no_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _none(*_a, **_k):
        return None

    monkeypatch.setattr(chain_mod, "ensure_chain_tracked", _none)
    monkeypatch.setattr(chain_mod, "chain_strike_mark", _none)


def _isolate(store: DirectionalOptionsPaperStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Detach the store from the shared Postgres book.

    The paper book lives in directional_paper_positions/journal DB tables, so
    un-patched tests read whatever the local DB happens to contain (the two
    long-failing legacy paper tests count real synced rows). These tests
    exercise sync_snapshot logic only.
    """
    state: dict[str, list] = {"open_positions": [], "closed_positions": []}

    async def _load() -> dict:
        return {
            "open_positions": [dict(r) for r in state["open_positions"]],
            "closed_positions": [dict(r) for r in state["closed_positions"]],
        }

    async def _save(payload: dict) -> None:
        state["open_positions"] = [dict(r) for r in payload.get("open_positions", [])]
        state["closed_positions"] = [dict(r) for r in payload.get("closed_positions", [])]

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(store, "_load_positions", _load)
    monkeypatch.setattr(store, "_save_positions", _save)
    monkeypatch.setattr(store, "_append_journal", _noop)

    async def _summary(open_positions, closed_positions):
        return {
            "open_positions": len(open_positions),
            "closed_positions": len(closed_positions),
        }

    monkeypatch.setattr(store, "_summary", _summary)
    import directional_options.paper as paper_mod

    monkeypatch.setattr(paper_mod.paper_trade_recorder, "record_event", _noop)


@pytest.mark.asyncio
async def test_premium_stop_fires_on_real_mark_ignoring_min_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_chain(monkeypatch)
    store = DirectionalOptionsPaperStore(
        tmp_path / "p",
        min_hold_bars=3,
        min_hold_floor_minutes=8.0,
        planned_stop_pct=0.35,
        profit_target_pct=0.45,
        trail_giveback_pct=0.18,
    )
    _isolate(store, monkeypatch)
    await store.sync_snapshot(_payload())
    open_rows = (await store.list_positions(symbol="NIFTY", status="open"))["open_positions"]
    pid = open_rows[0]["position_id"]

    # One minute later (far inside min-hold) the mark breaches the 35% stop —
    # risk exits must NOT wait for min-hold.
    # approved=False makes the cycle non-actionable: a risk exit must fire
    # regardless, and no same-cycle re-entry muddies the assertion.
    summary = await store.sync_snapshot(
        _payload(as_of="2026-04-21T09:46:00+00:00", approved=False),
        position_marks={pid: {"premium": 80.0, "spot": 22400.0, "price_source": "test"}},
    )
    closed = (await store.list_positions(symbol="NIFTY", status="closed"))["closed_positions"]
    assert summary["open_positions"] == 0
    assert closed[0]["close_reason"] == "premium_stop"
    assert closed[0]["exit_premium"] == 80.0


@pytest.mark.asyncio
async def test_trail_take_profit_rides_then_closes_on_giveback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_chain(monkeypatch)
    store = DirectionalOptionsPaperStore(
        tmp_path / "p",
        planned_stop_pct=0.35,
        profit_target_pct=0.45,
        trail_giveback_pct=0.18,
    )
    _isolate(store, monkeypatch)
    await store.sync_snapshot(_payload())
    pid = (await store.list_positions(symbol="NIFTY", status="open"))["open_positions"][0]["position_id"]

    # Peak clears entry*(1+45%) = 191.4 — rides (no close at the target).
    summary = await store.sync_snapshot(
        _payload(as_of="2026-04-21T10:00:00+00:00"),
        position_marks={pid: {"premium": 200.0, "spot": 22600.0, "price_source": "test"}},
    )
    assert summary["open_positions"] == 1

    # Gives back >18% from the 200 peak (<=164) — trail closes.
    summary = await store.sync_snapshot(
        _payload(as_of="2026-04-21T10:05:00+00:00", approved=False),
        position_marks={pid: {"premium": 163.0, "spot": 22550.0, "price_source": "test"}},
    )
    closed = (await store.list_positions(symbol="NIFTY", status="closed"))["closed_positions"]
    assert summary["open_positions"] == 0
    assert closed[0]["close_reason"] == "trail_take_profit"
    assert closed[0]["peak_premium"] == 200.0


@pytest.mark.asyncio
async def test_no_phantom_stop_without_a_fresh_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_chain(monkeypatch)
    store = DirectionalOptionsPaperStore(tmp_path / "p", planned_stop_pct=0.35)
    _isolate(store, monkeypatch)
    await store.sync_snapshot(_payload())
    # No caller marks, chain cache empty — the ladder must skip, never
    # evaluate a stop off the entry-frozen premium.
    summary = await store.sync_snapshot(_payload(as_of="2026-04-21T09:50:00+00:00"))
    assert summary["open_positions"] == 1


@pytest.mark.asyncio
async def test_same_direction_strike_rerank_refreshes_instead_of_flipping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_chain(monkeypatch)
    store = DirectionalOptionsPaperStore(tmp_path / "p")
    _isolate(store, monkeypatch)
    await store.sync_snapshot(_payload())

    # Same direction, different strike candidate (the Thompson picker
    # wandered one strike). Must refresh the HELD contract, not close+reopen.
    summary = await store.sync_snapshot(
        _payload(
            as_of="2026-04-21T11:00:00+00:00",
            strike=22600.0,
            trading_symbol="NIFTY 22600 CE",
            instrument_key="NSE_FO|NIFTY22600CE",
            option_price=140.0,
        )
    )
    open_rows = (await store.list_positions(symbol="NIFTY", status="open"))["open_positions"]
    closed = (await store.list_positions(symbol="NIFTY", status="closed"))["closed_positions"]
    assert summary["open_positions"] == 1
    assert closed == []
    assert open_rows[0]["strike"] == 22500.0  # original contract retained
    # The candidate's premium belongs to a DIFFERENT contract — must not be
    # applied to the held position.
    assert open_rows[0]["latest_premium"] != 140.0


@pytest.mark.asyncio
async def test_entry_and_flip_require_signal_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_chain(monkeypatch)
    store = DirectionalOptionsPaperStore(
        tmp_path / "p",
        min_hold_bars=0,
        signal_persistence_cycles=3,
    )
    _isolate(store, monkeypatch)

    # Entry gate: cycles 1 and 2 must not open; cycle 3 opens.
    assert (await store.sync_snapshot(_payload()))["open_positions"] == 0
    assert (await store.sync_snapshot(_payload()))["open_positions"] == 0
    assert (await store.sync_snapshot(_payload()))["open_positions"] == 1

    # Flip gate: PE cycles 1 and 2 must hold the CE; cycle 3 flips it.
    pe = lambda as_of: _payload(  # noqa: E731
        direction="PE",
        strike=22400.0,
        trading_symbol="NIFTY 22400 PE",
        instrument_key="NSE_FO|NIFTY22400PE",
        option_price=110.0,
        as_of=as_of,
    )
    assert (await store.sync_snapshot(pe("2026-04-21T10:01:00+00:00")))["open_positions"] == 1
    assert (await store.sync_snapshot(pe("2026-04-21T10:02:00+00:00")))["open_positions"] == 1
    summary = await store.sync_snapshot(pe("2026-04-21T10:03:00+00:00"))
    closed = (await store.list_positions(symbol="NIFTY", status="closed"))["closed_positions"]
    open_rows = (await store.list_positions(symbol="NIFTY", status="open"))["open_positions"]
    assert closed[0]["close_reason"] == "signal_flip"
    assert closed[0]["option_type"] == "CE"
    # The PE entry is blocked by the post-whipsaw re-entry cooldown only when
    # configured; with defaults (0) the persistent PE signal opens immediately.
    assert summary["open_positions"] == 1
    assert open_rows[0]["option_type"] == "PE"


@pytest.mark.asyncio
async def test_chain_mark_injection_feeds_the_close_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _track(*_a, **_k):
        return None

    async def _mark(*_a, **_k):
        return 99.0

    monkeypatch.setattr(chain_mod, "ensure_chain_tracked", _track)
    monkeypatch.setattr(chain_mod, "chain_strike_mark", _mark)
    store = DirectionalOptionsPaperStore(tmp_path / "p")
    _isolate(store, monkeypatch)
    await store.sync_snapshot(_payload())

    # Flat cycle with NO caller marks: the chain-cache injection must supply
    # the close mark — exit at 99.0, not frozen at the 132.0 entry.
    summary = await store.sync_snapshot(
        _payload(direction=None, approved=False, as_of="2026-04-21T10:30:00+00:00")
    )
    closed = (await store.list_positions(symbol="NIFTY", status="closed"))["closed_positions"]
    assert summary["open_positions"] == 0
    assert closed[0]["close_reason"] == "flat_signal"
    assert closed[0]["exit_premium"] == 99.0
    assert closed[0]["price_source"] == "chain_cache_live"
    assert closed[0]["realized_pnl"] < (99.0 - 132.0) * 75 + 1  # real loss net of charges
