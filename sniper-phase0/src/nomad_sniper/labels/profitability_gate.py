"""Option-economics gates for directional labels.

The gate maps an underlying triple-barrier candidate (`up`/`down`) to either the same direction or
`none` if the move is not expected to clear option expression costs. The default `atr_proxy` is
deliberately simple and data-light; richer gates use IV or realized option bars when available.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

import pandas as pd

from nomad_sniper.data.option_bars import ATMOptionSeries

Direction = Literal["up", "down", "none"]


@dataclass(frozen=True)
class GateContext:
    candidate_direction: Direction
    barrier_m: float
    magnitude_atr: float
    time_to_target_minutes: float
    horizon_minutes: int
    entry_price: float
    exit_price: float
    atm_series: ATMOptionSeries | None = None
    decision_time: object | None = None
    exit_time: object | None = None


class ProfitabilityGate(ABC):
    @abstractmethod
    def keep_direction(self, ctx: GateContext) -> Direction:
        """Return candidate direction if tradeable after costs, otherwise `none`."""


@dataclass(frozen=True)
class ATRProxyGate(ProfitabilityGate):
    """Data-light proxy: require enough ATR move and enough speed for weekly ATM theta."""

    m_breakeven: float = 0.75
    max_time_fraction: float = 1.0

    def keep_direction(self, ctx: GateContext) -> Direction:
        if ctx.candidate_direction == "none":
            return "none"
        if ctx.magnitude_atr < self.m_breakeven:
            return "none"
        if ctx.time_to_target_minutes > ctx.horizon_minutes * self.max_time_fraction:
            return "none"
        return ctx.candidate_direction


@dataclass(frozen=True)
class BSProxyGate(ProfitabilityGate):
    """Conservative placeholder for IV-aware option economics.

    Until a full Black-Scholes implementation is calibrated, this gate combines the ATR proxy with
    a minimum IV requirement. It is intentionally conservative and deterministic.
    """

    m_breakeven: float = 0.75
    min_iv: float = 0.05

    def keep_direction(self, ctx: GateContext) -> Direction:
        base = ATRProxyGate(self.m_breakeven).keep_direction(ctx)
        if base == "none":
            return "none"
        atm = ctx.atm_series
        if atm is None or ctx.decision_time is None:
            return "none"
        iv_series = atm.straddle[atm.straddle.index <= ctx.decision_time].get("iv")
        if iv_series is None:
            return "none"
        iv = pd.to_numeric(iv_series, errors="coerce").dropna()
        if iv.empty or float(iv.iloc[-1]) < self.min_iv:
            return "none"
        return base


@dataclass(frozen=True)
class ActualOptionGate(ProfitabilityGate):
    """Gate on realized ATM CE/PE P&L from option bars when available."""

    min_net_return: float = 0.0

    def keep_direction(self, ctx: GateContext) -> Direction:
        if ctx.candidate_direction == "none" or ctx.atm_series is None:
            return "none"
        if ctx.decision_time is None or ctx.exit_time is None:
            return "none"
        series = ctx.atm_series.ce if ctx.candidate_direction == "up" else ctx.atm_series.pe
        before = series[series.index <= ctx.decision_time]
        after = series[series.index <= ctx.exit_time]
        if before.empty or after.empty:
            return "none"
        entry = float(before["close"].iloc[-1])
        exit_ = float(after["close"].iloc[-1])
        if entry <= 0:
            return "none"
        return ctx.candidate_direction if (exit_ - entry) / entry > self.min_net_return else "none"


def build_profitability_gate(
    mode: Literal["atr_proxy", "bs_proxy", "actual_option"] = "atr_proxy",
    *,
    m_breakeven: float = 0.75,
) -> ProfitabilityGate:
    if mode == "atr_proxy":
        return ATRProxyGate(m_breakeven=m_breakeven)
    if mode == "bs_proxy":
        return BSProxyGate(m_breakeven=m_breakeven)
    if mode == "actual_option":
        return ActualOptionGate()
    raise ValueError(f"Unknown profitability gate mode: {mode}")
