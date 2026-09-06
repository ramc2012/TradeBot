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
    KELLY_MIN_T_STAT,
    MAX_PREMIUM_PER_TRADE_PCT,
    WEEKLY_LOSS_STOP_PCT,
    RISK_PER_TRADE_HARD_CAP_PCT,
    RISK_PER_TRADE_PCT,
    RiskState,
    SizingResult,
    kelly_risk_pct,
    risk_check,
    sizing_coherence,
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
    def execute(self, sql, params=None):
        self._result = (params["known_at"],) if "FROM ingest_log" in sql else None
    def fetchone(self): return self._result
    def fetchall(self): return []
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


def test_sizing_uses_the_smaller_of_the_risk_budget_and_the_premium_cap():
    """Both caps are live, and the tighter one wins.

    Capital 10L. Risk budget = 0.75% = Rs 7,500 at stop. Premium cap = 1.5%
    = Rs 15,000. Entry 50, stop 10 -> per-lot risk (50-10)*150 = Rs 6,000,
    per-lot premium 50*150 = Rs 7,500. Risk budget allows 1 lot (7500//6000),
    premium cap allows 2 (15000//7500), so 1 lot and the binding cap is the
    risk budget."""
    state = _state(capital=1_000_000.0)
    result = risk_check(state, _NoOpConnection(), symbol="TCS", sector20="IT",
                        entry_premium=50.0, stop_premium=10.0, lot_size=150, as_of=date(2026, 8, 26))
    assert result.allowed
    assert result.lots == 1
    assert result.risk_rupees == (50.0 - 10.0) * 150
    assert result.premium_rupees == 50.0 * 150
    assert result.binding_cap == "risk_at_stop"


def test_the_premium_cap_binds_when_the_stop_is_tight():
    """THE DEAD-CODE HALF OF THE OLD BUG. Sizing on full premium meant the
    0.75% risk budget was always tighter than the 1.5% premium cap, so the
    premium cap could never bind on any input. With risk-at-stop sizing, a
    tight stop makes the risk budget generous and the premium cap is what
    actually stops the position getting large."""
    state = _state(capital=1_000_000.0)
    result = risk_check(state, _NoOpConnection(), symbol="TCS", sector20="IT",
                        entry_premium=10.0, stop_premium=9.0, lot_size=100, as_of=date(2026, 8, 26))
    assert result.allowed
    assert result.binding_cap == "premium"
    # premium cap 1.5% of 10L = Rs 15,000; per-lot premium = Rs 1,000 -> 15 lots
    assert result.lots == 15
    assert result.premium_rupees <= state.capital * MAX_PREMIUM_PER_TRADE_PCT / 100.0


def test_risk_rupees_is_risk_at_stop_which_doubles_what_a_stopout_actually_costs():
    """THE DEFECT THIS REPLACES. sizing_risk_rupees used to hold the FULL
    premium while the stop sat at -15% of premium, so an actual stop-out cost
    0.1125% of capital and the -2% daily stand-down needed ~18 stop-outs in a
    session with a 3-position cap -- it could never fire. Risk-at-stop sizing
    doubles what a stop-out costs the book, to 0.225%. It does NOT get all the
    way to the intended 0.75% -- the premium cap binds first -- and the
    remaining gap is a configuration inconsistency, asserted separately
    below."""
    capital = 1_000_000.0
    state = _state(capital=capital)
    entry, stop = 100.0, 85.0          # M6's own 15% stop
    result = risk_check(state, _NoOpConnection(), symbol="TCS", sector20="IT",
                        entry_premium=entry, stop_premium=stop, lot_size=150,
                        as_of=date(2026, 8, 26))
    assert result.allowed
    assert result.risk_basis == "risk_at_stop"
    assert result.risk_rupees == result.lots * (entry - stop) * 150
    loss_pct_per_stopout = 100.0 * result.risk_rupees / capital
    # 0.225%, not the old 0.1125% -- the 6.7x looseness from sizing on full
    # premium is gone. A 3.3x looseness remains and it lives in the CONFIG,
    # not the code: see sizing_coherence() and the test below.
    assert loss_pct_per_stopout == 0.225
    assert loss_pct_per_stopout == 2 * 0.1125


def test_three_full_size_positions_fit_inside_the_heat_cap_and_a_fourth_would_not():
    """The spec's numbers only cohere on a risk-at-stop basis: 3 x 0.75% =
    2.25%, just inside the 2.5% cap. On the old premium basis the heat cap
    was measuring a different quantity entirely."""
    assert MAX_CONCURRENT_POSITIONS * RISK_PER_TRADE_PCT <= MAX_PORTFOLIO_HEAT_PCT
    assert (MAX_CONCURRENT_POSITIONS + 1) * RISK_PER_TRADE_PCT > MAX_PORTFOLIO_HEAT_PCT


