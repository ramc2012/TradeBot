"""Review fix: committed unit tests for the AI paper-lane position-discipline
rails — the 10% per-symbol capital clamp and the 25% premium hard-stop.

Commit 03ceae9d ("AI position discipline") claimed unit tests for these but none
were committed; previously they were exercised only indirectly. These pin the two
numeric safety limits directly.
"""
from types import SimpleNamespace

from auction_intelligence.paper.book import AI_INITIAL_CAPITAL, PaperPositionBook


def _book(**limits):
    return PaperPositionBook("/tmp/_ai_discipline_test_unused", limits=limits)


def test_symbol_capital_clamp_caps_premium_outgo_to_fraction():
    book = _book(max_symbol_capital_fraction=0.1)
    cap_capital = 0.1 * AI_INITIAL_CAPITAL
    premium, lot = 100.0, 75
    max_qty = int(cap_capital // premium)
    expected = (max_qty // lot) * lot                       # floored to whole lots
    # A large request is clamped to the 10% capital cap.
    assert book._clamp_quantity_to_symbol_cap(10_000, premium, lot) == expected
    assert expected * premium <= cap_capital                # premium outgo within cap
    # A request already under the cap passes through unchanged.
    assert book._clamp_quantity_to_symbol_cap(lot, premium, lot) == lot


def test_symbol_capital_clamp_refuses_when_a_single_lot_exceeds_cap():
    # Documented escape hatch: if even ONE lot's outgo exceeds the cap, keep one lot
    # (rather than skip) so the symbol can still trade. premium=8000 x lot 75 = 600k
    # outgo > 10% cap (500k).
    book = _book(max_symbol_capital_fraction=0.1)
    assert book._clamp_quantity_to_symbol_cap(75, 8000.0, 75) == 0


def test_hard_stop_triggers_at_premium_drawdown_threshold():
    book = _book(hard_stop_premium_fraction=0.25)
    bundle = SimpleNamespace(market_profile=SimpleNamespace(session_date=None))

    def reason(latest_premium):
        return book._exit_reason_for_position(
            {"entry_premium": 100.0, "latest_premium": latest_premium}, bundle=bundle
        )

    assert reason(70.0) == "hard_stop"     # 30% drawdown → stop
    assert reason(75.0) == "hard_stop"     # exactly 25% (<= threshold) → stop
    assert reason(76.0) != "hard_stop"     # 24% drawdown → held
    assert reason(0.0) == "premium_zero"   # zero premium guarded separately


def test_expiry_sweep_force_closes_unscanned_expired_positions():
    """The unconditional expiry sweep must close an expired position even when
    this cycle's bundle is for a DIFFERENT underlying (the lane stopped scanning
    the expired one). Regression for the SENSEX 76000 CE zombie that sat open for
    days because the decision-driven exit only touches the bundle's symbol."""
    import asyncio
    book = _book()

    async def _fake_close(*, position, bundle, now, reason, execution):
        position["status"] = "closed"
        position["close_reason"] = reason
        position["closed_at"] = now

    book._close_position = _fake_close  # avoid the DB premium-resolve in a unit test

    open_positions = [
        {"symbol": "SENSEX 76000 CE", "underlying_symbol": "SENSEX",
         "expiry": "2026-06-11", "status": "open", "quantity": 1500, "entry_premium": 221.0},
        {"symbol": "NIFTY 23500 CE", "underlying_symbol": "NIFTY",
         "expiry": "2026-06-30", "status": "open", "quantity": 75, "entry_premium": 100.0},
    ]
    closed: list = []
    # bundle is for NIFTY (NOT the expired SENSEX), session 2026-06-15
    bundle = SimpleNamespace(
        market_profile=SimpleNamespace(session_date="2026-06-15"),
        regime=SimpleNamespace(label="balanced"),
    )
    asyncio.run(book._sweep_expired_positions(
        bundle=bundle, open_positions=open_positions, closed_positions=closed, now="2026-06-15T00:00:00Z",
    ))
    # the expired SENSEX is swept; the still-valid NIFTY stays open
    assert len(open_positions) == 1 and open_positions[0]["underlying_symbol"] == "NIFTY"
    assert len(closed) == 1 and closed[0]["close_reason"] == "expired_contract"
