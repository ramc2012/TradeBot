"""Guards the commodity state-integrity fixes (2026-06-04).

Two plumbing gaps caused the user-visible symptoms — an exit that audited but
never logged a trade, a "SELL entry @ 8929 but the position was never entered
at that price/time" audit, and that same entry audit repeating:

  1. **State-reload churn.** ``_refresh_state_from_store`` full-replaced the
     runtime from the shared ``commodity_strategy_state`` blob every time the DB
     row changed. A competing writer (a second worker, an admin/API call on
     another process, an out-of-band script) could therefore WIPE a just-opened
     position or RESURRECT a just-closed one via last-writer-wins. The running
     scan loop is the AUTHORITY on its own live positions, so a refresh now
     preserves them (``preserve_runtime=True``) and only syncs config/control.

  2. **Audited-but-unbooked exits.** The main stop/target/macd-reversal exit had
     its own inline close block that audited the exit but omitted the self-heal
     ``book_close`` — so a close could be audited with realized P&L left unbooked.
     Every futures exit now routes through ``_close_futures_position``, which
     BOOKS the trade (on_fill or self-heal) BEFORE auditing it, and removes the
     position from both the runtime map and the portfolio ledger.
"""
from __future__ import annotations

import asyncio

import paper_engine.commodity_strategy_agent as csa
from paper_engine.commodity_strategy_agent import CommodityPositionState, CommodityStrategyAgent


def _mk_position(**over) -> CommodityPositionState:
    base = dict(
        position_key="commodity_futures:MCX:CRUDEOIL26JUNFUT",
        symbol="MCX:CRUDEOIL26JUNFUT",
        live_symbol="MCX:CRUDEOIL26JUNFUT",
        underlying="CRUDEOIL",
        strategy_key="commodity_futures",
        strategy_title="MP+OF Futures",
        instrument_type="FUT",
        action="SELL",
        qty=200,
        lots=2,
        lot_size=100,
        entry_price=9090.0,
        current_price=9090.0,
        stop_price=9140.0,
        target_price=9000.0,
        regime="bear",
        signal_reason="ib_break",
        atr=40.0,
        macd_value=None,
        mp_poc=None,
        mp_vah=None,
        mp_val=None,
        entered_at="2026-06-04T10:00:00+05:30",
        entry_bar_time="2026-06-04T10:00:00+05:30",
        contract_unit_label="100 bbl",
        quote_unit_label="Rs / bbl",
        display_name="Crude Oil",
        initial_qty=200,
        peak_price=9090.0,
    )
    base.update(over)
    return CommodityPositionState(**base)


def test_preserve_runtime_does_not_wipe_live_positions():
    """A running loop's live position survives a refresh that reloads a
    competing/stale blob carrying no positions — but a fresh load still adopts
    the persisted state."""
    agent = CommodityStrategyAgent()
    pos = _mk_position()
    agent._runtime.positions = {pos.position_key: pos}

    # A competing writer persists a blob WITHOUT this position (the churn trigger).
    blob = agent._build_saved_state()
    blob["runtime"]["positions"] = []
    blob["runtime"]["portfolio"]["trade_history"] = []

    # Loop active (authority) → the live position MUST survive.
    agent._apply_saved_state(blob, preserve_runtime=True)
    assert pos.position_key in agent._runtime.positions, "live position wiped despite preserve_runtime"

    # Fresh load (no loop owning positions) → the persisted blob wins.
    agent._apply_saved_state(blob, preserve_runtime=False)
    assert pos.position_key not in agent._runtime.positions


def test_unified_close_books_before_removing_from_runtime(monkeypatch):
    """_close_futures_position books the trade (self-heal when on_fill can't
    match — the post-restart freeze) BEFORE auditing, then removes the position
    from the runtime map."""
    audited: dict = {}

    async def _fake_audit(**kw):
        # Capture the audit AND assert the trade was already booked when it fired.
        audited.update(kw)
        audited["_trades_at_audit_time"] = len(agent._runtime.portfolio._trade_history)

    monkeypatch.setattr(csa, "record_audit_event", _fake_audit)
    monkeypatch.setattr(csa, "record_paper_trade", lambda **kw: True)
    monkeypatch.setattr("paper_engine.costs.PAPER_APPLY_COSTS", False)  # WS-1.4: assert gross close math here

    agent = CommodityStrategyAgent()
    pos = _mk_position()
    agent._runtime.positions = {pos.position_key: pos}
    pf = agent._runtime.portfolio
    pf._positions = {}  # simulate post-restart: no open VirtualPosition for on_fill to match
    trades_before = len(pf._trade_history)

    asyncio.run(agent._close_futures_position(pos.position_key, pos, 9000.0, "stop_loss"))

    # Trade booked via self-heal — SELL 9090 → 9000 on 200 qty = +18,000.
    assert len(pf._trade_history) == trades_before + 1
    assert abs(pf._trade_history[-1].pnl - (9090.0 - 9000.0) * 200) < 1e-6
    # Booking happened BEFORE the audit fired (never audited-but-unbooked).
    assert audited.get("_trades_at_audit_time") == trades_before + 1
    assert audited.get("event_type") == "position_exit" and audited.get("new_state") == "closed"
    # Position removed from the runtime map.
    assert pos.position_key not in agent._runtime.positions


