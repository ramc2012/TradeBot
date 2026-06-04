"""Indian F&O transaction-cost model for honest paper P&L (roadmap WS-1.4a).

Paper books historically modelled only a flat 5 bps slippage, so paper P&L
OVERSTATED live by the entire charge stack — which on intraday index options
is the dominant cost (STT alone is 0.10% of sell-side premium). This computes
the real round-trip charges so a paper trade's NET P&L reflects what the
strategy would actually keep.

Components (per leg; buy/sell asymmetry matters): brokerage, STT, exchange
transaction charges, SEBI turnover fee, GST, stamp duty. Covers index options
and futures across NSE / BSE / MCX.

Rates are post-2024-Oct (the STT hikes) discount-broker (₹20-flat) conventions,
kept as constants up top so they can be tuned against a real contract note.
This is a reusable module — directional / commodity / NSE paper books can all
deduct round_trip_cost() on close.
"""
from __future__ import annotations

from dataclasses import dataclass


# ── Rate table (fractions of turnover unless noted) ─────────────────────────
BROKERAGE_FLAT = 20.0           # Rs per executed order (discount broker)
GST_RATE = 0.18                 # on (brokerage + exchange_txn + SEBI)
SEBI_RATE = 0.000001            # Rs 10 / crore = 0.0001%

# Options — turnover = premium x qty
OPT_STT_SELL = 0.001000         # 0.10% of sell-side PREMIUM (raised Oct-2024)
OPT_EXCH_TXN = {"NSE": 0.0003503, "BSE": 0.000325}   # of premium turnover
OPT_STAMP_BUY = 0.00003         # 0.003% buy-side premium

# Futures — turnover = price x qty (contract notional)
FUT_STT_SELL = 0.000200         # 0.02% of sell-side turnover (raised Oct-2024)
FUT_EXCH_TXN = {"NSE": 0.0000173, "BSE": 0.0000173, "MCX": 0.0000210}
FUT_STAMP_BUY = 0.00002         # 0.002% buy-side
FUT_BROKERAGE_PCT = 0.0003      # 0.03%; broker charges min(flat, pct)

_MCX_ROOTS = (
    "CRUDE", "GOLD", "SILVER", "NATURALGAS", "COPPER",
    "ZINC", "ALUMIN", "NICKEL", "LEAD",
)


def exchange_for(underlying: str, instrument_type: str) -> str:
    """Best-effort exchange inference from the underlying + instrument type."""
    u = (underlying or "").upper()
    itype = (instrument_type or "").upper()
    if itype in ("FUT", "FUTURE", "FUTURES") and any(root in u for root in _MCX_ROOTS):
        return "MCX"
    if "SENSEX" in u or "BANKEX" in u:
        return "BSE"
    return "NSE"


def _is_option(instrument_type: str) -> bool:
    return str(instrument_type or "").upper() in ("CE", "PE", "OPT", "OPTION", "OPTIONS")


@dataclass
class CostBreakdown:
    brokerage: float
    stt: float
    exchange_txn: float
    sebi: float
    gst: float
    stamp_duty: float
    total: float


def leg_cost(
    *,
    side: str,
    instrument_type: str,
    exchange: str,
    price: float,
    quantity: float,
) -> CostBreakdown:
    """Charges for ONE leg (entry or exit). ``side`` = BUY | SELL.

    STT is charged on the SELL leg, stamp duty on the BUY leg — so a round
    trip must sum both legs (see round_trip_cost)."""
    turnover = abs(float(price or 0.0) * float(quantity or 0.0))
    is_sell = str(side).upper() == "SELL"
    if _is_option(instrument_type):
        brokerage = BROKERAGE_FLAT
        stt = turnover * OPT_STT_SELL if is_sell else 0.0
        txn = turnover * OPT_EXCH_TXN.get(exchange, OPT_EXCH_TXN["NSE"])
        stamp = 0.0 if is_sell else turnover * OPT_STAMP_BUY
    else:  # futures
        brokerage = min(BROKERAGE_FLAT, turnover * FUT_BROKERAGE_PCT)
        stt = turnover * FUT_STT_SELL if is_sell else 0.0
        txn = turnover * FUT_EXCH_TXN.get(exchange, FUT_EXCH_TXN["NSE"])
        stamp = 0.0 if is_sell else turnover * FUT_STAMP_BUY
    sebi = turnover * SEBI_RATE
    gst = (brokerage + txn + sebi) * GST_RATE
    total = brokerage + stt + txn + sebi + gst + stamp
    return CostBreakdown(
        brokerage=round(brokerage, 2),
        stt=round(stt, 2),
        exchange_txn=round(txn, 2),
        sebi=round(sebi, 4),
        gst=round(gst, 2),
        stamp_duty=round(stamp, 2),
        total=round(total, 2),
    )


def round_trip_cost(
    *,
    instrument_type: str,
    underlying: str,
    entry_price: float,
    exit_price: float,
    quantity: float,
    entry_side: str = "BUY",
    exchange: str | None = None,
) -> float:
    """Total entry+exit charges (rupees) for a round trip. Defaults to a
    long-premium / long-futures trade (BUY entry → SELL exit); pass
    entry_side='SELL' for a short. Subtract this from gross P&L."""
    ex = exchange or exchange_for(underlying, instrument_type)
    exit_side = "SELL" if str(entry_side).upper() == "BUY" else "BUY"
    entry = leg_cost(side=entry_side, instrument_type=instrument_type, exchange=ex, price=entry_price, quantity=quantity)
    exit_leg = leg_cost(side=exit_side, instrument_type=instrument_type, exchange=ex, price=exit_price, quantity=quantity)
    return round(entry.total + exit_leg.total, 2)
