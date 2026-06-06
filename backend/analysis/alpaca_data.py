"""Loader for the local 5y Alpaca 1-minute parquet dataset (off-prod compute lane).

Each symbol lives at <ALPACA_DIR>/<SYM>/bars_1min/<SYM>_bars_1min_YYYYMMDD.parquet
with a UTC tz-aware DatetimeIndex named 'timestamp' and columns
open/high/low/close/volume/trade_count/vwap, including extended hours. We filter to
US regular trading hours (09:30-16:00 ET) and return a flat OHLC frame with a
'time' column + oi=0 (so gann's _resample_15m, which aggregates oi, is happy).
"""
from __future__ import annotations

import glob
import os
from datetime import time as dtime

import pandas as pd

DATA_DIR = os.environ.get(
    "ALPACA_DIR",
    "/Users/chinnadurairamachandran/Claude Projects/TradingBot/alpaca data/data",
)


def load_alpaca_rth(symbol: str, *, rth: bool = True) -> pd.DataFrame:
    files = sorted(glob.glob(f"{DATA_DIR}/{symbol}/bars_1min/*.parquet"))
    if not files:
        return pd.DataFrame()
    df = pd.concat([pd.read_parquet(f) for f in files])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.reset_index()
    df = df.rename(columns={df.columns[0]: "time"})
    df["time"] = pd.to_datetime(df["time"], utc=True)
    if rth:
        et = df["time"].dt.tz_convert("America/New_York")
        df = df[(et.dt.time >= dtime(9, 30)) & (et.dt.time < dtime(16, 0))].reset_index(drop=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["oi"] = 0.0
    return df[["time", "open", "high", "low", "close", "volume", "oi"]]
