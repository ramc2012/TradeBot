"""Walk-forward time-ordered splits with purge gap.

Rule: never use information from the future to predict the past. A purge gap between train
and test prevents label leakage from overlapping holding windows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterator

import numpy as np
import pandas as pd

from nomad_sniper.utils.timeutil import IST


@dataclass
class Split:
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    embargo_minutes: int = 0

    def train_mask(
        self,
        ts: pd.Series,
        *,
        label_end_times: pd.Series | None = None,
    ) -> pd.Series:
        dates = ts.dt.date if hasattr(ts, "dt") else pd.to_datetime(ts).dt.date
        mask = (dates >= self.train_start) & (dates <= self.train_end)
        if label_end_times is None:
            return mask
        starts = pd.to_datetime(ts)
        ends = pd.to_datetime(label_end_times)
        test_start_dt = _start_of_day(self.test_start) - timedelta(minutes=self.embargo_minutes)
        test_end_dt = _end_of_day(self.test_end) + timedelta(minutes=self.embargo_minutes)
        overlaps_test = (starts <= test_end_dt) & (ends >= test_start_dt)
        return mask & ~overlaps_test

    def test_mask(self, ts: pd.Series) -> pd.Series:
        dates = ts.dt.date if hasattr(ts, "dt") else pd.to_datetime(ts).dt.date
        return (dates >= self.test_start) & (dates <= self.test_end)


def walk_forward(
    timestamps: pd.Series,
    *,
    train_months: int = 6,
    test_months: int = 1,
    purge_days: int = 2,
    embargo_minutes: int = 0,
    min_train_size: int = 50,
) -> Iterator[Split]:
    """Yield walk-forward (train, test) date ranges.

    Args:
        timestamps:     IST-aware Series of decision timestamps.
        train_months:   Length of each training window.
        test_months:    Length of each test window.
        purge_days:     Gap between train_end and test_start to prevent leakage from
                        overlapping holding periods.
        min_train_size: Skip splits where train would have fewer rows than this.
    """
    if timestamps.empty:
        return
    dates = pd.to_datetime(timestamps).dt.date
    first = min(dates)
    last = max(dates)

    train_start = first
    while True:
        train_end = _add_months(train_start, train_months) - timedelta(days=1)
        test_start = train_end + timedelta(days=purge_days + 1)
        test_end = _add_months(test_start, test_months) - timedelta(days=1)

        if test_start > last:
            break

        # Clip the final test window so we don't exceed available data
        test_end = min(test_end, last)

        split = Split(
            train_start=train_start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
            embargo_minutes=embargo_minutes,
        )

        # Skip if too few train rows
        train_count = ((dates >= train_start) & (dates <= train_end)).sum()
        if train_count >= min_train_size:
            yield split

        # Advance by `test_months`
        train_start = _add_months(train_start, test_months)


def _add_months(d: date, months: int) -> date:
    """Add months to a date, clamping to end-of-month."""
    year = d.year + (d.month - 1 + months) // 12
    month = (d.month - 1 + months) % 12 + 1
    # Clamp day
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    day = min(d.day, last_day)
    return date(year, month, day)


def sample_uniqueness_weights(label_windows: pd.DataFrame) -> pd.Series:
    """Lopez de Prado-style uniqueness weights for overlapping forward labels.

    `label_windows` must include `decision_time` and `label_end_time`. The returned weight is the
    average inverse concurrency over the sample's forward window.
    """
    if label_windows.empty:
        return pd.Series(dtype=float)
    starts = pd.to_datetime(label_windows["decision_time"])
    ends = pd.to_datetime(label_windows["label_end_time"])
    events = []
    for idx, start in starts.items():
        end = ends.loc[idx]
        if pd.isna(start) or pd.isna(end) or end <= start:
            continue
        events.append((idx, start, end))
    if not events:
        return pd.Series(1.0, index=label_windows.index)

    change_points = sorted({ts for _, start, end in events for ts in (start, end)})
    if len(change_points) < 2:
        return pd.Series(1.0, index=label_windows.index)

    intervals = []
    for a, b in zip(change_points[:-1], change_points[1:]):
        if b <= a:
            continue
        active = [idx for idx, start, end in events if start < b and end > a]
        if active:
            intervals.append((a, b, active, (b - a).total_seconds()))

    weights = {}
    for idx, start, end in events:
        total = 0.0
        weighted = 0.0
        for a, b, active, seconds in intervals:
            if start < b and end > a:
                overlap = (min(end, b) - max(start, a)).total_seconds()
                if overlap > 0:
                    total += overlap
                    weighted += overlap / len(active)
        weights[idx] = weighted / total if total > 0 else 1.0
    return pd.Series(weights, index=label_windows.index).fillna(1.0).clip(lower=0.0, upper=1.0)


def _start_of_day(d: date) -> datetime:
    return IST.localize(datetime.combine(d, time.min))


def _end_of_day(d: date) -> datetime:
    return IST.localize(datetime.combine(d, time.max))
