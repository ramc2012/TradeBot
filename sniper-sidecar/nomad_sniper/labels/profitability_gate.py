"""Option-economics gate (contract §4.2) — turns a directional candidate into a label.

A triple-barrier candidate (`up`/`down`/`none`) on the underlying is only *kept* as up/down
if the move would have been profitable expressed through the ATM option over the horizon H.
Otherwise it is relabeled `none`. This is what protects the book from slow-but-correct calls.

Three pluggable implementations:
  - `AtrProxyGate`   (v1 default): keep iff realized favourable excursion ≥ `m_breakeven` ATR.
  - `BsProxyGate`    : Black-Scholes price the ATM option you'd buy, decay theta along the
                       realized path, exit at the barrier/timeout, keep iff net P&L > 0.
  - `ActualOptionGate`: label on the realized P&L of the actual ATM option series.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import pandas as pd

from nomad_sniper.utils.black_scholes import bs_price as _bs_price

Direction = Literal["up", "down", "none"]


@dataclass
class GateContext:
    """Everything a gate needs to judge a candidate at one grid point."""

    candidate: Direction          # raw triple-barrier outcome (up/down/none)
    entry_time: datetime
    exit_time: datetime
    entry_price: float            # underlying entry
    exit_price: float             # underlying exit (barrier or timeout)
    mfe_atr: float                # favourable excursion in ATR units
    atr_ref: float | None         # ATR in points (for converting to price moves)
    horizon_minutes: int
    iv_estimate: float | None = None        # annualized IV for BS proxy (e.g. 0.14)
    atm_ce: pd.DataFrame | None = None       # realized CE bars (ActualOptionGate)
    atm_pe: pd.DataFrame | None = None       # realized PE bars (ActualOptionGate)
    cost_inr_per_unit: float = 0.0           # round-trip option cost per unit (proxy)


class ProfitabilityGate(ABC):
    """Maps a candidate + context → kept direction (`up`/`down`) or `none`."""

    name: str = "abstract"

    @abstractmethod
    def apply(self, ctx: GateContext) -> Direction: ...


@dataclass
class AtrProxyGate(ProfitabilityGate):
    """Keep iff the realized favourable move clears a calibrated breakeven in ATR units.

    `m_breakeven` is the single calibration knob (contract §8): the ATR-multiple an ATM
    weekly option must travel to clear theta + spread + cost over H. Calibrate offline once.
    """

    m_breakeven: float = 0.6
    name: str = "atr_proxy"

    def apply(self, ctx: GateContext) -> Direction:
        if ctx.candidate == "none":
            return "none"
        return ctx.candidate if ctx.mfe_atr >= self.m_breakeven else "none"


@dataclass
class BsProxyGate(ProfitabilityGate):
    """Price the ATM option with Black-Scholes at entry, decay along the realized path.

    Buys the directional ATM option (call for up, put for down), advances spot to exit_price,
    reduces time-to-expiry by the holding horizon, reprices, subtracts cost. Keep iff net > 0.
    """

    default_iv: float = 0.14
    risk_free: float = 0.065
    days_to_expiry_entry: float = 3.0     # representative ATM weekly DTE
    cost_inr_per_unit: float = 4.0
    name: str = "bs_proxy"

    def apply(self, ctx: GateContext) -> Direction:
        if ctx.candidate == "none" or ctx.atr_ref is None:
            return "none"
        iv = ctx.iv_estimate if (ctx.iv_estimate and ctx.iv_estimate > 0) else self.default_iv
        opt = "call" if ctx.candidate == "up" else "put"

        T_entry = max(self.days_to_expiry_entry, 1e-6) / 365.0
        T_exit = max(self.days_to_expiry_entry - ctx.horizon_minutes / (60.0 * 24.0), 1e-6) / 365.0
        K = ctx.entry_price  # ATM
        entry_premium = _bs_price(ctx.entry_price, K, T_entry, self.risk_free, iv, opt)
        exit_premium = _bs_price(ctx.exit_price, K, T_exit, self.risk_free, iv, opt)
        net = (exit_premium - entry_premium) - self.cost_inr_per_unit
        return ctx.candidate if net > 0 else "none"


@dataclass
class ActualOptionGate(ProfitabilityGate):
    """Label on the realized P&L of the actual ATM option series over [entry, exit].

    Strongest interpretation; requires strike-level option history (ctx.atm_ce / atm_pe).
    Falls back to `none` when the option series is missing.
    """

    cost_inr_per_unit: float = 4.0
    name: str = "actual_option"

    def apply(self, ctx: GateContext) -> Direction:
        if ctx.candidate == "none":
            return "none"
        series = ctx.atm_ce if ctx.candidate == "up" else ctx.atm_pe
        if series is None or series.empty:
            return "none"
        entry = series[series.index <= ctx.entry_time]
        exit_ = series[series.index <= ctx.exit_time]
        if entry.empty or exit_.empty:
            return "none"
        entry_premium = float(entry["close"].iloc[-1])
        exit_premium = float(exit_["close"].iloc[-1])
        net = (exit_premium - entry_premium) - self.cost_inr_per_unit
        return ctx.candidate if net > 0 else "none"


def make_gate(mode: str, **kwargs) -> ProfitabilityGate:
    """Factory: `mode` ∈ {atr_proxy, bs_proxy, actual_option}."""
    mode = (mode or "atr_proxy").lower()
    if mode == "atr_proxy":
        return AtrProxyGate(**{k: v for k, v in kwargs.items() if k in ("m_breakeven",)})
    if mode == "bs_proxy":
        return BsProxyGate(**{k: v for k, v in kwargs.items()
                              if k in ("default_iv", "risk_free", "days_to_expiry_entry", "cost_inr_per_unit")})
    if mode == "actual_option":
        return ActualOptionGate(**{k: v for k, v in kwargs.items() if k in ("cost_inr_per_unit",)})
    raise ValueError(f"Unknown gate mode {mode!r}")
