"""Backfill NIFTY data for Jan-May 2026: spot + monthly option chains.

Backfills:
  1. NIFTY 1-min + 30-min spot (Jan 1 → May 31)
  2. Monthly option chain expiries:
        Jan 29 (monthly)
        Feb 26 (monthly)
        Mar 26 (monthly) -- if absent
        Apr 28 (already exists)
        May 26 (already exists)
     Strikes: ±500 around the rough NIFTY level for that month.

Run:
    docker compose exec backend python -m scripts.backfill_nifty_jan_to_may
"""
from __future__ import annotations

import asyncio
import sys, pathlib
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import httpx
from sqlalchemy import text

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from api.routers.auth import get_broker_token, refresh_persistent_credentials
from db.database import AsyncSessionLocal


HTTP_TIMEOUT = 30
SPOT_KEY = "NSE_INDEX|Nifty 50"

# Expiries to backfill. NIFTY level estimates from chart context:
#   Jan 2026 NIFTY ≈ 25800-26200 → ATM strikes 25500-26500
#   Feb 2026 NIFTY ≈ 24500-25200 → ATM strikes 24500-25500
#   Mar 2026 NIFTY ≈ 23500-25000 → ATM strikes 23500-25000
EXPIRY_PLAN = [
    {"expiry": date(2026, 1, 29), "strike_min": 25500, "strike_max": 26500, "step": 50},
    {"expiry": date(2026, 2, 26), "strike_min": 24500, "strike_max": 25500, "step": 50},
    {"expiry": date(2026, 3, 26), "strike_min": 23500, "strike_max": 25000, "step": 50},
]


def _parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.fromisoformat(value[:19]).replace(tzinfo=timezone.utc)


async def fetch_chain(token: str, expiry: date) -> list[dict]:
    enc = quote(SPOT_KEY, safe="")
    url = f"https://api.upstox.com/v2/option/chain?instrument_key={enc}&expiry_date={expiry.isoformat()}"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as c:
        r = await c.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    if r.status_code != 200:
        print(f"  ❌ option chain {expiry}: {r.status_code} {r.text[:200]}")
        return []
    return r.json().get("data", [])


async def fetch_hist(token: str, ikey: str, iv: str, from_d: date, to_d: date) -> list[list]:
    enc = quote(ikey, safe="")
    url = f"https://api.upstox.com/v2/historical-candle/{enc}/{iv}/{to_d.isoformat()}/{from_d.isoformat()}"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as c:
        r = await c.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    if r.status_code != 200:
        return []
    return r.json().get("data", {}).get("candles", [])


async def persist_spot(candles: list[list], iv: str) -> int:
    if not candles:
        return 0
    n = 0
    async with AsyncSessionLocal() as session:
        for c in candles:
            result = await session.execute(text("""
                INSERT INTO underlying_spot_candles (
                    time, underlying, instrument_key, interval, open, high, low, close, volume, source, synced_at
                ) VALUES (
                    :time, 'NIFTY', :ikey, :iv, :o, :h, :l, :cl, :v, 'upstox', now()
                )
                ON CONFLICT (instrument_key, interval, time) DO NOTHING
            """), {
                "time": _parse_time(str(c[0])),
                "ikey": SPOT_KEY,
                "iv": iv,
                "o": float(c[1]), "h": float(c[2]), "l": float(c[3]), "cl": float(c[4]),
                "v": int(c[5] or 0),
            })
            n += result.rowcount
        await session.commit()
    return n


async def persist_option(candles: list[list], expiry: date, strike: float, opt: str, ikey: str) -> int:
    if not candles:
        return 0
    n = 0
    async with AsyncSessionLocal() as session:
        for c in candles:
            result = await session.execute(text("""
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
            """), {
                "time": _parse_time(str(c[0])),
                "expiry": expiry, "strike": strike, "opt": opt,
                "o": float(c[1]), "h": float(c[2]), "l": float(c[3]), "cl": float(c[4]),
                "v": int(c[5] or 0),
                "oi": int(c[6] or 0) if len(c) > 6 and c[6] is not None else None,
                "ikey": ikey,
            })
            n += result.rowcount
        await session.commit()
    return n


async def main() -> None:
    refresh_persistent_credentials(force=True)
    token = get_broker_token("upstox", allow_analytics_token=False)
    if not token:
        print("❌ No Upstox token")
        sys.exit(1)
    print(f"✓ token loaded ({len(token)} chars)")

    # ── Spot backfill ─────────────────────────────────────────────────────
    print("\n=== SPOT BACKFILL ===")
    for iv in ("30minute", "1minute"):
        for from_d, to_d in [
            (date(2026, 1, 1), date(2026, 2, 28)),
            (date(2026, 3, 1), date(2026, 4, 30)),
        ]:
            print(f"  fetching NIFTY {iv} {from_d} → {to_d}")
            candles = await fetch_hist(token, SPOT_KEY, iv, from_d, to_d)
            n = await persist_spot(candles, iv)
            print(f"    {len(candles)} returned, {n} inserted")

    # ── Option chain backfill ─────────────────────────────────────────────
    for plan in EXPIRY_PLAN:
        expiry = plan["expiry"]
        print(f"\n=== EXPIRY {expiry} ===")
        chain = await fetch_chain(token, expiry)
        if not chain:
            print(f"  ⚠ chain empty for {expiry}; skipping")
            continue
        chain_map = {int(round(float(row.get("strike_price", 0)))): row for row in chain}
        wanted = [s for s in range(plan["strike_min"], plan["strike_max"] + 1, plan["step"]) if s in chain_map]
        print(f"  {len(wanted)} strikes match plan range")
        # Date window: from 60 days before expiry to expiry day
        from_d = expiry - timedelta(days=60)
        to_d = expiry
        for strike in wanted:
            row = chain_map[strike]
            for opt, side_key in (("CE", "call_options"), ("PE", "put_options")):
                ikey = row.get(side_key, {}).get("instrument_key", "")
                if not ikey:
                    continue
                candles = await fetch_hist(token, ikey, "30minute", from_d, to_d)
                if not candles:
                    continue
                n = await persist_option(candles, expiry, float(strike), opt, ikey)
                print(f"    {strike}{opt}: {len(candles)} fetched, {n} inserted")

    # ── Report
    async with AsyncSessionLocal() as session:
        print("\n=== POST-BACKFILL COVERAGE ===")
        r = (await session.execute(text("""
            SELECT interval, MIN(time)::date AS first, MAX(time)::date AS last, COUNT(*) AS bars
            FROM underlying_spot_candles
            WHERE underlying='NIFTY' AND instrument_key=:k AND close>10000
            GROUP BY 1 ORDER BY 1
        """), {"k": SPOT_KEY})).mappings().all()
        for row in r:
            print(f"  spot {row['interval']}: {row['first']} → {row['last']} ({row['bars']} bars)")
        r2 = (await session.execute(text("""
            SELECT expiry, COUNT(DISTINCT strike) AS strikes, COUNT(*) AS bars,
                   MIN(time)::date AS first, MAX(time)::date AS last
            FROM option_premium_candles
            WHERE underlying='NIFTY' AND interval='30minute' AND expiry <= '2026-05-31'
            GROUP BY 1 ORDER BY 1
        """))).mappings().all()
        for row in r2:
            print(f"  opt {row['expiry']}: {row['strikes']} strikes, {row['bars']} bars ({row['first']} → {row['last']})")


if __name__ == "__main__":
    asyncio.run(main())
