"""Feature base classes with mandatory `data_available_at` timestamps.

This is the core mechanism for preventing look-ahead bias. Every feature value carries
the timestamp at which it became known. The pipeline only emits a feature for decision time
`t` if `data_available_at <= t`.

This is enforced by `assert_no_leakage(snapshot, decision_time)` and re-checked in the test
suite. Do NOT bypass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from nomad_sniper.utils.timeutil import ensure_ist


@dataclass(frozen=True)
class Feature:
    """A single feature value with its provenance.

    Attributes:
        name:                Feature name (snake_case).
        value:               The numeric / categorical value.
        data_available_at:   Wall-clock IST timestamp when this value first became knowable.
                             For a 3-min bar closing at 09:18, this is 09:18:00 IST, not 09:15.
        source:              Coarse category ('mp', 'of', 'context') for filtering.
    """

    name: str
    value: float | int | str | None
    data_available_at: datetime
    source: str

    def __post_init__(self):
        # frozen dataclass — bypass via object.__setattr__
        object.__setattr__(self, "data_available_at", ensure_ist(self.data_available_at))


@dataclass
class FeatureSnapshot:
    """All features known at a single decision time `t`.

    Use `.to_row(decision_time)` to get a flat dict for a model, after leakage check.
    """

    decision_time: datetime
    features: list[Feature] = field(default_factory=list)

    def __post_init__(self):
        self.decision_time = ensure_ist(self.decision_time)

    def add(self, feature: Feature) -> None:
        self.features.append(feature)

    def to_row(self, *, strict: bool = True) -> dict[str, Any]:
        """Flatten to a {feature_name: value} dict.

        Args:
            strict: If True, raise if any feature has `data_available_at > decision_time`.
                    Always leave True except for diagnostic introspection.
        """
        if strict:
            assert_no_leakage(self, self.decision_time)
        out: dict[str, Any] = {"decision_time": self.decision_time}
        for f in self.features:
            out[f.name] = f.value
        return out

    def filter_by_source(self, source: str) -> list[Feature]:
        return [f for f in self.features if f.source == source]


def assert_no_leakage(snapshot: FeatureSnapshot, decision_time: datetime) -> None:
    """Raise AssertionError if any feature uses information from the future."""
    decision_time = ensure_ist(decision_time)
    violators = [
        (f.name, f.data_available_at)
        for f in snapshot.features
        if f.data_available_at > decision_time
    ]
    if violators:
        raise AssertionError(
            f"Leakage detected at decision_time={decision_time.isoformat()}: "
            f"{len(violators)} features have data_available_at > decision_time. "
            f"First 5: {violators[:5]}"
        )
