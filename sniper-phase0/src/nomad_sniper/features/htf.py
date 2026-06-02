"""Higher-timeframe profile features (spec §11) — regime awareness via the profile stack.

Answers "where is today's auction inside the larger auction?" Builds previous + developing
weekly and monthly profiles and a rolling 20-day composite, then emits normalized location,
state, alignment, and compression features. All distances in ATR units; all leak-free
(previous-period profiles are fully known; developing-period profiles use only bars up to the
decision time).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from nomad_sniper.features.base import Feature, FeatureSnapshot
from nomad_sniper.profiles.profile import build_profile
from nomad_sniper.utils.normalize import atr_reference, in_atr
from nomad_sniper.utils.timeutil import ensure_ist

_HTF_NAMES = [
    "h_dist_prev_week_poc_atr", "h_dist_prev_week_vah_atr", "h_dist_prev_week_val_atr",
    "h_dist_prev_month_poc_atr", "h_dist_composite20_poc_atr",
    "h_week_location", "h_month_location",
    "h_daily_weekly_alignment", "h_weekly_monthly_alignment", "h_timeframe_conflict",
    "h_week_value_width_atr", "h_week_compression_score",
    "h_week_poc_shift_atr", "h_bullish_confluence", "h_bearish_confluence",
]


def build_htf_features(
    decision_time: datetime,
    bars: pd.DataFrame,
    *,
    atr_ref: float | None = None,
    snapshot: FeatureSnapshot | None = None,
) -> FeatureSnapshot:
    decision_time = ensure_ist(decision_time)
    if snapshot is None:
        snapshot = FeatureSnapshot(decision_time=decision_time)
    today = decision_time.date()
    if atr_ref is None:
        atr_ref = atr_reference(bars, today)
    if atr_ref is None:
        _emit_nulls(snapshot, decision_time)
        return snapshot

    cur = bars[(bars.index.date == today) & (bars.index <= decision_time)]
    if cur.empty:
        _emit_nulls(snapshot, decision_time)
        return snapshot
    price = float(cur.iloc[-1]["close"])
    avail = ensure_ist(cur.index[-1].to_pydatetime())

    # ISO week / month boundaries
    iso_year, iso_week, _ = today.isocalendar()
    prev_week_bars = _prev_iso_week_bars(bars, iso_year, iso_week, today)
    prev_month_bars = _prev_month_bars(bars, today)
    composite20 = _rolling_composite(bars, today, sessions=20)

    def add(name, points):
        snapshot.add(Feature(name, in_atr(points, atr_ref), avail, "htf"))

    # previous week
    if prev_week_bars is not None and not prev_week_bars.empty:
        pw = build_profile(prev_week_bars)
        add("h_dist_prev_week_poc_atr", price - pw.poc)
        add("h_dist_prev_week_vah_atr", price - pw.vah)
        add("h_dist_prev_week_val_atr", price - pw.val)
        add("h_week_value_width_atr", pw.profile_width)
        wk_loc = "above" if price > pw.vah else ("below" if price < pw.val else "inside")
        snapshot.add(Feature("h_week_location", wk_loc, avail, "htf"))
        # compression: narrow weekly value vs ATR
        comp = max(0.0, 1.0 - (pw.profile_width / (atr_ref * 5))) if atr_ref else 0.0
        snapshot.add(Feature("h_week_compression_score", float(min(1.0, comp)), avail, "htf"))
        # developing-week POC shift vs prev week POC
        dev_week = _dev_iso_week_bars(bars, iso_year, iso_week, decision_time)
        if dev_week is not None and not dev_week.empty:
            dw = build_profile(dev_week)
            add("h_week_poc_shift_atr", dw.poc - pw.poc)
        else:
            snapshot.add(Feature("h_week_poc_shift_atr", None, avail, "htf"))
    else:
        for n in ("h_dist_prev_week_poc_atr", "h_dist_prev_week_vah_atr",
                  "h_dist_prev_week_val_atr", "h_week_value_width_atr",
                  "h_week_location", "h_week_compression_score", "h_week_poc_shift_atr"):
            snapshot.add(Feature(n, None, avail, "htf"))
        wk_loc = None

    # previous month
    if prev_month_bars is not None and not prev_month_bars.empty:
        pm = build_profile(prev_month_bars)
        add("h_dist_prev_month_poc_atr", price - pm.poc)
        mo_loc = "above" if price > pm.vah else ("below" if price < pm.val else "inside")
        snapshot.add(Feature("h_month_location", mo_loc, avail, "htf"))
    else:
        snapshot.add(Feature("h_dist_prev_month_poc_atr", None, avail, "htf"))
        snapshot.add(Feature("h_month_location", None, avail, "htf"))
        mo_loc = None

    # rolling 20-day composite
    if composite20 is not None and not composite20.empty:
        cp = build_profile(composite20)
        add("h_dist_composite20_poc_atr", price - cp.poc)
    else:
        snapshot.add(Feature("h_dist_composite20_poc_atr", None, avail, "htf"))

    # alignment: daily location is read from the snapshot's MP family if present
    daily_loc = _lookup(snapshot, "u_location_vs_prev_value")
    da_wk = _agree(daily_loc, wk_loc)
    wk_mo = _agree(wk_loc, mo_loc)
    snapshot.add(Feature("h_daily_weekly_alignment", da_wk, avail, "htf"))
    snapshot.add(Feature("h_weekly_monthly_alignment", wk_mo, avail, "htf"))
    conflict = 0 if (da_wk in (1, None) and wk_mo in (1, None)) else 1
    snapshot.add(Feature("h_timeframe_conflict", conflict, avail, "htf"))

    # simple confluence counts (bounded)
    locs = [x for x in (daily_loc, wk_loc, mo_loc) if x is not None]
    bull = sum(1 for x in locs if x == "above") / 3.0
    bear = sum(1 for x in locs if x == "below") / 3.0
    snapshot.add(Feature("h_bullish_confluence", float(bull), avail, "htf"))
    snapshot.add(Feature("h_bearish_confluence", float(bear), avail, "htf"))
    return snapshot


def _agree(a, b):
    if a is None or b is None:
        return None
    return int(a == b and a in ("above", "below"))


def _lookup(snapshot: FeatureSnapshot, name: str):
    for f in snapshot.features:
        if f.name == name:
            return f.value
    return None


def _prev_iso_week_bars(bars, iso_year, iso_week, today):
    def in_prev_week(ts):
        y, w, _ = ts.date().isocalendar()
        if iso_week > 1:
            return (y, w) == (iso_year, iso_week - 1)
        return ts.date() < today and (y, w) != (iso_year, iso_week)
    mask = [in_prev_week(ts) and ts.date() < today for ts in bars.index]
    sub = bars[mask]
    return sub if not sub.empty else None


def _dev_iso_week_bars(bars, iso_year, iso_week, decision_time):
    def in_cur_week(ts):
        y, w, _ = ts.date().isocalendar()
        return (y, w) == (iso_year, iso_week)
    mask = [in_cur_week(ts) and ts <= decision_time for ts in bars.index]
    sub = bars[mask]
    return sub if not sub.empty else None


def _prev_month_bars(bars, today):
    y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    mask = [(ts.year, ts.month) == (y, m) for ts in bars.index]
    sub = bars[mask]
    return sub if not sub.empty else None


def _rolling_composite(bars, today, sessions=20):
    dates = sorted({d for d in bars.index.date if d < today})[-sessions:]
    if not dates:
        return None
    s = set(dates)
    sub = bars[[ts.date() in s for ts in bars.index]]
    return sub if not sub.empty else None


def _emit_nulls(snapshot: FeatureSnapshot, decision_time: datetime) -> None:
    avail = ensure_ist(decision_time)
    for n in _HTF_NAMES:
        snapshot.add(Feature(n, None, avail, "htf"))
