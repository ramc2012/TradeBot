"""Loader for underlying tick data.

Expects parquet files partitioned by date under `paths.underlying_ticks`,
one directory per instrument:
    underlying_ticks/NIFTY/date=2024-04-15/*.parquet
    underlying_ticks/BANKNIFTY/date=2024-04-15/*.parquet

Each parquet has at minimum: ts (UTC ns), ltp, last_qty.
We normalise ts to IST on load.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from sniper_phase0.utils.time import to_ist


def load_ticks(
    root: str | Path,
    instrument: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    """Load ticks for [start, end) for the given instrument."""
    root = Path(root) / instrument
    if not root.exists():
        return pd.DataFrame(columns=["ts", "ltp", "last_qty"])

    dates = pd.date_range(start.date(), end.date(), freq="D")
    frames = []
    for d in dates:
        part = root / f"date={d.date().isoformat()}"
        if part.exists():
            frames.append(pd.read_parquet(part))
    if not frames:
        return pd.DataFrame(columns=["ts", "ltp", "last_qty"])

    df = pd.concat(frames, ignore_index=True)
    df["ts"] = pd.to_datetime(df["ts"]).map(to_ist)
    df = df[(df["ts"] >= start) & (df["ts"] < end)].sort_values("ts").reset_index(drop=True)
    return df


def ticks_in_window(
    df: pd.DataFrame, end_ts: pd.Timestamp, lookback_seconds: int
) -> pd.DataFrame:
    """Slice ticks strictly before end_ts (no leakage), within lookback."""
    end_ts = to_ist(end_ts)
    start_ts = end_ts - pd.Timedelta(seconds=lookback_seconds)
    return df[(df["ts"] >= start_ts) & (df["ts"] < end_ts)]
