"""Family E — context / regime features, instrument-independent (contract §5.E).

These prevent the model from treating an expiry-day open like a mid-week post-lunch chop.
All outputs are bounded/percentile/categorical (prefix ``c_``) — no raw points or ranges.
"""

from __future__ import annotations

from datetime import date, datetime, time

import pandas as pd

from nomad_sniper.features.base import Feature, FeatureSnapshot
from nomad_sniper.utils.timeutil import ensure_ist

# NSE weekly expiry day. Phase 0 keeps it simple: weekly = Thursday.
WEEKLY_EXPIRY_DOW = 3  # Thursday

_CONTEXT_FEATURE_NAMES = (
    "c_minutes_into_session",
    "c_time_of_day_bucket",
    "c_day_of_week",
    "c_days_to_weekly_expiry",
    "c_is_expiry_day",
    "c_is_pre_expiry_day",
    "c_atr_percentile",
    "c_range_consumed_pct",
    "c_india_vix_level",
    "c_india_vix_change",
)


def build_context_features(
    decision_time: datetime,
    bars: pd.DataFrame,
    *,
    snapshot: FeatureSnapshot | None = None,
    atr_window_days: int = 14,
    india_vix: pd.Series | None = None,
) -> FeatureSnapshot:
    """Family E context features. `india_vix` (optional) is an IST-indexed close series;
    when absent the two VIX features are null (null-safe, contract §5.E)."""
    decision_time = ensure_ist(decision_time)
    if snapshot is None:
        snapshot = FeatureSnapshot(decision_time=decision_time)

    avail = decision_time  # knowable as of the decision moment itself
    t = decision_time.time()

    # ---- time of day ----
    minutes_into_session = max(0, (t.hour * 60 + t.minute) - (9 * 60 + 15))
    # Scale to [0,1] over the 375-minute session.
    snapshot.add(Feature("c_minutes_into_session", round(minutes_into_session / 375.0, 6), avail, "context"))

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

    # ---- day of week / expiry proximity ----
    snapshot.add(Feature("c_day_of_week", decision_time.weekday(), avail, "context"))
    days_to_expiry = _days_to_next_weekly_expiry(decision_time.date())
    snapshot.add(Feature("c_days_to_weekly_expiry", days_to_expiry, avail, "context"))
    snapshot.add(Feature("c_is_expiry_day", int(days_to_expiry == 0), avail, "context"))
    snapshot.add(Feature("c_is_pre_expiry_day", int(days_to_expiry == 1), avail, "context"))

    # ---- volatility regime: ATR percentile vs trailing year ----
    today = decision_time.date()
    prior_bars = bars[bars.index.date < today]
    atr_percentile: float | None = None
    if not prior_bars.empty:
        daily = (
            prior_bars.groupby(prior_bars.index.date)
            .agg(high=("high", "max"), low=("low", "min"), close=("close", "last"))
        )
        if len(daily) >= 2:
            daily["prev_close"] = daily["close"].shift(1)
            tr = pd.concat([
                daily["high"] - daily["low"],
                (daily["high"] - daily["prev_close"]).abs(),
                (daily["low"] - daily["prev_close"]).abs(),
            ], axis=1).max(axis=1)
            atr_series = tr.rolling(atr_window_days).mean().dropna()
            if len(atr_series) >= 2:
                current_atr = float(atr_series.iloc[-1])
                atr_percentile = float((atr_series < current_atr).mean() * 100.0)
    snapshot.add(Feature("c_atr_percentile", atr_percentile, avail, "context"))

    # ---- today's progress vs typical full-day range (%) ----
    range_consumed: float | None = None
    today_bars = bars[(bars.index.date == today) & (bars.index <= decision_time)]
    if not today_bars.empty and not prior_bars.empty:
        today_range = float(today_bars["high"].max() - today_bars["low"].min())
        daily = (
            prior_bars.groupby(prior_bars.index.date)
            .agg(high=("high", "max"), low=("low", "min"))
        )
        avg_full_range = float((daily["high"] - daily["low"]).tail(20).mean())
        if avg_full_range > 0:
            range_consumed = 100.0 * today_range / avg_full_range
    snapshot.add(Feature("c_range_consumed_pct", range_consumed, avail, "context"))

    # ---- India VIX (optional, null-safe) ----
    vix_level: float | None = None
    vix_change: float | None = None
    if india_vix is not None and not india_vix.empty:
        before = india_vix[india_vix.index <= decision_time]
        if len(before) >= 1:
            vix_level = float(before.iloc[-1])
        if len(before) >= 2:
            vix_change = float(before.iloc[-1] - before.iloc[-2])
    snapshot.add(Feature("c_india_vix_level", vix_level, avail, "context"))
    snapshot.add(Feature("c_india_vix_change", vix_change, avail, "context"))

    return snapshot


def _days_to_next_weekly_expiry(d: date) -> int:
    """Calendar days until the next Thursday (0 if today is Thursday)."""
    return (WEEKLY_EXPIRY_DOW - d.weekday()) % 7
