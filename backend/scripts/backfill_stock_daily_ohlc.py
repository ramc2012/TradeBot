"""
Backfill daily stock OHLC into underlying_spot_candles for the F&O stock
universe via Fyers history API.

Why: CBE Scanner's Volatility Compression (F1) + Volatility Cone (F6) need
~250 days of clean daily OHLC. Without it, CBE can only score the few
features that derive from the option chain, single-feature dominance kicks in,
and the watchlist is unreliable. This script does a one-shot 400-day backfill.

Rate-limit safe: ~3 req/sec to Fyers history. 211 stocks × 1 call each
≈ 1.5 minutes wall-clock.

Storage: writes to underlying_spot_candles with interval='1day' and source='fyers'.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from sqlalchemy import text

from db.database import AsyncSessionLocal


CREDS_FILE = Path("/app/credentials.json")
LOOKBACK_DAYS = 400
INTERVAL = "1day"
SOURCE = "fyers"
RATE_LIMIT_SLEEP = 0.35  # ~3 req/sec
IST = timezone(timedelta(hours=5, minutes=30))


async def get_fno_stock_universe() -> list[tuple[str, str]]:
    """Return list of (symbol, fyers_history_symbol) for F&O stocks."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(text(
            """
            SELECT symbol, spot_instrument_key
            FROM fo_underlying_catalog
            WHERE kind = 'STOCK'
              AND spot_instrument_key IS NOT NULL
              AND spot_instrument_key != ''
            ORDER BY symbol
            """
        ))
        rows = result.fetchall()
    universe = []
    for sym, key in rows:
        key = str(key or "").strip()
        if not key:
            continue
        # Convert Upstox key (NSE_EQ|ISIN) to Fyers history symbol when possible.
        if key.startswith("NSE_EQ|"):
            # No reliable conversion without a lookup — try the conventional form.
            fyers_sym = f"NSE:{sym}-EQ"
        elif key.startswith("NSE:") or key.startswith("BSE:"):
            fyers_sym = key
        else:
            fyers_sym = f"NSE:{sym}-EQ"
        universe.append((sym, fyers_sym))
    return universe


def load_fyers_client():
    from fyers_apiv3 import fyersModel
    creds = json.loads(CREDS_FILE.read_text())
    token = creds.get("fyers", {}).get("access_token", "").strip()
    if not token:
        raise RuntimeError("Fyers access_token missing in credentials.json")
    app_id = os.environ.get("FYERS_APP_ID", "")
    return fyersModel.FyersModel(client_id=app_id, is_async=False, token=token, log_path="")


def fetch_daily(fyers, fyers_symbol: str, from_dt: date, to_dt: date) -> list[dict]:
    """Fetch daily candles for one stock from Fyers."""
    payload = {
        "symbol": fyers_symbol,
        "resolution": "D",      # daily
        "date_format": "1",
        "range_from": from_dt.isoformat(),
        "range_to": to_dt.isoformat(),
        "cont_flag": "1",
    }
    resp = fyers.history(payload)
    candles = resp.get("candles") or []
    if not candles and resp.get("s") != "ok":
        return []
    out = []
    for c in candles:
        if not c or len(c) < 6:
            continue
        try:
            ts = datetime.fromtimestamp(int(c[0]), IST)
            out.append({
                "time": ts,
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": int(c[5] or 0),
            })
        except (TypeError, ValueError):
            continue
    return out


async def upsert_rows(symbol: str, instrument_key: str, rows: list[dict]) -> int:
    """Upsert rows into underlying_spot_candles."""
    if not rows:
        return 0
    async with AsyncSessionLocal() as session:
        await session.execute(text(
            """
            INSERT INTO underlying_spot_candles
                (time, instrument_key, underlying, interval, open, high, low, close, volume, oi, source)
            VALUES (:time, :instrument_key, :underlying, :interval, :open, :high, :low, :close, :volume, 0, :source)
            ON CONFLICT (instrument_key, interval, "time") DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume,
                source = EXCLUDED.source,
                synced_at = NOW()
            """
        ), [
            {
                "time": r["time"],
                "instrument_key": instrument_key,
                "underlying": symbol,
                "interval": INTERVAL,
                "open": r["open"],
                "high": r["high"],
                "low": r["low"],
                "close": r["close"],
                "volume": r["volume"],
                "source": SOURCE,
            }
            for r in rows
        ])
        await session.commit()
    return len(rows)


async def main():
    stocks = await get_fno_stock_universe()
    logger.info(f"Backfilling daily OHLC for {len(stocks)} F&O stocks")
    fyers = load_fyers_client()

    to_dt = date.today()
    from_dt = to_dt - timedelta(days=LOOKBACK_DAYS)

    succeeded = 0
    failed = 0
    total_rows = 0
    start = time.time()

    for idx, (symbol, fyers_symbol) in enumerate(stocks, start=1):
        try:
            rows = fetch_daily(fyers, fyers_symbol, from_dt, to_dt)
            n = await upsert_rows(symbol, fyers_symbol, rows)
            total_rows += n
            succeeded += 1
            if idx % 20 == 0:
                elapsed = time.time() - start
                logger.info(f"  [{idx}/{len(stocks)}] {symbol}: {n} rows. running total={total_rows}, elapsed={elapsed:.1f}s")
        except Exception as exc:
            failed += 1
            logger.warning(f"  [{idx}/{len(stocks)}] {symbol} FAILED: {exc}")
        time.sleep(RATE_LIMIT_SLEEP)

    elapsed = time.time() - start
    logger.success(
        f"Done in {elapsed:.1f}s. stocks_ok={succeeded} stocks_failed={failed} rows_total={total_rows}"
    )


if __name__ == "__main__":
    asyncio.run(main())
