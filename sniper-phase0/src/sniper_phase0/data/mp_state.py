"""Market Profile state at a given decision timestamp.

Two flavours:
  - compute_mp_state(ticks, decision_ts): intraday-so-far, strictly before decision_ts
  - compute_session_mp(ticks, session_date): completed prior-session profile

The session_mp also detects single prints, poor highs/lows, and the nearest
HVN/LVN to a reference price.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time

import numpy as np
import pandas as pd

from sniper_phase0.utils.time import IST, session_bounds, to_ist


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
    # Optional richer outputs (populated by compute_session_mp; left default for intraday).
    bins: np.ndarray | None = None
    counts: np.ndarray | None = None
    single_prints: int = 0
    poor_high: bool = False
    poor_low: bool = False
    hvn_prices: list[float] = field(default_factory=list)
    lvn_prices: list[float] = field(default_factory=list)


def _tpo_letters(ts: pd.Timestamp) -> str:
    """Return the TPO letter for a timestamp (A=09:15-09:45, B=09:45-10:15, ...)."""
    ts = to_ist(ts)
    open_dt, _ = session_bounds(ts)
    delta_min = int((ts - open_dt).total_seconds() // 60)
    period = delta_min // TPO_PERIOD_MINUTES
    return chr(ord("A") + period) if 0 <= period < 26 else "Z"


def compute_mp_state(
    ticks: pd.DataFrame, decision_ts: pd.Timestamp, tick_size: float = 0.05
) -> MPState | None:
    """Build MP state from ticks strictly before decision_ts.

    Returns None if not enough data (e.g. before 30 min into session).
    """
    decision_ts = to_ist(decision_ts)
    session_open, _ = session_bounds(decision_ts)
    intraday = ticks[(ticks["ts"] >= session_open) & (ticks["ts"] < decision_ts)]
    if len(intraday) < 30:
        return None

    intraday = intraday.assign(tpo=intraday["ts"].map(_tpo_letters))
    prices = intraday["ltp"].to_numpy()
    tpos = intraday["tpo"].to_numpy()
    if prices.size == 0:
        return None

    lo = np.floor(prices.min() / tick_size) * tick_size
    hi = np.ceil(prices.max() / tick_size) * tick_size
    bins = np.arange(lo, hi + tick_size, tick_size)

    # Count distinct TPO letters per price bin (TPO count, not tick count).
    bin_idx = np.minimum(((prices - lo) / tick_size).astype(int), len(bins) - 1)
    tpo_set: dict[int, set] = {}
    for i, t in zip(bin_idx, tpos):
        tpo_set.setdefault(i, set()).add(t)
    counts = np.array([len(tpo_set.get(i, set())) for i in range(len(bins))])
    if counts.sum() == 0:
        return None

    poc_idx = int(np.argmax(counts))
    poc = float(bins[poc_idx])

    total = counts.sum()
    target = 0.70 * total
    chosen = {poc_idx}
    chosen_sum = counts[poc_idx]
    lo_i, hi_i = poc_idx, poc_idx
    while chosen_sum < target and (lo_i > 0 or hi_i < len(bins) - 1):
        lo_v = counts[lo_i - 1] if lo_i > 0 else -1
        hi_v = counts[hi_i + 1] if hi_i < len(bins) - 1 else -1
        if hi_v >= lo_v and hi_v >= 0:
            hi_i += 1
            chosen.add(hi_i)
            chosen_sum += counts[hi_i]
        elif lo_v >= 0:
            lo_i -= 1
            chosen.add(lo_i)
            chosen_sum += counts[lo_i]
        else:
            break
    vah = float(bins[hi_i])
    val = float(bins[lo_i])

    ib_cutoff = session_open + pd.Timedelta(minutes=IB_PERIOD_MINUTES)
    ib = intraday[intraday["ts"] < ib_cutoff]
    if ib.empty:
        ib_high = ib_low = float("nan")
    else:
        ib_high = float(ib["ltp"].max())
        ib_low = float(ib["ltp"].min())

    return MPState(
        poc=poc,
        vah=vah,
        val=val,
        ib_high=ib_high,
        ib_low=ib_low,
        profile_high=float(prices.max()),
        profile_low=float(prices.min()),
        tpo_count=int(counts.sum()),
        data_available_at=decision_ts,
    )


def _detect_hvn_lvn(
    bins: np.ndarray, counts: np.ndarray, min_separation: int = 3
) -> tuple[list[float], list[float]]:
    """Return prices at local maxima (HVN) and local minima (LVN) of TPO histogram."""
    if len(counts) < 3:
        return [], []
    hvn_idx = []
    lvn_idx = []
    for i in range(1, len(counts) - 1):
        if counts[i] > counts[i - 1] and counts[i] > counts[i + 1] and counts[i] >= 3:
            hvn_idx.append(i)
        if counts[i] < counts[i - 1] and counts[i] < counts[i + 1] and counts[i] >= 1:
            lvn_idx.append(i)

    def _dedupe(idxs: list[int]) -> list[int]:
        if not idxs:
            return idxs
        kept = [idxs[0]]
        for i in idxs[1:]:
            if i - kept[-1] >= min_separation:
                kept.append(i)
        return kept

    hvn_idx = _dedupe(hvn_idx)
    lvn_idx = _dedupe(lvn_idx)
    return [float(bins[i]) for i in hvn_idx], [float(bins[i]) for i in lvn_idx]


def compute_session_mp(
    ticks: pd.DataFrame,
    session_date: pd.Timestamp,
    tick_size: float = 0.05,
) -> MPState | None:
    """Build the completed-session MP profile for `session_date`.

    `data_available_at` is set to the session close — these features may be
    used by any decision_ts on the NEXT trading day or later.
    """
    if ticks.empty:
        return None
    session_date = pd.Timestamp(session_date).normalize()
    day_ticks = ticks[ticks["ts"].dt.normalize() == session_date]
    if day_ticks.empty:
        return None

    intraday = day_ticks.assign(tpo=day_ticks["ts"].map(_tpo_letters))
    prices = intraday["ltp"].to_numpy()
    tpos = intraday["tpo"].to_numpy()
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
    poc = float(bins[poc_idx])
    total = counts.sum()
    target = 0.70 * total
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
    vah = float(bins[hi_i])
    val = float(bins[lo_i])

    ib_cutoff = (
        pd.Timestamp(session_date.replace(hour=9, minute=15)).tz_localize(IST)
        + pd.Timedelta(minutes=IB_PERIOD_MINUTES)
    )
    ib = intraday[intraday["ts"] < ib_cutoff]
    ib_high = float(ib["ltp"].max()) if not ib.empty else float("nan")
    ib_low = float(ib["ltp"].min()) if not ib.empty else float("nan")

    single_prints = int((counts == 1).sum())
    # Poor high/low: extreme bin has only 1-2 TPOs (range got cleanly rejected without much rotation).
    poor_high = bool(counts[-1] <= 2)
    poor_low = bool(counts[0] <= 2)
    hvn_prices, lvn_prices = _detect_hvn_lvn(bins, counts)

    session_close_ts = pd.Timestamp(
        session_date.replace(hour=15, minute=30)
    ).tz_localize(IST)

    return MPState(
        poc=poc,
        vah=vah,
        val=val,
        ib_high=ib_high,
        ib_low=ib_low,
        profile_high=float(prices.max()),
        profile_low=float(prices.min()),
        tpo_count=int(counts.sum()),
        data_available_at=session_close_ts,
        bins=bins,
        counts=counts,
        single_prints=single_prints,
        poor_high=poor_high,
        poor_low=poor_low,
        hvn_prices=hvn_prices,
        lvn_prices=lvn_prices,
    )
