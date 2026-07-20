"""Contract tests for the Gann TIME cycle library (gann_tp_delta.cycles).

The headline properties guarded here:

* every recognised Gann family is present and CITED, and the master long
  cycles are generated but flagged untestable rather than quietly scored;
* the testability gate is a function of the history actually available, and
  produces a REASON for every excluded cycle;
* anchors are strictly causal — a swing pivot is not usable until the
  confirmation bars after it have printed, which is the classic Gann lookahead
  trap and the one thing that would make every projected date fiction.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from gann_tp_delta import cycles as cy


def _frame(closes: list[float], start: str = "2024-01-01") -> pd.DataFrame:
    times = pd.date_range(start, periods=len(closes), freq="D")
    return pd.DataFrame(
        {
            "time": times,
            "open": closes,
            "high": [value + 1.0 for value in closes],
            "low": [value - 1.0 for value in closes],
            "close": closes,
            "volume": [0] * len(closes),
            "oi": [0] * len(closes),
        }
    )


# ── Library coverage ───────────────────────────────────────────────────────


def test_every_recognised_family_is_present_and_cited():
    families = {cycle.family for cycle in cy.all_cycles()}
    assert families == {
        "calendar",
        "fractional_year",
        "anniversary",
        "week",
        "sq9_time",
        "master_long",
    }
    for cycle in cy.all_cycles():
        assert cycle.source and "Gann" in cycle.source or cycle.family == "sq9_time"
        assert cycle.days > 0


def test_calendar_family_carries_the_classic_day_counts():
    days = {cycle.days for cycle in cy.calendar_cycles()}
    assert {30, 45, 60, 90, 120, 135, 144, 180, 270, 360} <= days


def test_week_counts_are_seven_thirteen_twentysix_fiftytwo_weeks():
    assert [cycle.days for cycle in cy.week_cycles()] == [49, 91, 182, 364]


def test_fractional_year_divisions_match_the_solar_year():
    days = {cycle.days for cycle in cy.fractional_year_cycles(max_multiple=1)}
    assert days == {46, 91, 122, 183, 243, 274}


def test_sq9_time_counts_reproduce_the_squares_of_time():
    days = {cycle.days for cycle in cy.sq9_time_cycles(max_days=400)}
    # theta = 540 -> 16, 720 -> 25, 900 -> 36, 1080 -> 49 …
    assert {25, 36, 49, 64, 81, 100} <= days


def test_master_long_cycles_are_generated_but_never_testable():
    master = cy.master_long_cycles()
    assert [cycle.days for cycle in master] == [3652, 7305, 10957, 21915]
    assert all(cycle.testable is False for cycle in master)
    assert all(cycle not in cy.testable_cycles(100_000) for cycle in master)


# ── Testability gate ───────────────────────────────────────────────────────


def test_testable_cycles_scale_with_available_history():
    # 1,855 calendar days (the index depth) / 20 observations => <= ~92 days.
    keep = cy.testable_cycles(1855, min_observations=20)
    assert keep, "index-depth history must admit at least the short cycles"
    assert max(cycle.days for cycle in keep) <= 92
    # 475 days (a ~1.3 year stock) admits almost nothing.
    thin = cy.testable_cycles(475, min_observations=20)
    assert max((cycle.days for cycle in thin), default=0) <= 23


def test_every_untestable_cycle_states_a_reason():
    excluded = cy.untestable_cycles(1855, min_observations=20)
    assert excluded
    keys = {cycle.key for cycle, _ in excluded}
    assert {"master_10y", "anniv_1y"} <= keys
    for cycle, reason in excluded:
        assert reason and ("exceeds" in reason or "below the" in reason)
        # UNTESTABLE must never be phrased as a weak result.
        assert "weak" not in reason.lower()


# ── Causality: the classic Gann lookahead trap ─────────────────────────────


def test_pivot_is_not_confirmable_until_right_bars_have_printed():
    closes = [100.0] * 5 + [120.0] + [100.0] * 10
    anchors = cy.causal_anchors(_frame(closes), left=5, right=5)
    peak = [a for a in anchors if a.kind == "swing_high" and a.index == 5]
    assert peak, "the planted swing high must be found"
    anchor = peak[0]
    assert anchor.confirmed_index == anchor.index + 5
    assert anchor.confirmed_date == anchor.pivot_date + timedelta(days=5)


def test_no_anchor_is_emitted_inside_the_unconfirmable_tail():
    closes = list(range(100, 140))
    frame = _frame([float(value) for value in closes])
    anchors = cy.causal_anchors(frame, left=5, right=5)
    last_index = len(frame.index) - 1
    assert all(anchor.index <= last_index - 5 for anchor in anchors)


def test_projection_before_anchor_confirmation_is_dropped():
    frame = _frame([100.0] * 5 + [120.0] + [100.0] * 10)
    anchors = cy.causal_anchors(frame, left=5, right=5)
    session_dates = [pd.Timestamp(value).date() for value in frame["time"]]
    # A 1-day "cycle" projects to the day AFTER the pivot — which is inside the
    # 5-session confirmation lag and must therefore be refused.
    too_soon = cy.CycleDef("t1", "test", 1, "1d", "test")
    assert cy.project_cycle_dates(anchors, [too_soon], session_dates) == []
    # A 10-day cycle clears the lag and survives.
    fine = cy.CycleDef("t10", "test", 10, "10d", "test")
    assert cy.project_cycle_dates(anchors, [fine], session_dates)


def test_next_projection_only_uses_already_confirmed_anchors():
    anchor = cy.CausalAnchor(
        "swing_low", 10, date(2026, 7, 1), 100.0, 15, date(2026, 7, 8), 0.05
    )
    cycle = cy.CycleDef("c30", "calendar", 30, "30d", "src")
    # As of a date BEFORE confirmation, nothing may be projected.
    assert cy.next_projection([anchor], [cycle], as_of=date(2026, 7, 5)) is None
    found = cy.next_projection([anchor], [cycle], as_of=date(2026, 7, 10))
    assert found is not None
    projected, matched, source, repeat = found
    assert projected == date(2026, 7, 31)
    assert matched.key == "c30" and source is anchor and repeat == 1


def test_next_projection_rolls_to_the_next_repetition():
    anchor = cy.CausalAnchor(
        "swing_low", 0, date(2026, 1, 1), 100.0, 5, date(2026, 1, 6), 0.05
    )
    cycle = cy.CycleDef("c30", "calendar", 30, "30d", "src")
    found = cy.next_projection([anchor], [cycle], as_of=date(2026, 3, 15))
    assert found is not None
    projected, _cycle, _anchor, repeat = found
    assert repeat == 3 and projected == date(2026, 4, 1)


# ── Square-of-Nine chart scale ─────────────────────────────────────────────


def test_resolve_price_unit_normalises_sq9_resolution_across_price_levels():
    import math

    for price in (13.5, 275.0, 300.0, 7941.0, 24241.0, 77491.0, 221612.0):
        unit = cy.resolve_price_unit(price)
        root = math.sqrt(price / unit)
        assert 60.0 <= root <= 600.0, f"{price} -> unit {unit}, root {root}"


def test_resolve_price_unit_leaves_the_legacy_index_symbols_at_one():
    for price in (24241.0, 57973.0, 77491.0, 7941.0, 141410.0, 221612.0):
        assert cy.resolve_price_unit(price) == 1.0
    # NATURALGAS is the one legacy symbol whose scale changes.
    assert cy.resolve_price_unit(274.9) == 0.01


def test_resolve_price_unit_is_safe_on_degenerate_input():
    assert cy.resolve_price_unit(0.0) == 1.0
    assert cy.resolve_price_unit(float("nan")) == 1.0


# ── Seasonal ───────────────────────────────────────────────────────────────


def test_seasonal_dates_return_the_four_cardinal_points_per_year():
    points = cy.seasonal_dates(date(2026, 1, 1), date(2026, 12, 31))
    assert len(points) == 4
    assert [point[1] for point in points] == [
        "vernal_equinox",
        "summer_solstice",
        "autumnal_equinox",
        "winter_solstice",
    ]


def test_cycle_catalog_is_serialisable_and_complete():
    catalog = cy.cycle_catalog()
    assert len(catalog) == len(cy.all_cycles())
    assert all(set(entry) == {"key", "family", "days", "label", "source", "testable_in_principle"} for entry in catalog)
