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
    EOD_STEPS,
    LIVE_PASS_DELAY_S,
    LIVE_STEPS,
    REALTIME_MARK_SECONDS,
    REALTIME_STEPS,
    bar_closes,
    in_live_window,
    in_market_hours,
    next_bar_boundary,
)

IST = ZoneInfo("Asia/Kolkata")


def _at(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=IST)


# ── market hours ────────────────────────────────────────────────────────────

def test_inside_the_session_is_market_hours():
    assert in_market_hours(_at(2026, 8, 27, 11, 0))       # Thursday midday


def test_the_window_opens_at_the_first_BAR_CLOSE_not_at_the_session_open():
    """09:15 is when the first bar STARTS. There is no completed bar to
    evaluate until 09:45, and a pass at 09:20 would re-evaluate yesterday's
    last bar."""
    assert not in_live_window(_at(2026, 8, 27, 9, 15))
    assert not in_live_window(_at(2026, 8, 27, 9, 44, 59))
    assert in_live_window(_at(2026, 8, 27, 9, 45))


def test_the_window_extends_past_the_close_so_the_final_bar_is_evaluated():
    """The 15:15 bar closes at the 15:30 bell. Cutting the window at 15:30
    exactly would leave that bar the only one of the session never seen."""
    assert in_live_window(_at(2026, 8, 27, 15, 30))
    assert in_live_window(_at(2026, 8, 27, 15, 33))
    assert not in_live_window(_at(2026, 8, 27, 15, 41))


def test_pre_open_and_overnight_are_not_in_the_live_window():
    assert not in_live_window(_at(2026, 8, 27, 3, 0))
    assert not in_live_window(_at(2026, 8, 27, 20, 0))


def test_weekends_are_never_market_hours_even_at_midday():
    assert not in_market_hours(_at(2026, 8, 29, 11, 0))   # Saturday
    assert not in_market_hours(_at(2026, 8, 30, 11, 0))   # Sunday


# ── bar boundaries ──────────────────────────────────────────────────────────

def test_the_grid_is_the_EXCHANGE_grid_not_the_wall_clock():
    """THE BUG THIS LOCKS DOWN. NSE opens at 09:15, so its 30-minute bars
    close at 09:45, 10:15, 10:45 ... The daemon used to wake on :00/:30, which
    is offset from that by 15 minutes -- every live pass evaluated a bar that
    had closed ~17 minutes earlier, spending over half a 30-minute IGNITION
    signal's life waiting for the scheduler."""
    closes = bar_closes(_at(2026, 8, 27, 12, 0))
    assert closes[0] == _at(2026, 8, 27, 9, 45)
    assert closes[1] == _at(2026, 8, 27, 10, 15)
    assert all(c.minute in (15, 45, 30) for c in closes)
    # the 15:15 bar closes at the bell, not at 15:45
    assert closes[-1] == _at(2026, 8, 27, 15, 30)
    assert closes[-2] == _at(2026, 8, 27, 15, 15)


def test_boundary_lands_on_the_next_exchange_bar_close_plus_the_read_delay():
    expected = _at(2026, 8, 27, 10, 15) + timedelta(seconds=LIVE_PASS_DELAY_S)
    assert next_bar_boundary(_at(2026, 8, 27, 10, 5)) == expected


def test_boundary_never_lands_on_the_old_wall_clock_grid():
    """A regression guard with teeth: from anywhere inside the session the
    next wake-up must be a :15/:45 close, never :00/:30."""
    for minute in (1, 14, 16, 29, 31, 44, 46, 59):
        got = next_bar_boundary(_at(2026, 8, 27, 11, minute))
        assert (got - timedelta(seconds=LIVE_PASS_DELAY_S)).minute in (15, 45)


def test_boundary_is_always_strictly_in_the_future():
    """Exactly ON a boundary must roll to the NEXT one, never return now --
    otherwise the loop would compute a zero/negative sleep and spin."""
    for moment in (_at(2026, 8, 27, 10, 15), _at(2026, 8, 27, 10, 45)):
        assert next_bar_boundary(moment) > moment


