"""Leakage tests. These MUST pass before any Phase 0 verdict can be `go`.

The core invariant: for any decision_time `t`, no feature's `data_available_at` may exceed `t`.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from nomad_sniper.features.base import assert_no_leakage
from nomad_sniper.features.pipeline import build_all_features


def test_no_feature_uses_future_data(synthetic_bars, synthetic_decision_time):
    """The decision_time invariant holds end-to-end through the pipeline."""
    snap = build_all_features(synthetic_decision_time, synthetic_bars)
    assert_no_leakage(snap, synthetic_decision_time)


def test_features_change_when_decision_time_advances(synthetic_bars, synthetic_decision_time):
    """A later decision time must see at least as much information as an earlier one."""
    early_snap = build_all_features(synthetic_decision_time, synthetic_bars)
    later = synthetic_decision_time + timedelta(minutes=30)
    later_snap = build_all_features(later, synthetic_bars)

    early_count = sum(1 for f in early_snap.features if f.value is not None)
    later_count = sum(1 for f in later_snap.features if f.value is not None)
    assert later_count >= early_count, (
        "Information monotonicity violated: later snapshot should have ≥ as many "
        f"non-null features. early={early_count}, later={later_count}"
    )


def test_ib_features_appear_only_after_10_15(synthetic_bars):
    """IB features must NOT appear before 10:15 IST (end of first hour)."""
    from datetime import datetime, time
    from nomad_sniper.utils.timeutil import IST

    early = IST.localize(datetime(2025, 1, 8, 9, 45))
    snap = build_all_features(early, synthetic_bars)
    ib_features = [f for f in snap.features if f.name.startswith("u_dist_ib") and f.value is not None]
    assert not ib_features, f"IB features present too early: {[f.name for f in ib_features]}"

    late = IST.localize(datetime(2025, 1, 8, 11, 0))
    snap_late = build_all_features(late, synthetic_bars)
    ib_features_late = [
        f for f in snap_late.features
        if f.name in ("u_dist_ib_high_atr", "u_dist_ib_low_atr", "u_price_above_ib") and f.value is not None
    ]
    assert len(ib_features_late) == 3, "IB high/low/range should be present after 10:15"


def test_assert_no_leakage_raises_on_violation(synthetic_decision_time, synthetic_bars):
    """A manually corrupted snapshot triggers the leakage check."""
    from datetime import timedelta

    from nomad_sniper.features.base import Feature, FeatureSnapshot

    snap = FeatureSnapshot(decision_time=synthetic_decision_time)
    # Add a feature claiming to know something from the future
    snap.add(Feature(
        name="from_the_future",
        value=1.0,
        data_available_at=synthetic_decision_time + timedelta(hours=1),
        source="test",
    ))
    with pytest.raises(AssertionError, match="Leakage detected"):
        assert_no_leakage(snap, synthetic_decision_time)


def test_naive_datetime_rejected():
    """Passing a tz-naive datetime to ensure_ist must raise."""
    from datetime import datetime

    from nomad_sniper.utils.timeutil import ensure_ist

    with pytest.raises(ValueError, match="Naive datetime"):
        ensure_ist(datetime(2025, 1, 8, 11, 30))
