"""Backfill 30m option-premium candles for F&O STOCK ATM contracts.

The auto-backfill machinery is indices-only, so stock option premium has a gap
(observed: ~May 27 – Jun 5 2026 captured indices only; stocks resume Jun 8+).
This enumerates the currently-tracked stock ATM contracts (from
atm_option_watchlist_snapshots) and pulls their 30-minute history for the
[--from, --to] window from the broker, persisting into option_premium_candles.

Constraints:
  • Active contracts only — Upstox does not serve EXPIRED stock-option history,
    so only the current (active) expiry's gap is fillable.
  • Rate-limited through the shared UPSTOX_DATA_LIMITER inside the fetch path.
  • Needs a valid broker session; run inside the backend container so the
    persisted credentials can be restored.

Usage (inside the backend container):
  python scripts/backfill_stock_options.py --from 2026-05-20 --to 2026-06-09
  python scripts/backfill_stock_options.py --from 2026-05-20 --to 2026-06-09 --limit 2 --dry
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date

from loguru import logger
from sqlalchemy import text

from db.database import AsyncSessionLocal
from market_data.option_history import option_history_service as svc


async def _stock_contracts(limit: int) -> list:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT DISTINCT a.underlying, a.expiry, a.strike, a.option_type, a.instrument_key
                    FROM atm_option_watchlist_snapshots a
                    JOIN fo_underlying_catalog c
                      ON c.symbol = a.underlying AND c.kind = 'STOCK'
                    WHERE a.expiry >= CURRENT_DATE
                      AND a.instrument_key IS NOT NULL
                    ORDER BY a.underlying, a.option_type
                    """
                )
            )
        ).all()
    return rows[:limit] if limit else rows


async def backfill(from_date: date, to_date: date, limit: int, dry: bool) -> None:
    contracts = await _stock_contracts(limit)
    logger.info(f"[stock-backfill] {len(contracts)} stock ATM contracts · window {from_date}..{to_date} · dry={dry}")
    persisted = 0
    fetched_total = 0
    with_data = 0
    errors = 0
    for i, c in enumerate(contracts):
        try:
            rows = await svc._fetch_broker_candles(
                instrument_key=c.instrument_key,
                from_date=from_date,
                to_date=to_date,
                interval="30minute",
            )
            fetched_total += len(rows)
            if rows:
                with_data += 1
            if rows and not dry:
                await svc._persist_broker_candles(
                    rows=rows,
                    underlying=c.underlying,
                    expiry=c.expiry,
                    strike=float(c.strike),
                    option_type=c.option_type,
                    instrument_key=c.instrument_key,
                    interval="30minute",
                    already_in_db=set(),
                )
                persisted += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.warning(f"[stock-backfill] {c.underlying} {c.option_type} {c.instrument_key} failed: {exc}")
        if (i + 1) % 25 == 0:
            logger.info(f"[stock-backfill] {i + 1}/{len(contracts)} · with_data={with_data} · rows={fetched_total} · errors={errors}")
    logger.info(
        f"[stock-backfill] DONE contracts={len(contracts)} with_data={with_data} "
        f"persisted={persisted} rows_fetched={fetched_total} errors={errors}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="from_date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--to", dest="to_date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--limit", type=int, default=0, help="cap contracts (0 = all)")
    ap.add_argument("--dry", action="store_true", help="fetch only, do not persist")
    a = ap.parse_args()
    asyncio.run(backfill(date.fromisoformat(a.from_date), date.fromisoformat(a.to_date), a.limit, a.dry))


if __name__ == "__main__":
    main()
