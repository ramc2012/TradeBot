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
