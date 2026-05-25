"""Pure-function replay of signal logic.

Loads raw candles, recomputes signals without side-effects, returns a list
suitable for diffing against `agent_signals` rows from the same window.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

import pandas as pd


@dataclass(frozen=True)
class ReplaySignal:
    bar_time: datetime
    underlying: str
    expiry: str
    strike: float
    option_type: str
    signal_type: str
    premium: float

    def key(self) -> tuple:
        # Canonical key for diffing. signal_type is intentionally NOT in the
        # key — the (bar_time, underlying, expiry, strike, option_type) tuple
        # already uniquely identifies a contract+bar event. Live recorder
        # uses generic "MACD_ZERO_CROSS" while replay computes explicit
        # UP/DOWN; the option_type field captures direction either way.
        return (
            self.bar_time.replace(microsecond=0).isoformat(),
            self.underlying,
            self.expiry,
            round(self.strike, 2),
            self.option_type,
        )


def compute_macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    sig = macd.ewm(span=signal, adjust=False).mean()
    return macd, sig, macd - sig


def detect_zero_cross_signals(
    candles: pd.DataFrame,
    *,
    underlying: str,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> list[ReplaySignal]:
    """Replicate S1's MACD(12,26,9) zero-line cross logic on a single contract.

    `candles` must be ascending by time and have columns:
        time, close, expiry, strike, option_type
    """
    if candles.empty:
        return []
    df = candles.sort_values("time").reset_index(drop=True).copy()
    macd, sig, _ = compute_macd(df["close"], fast, slow, signal)
    df["macd"] = macd
    df["prev_macd"] = df["macd"].shift(1)

    out: list[ReplaySignal] = []
    min_bars = slow + signal
    for i in range(min_bars, len(df)):
        row = df.iloc[i]
        if pd.isna(row["macd"]) or pd.isna(row["prev_macd"]):
            continue
        st = None
        if row["prev_macd"] < 0 and row["macd"] > 0:
            st = "ZERO_CROSS_UP"
        elif row["prev_macd"] > 0 and row["macd"] < 0:
            st = "ZERO_CROSS_DOWN"
        if st:
            out.append(
                ReplaySignal(
                    bar_time=row["time"].to_pydatetime() if hasattr(row["time"], "to_pydatetime") else row["time"],
                    underlying=underlying,
                    expiry=str(row.get("expiry", "")),
                    strike=float(row.get("strike", 0)),
                    option_type=str(row.get("option_type", "")),
                    signal_type=st,
                    premium=float(row["close"]),
                )
            )
    return out


def diff_signal_sets(
    replay: Iterable[ReplaySignal],
    live_keys: Iterable[tuple],
) -> dict:
    """Return match-count + lists of missing-from-live and missing-from-replay."""
    replay_keys = {s.key() for s in replay}
    live_set = set(live_keys)
    matches = replay_keys & live_set
    return {
        "match_count": len(matches),
        "missing_from_live": sorted(replay_keys - live_set),
        "missing_from_replay": sorted(live_set - replay_keys),
    }
