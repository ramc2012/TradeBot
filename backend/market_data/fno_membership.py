"""Refresh active F&O membership without deleting held/retired catalog records."""
import asyncio
from datetime import date, datetime, timezone
from sqlalchemy import text
from db.database import AsyncSessionLocal
from market_data.instrument_master import load_master


def active_stocks(master, today):
    equities = {r.get("trading_symbol"): r for r in master if r.get("segment") == "NSE_EQ" and r.get("instrument_type") == "EQ"}
    contracts = {}
    for r in master:
        name = r.get("underlying_symbol")
        if r.get("segment") != "NSE_FO" or r.get("instrument_type") not in {"FUT", "CE", "PE"} or name not in equities:
            continue
        try:
            expiry = datetime.fromtimestamp(float(r["expiry"]) / 1000, timezone.utc).date()
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
        if expiry < today or int(r.get("lot_size") or 0) <= 0:
            continue
        if name not in contracts or expiry < contracts[name][0]:
            contracts[name] = (expiry, r)
    return [{"symbol": name, "key": equities[name]["instrument_key"], "lot": int(r["lot_size"])}
            for name, (_, r) in sorted(contracts.items())]


async def sync_membership(force=False):
    async with AsyncSessionLocal() as session:
        stamp = (await session.execute(text("SELECT max(fno_snapshot_at) FROM fo_underlying_catalog"))).scalar()
        if not force and stamp and (datetime.now(timezone.utc) - stamp).total_seconds() < 21600:
            return {"status": "cached"}
    rows = active_stocks(await asyncio.to_thread(load_master), date.today())
    if len(rows) < 150:
        raise RuntimeError("Incomplete F&O membership; preserving previous universe")
    async with AsyncSessionLocal() as session:
        # Transactional membership swap. Existing instrument mappings are only
        # updated when verified from the EQ segment; books retain exact contracts.
        await session.execute(text("UPDATE fo_underlying_catalog SET fno_active=false WHERE kind='STOCK'"))
        await session.execute(text("""
            INSERT INTO fo_underlying_catalog(symbol,kind,spot_instrument_key,underlying_key,lot_size,fno_active,fno_snapshot_at)
            VALUES (:symbol,'STOCK',:key,:key,:lot,true,now())
            ON CONFLICT(symbol) DO UPDATE SET spot_instrument_key=EXCLUDED.spot_instrument_key,
              underlying_key=EXCLUDED.underlying_key,lot_size=EXCLUDED.lot_size,
              fno_active=true,fno_snapshot_at=now(),updated_at=now()
        """), rows)
        await session.commit()
    return {"status": "refreshed", "active_stocks": len(rows)}


async def run_membership_loop():
    """Core owns the periodic refresh; consumers only read the catalog."""
    from loguru import logger
    while True:
        try:
            result = await sync_membership()
            logger.info(f"[fno-membership] {result}")
        except Exception as exc:
            logger.warning(f"[fno-membership] refresh deferred: {exc}")
        await asyncio.sleep(21600)
