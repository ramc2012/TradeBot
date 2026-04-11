"""One-time script: fetch NSE lot sizes for all F&O underlyings from Upstox.

Calls Upstox get_option_contracts() per stock (10 req at a time), extracts
lot_size from any CE or PE contract, and saves to fo_underlying_catalog.lot_size.

Run inside the backend container:
  docker exec nomadcurie_backend python scripts/backfill_lot_sizes.py
"""
from __future__ import annotations

import asyncio
import os
import sys

# ensure app root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy import text

from db.database import AsyncSessionLocal


UPSTOX_EXPIRY = "2026-04-23"   # use April monthly to get contracts
SEMAPHORE_N = 5                 # 5 concurrent Upstox calls


async def fetch_lot_size_upstox(upstox, symbol: str, underlying_key: str) -> tuple[str, int | None]:
    """Return (symbol, lot_size) or (symbol, None) if unavailable."""
    try:
        contracts = await upstox.get_option_contracts(underlying_key, UPSTOX_EXPIRY)
        for c in contracts:
            ls = c.get("lot_size")
            if ls:
                return symbol, int(ls)
        # Try without expiry filter (get first available contract)
        contracts = await upstox.get_option_contracts(underlying_key)
        for c in contracts:
            ls = c.get("lot_size")
            if ls:
                return symbol, int(ls)
    except Exception as exc:
        print(f"  WARN {symbol}: {exc}")
    return symbol, None


async def main() -> None:
    from api.routers.auth import auto_restore_sessions, get_active_adapter
    from core.config import settings  # noqa: F401 — triggers settings load

    print("Restoring broker sessions...")
    await auto_restore_sessions()

    upstox = get_active_adapter("upstox")
    if upstox is None:
        print("ERROR: Upstox not connected. Connect Upstox first.")
        return

    # Load all underlyings with NULL lot_size
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT symbol, underlying_key FROM fo_underlying_catalog "
                "WHERE kind = 'STOCK' AND (lot_size IS NULL OR lot_size = 0) "
                "ORDER BY symbol"
            )
        )
        stocks = [(row.symbol, row.underlying_key) for row in result.fetchall()]

    print(f"Found {len(stocks)} stocks with missing lot_size — fetching from Upstox...")
    semaphore = asyncio.Semaphore(SEMAPHORE_N)

    async def fetch(symbol: str, key: str) -> tuple[str, int | None]:
        async with semaphore:
            result = await fetch_lot_size_upstox(upstox, symbol, key)
            await asyncio.sleep(0.3)  # gentle rate limit
            return result

    tasks = [fetch(sym, key) for sym, key in stocks]
    results = await asyncio.gather(*tasks, return_exceptions=False)

    success = 0
    missing = []
    async with AsyncSessionLocal() as session:
        for symbol, lot_size in results:
            if lot_size:
                await session.execute(
                    text("UPDATE fo_underlying_catalog SET lot_size = :ls WHERE symbol = :sym"),
                    {"ls": lot_size, "sym": symbol},
                )
                success += 1
                print(f"  {symbol}: {lot_size}")
            else:
                missing.append(symbol)
        await session.commit()

    print(f"\n✓ {success} lot sizes saved.")
    if missing:
        print(f"✗ {len(missing)} still missing: {missing[:20]}")

    # Summary
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM fo_underlying_catalog WHERE lot_size IS NOT NULL")
        )
        total = result.scalar()
        print(f"\nTotal underlyings with lot_size: {total}")


if __name__ == "__main__":
    asyncio.run(main())
