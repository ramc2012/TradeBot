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
