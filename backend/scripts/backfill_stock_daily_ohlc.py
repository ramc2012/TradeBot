"""
Backfill daily stock OHLC into underlying_spot_candles for the F&O stock
universe via the already-authenticated Fyers adapter.

Why: CBE Scanner's Volatility Compression (F1) + Volatility Cone (F6) need
~250 days of clean daily OHLC. Without it, CBE can only score the few
features that derive from the option chain, single-feature dominance kicks
in, and the watchlist is unreliable.

Reuses the running backend's broker session (no fresh login required).
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from sqlalchemy import text

from db.database import AsyncSessionLocal
from api.routers.auth import ensure_fyers_session, get_active_adapter


LOOKBACK_DAYS = 400
INTERVAL = "1day"
SOURCE = "fyers"
RATE_LIMIT_SLEEP = 0.35  # ~3 req/sec to stay under Fyers history limits


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

    universe: list[tuple[str, str]] = []
    for sym, key in rows:
        key = str(key or "").strip()
        if not key:
            continue
        if key.startswith("NSE_EQ|"):
            fyers_sym = f"NSE:{sym}-EQ"
        elif key.startswith("NSE:") or key.startswith("BSE:"):
            fyers_sym = key
        else:
            fyers_sym = f"NSE:{sym}-EQ"
        universe.append((sym, fyers_sym))
    return universe


async def upsert_rows(symbol: str, instrument_key: str, rows: list[dict]) -> int:
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

    # Reuse the live Fyers adapter
    adapter = get_active_adapter("fyers")
    if adapter is None:
        ok = await ensure_fyers_session(force_validate=False)
        if not ok:
            raise RuntimeError("No live Fyers session — login first")
        adapter = get_active_adapter("fyers")
        if adapter is None:
            raise RuntimeError("Fyers adapter unavailable after ensure")
    logger.info("Using live Fyers adapter")

    to_dt = date.today()
    from_dt = to_dt - timedelta(days=LOOKBACK_DAYS)

    succeeded = failed = total_rows = 0
    start = time.time()

    for idx, (symbol, fyers_symbol) in enumerate(stocks, start=1):
        try:
            raw = await adapter.get_historical_candles(
                symbol=fyers_symbol,
                resolution="D",
                range_from=from_dt.isoformat(),
                range_to=to_dt.isoformat(),
            )
            # Convert the adapter's "time" (ISO string ending Z) → datetime
            rows = []
            for r in raw:
                try:
                    ts = datetime.fromisoformat(str(r["time"]).replace("Z", "+00:00"))
                    rows.append({
                        "time": ts,
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                        "volume": int(r.get("volume") or 0),
                    })
                except (TypeError, ValueError, KeyError):
                    continue
            n = await upsert_rows(symbol, fyers_symbol, rows)
            total_rows += n
            succeeded += 1
            if idx % 25 == 0:
                elapsed = time.time() - start
                logger.info(
                    f"  [{idx}/{len(stocks)}] {symbol}: {n} rows. "
                    f"running total={total_rows}, elapsed={elapsed:.1f}s"
                )
        except Exception as exc:
            failed += 1
            logger.warning(f"  [{idx}/{len(stocks)}] {symbol} FAILED: {exc}")
        await asyncio.sleep(RATE_LIMIT_SLEEP)

    elapsed = time.time() - start
    logger.success(
        f"Done in {elapsed:.1f}s. stocks_ok={succeeded} stocks_failed={failed} rows_total={total_rows}"
    )


if __name__ == "__main__":
    asyncio.run(main())