def test_boundary_from_just_before_a_close_does_not_skip_it():
    got = next_bar_boundary(_at(2026, 8, 27, 10, 14, 59))
    assert got.hour == 10 and got.minute == 15 + LIVE_PASS_DELAY_S // 60


def test_after_the_last_bar_the_boundary_rolls_to_tomorrows_first_close():
    got = next_bar_boundary(_at(2026, 8, 27, 15, 40))
    assert got.day == 28
    assert (got - timedelta(seconds=LIVE_PASS_DELAY_S)).hour == 9
    assert (got - timedelta(seconds=LIVE_PASS_DELAY_S)).minute == 45


def test_boundary_rolls_across_midnight_without_crashing():
    got = next_bar_boundary(_at(2026, 8, 27, 23, 50))
    assert got.day == 28


# ── what the live pass actually runs ────────────────────────────────────────

def test_the_live_pass_runs_m2_after_the_surface_it_reads():
    """M2 belongs on the bar grid, and it must follow the IV surface.

    This test used to assert the opposite -- that the live pass NEVER runs M2 --
    on the reasoning that M2's IV source had died and scheduling it would write
    saturated single-ingredient scores. The source was repaired (IVS now comes
    from the solved `iv_surface`, PCR and the delta-OI conjunction from option
    open interest), so the premise is gone. Leaving M2 unscheduled was in fact
    what broke the lane: features_flow froze at 2026-07-28 and M6's flow_fresh
    leg, which allows 3 sessions, was handed 23.
    """
    names = [name for name, _ in LIVE_STEPS]
    argv = " ".join(a for _, args in LIVE_STEPS for a in args)
    assert "m2_flow" in argv
    assert names.index("IV surface") < names.index("M2 flow")
    assert names.index("M2 flow") < names.index("M6 select")


def test_eod_is_reserved_for_exchange_published_inputs():
    """The cadence rule: a step is EOD only if the EXCHANGE publishes its input
    once a day, or it is a rollup of the finished session. Being expensive, or
    having historically run at night, is not a reason -- that reasoning is what
    stranded five live market readings on the EOD pass.

    Anything derived from live prices (solved IV, a vol surface, a flow score)
    is still a live reading; the derivation does not make it daily.
    """
    argv = " ".join(a for _, args in EOD_STEPS for a in args)
    for live_reading in ("m_implied_vol", "m_iv_surface", "m_sentiment",
                         "m2_flow", "m3_gex", "m4_sector", "m5_timing"):
        assert live_reading not in argv, f"{live_reading} is a live reading, not an EOD one"


def test_the_live_pass_orders_features_before_selection_and_journaling():
    """M6 selects on the bar M3/M5 just wrote, and M9 journals what M6 just
    emitted. Reordering these silently evaluates a stale bar."""
    names = [name for name, _ in LIVE_STEPS]
    assert names.index("M3 regime") < names.index("M6 select")
    assert names.index("M5 timing") < names.index("M6 select")
    assert names.index("M6 select") < names.index("M9 paper")


def test_preclose_swing_and_separate_journals_are_automatic():
    names = [name for name, _ in LIVE_STEPS]
    by_name = {name: args for name, args in LIVE_STEPS}
    assert names.index("M6 select") < names.index("pre-close swing watchlist")
    assert "preclose_swing.py" in " ".join(by_name["pre-close swing watchlist"])
    assert "--allow-replay" not in by_name["pre-close swing watchlist"]
    assert "--sync" in by_name["strategy journals"]
    assert "--track-swing" in by_name["strategy journals"]
    assert REALTIME_MARK_SECONDS == 60
    assert "strategy_lanes.py" in " ".join(dict(REALTIME_STEPS)["strategy journals"])


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


def test_the_live_pass_disables_m5s_hardcoded_spot_checks():
    """m5_timing widens its own window back to the earliest SPOT_CHECKS date
    unless told not to, so a nominal 3-day live pass silently recomputed weeks
    of bars every 30 minutes against the shared production database."""
    by_name = {name: args for name, args in LIVE_STEPS}
    assert "--no-spot-check" in by_name["M5 timing"]


