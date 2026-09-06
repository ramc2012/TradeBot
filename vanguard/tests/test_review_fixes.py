"""Regression tests for defects an adversarial review confirmed on 2026-08-27.

Grouped in one file deliberately: each of these looks like an arbitrary
assertion in isolation, and keeping them together with the review date makes
it obvious they are locking in specific findings rather than describing
aspirational behaviour.
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parents[1]))

from backtest.exit_simulator import STOP_PCT, r_multiple  # noqa: E402
from backtest.harness import compute_metrics  # noqa: E402
from fusion.m7_risk import (  # noqa: E402
    WEEKLY_LOSS_STOP_PCT,
    RiskState,
    risk_check,
)
from paper import engine  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


class _NoOpCursor:
    def execute(self, sql, params=None):
        self._result = (params["known_at"],) if "FROM ingest_log" in sql else None
    def fetchone(self): return self._result
    def fetchall(self): return []
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _NoOpConnection:
    def cursor(self): return _NoOpCursor()


def _state(**kw):
    base = dict(capital=1_000_000.0, daily_pnl_pct=0.0, weekly_pnl_pct=0.0,
                open_positions=[], kelly_edge=None)
    base.update(kw)
    return RiskState(**base)


# ── the weekly loss stop is ENFORCED, not merely computed ──────────────────

def test_weekly_loss_stop_blocks_new_risk_even_when_no_single_day_breached():
    """The -4% weekly stop was computed into RiskState and then never
    consulted, so a book could bleed all week while every individual day
    stayed inside the -2% daily limit."""
    state = _state(daily_pnl_pct=-0.5, weekly_pnl_pct=WEEKLY_LOSS_STOP_PCT - 0.01)
    assert not state.stand_down, "the DAILY stop must not be what fires here"
    assert state.weekly_review_flag
    result = risk_check(state, _NoOpConnection(), symbol="TCS", sector20="IT",
                        entry_premium=50.0, stop_premium=42.5, lot_size=150,
                        as_of=date(2026, 8, 26))
    assert not result.allowed
    assert "WEEKLY-STOP" in result.reason


def test_weekly_pnl_exactly_at_the_stop_line_also_blocks():
    state = _state(weekly_pnl_pct=WEEKLY_LOSS_STOP_PCT)
    assert state.weekly_review_flag


def test_a_healthy_week_is_not_blocked():
    state = _state(weekly_pnl_pct=-1.0)
    result = risk_check(state, _NoOpConnection(), symbol="TCS", sector20="IT",
                        entry_premium=50.0, stop_premium=42.5, lot_size=150,
                        as_of=date(2026, 8, 26))
    assert result.allowed


# ── R has exactly ONE definition, shared by M8 and M9 ─────────────────────

def test_r_multiple_is_full_premium_not_stop_distance():
    """M8 divided by (entry * STOP_PCT) and M9 by the full premium — a 6.67x
    disagreement that made vanguard_backtest_runs and attribution_runs report
    R values in different units while labelling both 'avg_r'."""
    # entry 50 -> exit 60 is +20% of premium.
    assert r_multiple(50.0, 60.0) == 0.2
    stop_distance_r = (60.0 - 50.0) / (50.0 * STOP_PCT)
    assert abs(stop_distance_r - 1.3333) < 0.001
    assert r_multiple(50.0, 60.0) != round(stop_distance_r, 4)


def test_a_full_loss_of_premium_is_minus_one_r():
    assert r_multiple(50.0, 0.0) == -1.0


def test_r_multiple_returns_none_on_a_zero_entry_rather_than_raising():
    assert r_multiple(0.0, 10.0) is None


# ── M8 counts what it drops, and draws down in exit order ─────────────────

def _cand(symbol, ts, r, exit_ts, entry=50.0, exit_price=60.0, lot_size=150):
    return {"ts": ts, "symbol": symbol, "conviction": 90.0, "would_emit": True,
            "resolved": True,
            "exit": {"entry": entry, "exit_price": exit_price, "exit_reason": "target2",
                     "holding_bars": 1, "r_multiple": r, "lot_size": lot_size,
                     "exit_ts": exit_ts}}


def test_emitted_tickets_dropped_for_different_reasons_are_counted_separately():
    ts = datetime(2026, 8, 26, 10, 0, tzinfo=IST)
    result = {
        "all_candidates": [_cand("A", ts, 0.2, ts)],
        "emitted_trades": [
            {"ts": ts, "symbol": "A", "sizing_lots": 1},      # priced
            {"ts": ts, "symbol": "GHOST", "sizing_lots": 1},  # no exit match
            {"ts": ts, "symbol": "A", "sizing_lots": None},   # no sizing
        ],
    }
    metrics = compute_metrics(result)
    assert metrics["emitted_trades_closed"] == 1
    assert metrics["emitted_trades_unresolved"] == 1
    assert metrics["emitted_trades_without_sizing"] == 1


def test_max_drawdown_uses_exit_order_not_entry_order():
    """A trade entered later can close earlier. Ordering the P&L path by entry
    bar understates the trough."""
    early_entry = datetime(2026, 8, 26, 9, 30, tzinfo=IST)
    late_entry = datetime(2026, 8, 26, 14, 0, tzinfo=IST)
    # The LATE-entered trade closes FIRST and is the big loser.
    all_candidates = [
        _cand("WIN", early_entry, 1.0, datetime(2026, 8, 27, 15, 0, tzinfo=IST),
              entry=50.0, exit_price=100.0),
        _cand("LOSS", late_entry, -1.0, datetime(2026, 8, 26, 15, 0, tzinfo=IST),
              entry=50.0, exit_price=0.0),
    ]
    emitted = [{"ts": early_entry, "symbol": "WIN", "sizing_lots": 1},
               {"ts": late_entry, "symbol": "LOSS", "sizing_lots": 1}]
    metrics = compute_metrics({"all_candidates": all_candidates, "emitted_trades": emitted})
    # In EXIT order the loss lands first, so the equity path dips before it
    # recovers and the drawdown is real and negative.
    assert metrics["max_drawdown_rupees"] < 0


# ── M9 rolls up EVERY session a cycle touched, not just today ─────────────

class _DatesCursor:
    def __init__(self, rows): self._rows = rows
    def execute(self, *a, **k): pass
    def fetchall(self): return self._rows
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _DatesConnection:
    def __init__(self, rows): self._rows = rows
    def cursor(self): return _DatesCursor(self._rows)


def test_affected_capital_dates_includes_every_touched_session_sorted_oldest_first():
    """A T+3 sweep or a same-session walk can close positions dated days
    earlier; rolling up only as_of_date left that P&L invisible forever."""
    today = date(2026, 8, 26)
    conn = _DatesConnection([(date(2026, 8, 24),), (date(2026, 8, 25),)])
    days = engine._affected_capital_dates(conn, [1, 2], today)
    assert days == [date(2026, 8, 24), date(2026, 8, 25), today]
    assert days == sorted(days), "oldest first: each day's starting_equity chains off the prior"


def test_affected_capital_dates_is_just_today_when_nothing_closed():
    today = date(2026, 8, 26)
    assert engine._affected_capital_dates(_DatesConnection([]), [], today) == [today]
