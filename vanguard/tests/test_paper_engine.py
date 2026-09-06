"""Offline tests for M9's own orchestration logic (fill/walk/flatten/
capital-rollup ordering and arithmetic), using the same scripted fake
connection style as test_m6_select.py. walk_open_positions/apply_standdown_
flatten/force_close_stale_positions each drive real SQL against a fake
cursor, so these fakes model exact call order rather than monkeypatching."""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parents[1]))

from paper import engine  # noqa: E402
from paper.engine import (  # noqa: E402
    INITIAL_CAPITAL,
    current_capital,
    fill_pending_tickets,
    update_paper_capital,
)

IST = ZoneInfo("Asia/Kolkata")


class _ScriptedCursor:
    def __init__(self, queue, log):
        self._queue = queue
        self._log = log
        self._current = None

    def execute(self, sql, params=None):
        self._log.append((sql.strip().split()[0], params))
        self._current = self._queue.pop(0)

    def fetchall(self):
        return self._current if self._current is not None else []

    def fetchone(self):
        return self._current

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _ScriptedConnection:
    def __init__(self, results):
        self._queue = list(results)
        self.log = []

    def cursor(self):
        return _ScriptedCursor(self._queue, self.log)


def test_current_capital_falls_back_to_initial_when_no_prior_row_exists():
    conn = _ScriptedConnection([None])
    assert current_capital(conn, date(2026, 8, 26)) == INITIAL_CAPITAL


def test_current_capital_uses_the_latest_prior_days_ending_equity():
    conn = _ScriptedConnection([(1_050_000.0,)])
    assert current_capital(conn, date(2026, 8, 26)) == 1_050_000.0


def test_fill_pending_tickets_recovers_entry_premium_as_the_entry_zone_midpoint():
    """entry_zone_low/high were stored as entry*0.98/entry*1.02 by M6 --
    the midpoint must round-trip back to the original entry premium."""
    ticket_ts = datetime(2026, 8, 26, 9, 45, tzinfo=IST)
    as_of_ts = datetime(2026, 8, 26, 10, 0, tzinfo=IST)
    conn = _ScriptedConnection([
        [(101, ticket_ts, 49.0, 51.0)],   # pending: id, ts, entry_zone_low, entry_zone_high
        None, None, None,                  # INSERT decisions / INSERT fills / INSERT outcomes
    ])
    filled = fill_pending_tickets(conn, as_of_ts)
    assert filled == [101]
    # execute() call order: SELECT pending, INSERT decisions, INSERT fills, INSERT outcomes
    _, fills_params = conn.log[2]
    assert fills_params == (101, 50.0, ticket_ts)  # fill_ts is the TICKET's own ts, not as_of_ts


def test_fill_pending_tickets_is_a_noop_when_nothing_is_pending():
    conn = _ScriptedConnection([[]])
    assert fill_pending_tickets(conn, datetime(2026, 8, 26, 10, 0, tzinfo=IST)) == []


def test_update_paper_capital_compounds_starting_equity_plus_realized_pnl():
    conn = _ScriptedConnection([
        (1_000_000.0,),      # current_capital: prior ending_equity (fetchone-shaped)
        (12_500.0,),         # realized pnl sum for the day
        None,                 # upsert
    ])
    update_paper_capital(conn, date(2026, 8, 26))
    upsert_params = conn.log[-1][1]
    assert upsert_params["starting_equity"] == 1_000_000.0
    assert upsert_params["realized_pnl"] == 12_500.0
    assert upsert_params["ending_equity"] == 1_012_500.0


def test_update_paper_capital_handles_a_losing_day_without_fabricating_a_floor():
    conn = _ScriptedConnection([
        (1_000_000.0,),
        (-30_000.0,),
        None,
    ])
    update_paper_capital(conn, date(2026, 8, 26))
    upsert_params = conn.log[-1][1]
    assert upsert_params["ending_equity"] == 970_000.0


def test_run_cycle_calls_every_stage_in_the_documented_order(monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "current_capital", lambda c, d, ic: calls.append("capital") or 1_000_000.0)
    monkeypatch.setattr(engine, "apply_standdown_flatten", lambda c, ts, cap: calls.append("standdown") or [])
    monkeypatch.setattr(engine, "fill_pending_tickets", lambda c, ts: calls.append("fill") or [])
    monkeypatch.setattr(engine, "walk_open_positions", lambda c, ts: calls.append("walk") or [])
    monkeypatch.setattr(engine, "force_close_stale_positions", lambda c, ts: calls.append("t3") or [])
    monkeypatch.setattr(engine, "update_paper_capital", lambda c, d, ic: calls.append("capital_update"))
    engine.run_cycle(object(), datetime(2026, 8, 26, 10, 0, tzinfo=IST))
    assert calls == ["capital", "standdown", "fill", "walk", "t3", "capital_update"]
