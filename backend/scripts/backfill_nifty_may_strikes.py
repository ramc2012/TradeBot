"""Backfill NIFTY 2026-05-26 expiry option chain — strikes we're missing.

Uses Upstox's saved analytics token (works without a live trading session).
Pulls 30-min historical candles for both CE and PE across the strike range
that's currently absent from option_premium_candles.

Target strike list: 23850, 23900, 23950, 24000, 24100, 24150, 24200, 24250,
24300, 24350, 24400, 24450 — the ones we need to replay the 24200 PE move.

Run:
    docker compose exec backend python -m scripts.backfill_nifty_may_strikes
"""
from __future__ import annotations

import asyncio
import sys
import pathlib
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import httpx
from sqlalchemy import text

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from api.routers.auth import get_broker_token, refresh_persistent_credentials
from db.database import AsyncSessionLocal


EXPIRY = date(2026, 5, 26)
UNDERLYING_KEY = "NSE_INDEX|Nifty 50"
TARGET_STRIKES = [23850, 23900, 23950, 24000, 24100, 24150, 24200, 24250, 24300, 24350, 24400, 24450]
FROM_DATE = date(2026, 4, 20)
TO_DATE = date(2026, 5, 26)
HTTP_TIMEOUT = 30


async def fetch_option_chain(token: str) -> list[dict]:
    """Return one row per (strike) with both CE and PE instrument keys."""
    enc = quote(UNDERLYING_KEY, safe="")
    url = f"https://api.upstox.com/v2/option/chain?instrument_key={enc}&expiry_date={EXPIRY.isoformat()}"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    if r.status_code != 200:
        print(f"  ❌ option chain fetch failed: {r.status_code}  {r.text[:300]}")
        return []
    return r.json().get("data", [])


async def fetch_historical(token: str, instrument_key: str) -> list[list]:
    enc = quote(instrument_key, safe="")
    url = (
        f"https://api.upstox.com/v2/historical-candle/"
        f"{enc}/30minute/{TO_DATE.isoformat()}/{FROM_DATE.isoformat()}"
    )
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    if r.status_code != 200:
        print(f"    ⚠ historical fetch failed {r.status_code}: {r.text[:200]}")
        return []
    return r.json().get("data", {}).get("candles", [])


def _parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.fromisoformat(value[:19]).replace(tzinfo=timezone.utc)


async def persist(candles_raw: list[list], strike: float, opt_type: str, instrument_key: str) -> int:
    if not candles_raw:
        return 0
    n = 0
    async with AsyncSessionLocal() as session:
        for c in candles_raw:
            result = await session.execute(
                text("""
                    INSERT INTO option_premium_candles (
                        time, underlying, market, expiry, strike, option_type,
                        open, high, low, close, volume, oi,
                        instrument_key, interval, source, synced_at
                    ) VALUES (
                        :time, 'NIFTY', 'NSE', :expiry, :strike, :opt,
                        :o, :h, :l, :cl, :v, :oi,
                        :ikey, '30minute', 'upstox', now()
                    )
                    ON CONFLICT (instrument_key, interval, time) DO NOTHING
                """),
                {
                    "time": _parse_time(str(c[0])),
                    "expiry": EXPIRY,
                    "strike": strike,
                    "opt": opt_type,
                    "o": float(c[1]),
                    "h": float(c[2]),
                    "l": float(c[3]),
                    "cl": float(c[4]),
                    "v": int(c[5] or 0),
                    "oi": int(c[6] or 0) if len(c) > 6 and c[6] is not None else None,
                    "ikey": instrument_key,
                },
            )
            n += result.rowcount
        await session.commit()
    return n


async def main() -> None:
    print(f"Backfill plan: NIFTY {EXPIRY} option chain")
    print(f"  date range: {FROM_DATE} → {TO_DATE}")
    print(f"  target strikes: {TARGET_STRIKES}\n")

    # Refresh persistent credentials so the saved analytics token is loaded.
    refresh_persistent_credentials(force=True)
    token = get_broker_token("upstox")
    if not token:
        print("❌ No Upstox token available (analytics or active). Cannot proceed.")
        sys.exit(1)
    print(f"✓ Upstox token loaded ({len(token)} chars)")

    chain = await fetch_option_chain(token)
    if not chain:
        print("❌ Empty option chain — cannot resolve instrument keys.")
        sys.exit(1)
    print(f"✓ Fetched chain: {len(chain)} strike rows")

    # Build map: strike -> {"CE": instrument_key, "PE": instrument_key}
    chain_map: dict[int, dict[str, str]] = {}
    for row in chain:
        sk = int(round(float(row.get("strike_price", 0))))
        ce = row.get("call_options", {}).get("instrument_key", "")
        pe = row.get("put_options", {}).get("instrument_key", "")
        if sk:
            chain_map[sk] = {"CE": ce, "PE": pe}

    # Find any missing target strikes from the chain
    missing_from_chain = [s for s in TARGET_STRIKES if s not in chain_map]
    if missing_from_chain:
        print(f"  ⚠ chain does not contain strikes: {missing_from_chain}")
    available_strikes = [s for s in TARGET_STRIKES if s in chain_map]
    print(f"  resolved {len(available_strikes)} target strikes in chain")

    total_rows = 0
    failures = []
    for strike in available_strikes:
        for opt in ("CE", "PE"):
            ikey = chain_map[strike].get(opt, "")
            if not ikey:
                failures.append((strike, opt, "no instrument_key"))
                continue
            print(f"  fetch {strike}{opt}  key={ikey}")
            candles = await fetch_historical(token, ikey)
            if not candles:
                failures.append((strike, opt, "no candles returned"))
                continue
            n = await persist(candles, strike, opt, ikey)
            print(f"    ✓ {len(candles)} fetched, {n} inserted")
            total_rows += n

    print(f"\n=== DONE ===")
    print(f"  rows inserted: {total_rows}")
    if failures:
        print(f"  failures: {len(failures)}")
        for s, o, why in failures[:10]:
            print(f"    {s}{o}: {why}")

    # Confirm new coverage
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text("""
            SELECT strike::int, option_type, COUNT(*) AS bars,
                   MIN(time)::date AS first, MAX(time)::date AS last
            FROM option_premium_candles
            WHERE underlying='NIFTY' AND expiry=:e AND interval='30minute'
              AND strike = ANY(:strikes)
            GROUP BY 1, 2
            ORDER BY 1, 2
        """), {"e": EXPIRY, "strikes": TARGET_STRIKES})).mappings().all()
    print("\n=== POST-BACKFILL COVERAGE FOR TARGET STRIKES ===")
    for r in rows:
        print(f"  {r['strike']:>5}{r['option_type']}  bars={r['bars']:>4}  {r['first']} → {r['last']}")


if __name__ == "__main__":
    asyncio.run(main())
