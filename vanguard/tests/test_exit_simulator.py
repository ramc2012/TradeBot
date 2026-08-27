"""Offline tests for the shared stop / trail / target2 / time_stop_eod exit
rule used by both M8 (backtest) and M9 (paper engine). Pure logic, no database.

Several of these lock in fixes for defects an adversarial review confirmed on
2026-08-27; each such test says which one, because they all look like arbitrary
OHLC arithmetic otherwise.
"""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parents[1]))

from backtest.exit_simulator import Bar, _fillable, walk_exit  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
ENTRY = 50.0
# entry 50 -> initial stop 42.5, trail arms at 60.0, target2 at 75.0


def _ts(hour, minute):
    return datetime(2026, 8, 26, hour, minute, tzinfo=IST)


def _bar(hour, minute, open_, high, low, close):
    return Bar(ts=_ts(hour, minute), open=open_, high=high, low=low, close=close)


# ── the plain cases ─────────────────────────────────────────────────────────

def test_initial_stop_fires_before_any_trail_activation():
    bars = [_bar(10, 0, 50.0, 51.0, 42.0, 42.0)]
    result = walk_exit(ENTRY, _ts(9, 30), bars)
    assert result.exit_reason == "stop"
    assert result.exit_price == 42.5
    assert result.holding_bars == 1


def test_price_never_touching_any_level_leaves_the_position_open():
    bars = [_bar(10, 0, 50.0, 52.0, 48.0, 50.0), _bar(10, 30, 50.0, 53.0, 49.0, 52.0)]
    assert walk_exit(ENTRY, _ts(9, 30), bars) is None


def test_trail_arms_at_target1_then_gives_back_and_exits_as_trail_stop():
    # bar1 high 61 arms the trail -> 61*0.90 = 54.9. bar2's low breaches it.
    bars = [
        _bar(10, 0, 59.5, 61.0, 59.0, 60.0),
        _bar(10, 30, 60.0, 61.0, 54.0, 55.0),
    ]
    result = walk_exit(ENTRY, _ts(9, 30), bars)
    assert result.exit_reason == "trail_stop"
    assert result.exit_price == 54.9
    assert result.holding_bars == 2


def test_trail_ratchets_up_with_new_highs_and_never_ratchets_back_down():
    bars = [
        _bar(10, 0, 60.5, 62.0, 60.0, 61.0),   # arms -> 55.8
        _bar(10, 30, 61.0, 70.0, 65.0, 68.0),  # ratchets -> 63.0
        _bar(11, 0, 68.0, 69.0, 62.5, 63.0),   # low breaches the pre-existing 63.0
    ]
    result = walk_exit(ENTRY, _ts(9, 30), bars)
    assert result.exit_reason == "trail_stop"
    assert result.exit_price == 63.0
    assert result.holding_bars == 3


def test_target2_fires_when_reached_without_the_trail_catching_it():
    bars = [
        _bar(10, 0, 60.5, 62.0, 60.0, 61.0),
        _bar(10, 30, 74.0, 80.0, 73.0, 78.0),
    ]
    result = walk_exit(ENTRY, _ts(9, 30), bars)
    assert result.exit_reason == "target2"
    assert result.exit_price == 75.0


def test_time_stop_eod_fires_at_the_first_bar_at_or_after_1510_ist():
    bars = [
        _bar(14, 30, 50.0, 52.0, 48.0, 51.0),
        _bar(15, 0, 51.0, 53.0, 49.0, 52.0),
        _bar(15, 30, 52.0, 54.0, 50.0, 53.0),
    ]
    result = walk_exit(ENTRY, _ts(9, 30), bars)
    assert result.exit_reason == "time_stop_eod"
    assert result.exit_price == 53.0
    assert result.exit_ts == _ts(15, 30)


def test_time_stop_eod_does_not_preempt_a_stop_in_the_same_bar():
    bars = [_bar(15, 15, 50.0, 51.0, 40.0, 45.0)]
    assert walk_exit(ENTRY, _ts(9, 30), bars).exit_reason == "stop"


def test_low_exactly_at_the_initial_stop_still_triggers():
    bars = [_bar(10, 0, 50.0, 50.5, 42.5, 43.0)]
    result = walk_exit(ENTRY, _ts(9, 30), bars)
    assert result.exit_reason == "stop"
    assert result.exit_price == 42.5


# ── regression: stop-in-force is tested BEFORE this bar's own high ──────────

