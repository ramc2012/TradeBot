"""Zerodha trade log loader.

Phase 0 input: a CSV exported from console.zerodha.com → Reports → Trades.
Expected columns (Zerodha standard export):
    symbol, isin, trade_date, exchange, segment, series, trade_type,
    auction, quantity, price, trade_id, order_id, order_execution_time

We normalize to a strict internal schema (see `Trade` below) and refuse
anything malformed. Better to crash on load than silently mis-label trades.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import pandas as pd
from pydantic import BaseModel, Field, field_validator

from nomad_sniper.utils.logging import get_logger
from nomad_sniper.utils.timeutil import IST, ensure_ist

log = get_logger()


class Trade(BaseModel):
    """One executed trade leg from the broker. Buy and sell legs are pairs to be matched."""

    trade_id: str
    order_id: str
    symbol: str
    exchange: Literal["NSE", "BSE", "NFO", "BFO", "MCX", "CDS"]
    segment: str
    trade_type: Literal["buy", "sell"]
    quantity: int = Field(gt=0)
    price: float = Field(gt=0)
    executed_at: datetime  # IST-aware
    trade_date: datetime  # IST-aware, date at 00:00

    @field_validator("executed_at", "trade_date")
    @classmethod
    def _enforce_ist(cls, v):
        return ensure_ist(v)

    @property
    def is_fno(self) -> bool:
        return self.exchange in ("NFO", "BFO")

    @property
    def notional(self) -> float:
        return self.quantity * self.price


def load_zerodha_trades(
    csv_path: str | Path,
    *,
    fno_only: bool = True,
) -> list[Trade]:
    """Load Zerodha trade CSV, return list of `Trade` objects.

    Args:
        csv_path: Path to Zerodha trade CSV export.
        fno_only: If True, drop equity/cash trades. Default True for Sniper Phase 0.

    Raises:
        FileNotFoundError: csv_path missing.
        ValueError: Required columns missing or values fail validation.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"Trade log not found: {csv_path}")

    df = pd.read_csv(csv_path)
    log.info(f"Loaded {len(df)} raw rows from {csv_path.name}")

    # Normalize column names — Zerodha sometimes ships with title case
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    required = {
        "symbol",
        "exchange",
        "trade_type",
        "quantity",
        "price",
        "trade_id",
        "order_id",
        "order_execution_time",
        "trade_date",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"Zerodha CSV missing required columns: {sorted(missing)}. "
            f"Found: {sorted(df.columns)}"
        )

    # Normalize trade_type to lowercase
    df["trade_type"] = df["trade_type"].str.lower().str.strip()

    # Parse times — Zerodha gives 'YYYY-MM-DD HH:MM:SS' in IST (naive). Localize.
    df["executed_at"] = (
        pd.to_datetime(df["order_execution_time"], errors="coerce")
        .dt.tz_localize(IST, ambiguous="raise", nonexistent="raise")
    )
    df["trade_date_parsed"] = (
        pd.to_datetime(df["trade_date"], errors="coerce")
        .dt.tz_localize(IST, ambiguous="raise", nonexistent="raise")
    )

    bad = df[df["executed_at"].isna() | df["trade_date_parsed"].isna()]
    if not bad.empty:
        log.warning(f"Dropping {len(bad)} rows with unparseable timestamps")
        df = df.dropna(subset=["executed_at", "trade_date_parsed"])

    if fno_only:
        before = len(df)
        df = df[df["exchange"].isin(["NFO", "BFO"])]
        log.info(f"FNO filter: kept {len(df)} of {before} trades")

    if "segment" not in df.columns:
        df["segment"] = df["exchange"]

    trades: list[Trade] = []
    for _, row in df.iterrows():
        try:
            trades.append(
                Trade(
                    trade_id=str(row["trade_id"]),
                    order_id=str(row["order_id"]),
                    symbol=str(row["symbol"]),
                    exchange=str(row["exchange"]),
                    segment=str(row["segment"]),
                    trade_type=row["trade_type"],
                    quantity=int(row["quantity"]),
                    price=float(row["price"]),
                    executed_at=row["executed_at"].to_pydatetime(),
                    trade_date=row["trade_date_parsed"].to_pydatetime(),
                )
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"Skipping malformed row {row.get('trade_id', '?')}: {e}")

    log.info(f"Returning {len(trades)} validated trades")
    return trades


def trades_to_dataframe(trades: list[Trade]) -> pd.DataFrame:
    """Convert list of Trade → DataFrame, preserving IST timestamps."""
    return pd.DataFrame([t.model_dump() for t in trades])
