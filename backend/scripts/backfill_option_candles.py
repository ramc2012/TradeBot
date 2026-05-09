"""
One-time script: fetch 90-day 30-min historical candles for NIFTY and
BANKNIFTY ATM options from Upstox and persist them into option_premium_candles.

Run inside the backend Docker container:
    docker exec nomadcurie_backend python scripts/backfill_option_candles.py

Strategy
--------
1. For NIFTY — use the known ATM CE/PE instrument keys from the live ATM watchlist
2. For BANKNIFTY — try weekly then monthly option chain to discover ATM keys
3. Fetch 90-day 30-min historical candles via Upstox historical-candle API
4. Upsert into option_premium_candles (no duplicates)
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import httpx

import os, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from api.routers.auth import get_broker_token, ensure_upstox_session
from db.database import AsyncSessionLocal
from sqlalchemy import text


LOOKBACK_DAYS = 90

# Known NIFTY ATM contracts from live ATM watchlist (as of 2026-04-06)
KNOWN_CONTRACTS: list[dict] = [
    {
        "underlying": "NIFTY",
        "expiry": date(2026, 4, 7),
        "strike": 22700.0,
        "option_type": "CE",
        "instrument_key": "NSE_FO|40742",
    },
    {
        "underlying": "NIFTY",
        "expiry": date(2026, 4, 7),
        "strike": 22700.0,
        "option_type": "PE",
        "instrument_key": "NSE_FO|40745",
    },
]

# BANKNIFTY will be discovered at runtime via option chain
BANKNIFTY_META = {
    "symbol": "BANKNIFTY",
    "underlying_key": "NSE_INDEX|Nifty Bank",
    "weekly_expiry": date(2026, 4, 9),   # nearest Wednesday
    "monthly_expiry": date(2026, 4, 23),
    "strike_step": 100,
}
NIFTY_META = {
    "symbol": "NIFTY",
    "underlying_key": "NSE_INDEX|Nifty 50",
    "weekly_expiry": date(2026, 4, 7),
    "monthly_expiry": date(2026, 4, 23),
    "strike_step": 50,
}


async def get_upstox_token() -> str | None:
    token = get_broker_token("upstox")
    if not token:
        print("  → No Upstox token in memory; attempting session restore...")
        if await ensure_upstox_session():
            token = get_broker_token("upstox")
    return token


async def get_spot_price(token: str, instrument_key: str) -> float | None:
    encoded = quote(instrument_key, safe="")
    url = f"https://api.upstox.com/v2/market-quote/ltp?instrument_key={encoded}"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    if r.status_code != 200:
        return None
    data = r.json().get("data", {})
    for v in data.values():
        return float(v.get("last_price") or 0) or None
    return None


async def discover_atm_contracts(
    token: str,
    meta: dict,
    spot: float,
) -> list[dict]:
    """Try weekly expiry first, then monthly to find ATM CE/PE instrument keys."""
    for expiry in [meta["weekly_expiry"], meta["monthly_expiry"]]:
        contracts = await _fetch_chain_atm(token, meta["underlying_key"], expiry, spot, meta["strike_step"])
        if contracts:
            print(f"  → Found ATM contracts for expiry {expiry}")
            return [
                {**c, "underlying": meta["symbol"]}
                for c in contracts
            ]
    return []


async def _fetch_chain_atm(
    token: str,
    underlying_key: str,
    expiry: date,
    spot: float,
    strike_step: int,
) -> list[dict]:
    encoded = quote(underlying_key, safe="")
    url = f"https://api.upstox.com/v2/option/chain?instrument_key={encoded}&expiry_date={expiry.isoformat()}"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    if r.status_code != 200:
        return []
    chain = r.json().get("data", [])
    if not chain:
        return []
    best = min(chain, key=lambda x: abs(float(x.get("strike_price", 0)) - spot))
    results = []
    for side_key, opt_type in [("call_options", "CE"), ("put_options", "PE")]:
        side = best.get(side_key, {})
        ikey = side.get("instrument_key", "")
        if ikey:
            results.append({
                "option_type": opt_type,
                "strike": float(best["strike_price"]),
                "expiry": expiry,
                "instrument_key": ikey,
            })
    return results


async def fetch_historical(
    token: str,
    instrument_key: str,
    from_date: date,
    to_date: date,
) -> list[dict]:
    encoded = quote(instrument_key, safe="")
    url = (
        f"https://api.upstox.com/v2/historical-candle/"
        f"{encoded}/30minute/{to_date.isoformat()}/{from_date.isoformat()}"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    if r.status_code != 200:
        print(f"    ⚠ Historical fetch failed {r.status_code}: {r.text[:300]}")
        return []
    candles = r.json().get("data", {}).get("candles", [])
    rows = []
    for c in reversed(candles):
        rows.append({
            "time": str(c[0]),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": int(c[5] or 0),
            "oi": int(c[6] or 0) if len(c) > 6 and c[6] is not None else None,
        })
    return rows


def _parse_time(value: str) -> datetime:
    """Parse an ISO timestamp string (possibly with timezone) to datetime."""
    # Python 3.11+ fromisoformat handles '+05:30' offsets
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        # Fallback: strip timezone and treat as UTC
        return datetime.fromisoformat(value[:19]).replace(tzinfo=timezone.utc)


async def persist_candles(
    rows: list[dict],
    underlying: str,
    expiry: date,
    strike: float,
    option_type: str,
    instrument_key: str,
) -> int:
    if not rows:
        return 0
    inserted = 0
    async with AsyncSessionLocal() as session:
        for r in rows:
            result = await session.execute(
                text(
                    """
                    INSERT INTO option_premium_candles (
                        time, underlying, market, expiry, strike, option_type,
                        open, high, low, close, volume, oi,
                        instrument_key, interval, source, synced_at
                    ) VALUES (
                        :time, :underlying, 'NSE', :expiry, :strike, :option_type,
                        :open, :high, :low, :close, :volume, :oi,
                        :instrument_key, '30minute', 'upstox', now()
                    )
                    ON CONFLICT (instrument_key, interval, time) DO NOTHING
                    """
                ),
                {
                    "time": _parse_time(r["time"]),
                    "underlying": underlying,
                    "expiry": expiry,
                    "strike": strike,
                    "option_type": option_type,
                    "open": r["open"],
                    "high": r["high"],
                    "low": r["low"],
                    "close": r["close"],
                    "volume": r["volume"],
                    "oi": r.get("oi"),
                    "instrument_key": instrument_key,
                },
            )
            inserted += result.rowcount
        await session.commit()
    return inserted


async def process_contract(token: str, c: dict, from_date: date, to_date: date) -> int:
    underlying = c["underlying"]
    expiry = c["expiry"]
    strike = c["strike"]
    opt_type = c["option_type"]
    ikey = c["instrument_key"]
    print(f"  {underlying} {strike} {opt_type} [{expiry}] key={ikey}")
    candles = await fetch_historical(token, ikey, from_date, to_date)
    print(f"    → {len(candles)} candles fetched from Upstox")
    if not candles:
        return 0
    n = await persist_candles(candles, underlying, expiry, strike, opt_type, ikey)
    print(f"    ✓ {n} rows inserted into option_premium_candles")
    return n


async def main() -> None:
    print("=" * 60)
    print("Option Candles Backfill — NIFTY + BANKNIFTY")
    print("=" * 60)

    token = await get_upstox_token()
    if not token:
        print("❌ Could not obtain Upstox token. Exiting.")
        sys.exit(1)
    print(f"✓ Upstox token ready ({len(token)} chars)")

    from_date = date.today() - timedelta(days=LOOKBACK_DAYS)
    to_date = date.today()
    print(f"  Date range: {from_date} → {to_date}\n")

    all_contracts = list(KNOWN_CONTRACTS)

    # Discover BANKNIFTY ATM
    print("── BANKNIFTY spot lookup ──")
    bnf_spot = await get_spot_price(token, BANKNIFTY_META["underlying_key"])
    if bnf_spot:
        print(f"  BANKNIFTY spot: {bnf_spot:.2f}")
        bnf_contracts = await discover_atm_contracts(token, BANKNIFTY_META, bnf_spot)
        all_contracts.extend(bnf_contracts)
        if not bnf_contracts:
            print("  ⚠ Could not discover BANKNIFTY ATM contracts (market closed?)")
            # Estimate ATM and try direct historical fetch using NSE_FO pattern
            atm = round(bnf_spot / BANKNIFTY_META["strike_step"]) * BANKNIFTY_META["strike_step"]
            print(f"  Estimated BANKNIFTY ATM: {atm} — will try with known expiry")
    else:
        print("  ⚠ BANKNIFTY spot unavailable")

    # Also discover NIFTY monthly expiry contracts
    print("\n── NIFTY monthly ATM lookup ──")
    nifty_spot = await get_spot_price(token, NIFTY_META["underlying_key"])
    if nifty_spot:
        print(f"  NIFTY spot: {nifty_spot:.2f}")
        monthly_contracts = await discover_atm_contracts(token, NIFTY_META, nifty_spot)
        if monthly_contracts:
            # Only add monthly contracts that have a different expiry than weekly
            for mc in monthly_contracts:
                if not any(
                    c["instrument_key"] == mc["instrument_key"] for c in all_contracts
                ):
                    all_contracts.append(mc)
            print(f"  Added {len(monthly_contracts)} NIFTY monthly contracts")
        else:
            print("  ⚠ NIFTY monthly ATM not found via option chain")

    print(f"\n── Processing {len(all_contracts)} contracts ──")
    total = 0
    for c in all_contracts:
        total += await process_contract(token, c, from_date, to_date)

    print()
    print(f"✓ Backfill complete — {total} total rows inserted")

    # DB summary
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT underlying, option_type, COUNT(*) as cnt "
                "FROM option_premium_candles "
                "GROUP BY underlying, option_type ORDER BY underlying, option_type"
            )
        )
        rows = result.fetchall()
        if rows:
            print("\nDB option_premium_candles summary:")
            for row in rows:
                print(f"  {row.underlying} {row.option_type}: {row.cnt} rows")
        else:
            print("\nDB: option_premium_candles still empty.")


if __name__ == "__main__":
    asyncio.run(main())