def test_a_bar_that_both_arms_the_trail_and_breaches_the_initial_stop_is_a_LOSS():
    """Confirmed defect (2026-08-27): the old order folded the bar's own high
    into the trail FIRST, so a bar that spiked to +22% and then collapsed
    through the -15% stop exited at the trail level that same spike created --
    booking a winner (+9.9%) out of a trade that actually hit its stop.

    high 61 would arm the trail (>= 60) and imply a 54.9 trail; low 40 is below
    the 42.5 initial stop that was in force when the bar opened. The stop must
    win, at 42.5, labelled 'stop'."""
    bars = [_bar(10, 0, 50.0, 61.0, 40.0, 41.0)]
    result = walk_exit(ENTRY, _ts(9, 30), bars)
    assert result.exit_reason == "stop"
    assert result.exit_price == 42.5


def test_the_trail_only_protects_bars_AFTER_the_one_that_armed_it():
    """The bar that arms the trail cannot also be stopped out by that same
    trail -- only the following bars can."""
    bars = [
        _bar(10, 0, 59.0, 61.0, 58.0, 60.5),   # arms trail at 54.9; low 58 > 54.9
        _bar(10, 30, 60.0, 60.5, 54.0, 54.5),  # NOW the 54.9 trail bites
    ]
    result = walk_exit(ENTRY, _ts(9, 30), bars)
    assert result.exit_reason == "trail_stop"
    assert result.holding_bars == 2


# ── regression: gap-through must not fill at a price never traded ──────────

def test_a_gap_down_through_the_stop_fills_at_the_open_not_the_stop_level():
    """Confirmed defect (2026-08-27): a bar that opened BELOW the stop still
    booked a fill at the stop level -- a price the instrument never traded in
    that bar. Options gap hard; this flattered every gap-down."""
    bars = [_bar(10, 0, 30.0, 32.0, 28.0, 29.0)]  # opens at 30, far under the 42.5 stop
    result = walk_exit(ENTRY, _ts(9, 30), bars)
    assert result.exit_reason == "stop"
    assert result.exit_price == 30.0


def test_an_exit_price_is_always_inside_the_bars_own_high_low_range():
    bars = [_bar(10, 0, 41.0, 41.5, 20.0, 21.0)]
    result = walk_exit(ENTRY, _ts(9, 30), bars)
    assert 20.0 <= result.exit_price <= 41.5


def test_fillable_clamps_below_the_low_and_above_the_high():
    bar = _bar(10, 0, 30.0, 32.0, 28.0, 29.0)
    assert _fillable(42.5, bar) == 30.0     # gapped through -> fills at open
    assert _fillable(29.0, bar) == 29.0     # inside the range -> fills at the level
    assert _fillable(10.0, bar) == 28.0     # below the low -> clamped to the low


# ── regression: the tie-break must always take the WORSE fill ──────────────

def test_when_both_target2_and_the_trail_are_hit_the_lower_fill_wins():
    """live_stop (54.9 from bar1) < target2 (75) -> the stop is the worse
    outcome and must be the one booked."""
    bars = [
        _bar(10, 0, 59.5, 61.0, 59.0, 60.0),
        _bar(10, 30, 60.0, 80.0, 50.0, 60.0),
    ]
    result = walk_exit(ENTRY, _ts(9, 30), bars)
    assert result.exit_reason == "trail_stop"
    assert result.exit_price < 75.0


def test_once_the_trail_ratchets_above_target2_the_target2_fill_is_the_worse_one():
    """Confirmed defect (2026-08-27): 'stop always wins' inverted from
    conservative to anti-conservative once the trail exceeded target2. Here
    bar2's high of 100 ratchets the trail to 90 -- above target2 (75). Booking
    the 90 trail would CREDIT the trade 15 rupees more than target2. The
    conservative reading is target2 at 75."""
    bars = [
        _bar(10, 0, 59.5, 61.0, 59.0, 60.0),
        _bar(10, 30, 62.0, 100.0, 85.0, 88.0),
    ]
    result = walk_exit(ENTRY, _ts(9, 30), bars)
    assert result.exit_reason == "target2"
    assert result.exit_price == 75.0


def test_stop_and_trail_stop_are_distinguishable_in_the_journal():
    """Both are downside exits but they are different events: 'stop' never
    reached +20%, 'trail_stop' did and gave it back. M10 groups by
    exit_reason, so collapsing them would make attribution unrecoverable."""
    never_worked = walk_exit(ENTRY, _ts(9, 30), [_bar(10, 0, 50.0, 51.0, 40.0, 41.0)])
    gave_back = walk_exit(ENTRY, _ts(9, 30), [
        _bar(10, 0, 59.5, 61.0, 59.0, 60.0),
        _bar(10, 30, 60.0, 61.0, 54.0, 55.0),
    ])
    assert never_worked.exit_reason == "stop"
    assert gave_back.exit_reason == "trail_stop"
    assert never_worked.exit_reason != gave_back.exit_reason
