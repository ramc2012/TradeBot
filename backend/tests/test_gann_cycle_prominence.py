"""Tests for the cycle-prominence mapper (gann_tp_delta.cycle_prominence).

Two things must both be true, and the second is the one that usually is not:

* the pipeline must NOT declare prominence out of noise — the guards
  (minimum non-overlapping repetitions, an empirical null, BH-FDR, era
  stability, an OOS holdout, a placebo arm) exist for exactly that; and
* the pipeline must still be ABLE to find a cycle that is genuinely there.
  A mapper that returns "nothing is prominent" because it can detect nothing
  is worthless, and indistinguishable from an honest negative result. The
  planted-cycle positive control below is what separates the two.
"""
from __future__ import annotations

import math

import pandas as pd

from gann_tp_delta import cycle_prominence as cp
from gann_tp_delta.cycles import CycleDef


# ── Statistics primitives ──────────────────────────────────────────────────


def test_binom_sf_matches_closed_form_cases():
    assert cp.binom_sf(0, 10, 0.3) == 1.0
    assert cp.binom_sf(11, 10, 0.3) == 0.0
    # P(X >= 1) for n=1,p=0.25
    assert math.isclose(cp.binom_sf(1, 1, 0.25), 0.25, rel_tol=1e-9)
    # P(X >= 2) for n=2,p=0.5 == 0.25
    assert math.isclose(cp.binom_sf(2, 2, 0.5), 0.25, rel_tol=1e-9)
    # Monotone decreasing in k
    values = [cp.binom_sf(k, 20, 0.4) for k in range(1, 20)]
    assert values == sorted(values, reverse=True)


def test_benjamini_hochberg_rejects_the_expected_prefix():
    # n=5, q=0.05: the step-up threshold at rank k is 0.05*k/5, so ranks 1..3
    # (0.001<=0.01, 0.008<=0.02, 0.02<=0.03) are rejected and 0.2 is not.
    p_values = [0.001, 0.008, 0.02, 0.2, 0.5]
    flags = cp.benjamini_hochberg(p_values, q=0.05)
    assert flags == [True, True, True, False, False]


def test_benjamini_hochberg_rejects_nothing_when_all_are_null():
    p_values = [0.4, 0.5, 0.6, 0.7, 0.8]
    assert cp.benjamini_hochberg(p_values, q=0.10) == [False] * 5


def test_bh_adjusted_is_monotone_and_bounded():
    p_values = [0.001, 0.02, 0.03, 0.4]
    adjusted = cp.bh_adjusted(p_values)
    assert all(0.0 <= value <= 1.0 for value in adjusted)
    assert adjusted == sorted(adjusted)
    assert all(a >= p for a, p in zip(adjusted, p_values))


# ── Observation thinning: the independence guard ───────────────────────────


def _projection(index: int, tolerance: int = 3) -> cp.CycleProjection:
    from datetime import date

    return cp.CycleProjection(
        "c", 30, "swing_low", date(2026, 1, 1), 0, date(2026, 1, 31),
        index, index - tolerance, index + tolerance,
    )


def test_thin_projections_keeps_only_disjoint_windows():
    projections = [_projection(index) for index in (10, 12, 13, 30, 31, 60)]
    kept = cp.thin_projections(projections, tolerance=3)
    assert [p.projected_index for p in kept] == [10, 30, 60]


def test_thin_projections_drops_projections_outside_the_frame():
    projections = [_projection(-1), _projection(20)]
    assert [p.projected_index for p in cp.thin_projections(projections)] == [20]


# ── The null is empirical, not 0.5 ─────────────────────────────────────────


def test_turn_window_coverage_is_the_union_of_turn_windows():
    from datetime import date

    turns = [cp.Turn(10, date(2026, 1, 1), "swing_high", 0.02),
             cp.Turn(12, date(2026, 1, 3), "swing_low", 0.02)]
    # +/-1 windows around 10 and 12 => {9,10,11} U {11,12,13} = 5 sessions
    coverage = cp.turn_window_coverage(turns, lo=0, hi=100, tolerance=1)
    assert math.isclose(coverage, 0.05, rel_tol=1e-9)


def test_turn_window_coverage_is_zero_on_an_empty_region():
    assert cp.turn_window_coverage([], lo=0, hi=0, tolerance=3) == 0.0


# ── Minimum repetitions: UNTESTABLE, never "weak" ──────────────────────────


def _series(closes: list[float]) -> pd.DataFrame:
    times = pd.date_range("2020-01-01", periods=len(closes), freq="D")
    return pd.DataFrame(
        {
            "time": times,
            "open": closes,
            "high": [value * 1.005 for value in closes],
            "low": [value * 0.995 for value in closes],
            "close": closes,
            "volume": [0] * len(closes),
            "oi": [0] * len(closes),
        }
    )


