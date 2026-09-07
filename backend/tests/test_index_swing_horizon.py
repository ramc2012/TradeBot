"""Tests for the swing horizon and the factor panel.

The horizon tests exist because the first version of this lane measured its
holding period in wall-clock 30-minute bars, so an overnight gap counted as 36
bars and every position that survived a night tripped its max-hold the next
morning. The book proved it: 34 trades, average hold 0.262 days, 27 of them
closed in the session they opened. These assertions are what stops that
returning.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import pytest

from directional_options.index_paper.factors import (
    CONFIDENCE_WEIGHT,
    Factor,
    FactorContext,
    FactorPanel,
    factor_futures_oi_state,
    factor_futures_oi_z,
    factor_geometry,
    factor_trend,
    factor_vrp,
    squash,
    unavailable,
)
from directional_options.index_paper.horizon import (
    add_sessions,
    calendar_days_for_sessions,
    is_session,
    plan_hold,
    sessions_between,
    sessions_to_expiry,
)
from directional_options.vol.realized import DailyBar

FRI = date(2026, 9, 4)
MON = date(2026, 9, 7)


# ── session arithmetic ──────────────────────────────────────────────────────


def test_a_weekend_is_one_session_not_three_days():
    assert sessions_between(FRI, MON) == 1
    assert not is_session(date(2026, 9, 6))   # Sunday
    assert is_session(MON)


def test_sessions_between_is_exclusive_of_the_start():
    assert sessions_between(FRI, FRI) == 0
    assert sessions_between(MON, FRI) == 0, "a backwards span is zero, not negative"


def test_add_sessions_skips_non_sessions():
    assert add_sessions(FRI, 1) == MON
    assert add_sessions(FRI, 0) == FRI


def test_decay_is_charged_in_calendar_days_not_sessions():
    """Five sessions from a Friday is seven calendar days of theta."""
    assert calendar_days_for_sessions(FRI, 5) == 7.0
    assert calendar_days_for_sessions(FRI, 1) == 3.0   # Fri -> Mon
    assert calendar_days_for_sessions(FRI, 0) == 0.0


# ── hold planning ───────────────────────────────────────────────────────────


def test_hold_is_truncated_by_a_near_expiry_and_says_so():
    plan = plan_hold(FRI, date(2026, 9, 8), 5)      # 2 sessions away
    assert plan.feasible
    assert plan.planned_sessions == 1
    assert plan.truncated_by_expiry
    assert "truncated" in plan.reason


def test_a_far_expiry_grants_the_full_requested_hold():
    plan = plan_hold(FRI, date(2026, 9, 29), 5)
    assert plan.planned_sessions == 5
    assert not plan.truncated_by_expiry
    assert plan.calendar_days == 7.0


def test_no_room_to_hold_is_infeasible_not_zero_length():
    plan = plan_hold(FRI, MON, 3)                  # expiry is the next session
    assert not plan.feasible
    assert plan.planned_sessions == 0
    assert "no room to hold" in plan.reason


def test_the_buffer_keeps_a_session_clear_of_expiry():
    """The last session of an option's life is not modellable, so it is not held."""
    expiry = add_sessions(FRI, 4)
    with_buffer = plan_hold(FRI, expiry, 5, expiry_buffer_sessions=1)
    without = plan_hold(FRI, expiry, 5, expiry_buffer_sessions=0)
    assert with_buffer.planned_sessions == without.planned_sessions - 1
    assert sessions_to_expiry(FRI, expiry) == 4


# ── factor mechanics ────────────────────────────────────────────────────────


def _ctx(**kw) -> FactorContext:
    base = dict(
        underlying="BANKNIFTY",
        session_date=FRI,
        as_of=datetime(2026, 9, 4, 8, 45, tzinfo=timezone.utc),
        daily_bars=[],
        vol_ctx={},
    )
    base.update(kw)
    return FactorContext(**base)


def test_squash_is_monotone_and_bounded():
    assert squash(0.0, 1.0) == 0.0
    assert -1.0 <= squash(-99.0, 1.0) < 0.0
    assert 0.0 < squash(99.0, 1.0) <= 1.0
    # Monotone across the range factors actually occupy; tanh saturates only
    # beyond ~19 scale units, which no factor's chosen scale reaches.
    for a, b in ((1.0, 2.0), (2.0, 3.0), (3.0, 5.0)):
        assert squash(b, 1.0) > squash(a, 1.0)
    assert squash(1.0, 2.0) < squash(1.0, 1.0), "a larger scale must damp the reading"


def test_a_rich_variance_premium_scores_negative_for_a_premium_buyer():
    rich = factor_vrp(_ctx(vol_ctx={"vrp_spread": 0.04}))
    cheap = factor_vrp(_ctx(vol_ctx={"vrp_spread": -0.01}))
    assert rich.usable and cheap.usable
    assert rich.sign * rich.score < 0, "paying up for premium must reduce size"
    assert cheap.sign * cheap.score > 0
    assert rich.role == "size"


