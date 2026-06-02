"""Cost models for converting gross P&L into net P&L.

For Phase 0 we use the published Zerodha F&O charge schedule plus a calibratable
slippage component. Calibration target: realised slippage from the user's own historical
fills (midprice-at-decision vs actual fill). See `CostModel.calibrate_slippage()`.

Sources for Zerodha F&O charges (verify and update before relying):
    - Brokerage: ₹20 per executed order or 0.03%, whichever lower (futures + options).
    - STT: 0.02% on sell side (futures), 0.1% on sell side of option premium (options).
    - Exchange txn charge: ~0.0019% futures, ~0.05% options premium (NSE).
    - GST: 18% on (brokerage + txn charges + SEBI fees).
    - SEBI charges: ₹10 per crore turnover.
    - Stamp duty: 0.002% on buy side (futures), 0.003% on buy side (options).

Numbers below are encoded as constants you should re-confirm against the current schedule
before treating Phase 0 results as final.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class CostBreakdown:
    brokerage: float
    stt: float
    exchange_fee: float
    sebi_fee: float
    stamp_duty: float
    gst: float
    slippage: float

    @property
    def total(self) -> float:
        return (
            self.brokerage
            + self.stt
            + self.exchange_fee
            + self.sebi_fee
            + self.stamp_duty
            + self.gst
            + self.slippage
        )


class CostModel(ABC):
    """Abstract cost model. Implementations must return a CostBreakdown for a round trip."""

    @abstractmethod
    def compute(
        self,
        *,
        instrument_type: Literal["future", "option"],
        direction: Literal["long", "short"],
        entry_price: float,
        exit_price: float,
        quantity: int,
    ) -> CostBreakdown: ...


@dataclass
class ZerodhaFnoCostModel(CostModel):
    """Zerodha F&O cost model with calibratable slippage.

    Slippage is expressed as rupees per share, applied symmetrically on entry and exit.
    Default 0.10 INR/share is a placeholder; calibrate from your own fills.
    """

    slippage_inr_per_share: float = 0.10

    # Brokerage
    brokerage_per_order_inr: float = 20.0
    brokerage_pct_cap: float = 0.0003  # 0.03%

    # Futures
    fut_stt_sell_pct: float = 0.0002          # 0.02% on sell
    fut_exchange_pct: float = 0.0000019       # 0.00019% NSE F&O futures
    fut_stamp_buy_pct: float = 0.00002        # 0.002% on buy

    # Options (charged on premium turnover, not notional)
    opt_stt_sell_pct: float = 0.001           # 0.10% on sell premium
    opt_exchange_pct: float = 0.0005          # 0.05% on premium
    opt_stamp_buy_pct: float = 0.00003        # 0.003% on buy premium

    sebi_per_crore_inr: float = 10.0
    gst_pct: float = 0.18

    def compute(
        self,
        *,
        instrument_type: Literal["future", "option"],
        direction: Literal["long", "short"],
        entry_price: float,
        exit_price: float,
        quantity: int,
    ) -> CostBreakdown:
        # Brokerage: 2 legs (entry + exit). Cap at ₹20 or 0.03% of leg notional.
        entry_notional = entry_price * quantity
        exit_notional = exit_price * quantity
        broker_entry = min(self.brokerage_per_order_inr, entry_notional * self.brokerage_pct_cap)
        broker_exit = min(self.brokerage_per_order_inr, exit_notional * self.brokerage_pct_cap)
        brokerage = broker_entry + broker_exit

        # STT — applied on the SELL leg only.
        sell_leg_notional = exit_notional if direction == "long" else entry_notional
        if instrument_type == "future":
            stt = sell_leg_notional * self.fut_stt_sell_pct
        else:
            stt = sell_leg_notional * self.opt_stt_sell_pct

        # Exchange fees — both legs.
        if instrument_type == "future":
            exchange_fee = (entry_notional + exit_notional) * self.fut_exchange_pct
        else:
            exchange_fee = (entry_notional + exit_notional) * self.opt_exchange_pct

        # SEBI: ₹10 per crore on both legs combined.
        sebi_fee = ((entry_notional + exit_notional) / 1e7) * self.sebi_per_crore_inr

        # Stamp duty — on BUY leg only.
        buy_leg_notional = entry_notional if direction == "long" else exit_notional
        if instrument_type == "future":
            stamp_duty = buy_leg_notional * self.fut_stamp_buy_pct
        else:
            stamp_duty = buy_leg_notional * self.opt_stamp_buy_pct

        # GST: 18% on (brokerage + exchange fees + SEBI fees)
        gst = self.gst_pct * (brokerage + exchange_fee + sebi_fee)

        # Slippage: per-share, both legs
        slippage = self.slippage_inr_per_share * quantity * 2

        return CostBreakdown(
            brokerage=brokerage,
            stt=stt,
            exchange_fee=exchange_fee,
            sebi_fee=sebi_fee,
            stamp_duty=stamp_duty,
            gst=gst,
            slippage=slippage,
        )

    def calibrate_slippage(self, observed_slippage_inr_per_share: float) -> None:
        """Set the slippage parameter from observed historical fills.

        Compute `observed_slippage_inr_per_share` outside this class as the mean
        |fill_price - midprice_at_decision| across your historical orders, then call
        this to update the model.
        """
        if observed_slippage_inr_per_share < 0:
            raise ValueError("Slippage cannot be negative.")
        self.slippage_inr_per_share = observed_slippage_inr_per_share
