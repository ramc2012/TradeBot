"""Pull NIFTY OHLCV from the existing nomad-curie TimescaleDB.

Actual schema (verified on 15.206.56.206 / nomad-curie's DB):

    underlying_spot_candles (
      time            TIMESTAMPTZ NOT NULL,
      instrument_key  TEXT,
      underlying      TEXT,        -- 'NIFTY', 'SENSEX', 'CRUDEOIL', 'BANKNIFTY', ...
      interval        TEXT,        -- '1minute' | '30minute'
      open, high, low, close NUMERIC(12,4),
      volume, oi      BIGINT,
      source          TEXT,
      synced_at       TIMESTAMPTZ
    )
"""
from __future__ import annotations

from datetime import datetime

import asyncpg
import pandas as pd


async def fetch_underlying_candles(
    dsn: str,
    underlying: str,
    interval: str,
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    """Returns ts, open, high, low, close, volume in IST."""
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT time AS ts, open, high, low, close, volume
            FROM underlying_spot_candles
            WHERE underlying = $1 AND interval = $2
              AND time >= $3 AND time < $4
            ORDER BY time ASC
            """,
            underlying, interval, start, end,
        )
    finally:
        await conn.close()

    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df

    # asyncpg returns Decimal for NUMERIC; cast to float for downstream code.
    for c in ("open", "high", "low", "close"):
        if c in df.columns:
            df[c] = df[c].astype(float)
    df["ts"] = pd.to_datetime(df["ts"])
    if df["ts"].dt.tz is None:
        df["ts"] = df["ts"].dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata")
    else:
        df["ts"] = df["ts"].dt.tz_convert("Asia/Kolkata")
    return df


async def fetch_nifty_candles(
    dsn: str, start: datetime, end: datetime, timeframe: str = "30minute",
) -> pd.DataFrame:
    """Back-compat wrapper used by the CLI."""
    return await fetch_underlying_candles(dsn, "NIFTY", timeframe, start, end)


async def introspect(dsn: str) -> dict:
    """Sanity check — table layout + NIFTY data availability."""
    conn = await asyncpg.connect(dsn)
    try:
        cols = await conn.fetch(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = 'underlying_spot_candles'
            ORDER BY ordinal_position
            """
        )
        availability = await conn.fetch(
            """
            SELECT underlying, interval, count(*) AS n, MIN(time) AS min_ts, MAX(time) AS max_ts
            FROM underlying_spot_candles
            WHERE underlying IN ('NIFTY', 'SENSEX', 'CRUDEOIL', 'BANKNIFTY')
            GROUP BY underlying, interval
            ORDER BY underlying, interval
            """
        )
    finally:
        await conn.close()

    return {
        "underlying_spot_candles_columns": [dict(c) for c in cols],
        "availability": [dict(a) for a in availability],
    }
