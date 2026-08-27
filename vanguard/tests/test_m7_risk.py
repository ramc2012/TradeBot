"""Offline tests for M7's pure sizing/gate logic -- no network, no database
except where a real connection is unavoidable (event_guard_blocks), which
uses a throwaway table-free query path exercised via a fake cursor."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from fusion.m7_risk import (  # noqa: E402
    DAILY_LOSS_STOP_PCT,
    MAX_CONCURRENT_POSITIONS,
    MAX_PORTFOLIO_HEAT_PCT,
    MAX_POSITIONS_PER_SECTOR20,
    RISK_PER_TRADE_HARD_CAP_PCT,
    RISK_PER_TRADE_PCT,
    RiskState,
    SizingResult,
    kelly_risk_pct,
    risk_check,
)


def _state(capital=1_000_000.0, daily_pnl_pct=0.0, weekly_pnl_pct=0.0,
           open_positions=None, kelly_edge=None) -> RiskState:
    return RiskState(
        capital=capital,
        daily_pnl_pct=daily_pnl_pct,
        weekly_pnl_pct=weekly_pnl_pct,
        open_positions=open_positions or [],
        kelly_edge=kelly_edge,
    )


class _NoOpCursor:
    def execute(self, *a, **k):
        self._result = None
    def fetchone(self):
        return None
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class _NoOpConnection:
    """Stands in for a psycopg2 connection when the code path under test
    never actually needs results_calendar data (i.e. every candidate here
    resolves before or without reaching the event-guard query)."""
    def cursor(self):
        return _NoOpCursor()


def test_kelly_fraction_matches_hand_computed_value():
    # p=0.55, avg_win=1.8R, avg_loss=1.0R -> b=1.8
    # f* = p - (1-p)/b = 0.55 - 0.45/1.8 = 0.55 - 0.25 = 0.30
    # 0.25x Kelly -> 0.075 -> 7.5% -- clamped by the 1.0% hard cap to 1.0%
    edge = {"p": 0.55, "b": 1.8, "n": 60}
    assert kelly_risk_pct(edge) == RISK_PER_TRADE_HARD_CAP_PCT


def test_kelly_fraction_clamps_a_negative_edge_to_zero_not_negative_sizing():
    # p=0.3, b=1.0 -> f* = 0.3 - 0.7/1.0 = -0.4 (a real losing system)
    edge = {"p": 0.3, "b": 1.0, "n": 60}
    assert kelly_risk_pct(edge) == 0.0


def test_no_edge_yet_falls_back_to_fixed_fractional():
    assert kelly_risk_pct(None) == RISK_PER_TRADE_PCT


def test_stand_down_blocks_every_new_ticket_regardless_of_everything_else():
    state = _state(daily_pnl_pct=DAILY_LOSS_STOP_PCT - 0.01)
    assert state.stand_down
    result = risk_check(state, _NoOpConnection(), symbol="TCS", sector20="IT",
                        entry_premium=50.0, stop_premium=35.0, lot_size=150, as_of=date(2026, 8, 26))
    assert not result.allowed
    assert "STAND-DOWN" in result.reason


def test_daily_pnl_exactly_at_the_stop_line_also_triggers_stand_down():
    """<= per the spec's own wording, not strictly <."""
    state = _state(daily_pnl_pct=DAILY_LOSS_STOP_PCT)
    assert state.stand_down


def test_max_concurrent_positions_is_enforced():
    positions = [{"ticket_id": i, "symbol": f"SYM{i}", "sector20": "X", "risk_rupees": 1000.0}
                 for i in range(MAX_CONCURRENT_POSITIONS)]
    state = _state(open_positions=positions)
    result = risk_check(state, _NoOpConnection(), symbol="NEW", sector20="Y",
                        entry_premium=50.0, stop_premium=35.0, lot_size=150, as_of=date(2026, 8, 26))
    assert not result.allowed
    assert "concurrent" in result.reason


def test_max_positions_per_sector20_is_enforced_independent_of_the_concurrent_cap():
    positions = [{"ticket_id": i, "symbol": f"SYM{i}", "sector20": "Banking", "risk_rupees": 1000.0}
                 for i in range(MAX_POSITIONS_PER_SECTOR20)]
    state = _state(open_positions=positions)  # below MAX_CONCURRENT_POSITIONS
    result = risk_check(state, _NoOpConnection(), symbol="NEW", sector20="Banking",
                        entry_premium=50.0, stop_premium=35.0, lot_size=150, as_of=date(2026, 8, 26))
    assert not result.allowed
    assert "sector20=Banking" in result.reason


