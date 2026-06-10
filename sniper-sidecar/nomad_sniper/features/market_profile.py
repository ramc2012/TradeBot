"""Market Profile features for a single decision moment.

For each entry timestamp `t`, we compute features describing where price sits in the
*previously completed* daily and hourly auctions. The key invariant: every feature's
`data_available_at` is the close of the period it describes, never later.

Phase 0 implementation:
- Previous day's POC, VAH, VAL, range, value-area width
- Current day's developing POC at the time of `t` (using only bars strictly before `t`)
- Distance metrics: where is current price relative to those levels
- Opening location vs prior value (above / within / below)
- Initial Balance (first 60 minutes) and IB range — only if `t > 10:15`

We keep this deterministic and simple. The fancier composite/hourly profile logic is a
separate module to add once Phase 0 baseline works.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from nomad_sniper.features.base import Feature, FeatureSnapshot
from nomad_sniper.utils.timeutil import ensure_ist, session_end, session_start


@dataclass
class Profile:
    """Result of building a TPO/volume profile for one session/window."""

    poc: float
    vah: float
    val: float
    high: float
    low: float
    total_volume: float
    last_bar_close_time: datetime  # data_available_at for any feature derived from this


def build_volume_profile(bars: pd.DataFrame, *, tick_size: float = 0.05) -> Profile:
    """Build a price-binned volume profile from minute bars.

    POC = price bin with the most volume.
    Value area = contiguous bins around POC covering ~70% of volume.
    """
    if bars.empty:
        raise ValueError("Cannot build profile from empty bars.")

    # Use typical price per bar, weight by volume
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3.0
    vols = bars["volume"].astype(float)

    lo = float(bars["low"].min())
    hi = float(bars["high"].max())
    if hi <= lo:
        # Degenerate session
        return Profile(
            poc=lo, vah=lo, val=lo, high=lo, low=lo,
            total_volume=float(vols.sum()),
            last_bar_close_time=ensure_ist(bars.index[-1].to_pydatetime()),
        )

    n_bins = max(2, int(np.ceil((hi - lo) / tick_size)))
    edges = np.linspace(lo, hi, n_bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2.0
    binned, _ = np.histogram(typical, bins=edges, weights=vols)

    if binned.sum() == 0:
        return Profile(
            poc=float(typical.mean()), vah=hi, val=lo, high=hi, low=lo,
            total_volume=float(vols.sum()),
            last_bar_close_time=ensure_ist(bars.index[-1].to_pydatetime()),
        )

    poc_idx = int(np.argmax(binned))
    poc = float(centers[poc_idx])

    # Value area: expand outward from POC until 70% volume captured
    target = 0.7 * binned.sum()
    captured = binned[poc_idx]
    lo_idx, hi_idx = poc_idx, poc_idx
    while captured < target and (lo_idx > 0 or hi_idx < len(binned) - 1):
        next_lo = binned[lo_idx - 1] if lo_idx > 0 else -np.inf
        next_hi = binned[hi_idx + 1] if hi_idx < len(binned) - 1 else -np.inf
        if next_hi >= next_lo:
            hi_idx += 1
            captured += binned[hi_idx]
        else:
            lo_idx -= 1
            captured += binned[lo_idx]

    val = float(centers[lo_idx])
    vah = float(centers[hi_idx])
    return Profile(
        poc=poc, vah=vah, val=val,
        high=float(bars["high"].max()),
        low=float(bars["low"].min()),
        total_volume=float(vols.sum()),
        last_bar_close_time=ensure_ist(bars.index[-1].to_pydatetime()),
    )


def build_mp_features(
    decision_time: datetime,
    bars: pd.DataFrame,
    atr_ref: float | None,
    *,
    snapshot: FeatureSnapshot | None = None,
) -> FeatureSnapshot:
    """Family A — underlying Market Profile, ATR-normalized (contract §5.A).

    Emits ONLY instrument-independent features (prefix ``u_``): ATR-normalized distances,
    categoricals, and breakout binaries. Raw levels (POC/VAH/VAL/price/IB) are computed
    internally but never emitted as features (contract §2).

    Args:
        decision_time: IST-aware decision moment.
        bars:          IST-indexed minute bars covering prior + current session through t.
        atr_ref:       Prior-close 14-session ATR in points (from utils.normalize.atr_reference).
                       If None, all ATR-normalized features are null (schema stays stable).
    """
    decision_time = ensure_ist(decision_time)
    if snapshot is None:
        snapshot = FeatureSnapshot(decision_time=decision_time)

    from nomad_sniper.utils.barindex import prior_session_dates, session_frames

    today = decision_time.date()
    _, day_frames = session_frames(bars)
    prior = prior_session_dates(bars, today, 1)
    yesterday = prior[-1] if prior else None
    if yesterday is None:
        _emit_nulls(snapshot, decision_time)
        return snapshot

    prev_bars = day_frames.get(yesterday)
    if prev_bars is None or prev_bars.empty:
        _emit_nulls(snapshot, decision_time)
        return snapshot
    prev = build_volume_profile(prev_bars)
    prev_avail = session_end(yesterday)  # known once yesterday closed

    def _an(points: float) -> float | None:
        """ATR-normalize a points distance; None if atr_ref missing."""
        if atr_ref is None or atr_ref <= 0:
            return None
        return points / atr_ref

    # Prior value structure (width / range), in ATR units.
    snapshot.add(Feature("u_prev_value_width_atr", _an(prev.vah - prev.val), prev_avail, "mp"))
    snapshot.add(Feature("u_prev_range_atr", _an(prev.high - prev.low), prev_avail, "mp"))

    # Current developing session up to t.
    today_frame = day_frames.get(today)
    current_bars = today_frame[today_frame.index <= decision_time] if today_frame is not None else bars.iloc[:0]
    if current_bars.empty:
        # Pre-open or no bars yet: emit prior-structure nulls for the price-relative family.
        for name in (
            "u_dist_prev_poc_atr", "u_dist_prev_vah_atr", "u_dist_prev_val_atr",
            "u_location_vs_prev_value", "u_open_location", "u_gap_atr",
            "u_dist_dev_poc_atr", "u_value_migration_atr",
            "u_dist_ib_high_atr", "u_dist_ib_low_atr", "u_price_above_ib", "u_price_below_ib",
        ):
            snapshot.add(Feature(name, None, decision_time, "mp"))
        return snapshot

    last_bar_close = ensure_ist(current_bars.index[-1].to_pydatetime())
    current_price = float(current_bars.iloc[-1]["close"])

    # Distances to prior value, ATR-normalized.
    snapshot.add(Feature("u_dist_prev_poc_atr", _an(current_price - prev.poc), last_bar_close, "mp"))
    snapshot.add(Feature("u_dist_prev_vah_atr", _an(current_price - prev.vah), last_bar_close, "mp"))
    snapshot.add(Feature("u_dist_prev_val_atr", _an(current_price - prev.val), last_bar_close, "mp"))

    loc = "above" if current_price > prev.vah else ("below" if current_price < prev.val else "inside")
    snapshot.add(Feature("u_location_vs_prev_value", loc, last_bar_close, "mp"))

    # Opening location + gap (knowable at the open bar).
    open_bar = current_bars.iloc[0]
    open_price = float(open_bar["open"])
    open_time = ensure_ist(current_bars.index[0].to_pydatetime())
    open_loc = "above" if open_price > prev.vah else ("below" if open_price < prev.val else "inside")
    snapshot.add(Feature("u_open_location", open_loc, open_time, "mp"))
    snapshot.add(Feature("u_gap_atr", _an(open_price - prev.poc), open_time, "mp"))

    # Developing-session profile and value migration.
    dev = build_volume_profile(current_bars)
    snapshot.add(Feature("u_dist_dev_poc_atr", _an(current_price - dev.poc), last_bar_close, "mp"))
    snapshot.add(Feature("u_value_migration_atr", _an(dev.poc - prev.poc), last_bar_close, "mp"))

    # Initial Balance (first 60 min); real values only after IB completes (≥ 10:15 IST).
    # Before that, emit explicit NULLS instead of omitting the columns: the estimator
    # selects X[feature_names] and a missing column raises KeyError — which failed the
    # 04:00/04:30 UTC timer fires (ok=0/3) every trading day. None → NaN is safe and
    # in-distribution: ExcursionEstimator.predict coerces object columns to numeric
    # (LightGBM treats NaN as missing), and _emit_nulls already uses the same pattern
    # for the no-prior-session case.
    ib_end = session_start(today) + timedelta(minutes=60)
    ib_bars = (
        current_bars[current_bars.index <= ib_end]
        if decision_time >= ib_end
        else current_bars.iloc[0:0]
    )
    if not ib_bars.empty:
        ib_high = float(ib_bars["high"].max())
        ib_low = float(ib_bars["low"].min())
        snapshot.add(Feature("u_dist_ib_high_atr", _an(current_price - ib_high), last_bar_close, "mp"))
        snapshot.add(Feature("u_dist_ib_low_atr", _an(current_price - ib_low), last_bar_close, "mp"))
        snapshot.add(Feature("u_price_above_ib", int(current_price > ib_high), last_bar_close, "mp"))
        snapshot.add(Feature("u_price_below_ib", int(current_price < ib_low), last_bar_close, "mp"))
    else:
        for _ib_name in ("u_dist_ib_high_atr", "u_dist_ib_low_atr",
                         "u_price_above_ib", "u_price_below_ib"):
            snapshot.add(Feature(_ib_name, None, last_bar_close, "mp"))

    return snapshot


def _previous_session_date(bars: pd.DataFrame, today: date) -> date | None:
    """Find the most recent date in `bars` strictly before `today`."""
    dates_before = sorted({d for d in bars.index.date if d < today})
    return dates_before[-1] if dates_before else None


# Family-A feature names emitted when prior session is unavailable (schema stability).
_MP_FEATURE_NAMES = (
    "u_prev_value_width_atr", "u_prev_range_atr",
    "u_dist_prev_poc_atr", "u_dist_prev_vah_atr", "u_dist_prev_val_atr",
    "u_location_vs_prev_value", "u_open_location", "u_gap_atr",
    "u_dist_dev_poc_atr", "u_value_migration_atr",
    "u_dist_ib_high_atr", "u_dist_ib_low_atr", "u_price_above_ib", "u_price_below_ib",
)


def _emit_nulls(snapshot: FeatureSnapshot, decision_time: datetime) -> None:
    """When prior session is unavailable, emit explicit nulls so the schema is stable."""
    avail = ensure_ist(decision_time)
    for name in _MP_FEATURE_NAMES:
        snapshot.add(Feature(name, None, avail, "mp"))