def _sawtooth(period: int, repeats: int, amplitude: float = 8.0) -> list[float]:
    """Deterministic series that turns exactly every ``period`` days."""
    closes: list[float] = []
    for index in range(period * repeats):
        phase = (index % period) / period
        closes.append(100.0 + amplitude * math.sin(2.0 * math.pi * phase))
    return closes


def test_a_cycle_without_enough_repetitions_is_untestable_not_weak():
    frame = _series(_sawtooth(30, 8))  # 240 sessions: nowhere near 20 obs in-sample
    cycle = CycleDef("cal_30", "calendar", 30, "30d", "src")
    scores = cp.score_instrument("TEST", frame, [cycle], min_observations=20)
    assert len(scores) == 1
    score = scores[0]
    assert score.status == "UNTESTABLE"
    assert score.p_value is None and score.lift is None
    assert "below the 20 minimum" in (score.untestable_reason or "")


# ── Positive control: a planted cycle MUST be found ────────────────────────


def test_a_planted_cycle_survives_every_guard():
    """A series that genuinely turns every 30 days must come out PROMINENT.

    This is the control that makes the negative result on real data meaningful:
    without it, "nothing is prominent" could simply mean the mapper is broken.
    """
    frame = _series(_sawtooth(30, 70))  # 2,100 sessions of a clean 30-day cycle
    genuine = CycleDef("cal_30", "calendar", 30, "30d", "src")
    scores = cp.score_instrument("PLANTED", frame, [genuine])
    assert len(scores) == 1
    score = scores[0]
    assert score.status == "TESTED_NOT_PROMINENT", "must reach scoring, not UNTESTABLE"
    assert score.is_observations >= cp.MIN_OBSERVATIONS
    assert score.is_hit_rate is not None and score.is_hit_rate > (score.null_rate or 1.0)
    assert score.era_stable is True
    assert score.oos_confirms is True
    cp.finalise(scores, [])
    assert scores[0].status == "PROMINENT"
    assert scores[0].fdr_significant is True


def test_a_mismatched_cycle_on_the_same_series_is_not_prominent():
    frame = _series(_sawtooth(30, 70))
    wrong = CycleDef("cal_23", "calendar", 23, "23d", "src")
    scores = cp.score_instrument("PLANTED", frame, [wrong])
    cp.finalise(scores, [])
    assert scores[0].status != "PROMINENT"


# ── Placebo arm ────────────────────────────────────────────────────────────


def test_placebo_cycles_are_deterministic_and_distinct():
    first = cp.placebo_cycles(12, seed=7)
    second = cp.placebo_cycles(12, seed=7)
    assert [c.days for c in first] == [c.days for c in second]
    assert len({c.days for c in first}) == 12
    assert all(30 <= c.days <= 400 for c in first)
    assert all(c.family == "placebo" for c in first)


def test_prominence_summary_reports_both_arms_and_a_plain_verdict():
    frame = _series(_sawtooth(30, 70))
    genuine = cp.score_instrument("PLANTED", frame, [CycleDef("cal_30", "calendar", 30, "30d", "s")])
    placebo = cp.score_instrument(
        "PLANTED", frame, [CycleDef("placebo_37", "placebo", 37, "37d", "s")], placebo=True
    )
    cp.finalise(genuine, placebo)
    summary = cp.prominence_summary(genuine, placebo)
    assert summary["genuine"]["prominent"] == 1
    assert summary["placebo"]["prominent"] == 0
    assert summary["beats_placebo"] is True
    assert "placebo" in summary["verdict"].lower()


def test_summary_says_so_plainly_when_nothing_beats_random():
    summary = cp.prominence_summary([], [])
    assert summary["beats_placebo"] is False
    assert "do NOT beat randomly chosen cycle lengths" in summary["verdict"]


# ── Ranking ────────────────────────────────────────────────────────────────


def test_ranking_puts_prominent_first_and_excludes_untestable():
    prominent = cp.CycleScore("X", "a", "calendar", 30, status="PROMINENT",
                              lift=0.05, oos_hit_rate=0.6, oos_null_rate=0.4)
    tested = cp.CycleScore("X", "b", "calendar", 45, status="TESTED_NOT_PROMINENT",
                           lift=0.09, oos_hit_rate=0.5, oos_null_rate=0.45)
    untestable = cp.CycleScore("X", "c", "calendar", 360, status="UNTESTABLE")
    order = cp.ranking([tested, untestable, prominent], "X")
    assert [score.cycle_key for score in order] == ["a", "b"]
