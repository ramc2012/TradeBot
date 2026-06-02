"""Order-flow features inferred from minute bars (spec section 10).

Phase-0 honesty: no true tick/MBO data, so these are inferred from OHLCV and named with an
`u_inferred_`/`u_` prefix. Depth-based OF (absorption, sweeps, replenishment, book imbalance)
is a data-gated future stage (spec section 10) and is stubbed as nulls until tick/depth data
is wired. All emitted values are normalized (z-score / ratio / bounded).
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from nomad_sniper.features.base import Feature, FeatureSnapshot
from nomad_sniper.utils.normalize import rolling_tod_baseline, zscore
from nomad_sniper.utils.timeutil import ensure_ist, tod_bucket_key

_OF_NAMES = [
    "u_inferred_delta_z", "u_inferred_delta_slope_z", "u_volume_z",
    "u_volume_accel_ratio", "u_range_expansion_ratio", "u_oi_change_pct",
    "u_up_bar_fraction", "u_price_delta_divergence",
    # depth-based OF — stubbed until tick/depth data available
    "u_absorption_score", "u_buy_sweep_intensity", "u_sell_sweep_intensity",
    "u_book_imbalance_pct",
]


def build_of_features(
    decision_time: datetime,
    bars: pd.DataFrame,
    *,
    snapshot: FeatureSnapshot | None = None,
    lookback_minutes: int = 30,
) -> FeatureSnapshot:
    decision_time = ensure_ist(decision_time)
    if snapshot is None:
        snapshot = FeatureSnapshot(decision_time=decision_time)

    window = bars[bars.index <= decision_time].tail(lookback_minutes)
    if window.empty:
        for n in _OF_NAMES:
            snapshot.add(Feature(n, None, decision_time, "of"))
        return snapshot

    last_close = ensure_ist(window.index[-1].to_pydatetime())
    body = window["close"] - window["open"]
    rng = (window["high"] - window["low"]).replace(0, np.nan)
    direction = (body / rng).fillna(0)
    signed_vol = direction * window["volume"]

    delta_sum = float(signed_vol.sum())
    vol_sum = float(window["volume"].sum())

    # z-scores vs same-time-of-day trailing baseline (instrument-independent)
    delta_baseline = rolling_tod_baseline(bars, decision_time, "volume", tod_key=tod_bucket_key)
    snapshot.add(Feature("u_inferred_delta_z",
                         zscore(delta_sum, delta_baseline), last_close, "of"))
    snapshot.add(Feature("u_volume_z",
                         zscore(vol_sum, _scaled(delta_baseline, len(window))), last_close, "of"))

    # delta slope, scaled by its own magnitude (unitless)
    cum = signed_vol.cumsum()
    if len(cum) >= 3:
        x = np.arange(len(cum))
        slope = float(np.polyfit(x, cum.values, 1)[0])
        denom = abs(cum).mean() or 1.0
        snapshot.add(Feature("u_inferred_delta_slope_z", slope / denom, last_close, "of"))
    else:
        snapshot.add(Feature("u_inferred_delta_slope_z", None, last_close, "of"))

    half = len(window) // 2
    if half >= 2:
        recent = float(window["volume"].iloc[half:].sum())
        prior = float(window["volume"].iloc[:half].sum())
        snapshot.add(Feature("u_volume_accel_ratio",
                             (recent / prior) if prior > 0 else None, last_close, "of"))
    else:
        snapshot.add(Feature("u_volume_accel_ratio", None, last_close, "of"))

    bar_ranges = (window["high"] - window["low"]).astype(float)
    if len(bar_ranges) >= 6:
        rr = bar_ranges.iloc[-5:].mean() / (bar_ranges.iloc[:-5].mean() or np.nan)
        snapshot.add(Feature("u_range_expansion_ratio",
                             float(rr) if np.isfinite(rr) else None, last_close, "of"))
    else:
        snapshot.add(Feature("u_range_expansion_ratio", None, last_close, "of"))

    if "oi" in window.columns and window["oi"].notna().any():
        oi = window["oi"].dropna().astype(float)
        if len(oi) >= 2 and oi.iloc[0] > 0:
            snapshot.add(Feature("u_oi_change_pct",
                                 float(100 * (oi.iloc[-1] - oi.iloc[0]) / oi.iloc[0]),
                                 last_close, "of"))
        else:
            snapshot.add(Feature("u_oi_change_pct", None, last_close, "of"))
    else:
        snapshot.add(Feature("u_oi_change_pct", None, last_close, "of"))

    up_frac = float((body > 0).mean())
    snapshot.add(Feature("u_up_bar_fraction", up_frac, last_close, "of"))

    # price/delta divergence: sign mismatch between net price move and net delta (bounded -1..1)
    price_move = float(window["close"].iloc[-1] - window["close"].iloc[0])
    div = 0.0
    if price_move != 0 and delta_sum != 0:
        div = -1.0 if (np.sign(price_move) != np.sign(delta_sum)) else 1.0
    snapshot.add(Feature("u_price_delta_divergence", div, last_close, "of"))

    # depth-based OF stubs (null until tick/depth data wired — spec section 10 serious version)
    for n in ("u_absorption_score", "u_buy_sweep_intensity",
              "u_sell_sweep_intensity", "u_book_imbalance_pct"):
        snapshot.add(Feature(n, None, last_close, "of"))

    return snapshot


def _scaled(baseline, n):
    if baseline is None:
        return None
    mu, sigma = baseline
    return (mu * n, sigma * (n**0.5))
