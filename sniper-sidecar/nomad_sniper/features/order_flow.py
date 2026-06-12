"""Family B — underlying order flow (inferred from bars), instrument-independent.

**Honesty disclaimer:** Phase 0 has no true tick/MBO data. These are *inferred* from minute
OHLCV (candle direction × volume as a signed-volume proxy). Names keep the ``inferred``
intent but every emitted feature is normalized per contract §2: z-scores vs same-time-of-day
baselines, ratios, %, and bounded fractions. Raw volume / delta / OI never leave this module.

When Phase 1 ships real tick + 5-level book, this module is replaced; the feature names and
normalization contract stay the same so models transfer.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from nomad_sniper.features.base import Feature, FeatureSnapshot
from nomad_sniper.utils.normalize import pct_change, safe_ratio, zscore
from nomad_sniper.utils.timeutil import ensure_ist

_OF_FEATURE_NAMES = (
    "u_inferred_delta_z",
    "u_inferred_delta_slope_z",
    "u_volume_z",
    "u_volume_accel_ratio",
    "u_range_expansion_ratio",
    "u_oi_change_pct",
    "u_up_bar_fraction",
    # §16 disagreement / divergence (inferred from OHLCV; book/sweep/absorption need L2 → omitted)
    "u_delta_price_divergence",
    "u_price_up_delta_down",
    "u_price_down_delta_up",
    "u_new_high_without_delta",
    "u_new_low_without_delta",
    "u_failed_breakout_score",
    "u_failed_breakdown_score",
    "u_trapped_buyers_score",
    "u_trapped_sellers_score",
)


def build_of_features(
    decision_time: datetime,
    bars: pd.DataFrame,
    *,
    snapshot: FeatureSnapshot | None = None,
    lookback_minutes: int = 30,
    baseline_lookback_sessions: int = 20,
) -> FeatureSnapshot:
    """Family B order-flow features over the trailing `lookback_minutes`.

    Normalization bases (all leak-free, computed from data strictly before `decision_time`):
      - inferred-delta and volume → z-score vs same-time-of-day trailing baseline
      - volume acceleration / range expansion → recent-half vs prior-half ratio
      - OI change → percent of prior OI
      - up-bar fraction → bounded [0, 1]
    """
    decision_time = ensure_ist(decision_time)
    if snapshot is None:
        snapshot = FeatureSnapshot(decision_time=decision_time)

    window = bars[bars.index <= decision_time].tail(lookback_minutes)
    if window.empty:
        _emit_nulls(snapshot, decision_time)
        return snapshot

    last_bar_close = ensure_ist(window.index[-1].to_pydatetime())

    # ---- signed-volume proxy (inferred delta) ----
    body = window["close"] - window["open"]
    rng = (window["high"] - window["low"]).replace(0, np.nan)
    direction = (body / rng).fillna(0.0)            # [-1, 1]
    signed_vol = direction * window["volume"]
    inferred_delta = float(signed_vol.sum())
    cum_delta = signed_vol.cumsum()
    delta_slope = (
        float(np.polyfit(np.arange(len(cum_delta)), cum_delta.values, 1)[0])
        if len(cum_delta) >= 3 else 0.0
    )

    # Same-TOD baselines (built from prior sessions only → leak-free).
    delta_mu, delta_sigma = _window_tod_baseline(
        bars, decision_time, lookback_minutes, baseline_lookback_sessions, kind="delta"
    )
    vol_mu, vol_sigma = _window_tod_baseline(
        bars, decision_time, lookback_minutes, baseline_lookback_sessions, kind="volume"
    )
    window_volume = float(window["volume"].sum())

    snapshot.add(Feature("u_inferred_delta_z", zscore(inferred_delta, delta_mu, delta_sigma), last_bar_close, "of"))
    slope_sigma = (delta_sigma / lookback_minutes) if delta_sigma else None
    snapshot.add(Feature("u_inferred_delta_slope_z", zscore(delta_slope, 0.0, slope_sigma), last_bar_close, "of"))
    snapshot.add(Feature("u_volume_z", zscore(window_volume, vol_mu, vol_sigma), last_bar_close, "of"))

    # ---- volume acceleration: recent-half / prior-half ----
    half = len(window) // 2
    if half >= 2:
        recent = float(window["volume"].iloc[half:].sum())
        prior = float(window["volume"].iloc[:half].sum())
        snapshot.add(Feature("u_volume_accel_ratio", safe_ratio(recent, prior), last_bar_close, "of"))
    else:
        snapshot.add(Feature("u_volume_accel_ratio", None, last_bar_close, "of"))

    # ---- range expansion: recent 5-bar range / prior range ----
    bar_ranges = (window["high"] - window["low"]).astype(float)
    if len(bar_ranges) >= 5:
        recent_range = float(bar_ranges.iloc[-5:].mean())
        prior_range = float(bar_ranges.iloc[:-5].mean()) if len(bar_ranges) > 5 else recent_range
        snapshot.add(Feature("u_range_expansion_ratio", safe_ratio(recent_range, prior_range), last_bar_close, "of"))
    else:
        snapshot.add(Feature("u_range_expansion_ratio", None, last_bar_close, "of"))

    # ---- OI change as % of prior OI ----
    oi_pct: float | None = None
    if "oi" in window.columns and window["oi"].notna().any():
        oi_series = window["oi"].dropna().astype(float)
        if len(oi_series) >= 2:
            oi_pct = pct_change(oi_series.iloc[-1] - oi_series.iloc[0], oi_series.iloc[0])
    snapshot.add(Feature("u_oi_change_pct", oi_pct, last_bar_close, "of"))

    # ---- up-bar fraction (bounded) ----
    total = int((body != 0).sum())
    up = int((body > 0).sum())
    snapshot.add(Feature("u_up_bar_fraction", (up / total) if total else None, last_bar_close, "of"))

    # ---- §16 disagreement / divergence (inferred from OHLCV) ----
    def _of(name, val):
        snapshot.add(Feature(name, val, last_bar_close, "of"))

    if len(window) >= 4:
        cl = window["close"].to_numpy(float)
        hi = window["high"].to_numpy(float); lo = window["low"].to_numpy(float)
        pc = cl[-1] - cl[0]                                   # price change over window
        wv = float(window["volume"].sum())
        di = float(inferred_delta / wv) if wv > 0 else 0.0    # net directional volume fraction ∈[-1,1]
        disagree = pc != 0 and di != 0 and ((pc > 0) != (di > 0))
        _of("u_delta_price_divergence", float(di) if disagree else 0.0)  # signed toward delta
        _of("u_price_up_delta_down", float(pc > 0 and di < 0))
        _of("u_price_down_delta_up", float(pc < 0 and di > 0))
        cum = np.cumsum(signed_vol.to_numpy(float))
        eps = 1e-9
        _of("u_new_high_without_delta",
            float(hi[-1] >= hi.max() - eps and cum[-1] < cum.max() - eps))
        _of("u_new_low_without_delta",
            float(lo[-1] <= lo.min() + eps and cum[-1] > cum.min() + eps))
        k = max(2, len(window) * 2 // 3)
        ref_hi = float(hi[:k].max()); ref_lo = float(lo[:k].min())
        fbo = float(hi.max() > ref_hi and cl[-1] < ref_hi)   # poked above prior high, closed below
        fbd = float(lo.min() < ref_lo and cl[-1] > ref_lo)   # poked below prior low, closed above
        _of("u_failed_breakout_score", fbo)
        _of("u_failed_breakdown_score", fbd)
        vol = window["volume"].to_numpy(float)
        above = float(vol[hi > ref_hi].sum() / wv) if wv > 0 else 0.0
        below = float(vol[lo < ref_lo].sum() / wv) if wv > 0 else 0.0
        _of("u_trapped_buyers_score", fbo * min(1.0, above))
        _of("u_trapped_sellers_score", fbd * min(1.0, below))
    else:
        for n in _OF_FEATURE_NAMES[7:]:
            _of(n, None)

    return snapshot


def _window_tod_baseline(
    bars: pd.DataFrame,
    decision_time: datetime,
    lookback_minutes: int,
    baseline_sessions: int,
    *,
    kind: str,
) -> tuple[float | None, float | None]:
    """Same-TOD baseline (mean,std) of a windowed aggregate, computed leak-free.

    For each prior session, recompute the same trailing-`lookback_minutes` aggregate ending
    at the same clock time, then take mean/std across the trailing `baseline_sessions`.
    `kind` ∈ {"delta", "volume"}.
    """
    from nomad_sniper.utils.barindex import prior_session_dates, session_frames

    today = decision_time.date()
    prior_dates = prior_session_dates(bars, today, baseline_sessions)
    _, day_frames = session_frames(bars)
    vals: list[float] = []
    for d in prior_dates:
        end = decision_time.replace(year=d.year, month=d.month, day=d.day)
        day = day_frames.get(d)
        if day is None:
            continue
        w = day[day.index <= end].tail(lookback_minutes)
        if w.empty:
            continue
        if kind == "volume":
            vals.append(float(w["volume"].sum()))
        else:  # delta
            body = w["close"] - w["open"]
            rng = (w["high"] - w["low"]).replace(0, np.nan)
            vals.append(float(((body / rng).fillna(0.0) * w["volume"]).sum()))
    if len(vals) < 3:
        return None, None
    arr = np.array(vals, dtype=float)
    return float(arr.mean()), float(arr.std(ddof=1))


def _emit_nulls(snapshot: FeatureSnapshot, decision_time: datetime) -> None:
    avail = ensure_ist(decision_time)
    for name in _OF_FEATURE_NAMES:
        snapshot.add(Feature(name, None, avail, "of"))
