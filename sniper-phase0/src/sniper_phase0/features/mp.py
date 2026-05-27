"""Market Profile features.

All features are computed from MP state available STRICTLY BEFORE decision_ts.
The MPState object's `data_available_at` is the decision_ts itself (we did not
peek), so we tag each feature with that timestamp.
"""
from __future__ import annotations

import math

from sniper_phase0.data.mp_state import MPState
from sniper_phase0.features.base import FeatureSnapshot


def add_mp_features(
    snap: FeatureSnapshot, mp: MPState | None, spot_price: float
) -> None:
    if mp is None:
        for name in [
            "mp_dist_poc_pct",
            "mp_dist_vah_pct",
            "mp_dist_val_pct",
            "mp_in_value_area",
            "mp_above_vah",
            "mp_below_val",
            "mp_ib_position",
            "mp_va_width_pct",
            "mp_tpo_count",
        ]:
            snap.add(name, float("nan"), snap.decision_ts)
        return

    avail = mp.data_available_at

    def pct(a: float, b: float) -> float:
        return (a - b) / b * 100.0 if b else float("nan")

    snap.add("mp_dist_poc_pct", pct(spot_price, mp.poc), avail)
    snap.add("mp_dist_vah_pct", pct(spot_price, mp.vah), avail)
    snap.add("mp_dist_val_pct", pct(spot_price, mp.val), avail)
    snap.add("mp_in_value_area", float(mp.val <= spot_price <= mp.vah), avail)
    snap.add("mp_above_vah", float(spot_price > mp.vah), avail)
    snap.add("mp_below_val", float(spot_price < mp.val), avail)

    if math.isfinite(mp.ib_high) and math.isfinite(mp.ib_low) and mp.ib_high > mp.ib_low:
        ib_pos = (spot_price - mp.ib_low) / (mp.ib_high - mp.ib_low)
    else:
        ib_pos = float("nan")
    snap.add("mp_ib_position", ib_pos, avail)

    snap.add("mp_va_width_pct", pct(mp.vah, mp.val), avail)
    snap.add("mp_tpo_count", float(mp.tpo_count), avail)


def add_prior_session_mp_features(
    snap: FeatureSnapshot, prev_mp: MPState | None, spot_price: float
) -> None:
    """Features comparing current spot to YESTERDAY's completed session MP.

    Availability for these features = today's session open. The prev_mp object
    carries data_available_at = yesterday's session close, which is strictly
    before today's open — so we tag with that timestamp for the leakage audit.
    """
    if prev_mp is None:
        for name in [
            "mp_prev_dist_poc_pct",
            "mp_prev_dist_vah_pct",
            "mp_prev_dist_val_pct",
            "mp_prev_in_value_area",
            "mp_prev_above_vah",
            "mp_prev_below_val",
            "mp_value_migration_pct",
            "mp_single_prints_prev",
            "mp_poor_high_prev",
            "mp_poor_low_prev",
            "mp_nearest_hvn_dist_pct",
            "mp_nearest_lvn_dist_pct",
        ]:
            snap.add(name, float("nan"), snap.decision_ts)
        return

    avail = prev_mp.data_available_at

    def pct(a: float, b: float) -> float:
        return (a - b) / b * 100.0 if b else float("nan")

    snap.add("mp_prev_dist_poc_pct", pct(spot_price, prev_mp.poc), avail)
    snap.add("mp_prev_dist_vah_pct", pct(spot_price, prev_mp.vah), avail)
    snap.add("mp_prev_dist_val_pct", pct(spot_price, prev_mp.val), avail)
    snap.add("mp_prev_in_value_area", float(prev_mp.val <= spot_price <= prev_mp.vah), avail)
    snap.add("mp_prev_above_vah", float(spot_price > prev_mp.vah), avail)
    snap.add("mp_prev_below_val", float(spot_price < prev_mp.val), avail)

    snap.add("mp_single_prints_prev", float(prev_mp.single_prints), avail)
    snap.add("mp_poor_high_prev", float(prev_mp.poor_high), avail)
    snap.add("mp_poor_low_prev", float(prev_mp.poor_low), avail)

    if prev_mp.hvn_prices:
        nearest_hvn = min(prev_mp.hvn_prices, key=lambda p: abs(p - spot_price))
        snap.add("mp_nearest_hvn_dist_pct", pct(spot_price, nearest_hvn), avail)
    else:
        snap.add("mp_nearest_hvn_dist_pct", float("nan"), avail)

    if prev_mp.lvn_prices:
        nearest_lvn = min(prev_mp.lvn_prices, key=lambda p: abs(p - spot_price))
        snap.add("mp_nearest_lvn_dist_pct", pct(spot_price, nearest_lvn), avail)
    else:
        snap.add("mp_nearest_lvn_dist_pct", float("nan"), avail)

    # Value migration: signed pct change in POC from session-before-prev to prev.
    # We don't carry that history into prev_mp; this feature is left for a v0.1
    # add-on with a two-day MP cache. For now, emit NaN with availability
    # tagged to yesterday's close.
    snap.add("mp_value_migration_pct", float("nan"), avail)
