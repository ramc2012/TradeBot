"""Order-flow features derived from ticks and (when available) 5-level book.

Naming convention: everything inferred from tick prints (no MBO) is `inferred_*`.
Everything inferred from book imbalance without trade-side certainty is `apparent_*`.
This is research-integrity discipline, not pedantry.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from sniper_phase0.features.base import FeatureSnapshot
from sniper_phase0.utils.time import to_ist


def _inferred_delta(ticks: pd.DataFrame) -> float:
    """Tick-rule classifier: uptick → buy-initiated, downtick → sell-initiated.
    Returns net signed volume.
    """
    if len(ticks) < 2:
        return 0.0
    prices = ticks["ltp"].to_numpy()
    qtys = ticks["last_qty"].to_numpy() if "last_qty" in ticks else np.ones(len(ticks))
    diff = np.diff(prices, prepend=prices[0])
    sign = np.where(diff > 0, 1, np.where(diff < 0, -1, 0))
    return float((sign * qtys).sum())


def add_of_features(
    snap: FeatureSnapshot,
    ticks_5s: pd.DataFrame,
    ticks_30s: pd.DataFrame,
    ticks_300s: pd.DataFrame,
    book_snapshot: pd.Series | None,
) -> None:
    avail = snap.decision_ts

    snap.add("of_inferred_delta_5s", _inferred_delta(ticks_5s), avail)
    snap.add("of_inferred_delta_30s", _inferred_delta(ticks_30s), avail)
    snap.add("of_inferred_delta_300s", _inferred_delta(ticks_300s), avail)

    snap.add("of_tick_count_30s", float(len(ticks_30s)), avail)
    snap.add("of_tick_count_300s", float(len(ticks_300s)), avail)

    for label, df in [("5s", ticks_5s), ("30s", ticks_30s), ("300s", ticks_300s)]:
        if len(df) >= 2:
            r = np.log(df["ltp"].iloc[-1] / df["ltp"].iloc[0])
        else:
            r = 0.0
        snap.add(f"of_logret_{label}", float(r), avail)

    if book_snapshot is None:
        for name in [
            "book_apparent_imbalance_l1",
            "book_apparent_imbalance_l5",
            "book_spread_bps",
        ]:
            snap.add(name, float("nan"), avail)
        return

    bid_qty_1 = float(book_snapshot.get("bid_qty_1", np.nan))
    ask_qty_1 = float(book_snapshot.get("ask_qty_1", np.nan))
    bid_px_1 = float(book_snapshot.get("bid_px_1", np.nan))
    ask_px_1 = float(book_snapshot.get("ask_px_1", np.nan))

    total_bid_5 = sum(float(book_snapshot.get(f"bid_qty_{i}", np.nan)) for i in range(1, 6))
    total_ask_5 = sum(float(book_snapshot.get(f"ask_qty_{i}", np.nan)) for i in range(1, 6))

    def imbalance(b: float, a: float) -> float:
        if not np.isfinite(b) or not np.isfinite(a) or (b + a) == 0:
            return float("nan")
        return (b - a) / (b + a)

    snap.add("book_apparent_imbalance_l1", imbalance(bid_qty_1, ask_qty_1), avail)
    snap.add("book_apparent_imbalance_l5", imbalance(total_bid_5, total_ask_5), avail)

    if np.isfinite(bid_px_1) and np.isfinite(ask_px_1) and bid_px_1 > 0:
        spread_bps = (ask_px_1 - bid_px_1) / ((ask_px_1 + bid_px_1) / 2) * 1e4
    else:
        spread_bps = float("nan")
    snap.add("book_spread_bps", spread_bps, avail)


def slice_ticks_before(
    ticks: pd.DataFrame, end_ts: pd.Timestamp, lookback_seconds: int
) -> pd.DataFrame:
    end_ts = to_ist(end_ts)
    start_ts = end_ts - pd.Timedelta(seconds=lookback_seconds)
    return ticks[(ticks["ts"] >= start_ts) & (ticks["ts"] < end_ts)]
