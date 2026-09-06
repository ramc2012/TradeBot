"""Offline tests for the cross-sectional IC study's statistics.

The whole point of this module is that it produces a number people will act
on, so the tests are about the two ways such a number goes wrong: an IC
computed on a sample too thin to support it, and a standard error computed as
if same-session observations were independent.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parents[1]))

from research.cross_section_ic import (  # noqa: E402
    MIN_NAMES_PER_BAR,
    MIN_SESSIONS,
    aggregate_session_ics,
    bar_ic,
    decile_profile,
    run_study,
)


# ── bar_ic ─────────────────────────────────────────────────────────────────

def test_a_perfectly_ordered_bar_scores_ic_one():
    n = MIN_NAMES_PER_BAR + 5
    x = pd.Series(range(n), dtype=float)
    assert round(bar_ic(x, x * 3.0), 6) == 1.0


def test_a_perfectly_inverted_bar_scores_minus_one():
    n = MIN_NAMES_PER_BAR + 5
    x = pd.Series(range(n), dtype=float)
    assert round(bar_ic(x, -x), 6) == -1.0


def test_a_bar_with_too_few_names_is_none_not_zero():
    """A thin bar has no cross-section to correlate. Returning 0.0 would
    average into the session mean as a real 'no relationship' observation."""
    n = MIN_NAMES_PER_BAR - 1
    x = pd.Series(range(n), dtype=float)
    assert bar_ic(x, x) is None


def test_a_constant_predictor_has_no_rank_order_and_returns_none():
    n = MIN_NAMES_PER_BAR + 5
    assert bar_ic(pd.Series([1.0] * n), pd.Series(range(n), dtype=float)) is None


def test_nan_pairs_are_dropped_before_the_count_is_checked():
    """A bar that LOOKS wide enough but is mostly NULLs must not sneak past
    the width check on its row count."""
    n = MIN_NAMES_PER_BAR + 5
    x = pd.Series([np.nan] * (n - 3) + [1.0, 2.0, 3.0])
    y = pd.Series(range(n), dtype=float)
    assert bar_ic(x, y) is None


# ── aggregate_session_ics: the clustering that matters ─────────────────────

def test_the_standard_error_is_taken_across_sessions_not_observations():
    """THE POINT OF THIS MODULE. Every name in one session shares that
    session's market-wide shock. n in the t-statistic is the number of
    SESSIONS -- this lane's own directional research measured the naive
    per-observation SE running 1.6x to 4.7x too small on exactly this."""
    session_ics = [0.02, 0.04, -0.01, 0.03, 0.01]
    out = aggregate_session_ics(session_ics)
    assert out["n_sessions"] == 5
    expected_se = float(np.std(session_ics, ddof=1) / np.sqrt(5))
    assert round(out["se"], 12) == round(expected_se, 12)
    assert round(out["t_stat"], 6) == round(out["mean_ic"] / expected_se, 6)


def test_a_single_session_yields_a_mean_but_no_standard_error():
    """One observation has no spread. A t-statistic from it would be an
    invented certainty."""
    out = aggregate_session_ics([0.05])
    assert out["mean_ic"] == 0.05
    assert out["se"] is None and out["t_stat"] is None


def test_an_empty_input_returns_nulls_rather_than_a_zero_ic():
    out = aggregate_session_ics([])
    assert out["mean_ic"] is None
    assert out["n_sessions"] == 0


def test_the_confidence_interval_brackets_the_mean_and_uses_student_t():
    out = aggregate_session_ics([0.02, 0.04, -0.01, 0.03, 0.01])
    assert out["ci_low"] < out["mean_ic"] < out["ci_high"]
    # t(0.975, df=4) = 2.776 is materially wider than the normal's 1.96
    half_width = out["ci_high"] - out["mean_ic"]
    assert half_width / out["se"] > 2.5


def test_nones_from_skipped_bars_never_pull_the_mean_toward_zero():
    assert aggregate_session_ics([0.1, None, 0.1])["mean_ic"] == 0.1


# ── decile profile ─────────────────────────────────────────────────────────

def test_decile_profile_is_monotone_for_a_perfect_predictor():
    n = 500
    x = pd.Series(np.linspace(-1, 1, n))
    profile = decile_profile(x, x)
    assert len(profile) == 10
    means = [d["mean_fwd"] for d in profile]
    assert means == sorted(means)


def test_decile_profile_declines_a_sample_too_small_to_bucket():
    x = pd.Series(np.linspace(-1, 1, 20))
    assert decile_profile(x, x) == []


# ── run_study wiring ───────────────────────────────────────────────────────

def _frames(n_sessions=3, n_names=40):
    """A synthetic cross-section where signed_flow genuinely predicts the
    1-bar forward return and signed_rs is pure noise."""
    rng = np.random.default_rng(7)
    rows_eval, rows_fwd = [], []
    for s in range(n_sessions):
        for b in range(2):
            ts = pd.Timestamp("2026-08-03", tz="UTC") + pd.Timedelta(days=s, minutes=30 * b)
            for i in range(n_names):
                signal = rng.normal()
                rows_eval.append({
                    "ts": ts, "symbol": f"SYM{i}", "sector20": "X",
                    "conviction": 50.0 + signal, "direction": "bullish",
                    "signed_flow": signal, "signed_rs": rng.normal(),
                    "signed_timing": rng.normal(), "signed_regime": rng.normal(),
                })
                rows_fwd.append({
                    "ts": ts, "symbol": f"SYM{i}",
                    "fwd_ret_1": signal * 0.01 + rng.normal() * 0.0005,
                })
    return pd.DataFrame(rows_eval), pd.DataFrame(rows_fwd)


def test_run_study_recovers_a_planted_signal_and_not_a_planted_non_signal():
    evaluations, forwards = _frames()
    results = {r["component"]: r for r in run_study(evaluations, forwards, (1,))}
    assert results["signed_flow"]["mean_ic"] > 0.8
    assert abs(results["signed_rs"]["mean_ic"]) < 0.3


def test_a_short_window_is_reported_as_under_powered_rather_than_as_a_finding():
    """Three sessions is not a test of anything. The result is still stored
    -- suppressing it entirely would hide that the study ran -- but it is
    flagged, and the flag is what a reader is meant to act on."""
    evaluations, forwards = _frames(n_sessions=3)
    results = {r["component"]: r for r in run_study(evaluations, forwards, (1,))}
    assert results["signed_flow"]["n_sessions"] < MIN_SESSIONS
    assert results["signed_flow"]["sample_adequate"] is False


def test_conviction_is_scored_against_the_ABSOLUTE_move_not_the_signed_one():
    """Conviction is an unsigned strength claim -- 'this is a bigger
    opportunity' -- so correlating it with a signed return would be asking it
    a question it never answered."""
    evaluations, forwards = _frames()
    results = {r["component"]: r for r in run_study(evaluations, forwards, (1,))}
    assert results["conviction"]["unsigned_vs_abs_return"] is True
    assert results["signed_flow"]["unsigned_vs_abs_return"] is False


def test_an_empty_evaluation_frame_yields_no_results_rather_than_zeros():
    _, forwards = _frames()
    assert run_study(pd.DataFrame(), forwards, (1,)) == []


def test_a_horizon_with_no_forward_column_is_skipped_silently_not_faked():
    evaluations, forwards = _frames()
    assert run_study(evaluations, forwards, (99,)) == []