def test_init_does_not_persist_when_no_stored_row_was_read(monkeypatch):
    """A failed/absent state read must NEVER be written back as empty defaults.

    ``_load_saved_state`` silently falls back to normalized defaults
    (symbols=[]) when both the DB and disk reads come up empty, and
    ``load_runtime_state`` returns (None, None) when the pool can't be built.
    The constructor used to persist unconditionally, so one transient DB hiccup
    wrote those empties over a good universe — observed 2026-08-06 (8 -> []) and
    again 2026-08-14. ``load_commodity_history_rows`` builds a throwaway agent
    on EVERY call, so the blast radius is large.
    """
    writes: list[object] = []
    monkeypatch.setattr(
        csa, "_load_saved_state", lambda: (csa._normalize_saved_state(None), None)
    )
    monkeypatch.setattr(
        CommodityStrategyAgent, "_persist_state", lambda self: writes.append(1)
    )

    agent = CommodityStrategyAgent()

    assert agent._symbols == [], "defaults should still be applied in memory"
    assert writes == [], (
        "constructor persisted empty defaults after a failed/absent state read — "
        "this is the wipe that empties config.symbols"
    )


def test_init_persists_when_a_real_row_was_read(monkeypatch):
    """The guard must not disable the normal boot write for genuinely read state."""
    import datetime as _dt

    state = csa._normalize_saved_state({"config": {"symbols": ["MCX:GOLD26OCTFUT"]}})
    writes: list[object] = []
    monkeypatch.setattr(
        csa,
        "_load_saved_state",
        lambda: (state, _dt.datetime(2026, 8, 14, tzinfo=_dt.timezone.utc)),
    )
    monkeypatch.setattr(
        CommodityStrategyAgent, "_persist_state", lambda self: writes.append(1)
    )

    agent = CommodityStrategyAgent()

    assert agent._symbols == ["MCX:GOLD26OCTFUT"]
    assert writes == [1], "a real stored row should still be persisted on boot"


def test_owning_plane_never_infers_loop_liveness_from_the_blob(monkeypatch):
    """A restart must not be fooled by its own just-written heartbeat.

    Under split boot `_loop_active()` falls back to the persisted
    `loop_heartbeat_at`. On restart that blob is seconds old, so the STRATEGY
    plane — which owns the task — saw "already active" before its new task
    existed, and `start()` bailed at `if self._loop_active(): return` (force=True
    does NOT bypass that line). The task was never created, the heartbeat went
    stale, and nothing re-checked: the commodity scan was dead for all of
    2026-08-18 and 08-19, dropping MCX collection from 8 roots to 3.
    """
    import core.laneset as laneset
    from datetime import datetime, timedelta, timezone

    IST = timezone(timedelta(hours=5, minutes=30))
    agent = CommodityStrategyAgent()
    agent._task = None
    # A heartbeat written moments ago — exactly the post-restart condition.
    agent._loop_heartbeat_at = datetime.now(IST).isoformat()

    monkeypatch.setattr(laneset, "is_split", lambda: True)

    # The plane that OWNS the loop: no task means not active, whatever the blob says.
    monkeypatch.setattr(laneset, "boots_strategies", lambda: True)
    assert agent._loop_active() is False, (
        "owning plane trusted a stale-on-restart heartbeat — start() will skip "
        "creating the task and the lane goes silently dead"
    )

    # The CORE plane has no local task by design and still reads the heartbeat.
    monkeypatch.setattr(laneset, "boots_strategies", lambda: False)
    assert agent._loop_active() is True, "core plane must still derive liveness from the blob"


