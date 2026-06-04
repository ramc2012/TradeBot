"""WS-1.4 — Indian F&O transaction-cost model for paper P&L.

Paper realised P&L was gross (no brokerage / STT / exchange / GST), which
overstates live results and flatters high-churn strategies. This applies a
round-trip cost so paper ≈ live net.

Rates are 2024-25 NSE/MCX approximations and are deliberately configurable module
constants — they change and vary by broker, so tune them rather than trust to the
paisa. Set env PAPER_APPLY_COSTS=false to disable (e.g. to compare gross vs net).

Scope: options (CE/PE), NSE futures, MCX commodity futures — classified by
instrument_type + symbol prefix. Costs are realised on CLOSE (round trip);
unrealised mark-to-market P&L stays gross by convention.
"""
from __future__ import annotations

import os

PAPER_APPLY_COSTS = os.environ.get("PAPER_APPLY_COSTS", "true").strip().lower() not in (
    "0", "false", "no", "off",
)

# Per executed order (flat discount-broker F&O brokerage). Round trip = 2 orders.
BROKERAGE_PER_ORDER = 20.0
# STT / CTT — charged on the SELL leg's turnover only.
STT_OPTION_SELL = 0.001       # 0.100% of option premium (NSE, post-Oct-2024)
STT_FUTURE_SELL = 0.0002      # 0.020% of NSE futures turnover
CTT_MCX_SELL = 0.0001         # 0.010% of MCX (non-agri) futures turnover
# Exchange transaction charges — both legs, on turnover.
EXCH_OPTION = 0.0003503       # ~0.03503% NSE options (on premium)
EXCH_NSE_FUTURE = 0.0000173   # ~0.00173% NSE futures
EXCH_MCX_FUTURE = 0.000026    # ~0.0026% MCX (varies by commodity)
# SEBI turnover fee — both legs (~Rs 10 per crore).
SEBI_FEE = 0.000001
# Stamp duty — BUY leg only.
STAMP_OPTION = 0.00003        # 0.003%
STAMP_FUTURE = 0.00002        # 0.002%
# GST on (brokerage + exchange + SEBI).
GST = 0.18


def round_trip_charges(
    *,
    symbol: str,
    instrument_type: str,
    entry_price: float,
    exit_price: float,
    qty: int,
    entry_action: str,
) -> float:
    """Total charges for a round trip (entry + exit).

    ``qty`` is in units (lots x lot size — the same qty used for gross P&L).
    Never raises; returns 0.0 if disabled or inputs are degenerate.
    """
    try:
        if not PAPER_APPLY_COSTS:
            return 0.0
        q = int(qty)
        if q <= 0:  # invalid/degenerate quantity → no charge
            return 0.0
        entry_to = abs(float(entry_price)) * q
        exit_to = abs(float(exit_price)) * q
        total_to = entry_to + exit_to
        if total_to <= 0:
            return 0.0

        is_buy_entry = str(entry_action).upper() == "BUY"
        sell_to = exit_to if is_buy_entry else entry_to   # the leg we SELL on
        buy_to = entry_to if is_buy_entry else exit_to     # the leg we BUY on

        it = str(instrument_type or "").upper()
        is_option = it in ("CE", "PE")
        is_mcx = str(symbol or "").upper().startswith("MCX")

        brokerage = 2.0 * BROKERAGE_PER_ORDER
        if is_option:
            stt = STT_OPTION_SELL * sell_to
            exch = EXCH_OPTION * total_to
            stamp = STAMP_OPTION * buy_to
        elif is_mcx:
            stt = CTT_MCX_SELL * sell_to
            exch = EXCH_MCX_FUTURE * total_to
            stamp = STAMP_FUTURE * buy_to
        else:  # NSE future
            stt = STT_FUTURE_SELL * sell_to
            exch = EXCH_NSE_FUTURE * total_to
            stamp = STAMP_FUTURE * buy_to

        sebi = SEBI_FEE * total_to
        gst = GST * (brokerage + exch + sebi)
        return round(brokerage + stt + exch + sebi + stamp + gst, 2)
    except Exception:
        return 0.0
