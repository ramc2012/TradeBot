"""Loader for 5-level order book snapshots.

Honest about retail constraints: this is best-effort, forward-captured only.
Reconstructing historical book state for Phase 0 backtest is not possible.
For trades older than book capture started, book-derived features will be NaN
and downstream code must handle that gracefully (LightGBM does).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from sniper_phase0.utils.time import to_ist


BOOK_COLS = (
    ["ts"]
    + [f"bid_px_{i}" for i in range(1, 6)]
    + [f"bid_qty_{i}" for i in range(1, 6)]
    + [f"ask_px_{i}" for i in range(1, 6)]
    + [f"ask_qty_{i}" for i in range(1, 6)]
)


def load_book(
    root: str | Path,
    instrument: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    root = Path(root) / instrument
    if not root.exists():
        return pd.DataFrame(columns=BOOK_COLS)

    dates = pd.date_range(start.date(), end.date(), freq="D")
    frames = []
    for d in dates:
        part = root / f"date={d.date().isoformat()}"
        if part.exists():
            frames.append(pd.read_parquet(part))
    if not frames:
        return pd.DataFrame(columns=BOOK_COLS)

    df = pd.concat(frames, ignore_index=True)
    df["ts"] = pd.to_datetime(df["ts"]).map(to_ist)
    df = df[(df["ts"] >= start) & (df["ts"] < end)].sort_values("ts").reset_index(drop=True)
    return df


def book_snapshot_at_or_before(df: pd.DataFrame, ts: pd.Timestamp) -> pd.Series | None:
    """Strictly before-or-at ts; returns None if no snapshot available."""
    ts = to_ist(ts)
    eligible = df[df["ts"] <= ts]
    if eligible.empty:
        return None
    return eligible.iloc[-1]
