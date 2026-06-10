"""Per-session bar index cache — speed only, never changes feature values.

The grid feature builders repeatedly slice the underlying/option bars by session date
(`bars[bars.index.date == d]`). On 1-minute data each frame is ~90k rows, and
`bars.index.date` materializes a ~90k-element object array *every call* — inside loops that
run hundreds of thousands of times. That is the entire reason a 1-min build took ~35 min.

`session_frames(bars)` computes the date→day-frame split ONCE and memoizes it by a cheap
fingerprint (object id + length + endpoints), so subsequent lookups are O(1). The returned
day-frames are exact row-subsets of the input, so any computation over them is identical to
the original full-frame filter — pure speedup, leak-free.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

# fingerprint -> (sorted_dates, {date: day_frame})
_CACHE: dict[tuple, tuple[list[date], dict[date, pd.DataFrame]]] = {}
_MAX_ENTRIES = 8


def _fingerprint(bars: pd.DataFrame) -> tuple:
    if bars.empty:
        return (id(bars), 0)
    return (id(bars), len(bars), bars.index[0].value, bars.index[-1].value)


def session_frames(bars: pd.DataFrame) -> tuple[list[date], dict[date, pd.DataFrame]]:
    """Return (sorted_session_dates, {date: rows for that date}) — cached."""
    fp = _fingerprint(bars)
    hit = _CACHE.get(fp)
    if hit is not None:
        return hit
    if bars.empty:
        result: tuple[list[date], dict[date, pd.DataFrame]] = ([], {})
    else:
        groups = {d: g for d, g in bars.groupby(bars.index.date, sort=True)}
        result = (sorted(groups.keys()), groups)
    if len(_CACHE) >= _MAX_ENTRIES:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[fp] = result
    return result


def prior_session_dates(bars: pd.DataFrame, today: date, lookback: int) -> list[date]:
    """Up to `lookback` session dates strictly before `today`, ascending."""
    dates, _ = session_frames(bars)
    prior = [d for d in dates if d < today]
    return prior[-lookback:]
