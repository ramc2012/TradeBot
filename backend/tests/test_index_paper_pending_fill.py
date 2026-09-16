"""The parked-entry path: a decision made at bar t, filled when its +1 bar prints.

The defect this covers (2026-09-15): live, the +1 fill bar is still arriving when
the decision is made — the option-chain sweep lands 45-60 minutes behind its bar
— so the lane refused every candidate with `no_lagged_fill` while the contract
printed minutes later. 7 of 7 BANKNIFTY refusals that session, 8 on 11-Sep, 8 on
08-Sep, and zero entries.

Parking must not become a licence to fill at a better price: every test here
pins the fill to the +1 bar's OWN observed close.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import directional_options.index_paper.engine as engine
from directional_options.index_paper.engine import IndexPaperConfig, PassResult

BAR = datetime(2026, 9, 15, 7, 45, tzinfo=timezone.utc)      # 13:15 IST
FILL_BAR = BAR + timedelta(minutes=30)                        # 13:45 IST
EXPIRY = date(2026, 9, 29)


def _payload(**over):
    payload = {
        "side": "CE", "strike": 56300.0, "expiry": EXPIRY.isoformat(),
        "candidate": {"strike": 56300.0}, "sizing": {"lots": 1}, "panel": {},
        "vol_context": {}, "gates": [], "direction_score": 0.4,
        "round_trip_cost_fraction": 0.02, "signal_price": 667.2,
        "quantity": 35, "lots": 1, "lot_size": 35,
        "volume": 1000, "oi": 208320, "log_moneyness": 0.01,
        "implied_vol": 0.14, "vega": 12.0,
        "greeks": {"delta": 0.45, "gamma": 0.0001, "vega": 12.0, "theta": -8.0},
        "mv_delta": 0.44, "forward": 56000.0, "spot": 55908.0,
        "planned_sessions": 3, "adverse_move_per_unit": 120.0,
        "breakeven_ratio": 0.33,
    }
    payload.update(over)
    return payload


def _pending(**over):
    row = {
        "underlying": "BANKNIFTY", "session_date": BAR.date(), "bar_ts": BAR,
        "fill_bar_ts": FILL_BAR, "run_id": "live-test", "payload": _payload(),
    }
    row.update(over)
    return row


@pytest.fixture
def harness(monkeypatch):
    state = SimpleNamespace(
        opened=[], resolved=[], decisions=[], fills=[], quote=None,
        bars_after={}, open_positions=[], latest=FILL_BAR,
    )

    async def open_pending_entries(underlying):
        return list(state.pending)

    async def latest_bar_ts(underlying, as_of=None):
        return state.latest

    async def contract_quote(underlying, bar_ts, expiry, strike, option_type, **kw):
        return state.quote

    async def next_bars(underlying, after, lag, **kw):
        return list(state.bars_after.get(after, []))

    async def resolve_pending_entry(underlying, bar_ts, status, resolution):
        state.resolved.append((underlying, bar_ts, status, resolution))

    async def list_open():
        return list(state.open_positions)

    async def open_position(position):
        state.opened.append(position)

    async def record_fill(**kw):
        state.fills.append(kw)

    async def record_decision(**kw):
        state.decisions.append(kw)

    monkeypatch.setattr(engine, "open_pending_entries", open_pending_entries)
    monkeypatch.setattr(engine, "latest_bar_ts", latest_bar_ts)
    monkeypatch.setattr(engine, "contract_quote", contract_quote)
    monkeypatch.setattr(engine, "next_bars", next_bars)
    monkeypatch.setattr(engine, "resolve_pending_entry", resolve_pending_entry)
    monkeypatch.setattr(engine.book, "list_open", list_open)
    monkeypatch.setattr(engine.book, "open_position", open_position)
    monkeypatch.setattr(engine.book, "new_position_id", lambda underlying: "pos-1")
    monkeypatch.setattr(engine.journal, "record_fill", record_fill)
    monkeypatch.setattr(engine.journal, "record_decision", record_decision)
    state.pending = [_pending()]
    return state


async def _run(state):
    result = PassResult("BANKNIFTY", FILL_BAR, "ok")
    await engine.fill_pending_entries("BANKNIFTY", IndexPaperConfig(), "live-test", result)
    return result


@pytest.mark.asyncio
async def test_parked_decision_fills_at_the_plus_one_bars_own_close(harness):
    harness.quote = {"close": 642.0}

    result = await _run(harness)

    assert result.entries == 1
    position = harness.opened[0]
    assert position.entry_ts == FILL_BAR, "the fill belongs to the +1 bar, not the decision bar"
    # Always-adverse: a buy never fills below the observed close, and never at
    # the decision bar's own (better) price.
    assert position.entry_premium >= 642.0
    assert position.payload["fill_reference_price"] == 642.0
    assert position.payload["signal_price"] == 667.2
    assert position.payload["deferred_fill"] is True
    assert harness.resolved == [("BANKNIFTY", BAR, "filled", "pos-1")]


@pytest.mark.asyncio
async def test_a_contract_that_never_printed_is_refused_once_the_bar_is_closed(harness):
    harness.quote = None
    harness.bars_after = {FILL_BAR: [FILL_BAR + timedelta(minutes=30)]}

    result = await _run(harness)

    assert result.entries == 0 and not harness.opened
    assert harness.resolved[0][2] == "refused"
    assert harness.decisions[0]["reason_code"] == "no_lagged_fill"


@pytest.mark.asyncio
async def test_a_still_arriving_bar_stays_parked_rather_than_refused(harness):
    """The whole point: no later bar exists, so the fill bar is still filling."""
    harness.quote = None
    harness.bars_after = {}

    result = await _run(harness)

    assert result.entries == 0
    assert harness.resolved == [] and harness.decisions == []


@pytest.mark.asyncio
async def test_a_decision_never_crosses_a_session_boundary(harness):
    harness.quote = {"close": 642.0}
    harness.latest = BAR + timedelta(days=1)

    result = await _run(harness)

    assert result.entries == 0 and not harness.opened
    assert harness.resolved[0][2] == "expired"


@pytest.mark.asyncio
async def test_exposure_is_rechecked_at_fill_time_not_only_at_decision_time(harness):
    harness.quote = {"close": 642.0}
    harness.open_positions = [SimpleNamespace(underlying="BANKNIFTY")]

    result = await _run(harness)

    assert result.entries == 0 and not harness.opened
    assert harness.resolved[0][2] == "expired"
    assert "exposure" in harness.resolved[0][3]


@pytest.mark.asyncio
async def test_session_exclusivity_counts_entries_and_ignores_the_run_id(monkeypatch):
    """`one_entry_decision_per_session` means one ENTRY, not one decision per run.

    Scoping by run_id made every 15-minute pass re-decide the same bar: 25
    journal rows for 12 real decisions on 2026-09-15.
    """
    seen = {}

    async def already_entered_this_session(underlying, session_date):
        seen["args"] = (underlying, session_date)
        return True

    monkeypatch.setattr(engine, "already_entered_this_session", already_entered_this_session)

    assert await engine.already_decided_this_session("NIFTY", BAR.date(), "run-A") is True
    assert await engine.already_decided_this_session("NIFTY", BAR.date(), None) is True
    assert seen["args"] == ("NIFTY", BAR.date())
