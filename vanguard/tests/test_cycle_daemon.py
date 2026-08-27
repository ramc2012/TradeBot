"""Offline tests for the scheduler's pure timing logic.

The daemon's whole job is deciding WHEN to run, so that decision is the part
worth locking down. Nothing here touches a database or spawns a subprocess.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parents[1]))

from scripts.cycle_daemon import (  # noqa: E402
    LIVE_PASS_DELAY_S,
    LIVE_STEPS,
    in_market_hours,
    next_bar_boundary,
)

IST = ZoneInfo("Asia/Kolkata")


def _at(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=IST)


# ── market hours ────────────────────────────────────────────────────────────

def test_inside_the_session_is_market_hours():
    assert in_market_hours(_at(2026, 8, 27, 11, 0))       # Thursday midday


def test_the_open_and_close_minutes_are_inclusive():
    assert in_market_hours(_at(2026, 8, 27, 9, 15))
    assert in_market_hours(_at(2026, 8, 27, 15, 30))


def test_pre_open_and_post_close_are_not_market_hours():
    assert not in_market_hours(_at(2026, 8, 27, 9, 14, 59))
    assert not in_market_hours(_at(2026, 8, 27, 15, 30, 1))
    assert not in_market_hours(_at(2026, 8, 27, 3, 0))


def test_weekends_are_never_market_hours_even_at_midday():
    assert not in_market_hours(_at(2026, 8, 29, 11, 0))   # Saturday
    assert not in_market_hours(_at(2026, 8, 30, 11, 0))   # Sunday


# ── bar boundaries ──────────────────────────────────────────────────────────

def test_boundary_lands_on_the_next_half_hour_plus_the_read_delay():
    expected = _at(2026, 8, 27, 10, 30) + timedelta(seconds=LIVE_PASS_DELAY_S)
    assert next_bar_boundary(_at(2026, 8, 27, 10, 5)) == expected


def test_boundary_is_always_strictly_in_the_future():
    """Exactly ON a boundary must roll to the NEXT one, never return now --
    otherwise the loop would compute a zero/negative sleep and spin."""
    for moment in (_at(2026, 8, 27, 10, 0), _at(2026, 8, 27, 10, 30)):
        assert next_bar_boundary(moment) > moment


def test_boundary_from_just_before_the_half_hour_does_not_skip_it():
    got = next_bar_boundary(_at(2026, 8, 27, 10, 29, 59))
    assert got.hour == 10 and got.minute == 30 + LIVE_PASS_DELAY_S // 60


def test_boundary_rolls_across_the_hour():
    got = next_bar_boundary(_at(2026, 8, 27, 10, 45))
    assert got.hour == 11 and got.minute == LIVE_PASS_DELAY_S // 60


def test_boundary_rolls_across_midnight_without_crashing():
    got = next_bar_boundary(_at(2026, 8, 27, 23, 50))
    assert got.day == 28
    assert got.hour == 0


# ── what the live pass actually runs ────────────────────────────────────────

def test_the_live_pass_never_runs_m2():
    """M2's IV source died 2026-07-28. Scheduling it would write saturated
    single-ingredient flow scores instead of leaving an honest gap, so its
    absence here is a deliberate guarantee, not an oversight."""
    argv = " ".join(a for _, args in LIVE_STEPS for a in args)
    assert "m2_flow" not in argv


def test_the_live_pass_orders_features_before_selection_and_journaling():
    """M6 selects on the bar M3/M5 just wrote, and M9 journals what M6 just
    emitted. Reordering these silently evaluates a stale bar."""
    names = [name for name, _ in LIVE_STEPS]
    assert names.index("M3 regime") < names.index("M6 select")
    assert names.index("M5 timing") < names.index("M6 select")
    assert names.index("M6 select") < names.index("M9 paper")


def test_every_write_capable_step_actually_passes_its_write_flag():
    """m5_timing and m6_select are no-ops without --write; a scheduler that
    ran them read-only would look healthy and persist nothing."""
    by_name = {name: args for name, args in LIVE_STEPS}
    assert "--write" in by_name["M5 timing"]
    assert "--write" in by_name["M6 select"]


def test_lookbacks_are_short_enough_to_be_incremental():
    """The point of the live pass is to APPEND, not to rebuild history every
    30 minutes against a shared production database."""
    by_name = {name: args for name, args in LIVE_STEPS}
    assert int(by_name["M5 timing"][by_name["M5 timing"].index("--lookback-days") + 1]) <= 5
    assert int(by_name["M3 regime"][by_name["M3 regime"].index("--lookback-days") + 1]) <= 90
