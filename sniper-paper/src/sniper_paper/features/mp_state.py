"""Market Profile state computation. Copied from sniper-phase0 (stable interface).

Two flavours:
  - compute_mp_state: intraday-so-far, strictly before decision_ts
  - compute_session_mp: completed prior-session profile
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from sniper_paper.common.time import IST, to_ist

TPO_PERIOD_MINUTES = 30
IB_PERIOD_MINUTES = 60


@dataclass
class MPState:
    poc: float
    vah: float
    val: float
    ib_high: float
    ib_low: float
    profile_high: float
    profile_low: float
    tpo_count: int
    data_available_at: pd.Timestamp
    bins: np.ndarray | None = None
    counts: np.ndarray | None = None
    single_prints: int = 0
    poor_high: bool = False
    poor_low: bool = False
    hvn_prices: list[float] = field(default_factory=list)
    lvn_prices: list[float] = field(default_factory=list)


def _tpo_letters(ts: pd.Timestamp, session_open: pd.Timestamp) -> str:
    delta_min = int((to_ist(ts) - to_ist(session_open)).total_seconds() // 60)
    period = delta_min // TPO_PERIOD_MINUTES
    return chr(ord("A") + period) if 0 <= period < 26 else "Z"


def _compute(prices, tpos, tick_size: float) -> tuple | None:
    if prices.size == 0:
        return None
    lo = np.floor(prices.min() / tick_size) * tick_size
    hi = np.ceil(prices.max() / tick_size) * tick_size
    bins = np.arange(lo, hi + tick_size, tick_size)
    bin_idx = np.minimum(((prices - lo) / tick_size).astype(int), len(bins) - 1)
    tpo_set: dict[int, set] = {}
    for i, t in zip(bin_idx, tpos):
        tpo_set.setdefault(i, set()).add(t)
    counts = np.array([len(tpo_set.get(i, set())) for i in range(len(bins))])
    if counts.sum() == 0:
        return None
    poc_idx = int(np.argmax(counts))
    target = 0.70 * counts.sum()
    chosen_sum = counts[poc_idx]
    lo_i, hi_i = poc_idx, poc_idx
    while chosen_sum < target and (lo_i > 0 or hi_i < len(bins) - 1):
        lo_v = counts[lo_i - 1] if lo_i > 0 else -1
        hi_v = counts[hi_i + 1] if hi_i < len(bins) - 1 else -1
        if hi_v >= lo_v and hi_v >= 0:
            hi_i += 1
            chosen_sum += counts[hi_i]
        elif lo_v >= 0:
            lo_i -= 1
            chosen_sum += counts[lo_i]
        else:
            break
    return bins, counts, float(bins[poc_idx]), float(bins[hi_i]), float(bins[lo_i])


def compute_mp_state(
    ticks: pd.DataFrame,
    decision_ts: pd.Timestamp,
    session_open: pd.Timestamp,
    tick_size: float = 0.05,
) -> MPState | None:
    decision_ts = to_ist(decision_ts)
    session_open = to_ist(session_open)
    intraday = ticks[(ticks["ts"] >= session_open) & (ticks["ts"] < decision_ts)]
    if len(intraday) < 30:
        return None
    tpos = np.array([_tpo_letters(t, session_open) for t in intraday["ts"]])
    prices = intraday["ltp"].to_numpy()
    res = _compute(prices, tpos, tick_size)
    if res is None:
        return None
    bins, counts, poc, vah, val = res

    ib_cutoff = session_open + pd.Timedelta(minutes=IB_PERIOD_MINUTES)
    ib = intraday[intraday["ts"] < ib_cutoff]
    ib_high = float(ib["ltp"].max()) if not ib.empty else float("nan")
    ib_low = float(ib["ltp"].min()) if not ib.empty else float("nan")

    return MPState(
        poc=poc, vah=vah, val=val,
        ib_high=ib_high, ib_low=ib_low,
        profile_high=float(prices.max()), profile_low=float(prices.min()),
        tpo_count=int(counts.sum()),
        data_available_at=decision_ts,
        bins=bins, counts=counts,
    )


def compute_session_mp(
    ticks: pd.DataFrame, session_open: pd.Timestamp, session_close: pd.Timestamp, tick_size: float = 0.05
) -> MPState | None:
    """Build completed-session MP. data_available_at = session_close."""
    if ticks.empty:
        return None
    day = ticks[(ticks["ts"] >= session_open) & (ticks["ts"] <= session_close)]
    if day.empty:
        return None
    tpos = np.array([_tpo_letters(t, session_open) for t in day["ts"]])
    prices = day["ltp"].to_numpy()
    res = _compute(prices, tpos, tick_size)
    if res is None:
        return None
    bins, counts, poc, vah, val = res

    ib_cutoff = session_open + pd.Timedelta(minutes=IB_PERIOD_MINUTES)
    ib = day[day["ts"] < ib_cutoff]
    ib_high = float(ib["ltp"].max()) if not ib.empty else float("nan")
    ib_low = float(ib["ltp"].min()) if not ib.empty else float("nan")

    single_prints = int((counts == 1).sum())
    poor_high = bool(counts[-1] <= 2)
    poor_low = bool(counts[0] <= 2)
    hvn_prices, lvn_prices = _detect_hvn_lvn(bins, counts)

    return MPState(
        poc=poc, vah=vah, val=val,
        ib_high=ib_high, ib_low=ib_low,
        profile_high=float(prices.max()), profile_low=float(prices.min()),
        tpo_count=int(counts.sum()),
        data_available_at=to_ist(session_close),
        bins=bins, counts=counts,
        single_prints=single_prints,
        poor_high=poor_high, poor_low=poor_low,
        hvn_prices=hvn_prices, lvn_prices=lvn_prices,
    )


def _detect_hvn_lvn(bins, counts) -> tuple[list[float], list[float]]:
    if len(counts) < 3:
        return [], []
    hvn, lvn = [], []
    for i in range(1, len(counts) - 1):
        if counts[i] > counts[i - 1] and counts[i] > counts[i + 1] and counts[i] >= 3:
            hvn.append(float(bins[i]))
        if counts[i] < counts[i - 1] and counts[i] < counts[i + 1] and counts[i] >= 1:
            lvn.append(float(bins[i]))
    return hvn, lvn