def test_the_eod_pass_runs_the_cross_sectional_ic_study():
    """It is the only measurement in the lane that scores the FULL universe
    rather than the handful of names that already passed M2's own filter, so
    it is the only one that can currently falsify M2."""
    argv = " ".join(a for _, args in EOD_STEPS for a in args)
    assert "cross_section_ic" in argv


def test_directional_shadow_scores_before_the_daily_list_is_frozen():
    names = [name for name, _ in EOD_STEPS]
    assert names.index("1-2 session directional shadow") < names.index("freeze model watchlist")
    args = dict(EOD_STEPS)["1-2 session directional shadow"]
    assert "score_directional_swing.py" in " ".join(args)
    assert "--write" in args


def test_the_eod_pass_refreshes_open_interest_and_positioning():
    """MWPL is a once-daily publication and the price leg needs a settled
    close, so this belongs at EOD -- but it must actually be there, or the
    desk's OI columns quietly age."""
    argv = " ".join(a for _, args in EOD_STEPS for a in args)
    assert "m_oi_positioning" in argv


def test_open_interest_is_not_recomputed_every_bar():
    argv = " ".join(a for _, args in LIVE_STEPS for a in args)
    assert "m_oi_positioning" not in argv


def test_the_live_pass_solves_implied_vol_before_it_aggregates_a_surface():
    """The surface reads what the solver writes, and the sentiment blend reads
    the surface. Reordering these silently aggregates yesterday's IVs.

    The invariant is unchanged; only the pass it applies to moved, since the
    whole solve/aggregate/blend chain is a live reading.
    """
    names = [name for name, _ in LIVE_STEPS]
    assert names.index("implied vol") < names.index("IV surface")
    assert names.index("IV surface") < names.index("sentiment")


def test_implied_vol_is_solved_every_bar_over_a_bounded_window():
    """IV is solved FROM PRICES, so it is a live reading and is re-solved each
    bar -- that is what keeps the current session's surface current, and it
    took 9s measured.

    This asserted the opposite until 2026-08-28, on the reasoning that solving
    "needs settled end-of-session prints". It does not: it inverts whatever
    prints exist, and a mid-session run writes the session so far. The cost
    objection is real but is answered by the WINDOW, not by the cadence -- so
    the live step must stay bounded to the current session rather than
    re-solving the EOD pass's multi-day backfill thirteen times a day.
    """
    live = {name: args for name, args in LIVE_STEPS}
    assert "implied vol" in live
    args = live["implied vol"]
    assert "--lookback-days" in args
    lookback = int(args[args.index("--lookback-days") + 1])
    assert lookback <= 2, f"live solve must stay on the current session, got {lookback} days"


def test_a_successful_step_logs_the_verdict_it_printed():
    """`ok (3s)` is not a result.

    On 2026-09-04 the pre-close emitter returned `created: False, reason: no
    liquid contract expressions` and the daemon logged it as a plain success,
    so a day with no watchlist looked identical to a day with one.
    """
    from scripts.cycle_daemon import _verdict
    assert _verdict("funnel line\nanother\n{'created': True, 'item_count': 20}\n") == (
        " :: {'created': True, 'item_count': 20}")
    assert _verdict("") == ""
    assert _verdict("   \n  \n") == ""
    assert _verdict("x" * 400).endswith("…")


def test_marks_keep_running_after_the_last_bar_close():
    """The 14:45 entry chain is still landing when the live window shuts.

    On 2026-09-04 the EOD pass marked 1 of 20 swing items for exactly this
    reason. Marks are a read; only evaluation is gated to the live window.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from scripts.cycle_daemon import in_live_window, in_mark_window

    ist = ZoneInfo("Asia/Kolkata")
    after_close = datetime(2026, 9, 4, 16, 15, tzinfo=ist)
    assert not in_live_window(after_close)
    assert in_mark_window(after_close)
    # ...but not all night, and never at the weekend.
    assert not in_mark_window(datetime(2026, 9, 4, 23, 30, tzinfo=ist))
    assert not in_mark_window(datetime(2026, 9, 5, 16, 15, tzinfo=ist).replace(day=6))
