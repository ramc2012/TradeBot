"""Market Profile / volume profile primitive (spec §9, §13).

Builds one profile from a set of minute bars and exposes the auction structure the feature
layer needs: POC, VAH, VAL, value-area width, HVN/LVN nodes, single prints, poor high/low,
excess scores, distribution shape (skew/kurtosis), initial balance, and range extension.

All outputs are in *price points*; converting to instrument-independent units (ATR or
profile-width) is the feature layer's job (`features/market_profile.py`), never this module's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from nomad_sniper.utils.timeutil import ensure_ist, session_start


@dataclass
class MarketProfile:
    poc: float
    vah: float
    val: float
    high: float
    low: float
    profile_width: float          # VAH - VAL, in points
    total_volume: float
    bin_centers: np.ndarray = field(repr=False)
    bin_volumes: np.ndarray = field(repr=False)
    hvn_prices: list[float] = field(default_factory=list)
    lvn_prices: list[float] = field(default_factory=list)
    single_print_count: int = 0
    poor_high: bool = False
    poor_low: bool = False
    excess_high_score: float = 0.0
    excess_low_score: float = 0.0
    volume_skew: float = 0.0
    volume_kurtosis: float = 0.0
    ib_high: float | None = None
    ib_low: float | None = None
    range_extension_up: float = 0.0
    range_extension_down: float = 0.0
    last_bar_close_time: datetime | None = None

    def nearest_hvn_above(self, price: float) -> float | None:
        above = [p for p in self.hvn_prices if p > price]
        return min(above) if above else None

    def nearest_hvn_below(self, price: float) -> float | None:
        below = [p for p in self.hvn_prices if p < price]
        return max(below) if below else None

    def nearest_lvn_above(self, price: float) -> float | None:
        above = [p for p in self.lvn_prices if p > price]
        return min(above) if above else None

    def nearest_lvn_below(self, price: float) -> float | None:
        below = [p for p in self.lvn_prices if p < price]
        return max(below) if below else None


def build_profile(
    bars: pd.DataFrame,
    *,
    tick_size: float = 0.05,
    value_area_pct: float = 0.70,
    ib_minutes: int = 60,
    session_date=None,
) -> MarketProfile:
    """Build a Market Profile from minute bars.

    Args:
        bars:           IST-indexed OHLCV bars for ONE profiling window (a session, week, etc.).
        tick_size:      Price granularity for binning.
        value_area_pct: Fraction of volume defining the value area (0.70 standard).
        ib_minutes:     Initial-balance window length (intraday only; ignored if no session_date).
        session_date:   If given, IB and range extension are computed relative to this session's
                        09:15 start. For multi-day windows leave None.
    """
    if bars.empty:
        raise ValueError("Cannot build profile from empty bars.")

    typical = (bars["high"] + bars["low"] + bars["close"]) / 3.0
    vols = bars["volume"].astype(float)
    lo = float(bars["low"].min())
    hi = float(bars["high"].max())
    last_close_time = ensure_ist(bars.index[-1].to_pydatetime())

    if hi <= lo:
        return MarketProfile(
            poc=lo, vah=lo, val=lo, high=hi, low=lo, profile_width=0.0,
            total_volume=float(vols.sum()),
            bin_centers=np.array([lo]), bin_volumes=np.array([float(vols.sum())]),
            last_bar_close_time=last_close_time,
        )

    n_bins = max(2, int(np.ceil((hi - lo) / tick_size)))
    edges = np.linspace(lo, hi, n_bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2.0
    binned, _ = np.histogram(typical, bins=edges, weights=vols)

    if binned.sum() == 0:
        binned = np.ones_like(centers)

    poc_idx = int(np.argmax(binned))
    poc = float(centers[poc_idx])

    # Value area: expand from POC until value_area_pct of volume captured
    target = value_area_pct * binned.sum()
    captured = binned[poc_idx]
    lo_idx = hi_idx = poc_idx
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

    # HVN / LVN: local maxima / minima of the (smoothed) volume-by-price curve
    hvn_prices, lvn_prices = _find_nodes(centers, binned)

    # Single prints / poor extremes / excess
    single_prints = int((binned > 0).sum() and (binned == binned[binned > 0].min()).sum())
    poor_high, excess_high = _extreme_quality(binned, centers, side="high")
    poor_low, excess_low = _extreme_quality(binned, centers, side="low")

    # Distribution shape (volume-weighted moments about POC)
    vol_skew, vol_kurt = _weighted_shape(centers, binned)

    # Initial balance + range extension (intraday only)
    ib_high = ib_low = None
    rng_ext_up = rng_ext_down = 0.0
    if session_date is not None:
        start = session_start(session_date)
        ib_end = start + timedelta(minutes=ib_minutes)
        ib_bars = bars[bars.index <= ib_end]
        if not ib_bars.empty:
            ib_high = float(ib_bars["high"].max())
            ib_low = float(ib_bars["low"].min())
            rng_ext_up = max(0.0, hi - ib_high)
            rng_ext_down = max(0.0, ib_low - lo)

    return MarketProfile(
        poc=poc, vah=vah, val=val, high=hi, low=lo,
        profile_width=vah - val,
        total_volume=float(vols.sum()),
        bin_centers=centers, bin_volumes=binned,
        hvn_prices=hvn_prices, lvn_prices=lvn_prices,
        single_print_count=single_prints,
        poor_high=poor_high, poor_low=poor_low,
        excess_high_score=excess_high, excess_low_score=excess_low,
        volume_skew=vol_skew, volume_kurtosis=vol_kurt,
        ib_high=ib_high, ib_low=ib_low,
        range_extension_up=rng_ext_up, range_extension_down=rng_ext_down,
        last_bar_close_time=last_close_time,
    )


def _find_nodes(centers: np.ndarray, binned: np.ndarray) -> tuple[list[float], list[float]]:
    """Identify high-volume nodes (local maxima) and low-volume nodes (local minima)."""
    if len(binned) < 3:
        return [float(centers[int(np.argmax(binned))])], []
    # light smoothing
    k = np.array([0.25, 0.5, 0.25])
    sm = np.convolve(binned, k, mode="same")
    hvn, lvn = [], []
    mean_v = sm.mean()
    for i in range(1, len(sm) - 1):
        if sm[i] >= sm[i - 1] and sm[i] >= sm[i + 1] and sm[i] > mean_v:
            hvn.append(float(centers[i]))
        if sm[i] <= sm[i - 1] and sm[i] <= sm[i + 1] and sm[i] < mean_v:
            lvn.append(float(centers[i]))
    return hvn, lvn


def _extreme_quality(binned: np.ndarray, centers: np.ndarray, *, side: str) -> tuple[bool, float]:
    """Poor extreme = volume at the extreme bin is high (flat, unfinished, likely revisited).
    Excess = volume at the extreme is near-zero relative to the body (a tail/rejection).

    Returns (poor_flag, excess_score in [0,1]).
    """
    if len(binned) < 4 or binned.sum() == 0:
        return False, 0.0
    body_mean = binned[binned > 0].mean()
    edge = binned[-1] if side == "high" else binned[0]
    # poor: edge volume is a meaningful fraction of the body mean (no thinning at the extreme)
    poor = bool(edge >= 0.6 * body_mean)
    # excess: the outermost few bins thin out sharply -> tail
    tail = binned[-3:] if side == "high" else binned[:3]
    excess = float(np.clip(1.0 - (tail.mean() / (body_mean + 1e-9)), 0.0, 1.0))
    return poor, excess


def _weighted_shape(centers: np.ndarray, weights: np.ndarray) -> tuple[float, float]:
    """Volume-weighted skewness and excess kurtosis of the price distribution."""
    w = weights.astype(float)
    if w.sum() == 0:
        return 0.0, 0.0
    p = w / w.sum()
    mean = float((centers * p).sum())
    var = float(((centers - mean) ** 2 * p).sum())
    if var <= 0:
        return 0.0, 0.0
    std = var**0.5
    skew = float(((centers - mean) ** 3 * p).sum() / std**3)
    kurt = float(((centers - mean) ** 4 * p).sum() / std**4 - 3.0)
    return skew, kurt
