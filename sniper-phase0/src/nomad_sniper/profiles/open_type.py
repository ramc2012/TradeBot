"""Opening-type classification (spec §9.2; Dalton, *Mind Over Markets*).

Four canonical opens, in descending conviction:
  - open_drive:           opens and goes one way; price never trades back through the open.
  - open_test_drive:      tests a reference, then reverses and drives back through the open.
  - open_rejection_reverse: pushes one way, fails, reverses back through the opening range.
  - open_auction:         two-way rotation, no conviction (often inside value).

We classify from the first `window_minutes` of bars relative to the open price and prior value.
Returns a dict of one-hot scores plus a confidence in [0,1]. Computed only from bars at/after
the open, so it is leak-free for any decision time at or after the classification window end.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from nomad_sniper.utils.timeutil import ensure_ist


def classify_open_type(
    session_bars: pd.DataFrame,
    *,
    window_minutes: int = 30,
    drive_threshold: float = 0.85,
) -> dict:
    """Classify the open from the first `window_minutes` of the session.

    `session_bars` must be the current session's bars (IST-indexed), at least covering the
    opening window. Returns scores keyed by open type + `open_type_confidence` and the time
    the classification became available (`available_at`).
    """
    out = {
        "open_drive": 0,
        "open_test_drive": 0,
        "open_rejection_reverse": 0,
        "open_auction": 0,
        "open_type_confidence": 0.0,
        "available_at": None,
    }
    if session_bars.empty:
        return out

    start = ensure_ist(session_bars.index[0].to_pydatetime())
    window_end = start + timedelta(minutes=window_minutes)
    w = session_bars[session_bars.index <= window_end]
    if len(w) < 3:
        return out

    open_px = float(w.iloc[0]["open"])
    hi = float(w["high"].max())
    lo = float(w["low"].min())
    close_px = float(w.iloc[-1]["close"])
    rng = hi - lo
    out["available_at"] = ensure_ist(w.index[-1].to_pydatetime())
    if rng <= 0:
        out["open_auction"] = 1
        out["open_type_confidence"] = 0.2
        return out

    up_extent = hi - open_px
    down_extent = open_px - lo
    # How far price travelled net vs how far it ranged
    directional = (close_px - open_px) / rng
    one_sided = max(up_extent, down_extent) / rng  # ~1 if open is at one extreme

    # Did price ever trade back through the open after the initial push?
    crossed_back = (lo < open_px < hi)

    if one_sided >= drive_threshold and not crossed_back:
        # open is essentially the extreme; price only went one way -> drive
        out["open_drive"] = 1
        out["open_type_confidence"] = float(min(1.0, one_sided))
    elif abs(directional) >= 0.5 and crossed_back and one_sided >= 0.6:
        # tested one side then drove back through the open -> test-drive
        out["open_test_drive"] = 1
        out["open_type_confidence"] = float(min(1.0, abs(directional)))
    elif crossed_back and abs(directional) >= 0.3:
        # pushed, failed, reversed through opening range -> rejection-reverse
        out["open_rejection_reverse"] = 1
        out["open_type_confidence"] = float(min(1.0, abs(directional)))
    else:
        out["open_auction"] = 1
        out["open_type_confidence"] = float(1.0 - abs(directional))
    return out
