"""WS-1.4 — paper transaction-cost model + net-P&L wiring."""
import pytest

from paper_engine.costs import round_trip_charges
from paper_engine.portfolio import PaperPortfolio


def test_option_round_trip_costs_are_sane():
    # Long NIFTY CE 100->120, 1 lot (75u): gross 1500.
    c = round_trip_charges(
        symbol="NSE:NIFTY...CE", instrument_type="CE",
        entry_price=100, exit_price=120, qty=75, entry_action="BUY",
    )
    assert 40 < c < 100          # >= brokerage floor (40), realistic upper bound
    assert c < 1500 * 0.10       # comfortably under 10% of gross


def test_future_stt_dominates_on_large_notional():
    # NSE future, ~5.77L sell turnover -> STT (0.02%) ~115 dominates.
    c = round_trip_charges(
        symbol="NSE:NIFTYFUT", instrument_type="FUT",
        entry_price=23000, exit_price=23100, qty=25, entry_action="BUY",
    )
    assert c > 150


def test_disabled_returns_zero(monkeypatch):
    monkeypatch.setattr("paper_engine.costs.PAPER_APPLY_COSTS", False)
    assert round_trip_charges(
        symbol="NSE:X", instrument_type="CE",
        entry_price=100, exit_price=120, qty=75, entry_action="BUY",
    ) == 0.0


def test_degenerate_inputs_are_safe():
    assert round_trip_charges(symbol="X", instrument_type="CE", entry_price=0, exit_price=0, qty=0, entry_action="BUY") == 0.0
    assert round_trip_charges(symbol="X", instrument_type="CE", entry_price=100, exit_price=120, qty=-5, entry_action="BUY") == 0.0


def test_portfolio_close_applies_costs_and_records_breakdown(monkeypatch):
    monkeypatch.setattr("paper_engine.costs.PAPER_APPLY_COSTS", True)
    pf = PaperPortfolio(initial_capital=1_000_000.0, session_id="t")
    pf.book_close(
        symbol="NSE:NIFTY...CE", entry_action="BUY", qty=75,
        entry_price=100.0, exit_price=120.0, instrument_type="CE",
    )
    t = pf._trade_history[-1]
    assert t.gross_pnl == pytest.approx(1500.0)            # (120-100)*75
    assert t.charges > 0
    assert t.pnl == pytest.approx(t.gross_pnl - t.charges)  # net = gross - charges
    assert t.pnl < t.gross_pnl                              # costs reduce realised P&L
