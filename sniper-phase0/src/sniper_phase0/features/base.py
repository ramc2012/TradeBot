"""Feature snapshot dataclass and leakage guard.

Every feature value carries `data_available_at`. The guard asserts that
data_available_at <= decision_time for every row, every column. Any feature
builder that violates this raises before the row is added to the dataset.

This is the single most important invariant in Phase 0. See tests/test_no_leakage.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class FeatureSnapshot:
    trade_id: int
    decision_ts: pd.Timestamp
    instrument: str
    side: str
    values: dict[str, float] = field(default_factory=dict)
    data_available_at: dict[str, pd.Timestamp] = field(default_factory=dict)

    def add(self, name: str, value: float, available_at: pd.Timestamp) -> None:
        if available_at > self.decision_ts:
            raise LeakageError(
                f"Feature {name!r} would leak: available_at={available_at} > "
                f"decision_ts={self.decision_ts}"
            )
        self.values[name] = value
        self.data_available_at[name] = available_at

    def to_row(self) -> dict:
        row = {
            "trade_id": self.trade_id,
            "decision_ts": self.decision_ts,
            "instrument": self.instrument,
            "side": self.side,
        }
        row.update(self.values)
        return row


class LeakageError(AssertionError):
    pass


def assert_no_leakage(features_df: pd.DataFrame, availability_df: pd.DataFrame) -> None:
    """Audit a finished features DataFrame.

    `availability_df` has the same shape as `features_df` and holds, for each
    feature column, the timestamp at which that value became known.
    """
    if "decision_ts" not in features_df.columns:
        raise ValueError("features_df must contain decision_ts")
    decision = features_df["decision_ts"]
    feature_cols = [
        c for c in features_df.columns
        if c not in {"trade_id", "decision_ts", "instrument", "side"}
    ]
    for col in feature_cols:
        if col not in availability_df.columns:
            raise LeakageError(f"No availability timestamp recorded for feature {col!r}")
        bad = availability_df[col] > decision
        if bad.any():
            n = int(bad.sum())
            raise LeakageError(
                f"Feature {col!r} leaks on {n} rows "
                f"(availability after decision_ts)."
            )
