"""Market Profile feature builder — normalized auction geometry + shape + state (spec §9, §13).

Every feature is instrument-independent: distances are in ATR or profile-width units, flags are
binary, scores are bounded. Raw price levels are computed internally (via `profiles/`) but never
emitted. Each feature carries a leak-free `data_available_at`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

import pandas as pd

from nomad_sniper.features.base import Feature, FeatureSnapshot
from nomad_sniper.profiles.day_type import day_type_scores
from nomad_sniper.profiles.open_type import classify_open_type
from nomad_sniper.profiles.profile import build_profile
from nomad_sniper.utils.normalize import atr_reference, in_atr, in_profile_width
from nomad_sniper.utils.timeutil import ensure_ist, session_end

_MP_NAMES = [
    # geometry vs previous session
    "u_dist_prev_poc_atr", "u_dist_prev_vah_atr", "u_dist_prev_val_atr",
    "u_dist_prev_poc_pw", "u_prev_value_width_atr", "u_prev_range_atr",
    "u_location_vs_prev_value", "u_open_location", "u_gap_atr",
    # developing-session geometry
    "u_dist_dev_poc_atr", "u_dist_dev_vah_atr", "u_dist_dev_val_atr",
    "u_dist_dev_poc_pw", "u_value_migration_atr",
    # nodes / magnets
    "u_dist_nearest_hvn_above_atr", "u_dist_nearest_hvn_below_atr",
    "u_dist_nearest_lvn_above_atr", "u_dist_nearest_lvn_below_atr",
    "u_upside_room_to_magnet_atr", "u_downside_room_to_magnet_atr",
    # shape / quality
    "u_dev_volume_skew", "u_dev_volume_kurtosis", "u_single_print_count",
    "u_prev_poor_high", "u_prev_poor_low", "u_prev_excess_high", "u_prev_excess_low",
    # initial balance / extension
    "u_dist_ib_high_atr", "u_dist_ib_low_atr", "u_price_above_ib", "u_price_below_ib",
    "u_range_ext_up_atr", "u_range_ext_down_atr",
    # auction state
    "u_open_drive", "u_open_test_drive", "u_open_rejection_reverse", "u_open_auction",
    "u_open_type_confidence", "u_trend_day_score", "u_balanced_day_score", "u_neutral_day_score",
]


def build_mp_features(
    decision_time: datetime,
    bars: pd.DataFrame,
    *,
    atr_ref: float | None = None,
    snapshot: FeatureSnapshot | None = None,
) -> FeatureSnapshot:
    """Compute normalized Market Profile features for one decision time."""
    decision_time = ensure_ist(decision_time)
    if snapshot is None:
        snapshot = FeatureSnapshot(decision_time=decision_time)

    today = decision_time.date()
    if atr_ref is None:
        atr_ref = atr_reference(bars, today)

    yest = _previous_session_date(bars, today)
    if yest is None or atr_ref is None:
        _emit_nulls(snapshot, decision_time)
        return snapshot

    # ---- previous session profile (known at yesterday's close) ----
    prev_bars = bars[bars.index.date == yest]
    if prev_bars.empty:
        _emit_nulls(snapshot, decision_time)
        return snapshot
    prev = build_profile(prev_bars)
    prev_avail = session_end(yest)

    # ---- developing session up to decision_time ----
    cur_bars = bars[(bars.index.date == today) & (bars.index <= decision_time)]
    if cur_bars.empty:
        _emit_nulls(snapshot, decision_time)
        return snapshot
    last_close = ensure_ist(cur_bars.index[-1].to_pydatetime())
    price = float(cur_bars.iloc[-1]["close"])
    dev = build_profile(cur_bars, session_date=today)

    def add(name, points_or_value, avail, *, raw=False):
        if raw:
            snapshot.add(Feature(name, points_or_value, avail, "mp"))
        else:
            snapshot.add(Feature(name, in_atr(points_or_value, atr_ref), avail, "mp"))

    # geometry vs previous value
    add("u_dist_prev_poc_atr", price - prev.poc, last_close)
    add("u_dist_prev_vah_atr", price - prev.vah, last_close)
    add("u_dist_prev_val_atr", price - prev.val, last_close)
    snapshot.add(Feature("u_dist_prev_poc_pw",
                         in_profile_width(price - prev.poc, prev.profile_width), last_close, "mp"))
    add("u_prev_value_width_atr", prev.profile_width, prev_avail)
    add("u_prev_range_atr", prev.high - prev.low, prev_avail)

    loc = "above" if price > prev.vah else ("below" if price < prev.val else "inside")
    snapshot.add(Feature("u_location_vs_prev_value", loc, last_close, "mp"))

    open_px = float(cur_bars.iloc[0]["open"])
    open_time = ensure_ist(cur_bars.index[0].to_pydatetime())
    open_loc = "above_value" if open_px > prev.vah else ("below_value" if open_px < prev.val else "in_value")
    snapshot.add(Feature("u_open_location", open_loc, open_time, "mp"))
    add("u_gap_atr", open_px - prev.poc, open_time)

    # developing geometry
    add("u_dist_dev_poc_atr", price - dev.poc, last_close)
    add("u_dist_dev_vah_atr", price - dev.vah, last_close)
    add("u_dist_dev_val_atr", price - dev.val, last_close)
    snapshot.add(Feature("u_dist_dev_poc_pw",
                         in_profile_width(price - dev.poc, dev.profile_width), last_close, "mp"))
    add("u_value_migration_atr", dev.poc - prev.poc, last_close)

    # nodes / magnets
    hvn_a, hvn_b = dev.nearest_hvn_above(price), dev.nearest_hvn_below(price)
    lvn_a, lvn_b = dev.nearest_lvn_above(price), dev.nearest_lvn_below(price)
    add("u_dist_nearest_hvn_above_atr", (hvn_a - price) if hvn_a else None, last_close)
    add("u_dist_nearest_hvn_below_atr", (price - hvn_b) if hvn_b else None, last_close)
    add("u_dist_nearest_lvn_above_atr", (lvn_a - price) if lvn_a else None, last_close)
    add("u_dist_nearest_lvn_below_atr", (price - lvn_b) if lvn_b else None, last_close)
    # room to next magnet upside = nearest of prev VAH / hvn above
    up_magnets = [m for m in (prev.vah, hvn_a, prev.high) if m and m > price]
    dn_magnets = [m for m in (prev.val, hvn_b, prev.low) if m and m < price]
    add("u_upside_room_to_magnet_atr", (min(up_magnets) - price) if up_magnets else None, last_close)
    add("u_downside_room_to_magnet_atr", (price - max(dn_magnets)) if dn_magnets else None, last_close)

    # shape / quality (bounded values emitted raw)
    snapshot.add(Feature("u_dev_volume_skew", dev.volume_skew, last_close, "mp"))
    snapshot.add(Feature("u_dev_volume_kurtosis", dev.volume_kurtosis, last_close, "mp"))
    snapshot.add(Feature("u_single_print_count", dev.single_print_count, last_close, "mp"))
    snapshot.add(Feature("u_prev_poor_high", int(prev.poor_high), prev_avail, "mp"))
    snapshot.add(Feature("u_prev_poor_low", int(prev.poor_low), prev_avail, "mp"))
    snapshot.add(Feature("u_prev_excess_high", prev.excess_high_score, prev_avail, "mp"))
    snapshot.add(Feature("u_prev_excess_low", prev.excess_low_score, prev_avail, "mp"))

    # initial balance / extension — only after the IB window has completed
    from nomad_sniper.utils.timeutil import session_start
    from datetime import timedelta as _td
    ib_complete = decision_time >= session_start(today) + _td(minutes=60)
    if ib_complete and dev.ib_high is not None:
        add("u_dist_ib_high_atr", price - dev.ib_high, last_close)
        add("u_dist_ib_low_atr", price - dev.ib_low, last_close)
        snapshot.add(Feature("u_price_above_ib", int(price > dev.ib_high), last_close, "mp"))
        snapshot.add(Feature("u_price_below_ib", int(price < dev.ib_low), last_close, "mp"))
        add("u_range_ext_up_atr", dev.range_extension_up, last_close)
        add("u_range_ext_down_atr", dev.range_extension_down, last_close)
    else:
        for n in ("u_dist_ib_high_atr", "u_dist_ib_low_atr", "u_price_above_ib",
                  "u_price_below_ib", "u_range_ext_up_atr", "u_range_ext_down_atr"):
            snapshot.add(Feature(n, None, last_close, "mp"))

    # auction state
    ot = classify_open_type(cur_bars)
    ot_avail = ot["available_at"] or last_close
    for k in ("open_drive", "open_test_drive", "open_rejection_reverse", "open_auction"):
        snapshot.add(Feature(f"u_{k}", ot[k], ot_avail, "mp"))
    snapshot.add(Feature("u_open_type_confidence", ot["open_type_confidence"], ot_avail, "mp"))
    dt = day_type_scores(dev)
    for k, v in dt.items():
        snapshot.add(Feature(f"u_{k}", v, last_close, "mp"))

    return snapshot


def _previous_session_date(bars: pd.DataFrame, today):
    prior = sorted({d for d in bars.index.date if d < today})
    return prior[-1] if prior else None


def _emit_nulls(snapshot: FeatureSnapshot, decision_time: datetime) -> None:
    avail = ensure_ist(decision_time)
    for name in _MP_NAMES:
        snapshot.add(Feature(name, None, avail, "mp"))
