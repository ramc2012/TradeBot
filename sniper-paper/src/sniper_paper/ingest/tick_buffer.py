"""In-memory ring buffer of recent ticks per symbol — for fast feature computation."""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta

import pandas as pd


class TickBuffer:
    """Per-symbol bounded deque of recent ticks."""

    def __init__(self, max_per_symbol: int = 100_000):
        self.max_per_symbol = max_per_symbol
        self._buf: dict[str, deque[dict]] = defaultdict(lambda: deque(maxlen=max_per_symbol))

    def add(self, tick: dict) -> None:
        self._buf[tick["symbol"]].append(tick)

    def recent_df(self, symbol: str, lookback_seconds: int) -> pd.DataFrame:
        if symbol not in self._buf or not self._buf[symbol]:
            return pd.DataFrame(columns=["ts", "ltp", "last_qty"])
        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(seconds=lookback_seconds)
        rows = [t for t in self._buf[symbol] if pd.Timestamp(t["ts"]) >= cutoff]
        return pd.DataFrame(rows)

    def session_df(self, symbol: str, session_open: datetime, decision_ts: datetime) -> pd.DataFrame:
        if symbol not in self._buf:
            return pd.DataFrame(columns=["ts", "ltp", "last_qty"])
        rows = [
            t for t in self._buf[symbol]
            if session_open <= pd.Timestamp(t["ts"]) < decision_ts
        ]
        return pd.DataFrame(rows)

    def last_price(self, symbol: str) -> float | None:
        if not self._buf.get(symbol):
            return None
        return float(self._buf[symbol][-1]["ltp"])
