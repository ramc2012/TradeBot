"""Cost model — applied at LABEL time, not eval time.

Models train on net outcomes from day one. Parameters in configs/base.yaml.
Slippage is a placeholder until recalibrated from actual Zerodha fills.
"""
from __future__ import annotations

from dataclasses import dataclass

from sniper_phase0.utils.settings import Costs


@dataclass
class TradeCosts:
    brokerage: float
    exchange_charges: float
    sebi_charges: float
    stt: float
    stamp_duty: float
    gst: float
    slippage: float
    total: float


def compute_costs(
    entry_price: float,
    exit_price: float,
    qty: int,
    costs: Costs,
    slippage_multiplier: float = 1.0,
    is_event_day: bool = False,
) -> TradeCosts:
    """Return total round-trip costs in INR for one trade.

    Slippage is symmetric (entry + exit), in bps of mid notional.
    """
    buy_notional = entry_price * qty
    sell_notional = exit_price * qty
    turnover = buy_notional + sell_notional

    brokerage = costs.brokerage_per_order_inr * 2

    exchange = turnover * costs.exchange_txn_charge_bps / 1e4
    sebi = turnover * costs.sebi_charge_bps / 1e4
    stt = sell_notional * costs.stt_bps_sell_side / 1e4
    stamp = buy_notional * costs.stamp_duty_bps_buy_side / 1e4

    gst = (
        brokerage * costs.gst_on_brokerage_pct / 100.0
        + exchange * costs.gst_on_exchange_pct / 100.0
    )

    slippage_bps = (
        costs.slippage_bps_event_day if is_event_day else costs.slippage_bps_default
    )
    slippage_bps *= slippage_multiplier
    slippage = turnover * slippage_bps / 1e4

    total = brokerage + exchange + sebi + stt + stamp + gst + slippage
    return TradeCosts(
        brokerage=brokerage,
        exchange_charges=exchange,
        sebi_charges=sebi,
        stt=stt,
        stamp_duty=stamp,
        gst=gst,
        slippage=slippage,
        total=total,
    )


def net_pnl(
    entry_price: float,
    exit_price: float,
    qty: int,
    side: str,
    costs: Costs,
    slippage_multiplier: float = 1.0,
    is_event_day: bool = False,
) -> tuple[float, float, TradeCosts]:
    """Return (gross_pnl, net_pnl, costs)."""
    if side == "long":
        gross = (exit_price - entry_price) * qty
    else:
        gross = (entry_price - exit_price) * qty
    tc = compute_costs(entry_price, exit_price, qty, costs, slippage_multiplier, is_event_day)
    return gross, gross - tc.total, tc
