"""Walk-forward time-ordered splits with purge gap + embargo + uniqueness weights.

Rule: never use information from the future to predict the past. With grid labels whose
forward windows overlap (contract §6), a purge gap alone is insufficient — we additionally
(a) embargo a window of the label horizon around each test boundary and (b) down-weight
samples by how many others share their label window (López de Prado uniqueness weights).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd


@dataclass
class Split:
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    embargo_minutes: int = 0  # label horizon H; train rows whose label window reaches the
                              # test span (within this many minutes of test_start) are dropped.

    def test_mask(self, ts: pd.Series) -> pd.Series:
        dates = ts.dt.date if hasattr(ts, "dt") else pd.to_datetime(ts).dt.date
        return (dates >= self.test_start) & (dates <= self.test_end)

    def train_mask(self, ts: pd.Series) -> pd.Series:
        """Train rows are within [train_start, train_end] AND end before the embargo cliff.

        The embargo cliff = test_start 00:00 IST minus `embargo_minutes`. A grid row at time
        `t` carries a forward label window of `embargo_minutes`; if `t + embargo` crosses into
        the test span the row leaks, so we require `t <= cliff`.
        """
        tsd = ts if hasattr(ts, "dt") else pd.to_datetime(ts)
        dates = tsd.dt.date
        in_window = (dates >= self.train_start) & (dates <= self.train_end)
        if self.embargo_minutes <= 0:
            return in_window
        cliff = pd.Timestamp(self.test_start) - pd.Timedelta(minutes=self.embargo_minutes)
        if tsd.dt.tz is not None:
            cliff = cliff.tz_localize(tsd.dt.tz) if cliff.tzinfo is None else cliff.tz_convert(tsd.dt.tz)
        return in_window & (tsd <= cliff)


def walk_forward(
    timestamps: pd.Series,
    *,
    train_months: int = 6,
    test_months: int = 1,
    purge_days: int = 2,
    embargo_minutes: int = 60,
    min_train_size: int = 50,
) -> Iterator[Split]:
    """Yield walk-forward (train, test) date ranges with an intraday embargo.

    Args:
        timestamps:      IST-aware Series of decision timestamps.
        train_months:    Length of each training window.
        test_months:     Length of each test window.
        purge_days:      Whole-day gap between train_end and test_start.
        embargo_minutes: Label horizon H — intraday embargo applied at the test boundary so
                         train rows whose forward label reaches the test span are dropped.
        min_train_size:  Skip splits where train would have fewer rows than this.
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

        split = Split(train_start=train_start, train_end=train_end,
                      test_start=test_start, test_end=test_end,
                      embargo_minutes=embargo_minutes)

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


def sample_uniqueness_weights(
    decision_times: pd.Series,
    *,
    horizon_minutes: int = 60,
) -> pd.Series:
    """López de Prado uniqueness weights for overlapping forward labels (contract §6).

    Each grid label spans [t, t + horizon]. A sample's weight is the inverse of the average
    number of concurrently-overlapping labels across its own span — samples in dense clusters
    are down-weighted so CV scores and feature importances are not inflated.

    Computed per-day (labels never overlap across sessions). Returns a Series aligned to the
    input index, normalized to mean 1.0.
    """
    ts = pd.to_datetime(decision_times)
    idx = decision_times.index
    weights = pd.Series(1.0, index=idx)
    horizon = pd.Timedelta(minutes=horizon_minutes)

    frame = pd.DataFrame({"t": ts.values}, index=idx)
    frame["day"] = frame["t"].dt.date
    for _, grp in frame.groupby("day"):
        starts = grp["t"].values.astype("datetime64[ns]")
        ends = (grp["t"] + horizon).values.astype("datetime64[ns]")
        n = len(grp)
        # concurrency[i] = # of labels whose span overlaps label i's span.
        conc = np.ones(n, dtype=float)
        for i in range(n):
            overlap = (starts < ends[i]) & (ends > starts[i])
            conc[i] = max(1.0, overlap.sum())
        weights.loc[grp.index] = 1.0 / conc

    # Normalize to mean 1.0 so absolute loss scale is unchanged.
    m = weights.mean()
    if m > 0:
        weights = weights / m
    return weights
