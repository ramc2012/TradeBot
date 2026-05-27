"""The non-negotiable test. If this fails, no model trains."""
from __future__ import annotations

import pandas as pd
import pytest

from sniper_phase0.features.base import (
    FeatureSnapshot,
    LeakageError,
    assert_no_leakage,
)


def test_snapshot_rejects_future_availability() -> None:
    snap = FeatureSnapshot(
        trade_id=1,
        decision_ts=pd.Timestamp("2024-04-15 10:00:00"),
        instrument="NIFTY",
        side="long",
    )
    with pytest.raises(LeakageError):
        snap.add("oops", 1.0, pd.Timestamp("2024-04-15 10:00:01"))


def test_snapshot_accepts_past_availability() -> None:
    snap = FeatureSnapshot(
        trade_id=1,
        decision_ts=pd.Timestamp("2024-04-15 10:00:00"),
        instrument="NIFTY",
        side="long",
    )
    snap.add("fine", 1.0, pd.Timestamp("2024-04-15 09:59:59"))
    assert "fine" in snap.values


def test_audit_catches_leak_in_dataframe() -> None:
    features = pd.DataFrame(
        {
            "trade_id": [1, 2],
            "decision_ts": pd.to_datetime(["2024-04-15 10:00", "2024-04-15 11:00"]),
            "instrument": ["NIFTY", "NIFTY"],
            "side": ["long", "long"],
            "feat_a": [0.1, 0.2],
        }
    )
    availability = pd.DataFrame(
        {
            "trade_id": [1, 2],
            "decision_ts": pd.to_datetime(["2024-04-15 10:00", "2024-04-15 11:00"]),
            "feat_a": pd.to_datetime(["2024-04-15 09:59", "2024-04-15 11:30"]),  # row 2 leaks
        }
    )
    with pytest.raises(LeakageError):
        assert_no_leakage(features, availability)


def test_audit_passes_when_clean() -> None:
    features = pd.DataFrame(
        {
            "trade_id": [1],
            "decision_ts": pd.to_datetime(["2024-04-15 10:00"]),
            "instrument": ["NIFTY"],
            "side": ["long"],
            "feat_a": [0.1],
        }
    )
    availability = pd.DataFrame(
        {
            "trade_id": [1],
            "decision_ts": pd.to_datetime(["2024-04-15 10:00"]),
            "feat_a": pd.to_datetime(["2024-04-15 10:00"]),
        }
    )
    assert_no_leakage(features, availability)