def test_futures_oi_sizes_and_never_points():
    """IC +0.139 against |5d return|, +0.025 against the signed return."""
    factor = factor_futures_oi_z(_ctx(futures_oi={"oi_z": 1.5, "age_days": 0, "dte": 25}))
    assert factor.role == "size"
    assert factor.confidence == "measured"
    assert factor.weight == CONFIDENCE_WEIGHT["measured"]
    assert factor.score > 0


def test_a_stale_futures_row_is_refused_not_read_as_positioning():
    """The collector behind this table is a one-off backfill, not a daily job."""
    factor = factor_futures_oi_z(_ctx(futures_oi={"oi_z": 1.5, "age_days": 30, "dte": 25}))
    assert factor.status == "stale"
    assert not factor.acting
    assert factor.weight == 0.0


def test_oi_state_uses_the_tables_own_vocabulary():
    """Guessed labels made this factor silently unavailable on 7 of 10 decisions."""
    for state, expected_sign in (("long_buildup", 1), ("short_buildup", -1),
                                 ("short_covering", 1), ("long_unwind", -1)):
        factor = factor_futures_oi_state(_ctx(futures_oi={"oi_state": state}))
        assert factor.usable, state
        assert math.copysign(1, factor.score) == expected_sign, state
    assert not factor_futures_oi_state(_ctx(futures_oi={"oi_state": "long_build"})).usable


def test_geometry_prefers_the_contract_that_travels_least():
    near = factor_geometry(_ctx(breakeven_ratio=0.15))   # 25 DTE
    far = factor_geometry(_ctx(breakeven_ratio=0.43))    # 4 DTE
    assert near.sign * near.score > far.sign * far.score
    assert near.confidence == "measured"


def test_trend_tolerates_a_dropped_session_but_not_many():
    def bars(n, gaps_at=()):
        return [
            DailyBar(day=FRI, open=100.0, high=101.0, low=99.0, close=100.0 + i,
                     contiguous=(i not in gaps_at))
            for i in range(n)
        ]
    assert factor_trend(_ctx(daily_bars=bars(25, gaps_at=(5,))), window=20).usable
    assert not factor_trend(_ctx(daily_bars=bars(25, gaps_at=tuple(range(1, 12)))), window=20).usable


# ── panel composition ───────────────────────────────────────────────────────


def _panel(*factors: Factor) -> FactorPanel:
    panel = FactorPanel(underlying="NIFTY", session_date=FRI,
                        as_of=datetime(2026, 9, 4, 8, 45, tzinfo=timezone.utc))
    for f in factors:
        panel.factors[f.name] = f
    return panel


def _dir(name: str, score: float, weight: float = 0.15, sign: int = 1) -> Factor:
    return Factor(name=name, family="t", role="direction", value=score,
                  confidence="speculative", sign=sign, score=score, weight=weight)


def test_direction_is_none_when_nothing_acts():
    panel = _panel(unavailable("a", "t", "direction", "no data"))
    score, terms = panel.direction_score()
    assert score is None
    assert terms


def test_agreement_distinguishes_consensus_from_a_surviving_remainder():
    """A small net of two factors pulling apart is not two factors agreeing."""
    consensus = _panel(_dir("a", 0.4), _dir("b", 0.3))
    opposed = _panel(_dir("a", 0.9), _dir("b", -0.8))

    assert consensus.direction_agreement() == (2, 2)
    assert opposed.direction_agreement() == (1, 2)
    # Both can produce a positive composite; only one is agreement.
    assert consensus.direction_score()[0] > 0
    assert opposed.direction_score()[0] > 0


def test_sign_is_applied_to_the_composite():
    """A -1 sign factor scoring positive must push the composite DOWN."""
    panel = _panel(_dir("inverted", 0.8, sign=-1))
    score, _ = panel.direction_score()
    assert score < 0


def test_shadow_factors_carry_weight_so_the_lane_can_learn():
    """Zero-weight everywhere means the lane never trades, so never measures."""
    assert CONFIDENCE_WEIGHT["measured"] > CONFIDENCE_WEIGHT["plausible"]
    assert CONFIDENCE_WEIGHT["plausible"] > CONFIDENCE_WEIGHT["speculative"]
    assert CONFIDENCE_WEIGHT["speculative"] > 0.0


def test_panel_serialises_every_factor_including_the_silent_ones():
    panel = _panel(_dir("acting", 0.5), unavailable("silent", "t", "direction", "no data"))
    payload = panel.as_dict()
    assert set(payload["factors"]) == {"acting", "silent"}
    assert payload["n_unavailable"] == 1
    assert payload["n_acting"] == 1
