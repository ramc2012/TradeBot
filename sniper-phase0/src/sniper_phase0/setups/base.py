"""Candidate dataclass and helpers shared by all setup-family detectors.

A Candidate is a deterministic, MP-rule-derived trade opportunity. The model
is trained to FILTER these (skip vs take), not to invent setups from nothing.
This is what gives Phase 0 the "decision_ts" set when we're not bootstrapping
from a real Zerodha trade log.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from sniper_phase0.data.mp_state import MPState


SetupName = str  # one of: "va_rejection", "va_acceptance", "ib_breakout", "lvn_rejection", "poc_magnet", "failed_auction"


@dataclass
class Candidate:
    decision_ts: pd.Timestamp
    instrument: str
    side: str  # "long" or "short"
    entry_price: float
    stop_price: float
    target_price: float
    setup_name: SetupName

    def to_row(self, trade_id: int) -> dict:
        return {
            "trade_id": trade_id,
            "decision_ts": self.decision_ts,
            "instrument": self.instrument,
            "side": self.side,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "setup_name": self.setup_name,
            # qty is set later from a config (lot size × multiplier).
        }


def _valid_barriers(side: str, entry: float, stop: float, target: float) -> bool:
    if side == "long":
        return stop < entry < target
    return target < entry < stop