def test_malformed_nonempty_input_is_rejected_not_wiped(monkeypatch):
    """A non-empty request that normalizes to nothing must be REFUSED, not
    silently applied as an empty universe.

    Observed 2026-08-27 00:01 IST: config.symbols went 8 -> 0 with no restart,
    no PG storm, and no internal caller anywhere in the codebase besides
    `update_symbols` itself (its only call site is the PUT
    /strategy-agent/config endpoint) — so it was an external HTTP call.
    `_normalize_symbols` silently drops anything without a ":", so a caller
    passing token-format instrument keys (MCX_FO|483079) instead of symbol
    strings (MCX:GOLD26OCTFUT) collapses to []. That shape - non-empty input,
    empty output - is the signature of a caller bug, not a deliberate clear.
    """
    agent = CommodityStrategyAgent()
    agent._symbols = ["MCX:GOLD26OCTFUT", "MCX:SILVERM26AUGFUT"]
    monkeypatch.setattr(agent, "_refresh_state_from_store", lambda: None)
    monkeypatch.setattr(agent, "_persist_state", lambda: None)

    result = agent.update_symbols(["MCX_FO|483079", "MCX_FO|471726"])

    assert result.get("error") == "no_valid_symbols_in_request"
    assert agent._symbols == ["MCX:GOLD26OCTFUT", "MCX:SILVERM26AUGFUT"], (
        "a malformed non-empty request wiped the universe instead of being refused"
    )


def test_explicit_empty_list_still_clears_the_universe(monkeypatch):
    """The guard must not block a genuine, deliberate clear."""
    agent = CommodityStrategyAgent()
    agent._symbols = ["MCX:GOLD26OCTFUT"]
    monkeypatch.setattr(agent, "_refresh_state_from_store", lambda: None)
    monkeypatch.setattr(agent, "_persist_state", lambda: None)

    result = agent.update_symbols([])

    assert result == {"symbols": []}
    assert agent._symbols == []


def test_valid_nonempty_input_still_updates_normally(monkeypatch):
    agent = CommodityStrategyAgent()
    agent._symbols = []
    monkeypatch.setattr(agent, "_refresh_state_from_store", lambda: None)
    monkeypatch.setattr(agent, "_persist_state", lambda: None)

    result = agent.update_symbols(["mcx:gold26octfut", " MCX:SILVERM26AUGFUT "])

    assert result == {"symbols": ["MCX:GOLD26OCTFUT", "MCX:SILVERM26AUGFUT"]}
    assert agent._symbols == ["MCX:GOLD26OCTFUT", "MCX:SILVERM26AUGFUT"]


def test_refresh_never_drains_a_live_running_loop_to_empty(monkeypatch):
    """`_apply_saved_state` used to assign `self._symbols` unconditionally, with
    no protection analogous to the positions guard just below it.

    Observed 2026-08-27 11:34 IST: config.symbols went 8 -> 0 with the loop
    heartbeat still advancing (loop alive), no restart, and no HTTP call to
    the config endpoint on either plane in the prior 8 hours - ruling out both
    the already-fixed restart race and a legitimate update_symbols() call. The
    DB row itself had already gone empty; this method then faithfully
    propagated that corruption into the authoritative, running instance.
    """
    agent = CommodityStrategyAgent()
    agent._symbols = ["MCX:GOLD26OCTFUT", "MCX:SILVERM26AUGFUT"]

    corrupted_state = {
        "config": {"symbols": [], "lots_per_trade": 1},
        "control": {},
        "runtime": {},
    }
    agent._apply_saved_state(corrupted_state, preserve_runtime=True)

    assert agent._symbols == ["MCX:GOLD26OCTFUT", "MCX:SILVERM26AUGFUT"], (
        "a refresh drained the live loop's universe to empty instead of "
        "refusing the regression"
    )


def test_refresh_still_applies_a_legitimate_nonempty_change(monkeypatch):
    """The guard must not block ordinary cross-worker propagation - e.g.
    widening the universe from another process while the loop keeps running."""
    agent = CommodityStrategyAgent()
    agent._symbols = ["MCX:GOLD26OCTFUT"]

    widened_state = {
        "config": {"symbols": ["MCX:GOLD26OCTFUT", "MCX:SILVERM26AUGFUT"], "lots_per_trade": 1},
        "control": {},
        "runtime": {},
    }
    agent._apply_saved_state(widened_state, preserve_runtime=True)

    assert agent._symbols == ["MCX:GOLD26OCTFUT", "MCX:SILVERM26AUGFUT"]


def test_refresh_applies_empty_when_the_loop_is_not_running(monkeypatch):
    """Outside `preserve_runtime` (no live loop to protect - e.g. the CORE
    plane's own mirror instance, or a fresh construction) a genuinely-empty
    DB row must still load normally; this guard is scoped to the running-loop
    case only, not a blanket refusal."""
    agent = CommodityStrategyAgent()
    agent._symbols = ["MCX:GOLD26OCTFUT"]

    empty_state = {
        "config": {"symbols": [], "lots_per_trade": 1},
        "control": {},
        "runtime": {"watchlist": [], "futures_watchlist": [], "positions": []},
    }
    agent._apply_saved_state(empty_state, preserve_runtime=False)

    assert agent._symbols == []
