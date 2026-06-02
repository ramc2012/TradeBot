"""Per-leg + round-trip cost model. Mirrors sniper-phase0 but parameterised per exchange.

Returns INR costs (not bps) for a single trade with `qty` and entry/exit prices.
"""
from __future__ import annotations

from sniper_paper.common.settings import Costs


def round_trip_costs(
    costs: Costs,
    exchange: str,
    qty: int,
    entry_price: float,
    exit_price: float,
    slippage_multiplier: float = 1.0,
    event_day: bool = False,
) -> dict:
    notional_entry = entry_price * qty
    notional_exit = exit_price * qty

    brokerage = costs.brokerage_per_order_inr * 2  # entry + exit

    exch_bps = costs.exchange_txn_charge_bps.get(exchange, 0.345) / 100  # bps → fraction
    exchange_fee = (notional_entry + notional_exit) * exch_bps / 100

    sebi_fee = (notional_entry + notional_exit) * (costs.sebi_charge_bps / 100) / 100

    stt_bps = costs.stt_bps_sell_side.get(exchange, 1.25) / 100
    stt = notional_exit * stt_bps / 100  # sell-side only

    stamp = notional_entry * (costs.stamp_duty_bps_buy_side / 100) / 100

    gst_on_brk = brokerage * costs.gst_on_brokerage_pct / 100
    gst_on_exch = (exchange_fee + sebi_fee) * costs.gst_on_exchange_pct / 100

    base_slip_bps = costs.slippage_bps_event_day if event_day else costs.slippage_bps_default
    slip_bps = base_slip_bps * slippage_multiplier / 100
    slippage = (notional_entry + notional_exit) * slip_bps / 100

    total = brokerage + exchange_fee + sebi_fee + stt + stamp + gst_on_brk + gst_on_exch + slippage
    return {
        "brokerage": brokerage,
        "exchange_fee": exchange_fee,
        "sebi_fee": sebi_fee,
        "stt": stt,
        "stamp": stamp,
        "gst": gst_on_brk + gst_on_exch,
        "slippage": slippage,
        "total": total,
    }


def slippage_inr_one_side(
    costs: Costs, price: float, qty: int, event_day: bool = False
) -> float:
    """Slippage applied to ONE leg (entry or exit). For paper-fill simulation."""
    base_bps = costs.slippage_bps_event_day if event_day else costs.slippage_bps_default
    return price * qty * (base_bps / 10000.0)
