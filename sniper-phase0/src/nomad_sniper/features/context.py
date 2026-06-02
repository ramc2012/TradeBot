"""Context / regime features (spec section 15, 24, 25) — normalized, instrument-independent."""

from __future__ import annotations

from datetime import date, datetime, time

import numpy as np
import pandas as pd

from nomad_sniper.features.base import Feature, FeatureSnapshot
from nomad_sniper.utils.normalize import atr_reference
from nomad_sniper.utils.timeutil import ensure_ist

WEEKLY_EXPIRY_DOW = 3  # Thursday

_CTX_NAMES = [
    "c_minutes_into_session", "c_time_of_day_bucket", "c_day_of_week",
    "c_days_to_weekly_expiry", "c_is_expiry_day", "c_is_pre_expiry_day",
    "c_atr_percentile", "c_range_consumed_pct",
    "c_india_vix_level", "c_india_vix_change",
]


def build_context_features(
    decision_time: datetime,
    bars: pd.DataFrame,
    *,
    snapshot: FeatureSnapshot | None = None,
    atr_window_days: int = 14,
) -> FeatureSnapshot:
    decision_time = ensure_ist(decision_time)
    if snapshot is None:
        snapshot = FeatureSnapshot(decision_time=decision_time)
    avail = decision_time
    t = decision_time.time()

    mins = max(0, (t.hour * 60 + t.minute) - (9 * 60 + 15))
    snapshot.add(Feature("c_minutes_into_session", mins, avail, "context"))

    if t < time(9, 45):
        tod = "open"
    elif t < time(12, 0):
        tod = "mid_morning"
    elif t < time(13, 30):
        tod = "lunch"
    elif t < time(15, 0):
        tod = "afternoon"
    else:
        tod = "close"
    snapshot.add(Feature("c_time_of_day_bucket", tod, avail, "context"))
    snapshot.add(Feature("c_day_of_week", decision_time.weekday(), avail, "context"))

    dte = (WEEKLY_EXPIRY_DOW - decision_time.weekday()) % 7
    snapshot.add(Feature("c_days_to_weekly_expiry", dte, avail, "context"))
    snapshot.add(Feature("c_is_expiry_day", int(dte == 0), avail, "context"))
    snapshot.add(Feature("c_is_pre_expiry_day", int(dte == 1), avail, "context"))

    today = decision_time.date()
    prior = bars[bars.index.date < today]
    atr_ref = atr_reference(bars, today, window=atr_window_days)
    if not prior.empty:
        daily = (prior.groupby(prior.index.date)
                 .agg(high=("high", "max"), low=("low", "min"), close=("close", "last")))
        if len(daily) >= 2:
            daily["pc"] = daily["close"].shift(1)
            tr = pd.concat([daily["high"] - daily["low"],
                            (daily["high"] - daily["pc"]).abs(),
                            (daily["low"] - daily["pc"]).abs()], axis=1).max(axis=1)
            atr_series = tr.rolling(atr_window_days).mean().dropna()
            if atr_ref is not None and len(atr_series) >= atr_window_days:
                pct = float((atr_series < atr_ref).mean() * 100)
                snapshot.add(Feature("c_atr_percentile", pct, avail, "context"))
            else:
                snapshot.add(Feature("c_atr_percentile", None, avail, "context"))
        else:
            snapshot.add(Feature("c_atr_percentile", None, avail, "context"))
    else:
        snapshot.add(Feature("c_atr_percentile", None, avail, "context"))

    cur = bars[(bars.index.date == today) & (bars.index <= decision_time)]
    if not cur.empty and not prior.empty:
        today_range = float(cur["high"].max() - cur["low"].min())
        daily = (prior.groupby(prior.index.date)
                 .agg(high=("high", "max"), low=("low", "min")))
        avg_range = float((daily["high"] - daily["low"]).tail(20).mean())
        snapshot.add(Feature("c_range_consumed_pct",
                             float(100 * today_range / avg_range) if avg_range > 0 else None,
                             avail, "context"))
    else:
        snapshot.add(Feature("c_range_consumed_pct", None, avail, "context"))

    # India VIX context — null-safe (wire from an external column/series when available)
    vix = None
    if "india_vix" in bars.columns:
        cur_vix = bars[bars.index <= decision_time]["india_vix"].dropna()
        if not cur_vix.empty:
            vix = float(cur_vix.iloc[-1])
    snapshot.add(Feature("c_india_vix_level", vix, avail, "context"))
    snapshot.add(Feature("c_india_vix_change", None, avail, "context"))
    return snapshot
