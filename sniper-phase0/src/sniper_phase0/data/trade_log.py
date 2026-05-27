"""Loader for the Zerodha Console F&O tradebook export.

Schema is the standard Console CSV. We normalise to:
    trade_id, symbol, instrument_type, entry_ts (IST), exit_ts (IST),
    side ('long'|'short'), qty, entry_price, exit_price, gross_pnl, net_pnl_actual.

`net_pnl_actual` is what Zerodha actually reported — used as a sanity check
against our own cost model in tests.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from sniper_phase0.utils.time import to_ist


REQUIRED_COLS = {
    "Symbol",
    "Trade Date",
    "Buy Date/Time",
    "Sell Date/Time",
    "Buy Quantity",
    "Sell Quantity",
    "Buy Average",
    "Sell Average",
}


def load_trade_log(path: str | Path) -> pd.DataFrame:
    """Load the Zerodha tradebook CSV and normalise it.

    Assumes one row per round-trip trade. If the user's export is per-leg,
    this function should be extended to pair legs by symbol + date.
    """
    df = pd.read_csv(path)
    missing = REQUIRED_COLS - set(df.columns)
    if missing:
        raise ValueError(
            f"Tradebook CSV is missing expected columns: {sorted(missing)}. "
            "Check that you exported the F&O Tradebook, not the Contract Note."
        )

    out = pd.DataFrame()
    out["symbol"] = df["Symbol"].astype(str).str.strip()
    out["entry_ts"] = pd.to_datetime(df["Buy Date/Time"]).map(to_ist)
    out["exit_ts"] = pd.to_datetime(df["Sell Date/Time"]).map(to_ist)
    out["qty"] = df["Buy Quantity"].astype(int)
    out["entry_price"] = df["Buy Average"].astype(float)
    out["exit_price"] = df["Sell Average"].astype(float)

    # Side: if buy precedes sell → long; if sell precedes buy → short.
    out["side"] = (out["entry_ts"] <= out["exit_ts"]).map({True: "long", False: "short"})
    long_mask = out["side"] == "long"
    out["gross_pnl"] = (
        (out["exit_price"] - out["entry_price"]) * out["qty"]
    ).where(long_mask, (out["entry_price"] - out["exit_price"]) * out["qty"])

    # Best-effort net pnl from Zerodha if column exists.
    if "Net PNL" in df.columns:
        out["net_pnl_actual"] = df["Net PNL"].astype(float)
    else:
        out["net_pnl_actual"] = pd.NA

    out["instrument_type"] = out["symbol"].str.extract(r"(FUT|CE|PE)$").fillna("UNK")
    out["trade_id"] = range(len(out))

    out = out.sort_values("entry_ts").reset_index(drop=True)
    return out
