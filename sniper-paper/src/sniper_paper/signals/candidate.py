from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class Candidate:
    decision_ts: pd.Timestamp
    instrument: str
    symbol: str
    side: str   # 'long' or 'short'
    entry_price: float
    stop_price: float
    target_price: float
    setup_name: str

    def risk_per_unit(self) -> float:
        return abs(self.entry_price - self.stop_price)

    def reward_per_unit(self) -> float:
        return abs(self.target_price - self.entry_price)

    def gross_RR(self) -> float:
        r = self.risk_per_unit()
        return self.reward_per_unit() / r if r > 0 else 0.0


def valid_barriers(side: str, entry: float, stop: float, target: float) -> bool:
    if side == "long":
        return stop < entry < target
    return target < entry < stop