def test_a_different_sector20_is_not_blocked_by_another_sector_s_positions():
    positions = [{"ticket_id": i, "symbol": f"SYM{i}", "sector20": "Banking", "risk_rupees": 1000.0}
                 for i in range(MAX_POSITIONS_PER_SECTOR20)]
    state = _state(open_positions=positions)
    result = risk_check(state, _NoOpConnection(), symbol="NEW", sector20="IT Services",
                        entry_premium=50.0, stop_premium=35.0, lot_size=150, as_of=date(2026, 8, 26))
    assert result.allowed


def test_portfolio_heat_cap_blocks_a_new_ticket_once_at_the_ceiling():
    state = _state(capital=1_000_000.0,
                   open_positions=[{"ticket_id": 1, "symbol": "X", "sector20": "A", "risk_rupees": 25_000.0}])
    assert state.portfolio_heat_pct == MAX_PORTFOLIO_HEAT_PCT
    result = risk_check(state, _NoOpConnection(), symbol="NEW", sector20="B",
                        entry_premium=50.0, stop_premium=35.0, lot_size=150, as_of=date(2026, 8, 26))
    assert not result.allowed
    assert "heat" in result.reason


def test_sizing_uses_the_smaller_of_risk_budget_and_premium_cap_lots():
    """Risk budget at 0.75% of 10L = Rs 7,500. Premium cap at 1.5% of 10L =
    Rs 15,000. Per-lot premium risk = 50 * 150 = Rs 7,500 (doctrine: FULL
    premium, not stop-distance). risk-budget allows 1 lot (7500/7500);
    premium-cap allows 2 lots (15000/7500) -- the binding constraint (the
    smaller) must win, so exactly 1 lot."""
    state = _state(capital=1_000_000.0)
    result = risk_check(state, _NoOpConnection(), symbol="TCS", sector20="IT",
                        entry_premium=50.0, stop_premium=10.0, lot_size=150, as_of=date(2026, 8, 26))
    assert result.allowed
    assert result.lots == 1
    assert result.risk_rupees == 50.0 * 150  # full premium, not (50-10)*150


def test_sizing_risk_rupees_is_the_full_premium_not_the_stop_distance():
    """A tight stop must not let sizing pretend less capital is at risk than
    the doctrine says is actually at risk for a long option."""
    state = _state(capital=10_000_000.0)  # large capital so premium cap, not risk budget, binds
    result = risk_check(state, _NoOpConnection(), symbol="TCS", sector20="IT",
                        entry_premium=100.0, stop_premium=99.0, lot_size=150, as_of=date(2026, 8, 26))
    assert result.allowed
    assert result.risk_rupees == pytest_approx_full_premium(result.lots, 100.0, 150)


def pytest_approx_full_premium(lots, entry_premium, lot_size):
    return lots * entry_premium * lot_size


def test_stop_at_or_above_entry_is_rejected_for_a_long_option():
    state = _state()
    result = risk_check(state, _NoOpConnection(), symbol="TCS", sector20="IT",
                        entry_premium=50.0, stop_premium=50.0, lot_size=150, as_of=date(2026, 8, 26))
    assert not result.allowed
    assert "stop_premium" in result.reason


def test_a_position_too_expensive_for_even_one_lot_is_rejected_not_rounded_up():
    state = _state(capital=100_000.0)  # 0.75% = Rs 750; one lot alone costs far more
    result = risk_check(state, _NoOpConnection(), symbol="TCS", sector20="IT",
                        entry_premium=500.0, stop_premium=400.0, lot_size=150, as_of=date(2026, 8, 26))
    assert not result.allowed
    assert "0 lots" in result.reason


def test_event_guard_blocks_the_day_before_and_the_day_of_results():
    """Live regression test for a real bug found by hand, not by the
    offline suite: the query used '>', which silently excluded
    as_of == results_date, so the guard never fired ON the results day
    itself -- only the day before it. Uses the real GMRAIRPORT results_date
    (2026-08-12) already in results_calendar."""
    import psycopg2
    from fusion.m7_risk import event_guard_blocks

    conn = psycopg2.connect("postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie")
    try:
        assert event_guard_blocks(conn, "GMRAIRPORT", date(2026, 8, 11)) is not None
        assert event_guard_blocks(conn, "GMRAIRPORT", date(2026, 8, 12)) is not None
        assert event_guard_blocks(conn, "GMRAIRPORT", date(2026, 8, 10)) is None
        assert event_guard_blocks(conn, "GMRAIRPORT", date(2026, 8, 13)) is None
    finally:
        conn.close()
