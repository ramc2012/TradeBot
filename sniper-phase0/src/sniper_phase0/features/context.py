"""Context features: session position, day-of-week, expiry distance, ATR regime, gap."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from sniper_phase0.features.base import FeatureSnapshot
from sniper_phase0.utils.time import minutes_into_session, to_ist


def add_context_features(
    snap: FeatureSnapshot,
    expiry_date: pd.Timestamp | None,
    prior_close: float | None,
    today_open: float | None,
    atr_14d: float | None,
) -> None:
    ts = to_ist(snap.decision_ts)
    avail = ts

    snap.add("ctx_minutes_into_session", float(minutes_into_session(ts)), avail)
    snap.add("ctx_dow", float(ts.weekday()), avail)
    snap.add("ctx_is_monday", float(ts.weekday() == 0), avail)
    snap.add("ctx_is_friday", float(ts.weekday() == 4), avail)

    if expiry_date is not None:
        dte = (pd.Timestamp(expiry_date).date() - ts.date()).days
        snap.add("ctx_dte", float(dte), avail)
        snap.add("ctx_is_expiry_day", float(dte == 0), avail)
        snap.add("ctx_is_expiry_week", float(0 <= dte <= 4), avail)
    else:
        for n in ("ctx_dte", "ctx_is_expiry_day", "ctx_is_expiry_week"):
            snap.add(n, float("nan"), avail)

    if prior_close is not None and today_open is not None and prior_close > 0:
        gap_pct = (today_open - prior_close) / prior_close * 100.0
        snap.add("ctx_overnight_gap_pct", float(gap_pct), avail)
        snap.add("ctx_gap_up", float(gap_pct > 0.3), avail)
        snap.add("ctx_gap_down", float(gap_pct < -0.3), avail)
    else:
        for n in ("ctx_overnight_gap_pct", "ctx_gap_up", "ctx_gap_down"):
            snap.add(n, float("nan"), avail)

    snap.add(
        "ctx_atr_14d",
        float(atr_14d) if atr_14d is not None and math.isfinite(atr_14d) else float("nan"),
        avail,
    )