def test_a_gap_to_zero_is_still_bounded_by_the_premium_cap():
    """The old sizing basis existed to control gap risk, and that concern was
    correct -- it is now handled by the cap built for it rather than by
    disabling every other limit."""
    capital = 1_000_000.0
    state = _state(capital=capital)
    result = risk_check(state, _NoOpConnection(), symbol="TCS", sector20="IT",
                        entry_premium=100.0, stop_premium=85.0, lot_size=150,
                        as_of=date(2026, 8, 26))
    assert result.allowed
    assert 100.0 * result.premium_rupees / capital <= MAX_PREMIUM_PER_TRADE_PCT + 1e-9


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
    from fusion.m7_risk import event_guard_blocks
    class Cursor(_NoOpCursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if "FROM results_calendar" in sql:
                self._result = (date(2026, 9, 7),) if params["as_of"] <= date(2026, 9, 7) else None
    class Connection:
        def cursor(self): return Cursor()
    conn = Connection()
    assert event_guard_blocks(conn, "TCS", date(2026, 9, 4)) is not None
    assert event_guard_blocks(conn, "TCS", date(2026, 9, 7)) is not None
    assert event_guard_blocks(conn, "TCS", date(2026, 9, 3)) is None
    assert event_guard_blocks(conn, "TCS", date(2026, 9, 8)) is None


# ── Kelly may size down freely, but may only size up on evidence ───────────

def test_kelly_may_always_size_down_without_needing_evidence():
    """A weak measured edge must be allowed to shrink the position even
    though the same sample could not justify growing it."""
    edge = {"p": 0.40, "b": 1.0, "n": 60}          # f* = 0.40 - 0.60 = negative
    assert kelly_risk_pct(edge) == 0.0


def test_kelly_will_not_size_up_on_an_edge_its_own_sample_cannot_support():
    """A 60-trade win rate carries an SE near 6 points. p=0.53 against a
    break-even of 0.50 is well inside one SE, so the point estimate is not
    evidence and the fixed-fractional default must stand."""
    edge = {"p": 0.53, "b": 1.0, "n": 60}
    f_star = 0.53 - 0.47 / 1.0
    naive = min(max(0.0, f_star) * 0.25 * 100.0, RISK_PER_TRADE_HARD_CAP_PCT)
    assert naive > RISK_PER_TRADE_PCT                    # the old code would have sized up
    assert kelly_risk_pct(edge) == RISK_PER_TRADE_PCT     # the new code does not


def test_kelly_sizes_up_once_the_edge_clears_its_own_confidence_bar():
    edge = {"p": 0.75, "b": 1.5, "n": 200}
    breakeven = 1.0 / (1.0 + 1.5)
    se = ((0.75 * 0.25) / 200) ** 0.5
    assert (0.75 - breakeven) / se > KELLY_MIN_T_STAT
    assert kelly_risk_pct(edge) > RISK_PER_TRADE_PCT


# ── the remaining inconsistency is in the configuration, and is reported ────

def test_the_three_configured_numbers_do_not_agree_at_a_15_percent_stop():
    """A guard on a KNOWN, DELIBERATELY UNRESOLVED inconsistency.

    Risking 0.75% of capital behind a stop 15% away requires holding 5% of
    capital in premium, which the 1.5% premium cap forbids. The premium cap
    therefore always binds and the effective risk is 0.225%.

    This test asserts the arithmetic, not an opinion about which number is
    wrong -- changing any of the three is an owner decision (see
    sizing_coherence's docstring for the three coherent resolutions). If
    someone retunes them into agreement, this test fails and should be
    rewritten to assert coherence, not deleted."""
    c = sizing_coherence(0.15)
    assert c["coherent"] is False
    assert c["binding_cap"] == "premium"
    assert c["premium_needed_for_intended_risk_pct"] == 5.0
    assert round(c["effective_risk_pct"], 4) == 0.225
    assert not c["daily_standdown_reachable_in_one_session"]


def test_a_wider_stop_makes_the_same_three_numbers_agree():
    """0.75% risk / 1.5% premium implies a 50% stop. Stated so the trade-off
    is visible: coherence is reachable, it just costs a different trade."""
    c = sizing_coherence(0.50)
    assert c["coherent"] is True
    assert c["binding_cap"] == "risk_at_stop"
    assert round(c["effective_risk_pct"], 4) == RISK_PER_TRADE_PCT


def test_the_weekly_stop_needs_more_stopouts_than_the_daily_one():
    """Trivially true, and worth pinning: a weekly limit that could fire
    before the daily one would make the daily stand-down unreachable in a
    different way."""
    c = sizing_coherence(0.15)
    assert c["stopouts_to_weekly_stop"] > c["stopouts_to_daily_standdown"]
    assert abs(WEEKLY_LOSS_STOP_PCT) > abs(DAILY_LOSS_STOP_PCT)
