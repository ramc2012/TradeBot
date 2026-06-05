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


def test_symbol_capital_clamp_keeps_one_lot_when_a_single_lot_exceeds_cap():
    # Documented escape hatch: if even ONE lot's outgo exceeds the cap, keep one lot
    # (rather than skip) so the symbol can still trade. premium=8000 x lot 75 = 600k
    # outgo > 10% cap (500k).
    book = _book(max_symbol_capital_fraction=0.1)
    assert book._clamp_quantity_to_symbol_cap(75, 8000.0, 75) == 75


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
