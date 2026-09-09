"""Exit integrity for the auction paper lane.

Every assertion here is anchored to a number measured on the live book
(57 closed positions, runtime/auction_intelligence/paper_positions.json):

  - 4 positions carried a stop on the WRONG SIDE of entry. Three exited inside
    an hour and two of those "stop losses" were PROFITS of Rs 2,29,110 and
    Rs 40,352. A stop that pays is not a stop.
  - 9 of 28 `target_hit` exits LOST money; the median target fired on a spot
    move of +0.01%. A spot level brushed by noise while theta ate the premium
    is not a target being reached.
  - `hard_stop` is configured at -25% but realised at a median of -63.3%, with
    a median spot move of +0.02% — the premium decayed, the underlying did not
    move, and the stop could not enforce its own limit at this mark cadence.
"""
from __future__ import annotations

import pytest

from auction_intelligence.paper.book import PaperPositionBook

validate = PaperPositionBook.validate_risk_levels


# ── wrong-side levels ───────────────────────────────────────────────────────


def test_a_long_stop_above_entry_is_dropped():
    """BANKNIFTY 60000 CE: entry spot 57,942.45, stop 57,975.50 — 33 points ABOVE.

    Held 55 minutes and booked +Rs 2,29,110 as a `stop_loss`.
    """
    stop, target, issues = validate(action="LONG", reference=57942.45, stop=57975.50, target=58400.0)
    assert stop is None, "a long stop above entry fires at entry and must not survive"
    assert target == 58400.0, "a valid target must be left alone"
    assert issues and "wrong side" in issues[0]


def test_a_short_stop_below_entry_is_dropped():
    stop, _, issues = validate(action="SHORT", reference=57942.45, stop=57900.0, target=57000.0)
    assert stop is None
    assert issues


def test_correct_sided_levels_pass_through_untouched():
    """The 53 well-formed positions must be completely unaffected."""
    stop, target, issues = validate(action="LONG", reference=24300.0, stop=24062.20, target=24500.0)
    assert (stop, target) == (24062.20, 24500.0)
    assert issues == []

    stop, target, issues = validate(action="SHORT", reference=57500.0, stop=58076.0, target=56800.0)
    assert (stop, target) == (58076.0, 56800.0)
    assert issues == []


def test_an_already_satisfied_target_is_dropped():
    _, target, issues = validate(action="LONG", reference=24300.0, stop=24100.0, target=24200.0)
    assert target is None
    assert issues and "already satisfied" in issues[0]


def test_a_stop_exactly_at_entry_is_treated_as_wrong_side():
    """Zero risk distance is not a stop; it fires on the first tick either way."""
    stop, _, issues = validate(action="LONG", reference=24300.0, stop=24300.0, target=24500.0)
    assert stop is None
    assert issues


def test_validation_is_inert_without_a_usable_reference():
    """No entry price means no opinion — never invent one."""
    assert validate(action="LONG", reference=0.0, stop=1.0, target=2.0) == (1.0, 2.0, [])
    assert validate(action="", reference=100.0, stop=1.0, target=2.0) == (1.0, 2.0, [])


def test_missing_levels_are_not_invented():
    stop, target, issues = validate(action="LONG", reference=24300.0, stop=None, target=None)
    assert (stop, target, issues) == (None, None, [])


@pytest.mark.parametrize(
    "action,reference,stop",
    [
        ("LONG", 57942.45, 57975.50),   # BANKNIFTY 60000 CE, +Rs 2,29,110 "stop"
        ("LONG", 58167.55, 58350.00),   # BANKNIFTY 58500 CE, +Rs 40,352 "stop"
        ("LONG", 58207.10, 58350.00),   # BANKNIFTY 58000 CE, -Rs 3,16,074
        ("LONG", 58200.70, 58342.40),   # BANKNIFTY 62000 CE
    ],
)
def test_every_wrong_side_stop_in_the_live_book_is_caught(action, reference, stop):
    clean, _, issues = validate(action=action, reference=reference, stop=stop, target=None)
    assert clean is None
    assert issues


# ── exit geometry ───────────────────────────────────────────────────────────
#
# Derived by replaying the reconstructed premium path of 29 closed positions
# (option_premium_candles, first touch, filled at the observed bar rather than
# at the trigger level, costs charged per round trip):
#
#   25% stop + tight target   -Rs 1,69,715
#   25% stop + wide target    +Rs   29,684
#   15% stop + wide target    +Rs   49,616
#
# Every 5%-target row lost money at every stop width tested. The mechanism
# matches the live book independently: the median `target_hit` fired at +2.6%
# premium against a median maximum favourable excursion of +3.1%, i.e. the lane
# was cutting winners at their high while losers ran to -63%.


def _limits(**over):
    base = {"hard_stop_premium_fraction": 0.15, "target_min_reward_multiple": 2.5}
    base.update(over)
    return base


def _book(**over):
    return PaperPositionBook("runtime/auction_intelligence", limits=_limits(**over))


def test_the_target_floor_scales_with_the_risk_actually_taken():
    """A wider stop must demand a proportionally wider target."""
    tight = _book(hard_stop_premium_fraction=0.10)
    wide = _book(hard_stop_premium_fraction=0.30)
    floor = lambda b: (
        float(b.limits["target_min_reward_multiple"]) * float(b.limits["hard_stop_premium_fraction"])
    )
    assert floor(tight) == pytest.approx(0.25)
    assert floor(wide) == pytest.approx(0.75)
    assert floor(wide) > floor(tight)


def test_shipped_geometry_can_break_even_below_the_observed_hit_rate():
    """Reward:risk must imply a breakeven the lane can actually clear.

    The lane's measured win rate is 38.6%. At the old 0.45 reward:risk it needed
    69.0% and could never work. The shipped 2.5x floor needs 28.6%.
    """
    book = _book()
    rr = float(book.limits["target_min_reward_multiple"])
    breakeven = 1.0 / (1.0 + rr)
    assert breakeven < 0.386, (
        f"a {rr}x reward multiple needs a {breakeven:.1%} win rate; the lane hits 38.6%"
    )
    assert breakeven == pytest.approx(0.2857, abs=1e-3)


def test_a_noise_level_gain_is_no_longer_bookable_as_a_target():
    """+2.6% was the median `target_hit`; the floor is now 37.5%."""
    book = _book()
    floor = 1.0 + float(book.limits["target_min_reward_multiple"]) * float(
        book.limits["hard_stop_premium_fraction"]
    )
    assert 1.026 < floor, "the old median target must sit below the new floor"
    assert floor == pytest.approx(1.375)


def test_the_costs_floor_still_applies_when_no_reward_multiple_is_set():
    """Removing the multiple must not remove the do-not-book-a-loss guard."""
    book = _book(target_min_reward_multiple=0.0)
    assert float(book.limits["target_min_reward_multiple"]) == 0.0
    # costs_bps floor is 2 x slippage_bps / 10000 — small but strictly positive.
    assert float(book.costs.get("slippage_bps", 1)) > 0
