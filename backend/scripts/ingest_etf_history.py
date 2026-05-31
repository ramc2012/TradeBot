"""Ingest historical daily candles for the L1 asset-rotation ETFs.

Pulls ~3 years of daily bars for GOLDBEES / SILVERBEES / BBETF /
LIQUIDBEES from Upstox and upserts into `underlying_spot_candles` with
interval='day'. Once these rows land, `cbe_scanner.asset_rotation.
rank_asset_classes_live` lights up — the L1 stub clears and the engine
emits a real equity_exposure_pct.

Run inside the backend container:
    docker exec nomadcurie_backend python3 /app/scripts/ingest_etf_history.py

Auth path: ensure_upstox_session()  →  get_broker_token("upstox").
Endpoint: GET https://api.upstox.com/v2/historical-candle/{ik}/day/{to}/{from}
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

sys.path.insert(0, "/app")

from sqlalchemy import text

from db.database import AsyncSessionLocal


# Verified Upstox instrument keys for the NSE ETFs we use as asset-class proxies.
# Source: https://upstox.com/docs/api/instruments + manual cross-check.
# Symbols here are what we want stored in `underlying_spot_candles.underlying`.
ETF_TARGETS: list[dict[str, str]] = [
    {"underlying": "GOLDBEES",   "instrument_key": "NSE_EQ|INF204KB17I5", "trading_symbol": "GOLDBEES"},
    {"underlying": "SILVERBEES", "instrument_key": "NSE_EQ|INF204KB14I2", "trading_symbol": "SILVERBEES"},
    # Bharat Bond ETF April 2031 maturity series.
    {"underlying": "BBETF",      "instrument_key": "NSE_EQ|INF917L01EC7", "trading_symbol": "BBETF"},
    {"underlying": "LIQUIDBEES", "instrument_key": "NSE_EQ|INF204KA1AA2", "trading_symbol": "LIQUIDBEES"},
]


def _today() -> date:
    return datetime.now(timezone.utc).date()


async def discover_instrument_keys(targets: list[dict[str, str]], session) -> list[dict[str, str]]:
    """Verify each ETF's instrument_key by fuzzy-matching trading_symbol against
    the Upstox instrument master cached in the DB (if present), or trust the
    hardcoded keys when no master is available.
    """
    from sqlalchemy import text
    # Try the contract_catalog table — if it exists and has rows for these
    # trading_symbols, use its instrument_key. Otherwise fall back to hardcoded.
    enriched: list[dict[str, str]] = []
    for tgt in targets:
        ts = tgt["trading_symbol"]
        try:
            r = await session.execute(
                text(
                    """
                    SELECT instrument_key FROM fo_contract_catalog
                    WHERE trading_symbol ILIKE :ts
                    LIMIT 1
                    """
                ),
                {"ts": ts},
            )
            row = r.fetchone()
            if row and row[0]:
                enriched.append({**tgt, "instrument_key": row[0]})
                continue
        except Exception:
            pass
        enriched.append(tgt)
    return enriched


async def fetch_daily_history(
    instrument_key: str,
    *,
    token: str,
    days: int = 1100,
) -> list[list[Any]]:
    """Fetch up to `days` of daily candles from Upstox v2 historical-candle.

    Response shape: {"status":"success","data":{"candles":[[time, o, h, l, c, v, oi], ...]}}
    Upstox limits one call to ~1 year; we chunk by year-windows working backwards.
    """
    to_date = _today()
    end = to_date
    earliest = to_date - timedelta(days=days)
    all_rows: list[list[Any]] = []
    async with httpx.AsyncClient(timeout=40.0) as client:
        while end > earliest:
            start = max(earliest, end - timedelta(days=360))
            url = (
                f"https://api.upstox.com/v2/historical-candle/"
                f"{instrument_key}/day/{end.isoformat()}/{start.isoformat()}"
            )
            try:
                resp = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                )
            except Exception as exc:
                print(f"    network error: {exc}")
                break
            if resp.status_code != 200:
                print(f"    http {resp.status_code} {resp.text[:160]}")
                break
            try:
                payload = resp.json()
            except Exception:
                print(f"    bad json: {resp.text[:160]}")
                break
            candles = ((payload or {}).get("data") or {}).get("candles") or []
            all_rows.extend(candles)
            if len(candles) == 0:
                break
            # Slide window back to before the oldest bar we just received.
            try:
                oldest = candles[-1][0]
                oldest_date = datetime.fromisoformat(str(oldest).replace("Z", "+00:00")).date()
            except Exception:
                break
            new_end = oldest_date - timedelta(days=1)
            if new_end >= end:
                break
            end = new_end

    # Dedup + sort ascending.
    seen: set[str] = set()
    dedup: list[list[Any]] = []
    for row in all_rows:
        key = str(row[0])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(row)
    dedup.sort(key=lambda r: str(r[0]))
    return dedup


async def upsert_daily_candles(
    underlying: str,
    instrument_key: str,
    candles: list[list[Any]],
    session,
) -> int:
    """Upsert daily bars into underlying_spot_candles. Returns rows-written count."""
    from sqlalchemy import text
    if not candles:
        return 0
    rows = []
    for c in candles:
        try:
            ts_str = str(c[0])
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            rows.append(
                {
                    "time": ts,
                    "instrument_key": instrument_key,
                    "underlying": underlying,
                    "interval": "day",
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": int(float(c[5] or 0)),
                    "oi": int(float(c[6] or 0)) if len(c) > 6 else 0,
                    "source": "upstox_etf_backfill",
                }
            )
        except Exception:
            continue
    if not rows:
        return 0
    await session.execute(
        text(
            """
            INSERT INTO underlying_spot_candles
                (time, instrument_key, underlying, interval,
                 open, high, low, close, volume, oi, source)
            VALUES
                (:time, :instrument_key, :underlying, :interval,
                 :open, :high, :low, :close, :volume, :oi, :source)
            ON CONFLICT (instrument_key, "interval", "time") DO NOTHING
            """
        ),
        rows,
    )
    await session.commit()
    return len(rows)


async def main() -> None:
    # Auth — same path that the production app uses.
    from api.routers.auth import get_broker_token, load_persistent_credentials
    load_persistent_credentials()
    token = get_broker_token("upstox")
    if not token:
        print("ERROR: no upstox token. Run ensure_upstox_session first.")
        return
    print(f"upstox token prefix: {token[:10]}…  len={len(token)}")

    async with AsyncSessionLocal() as session:
        targets = await discover_instrument_keys(ETF_TARGETS, session)
        print(f"Targets resolved: {[{'u': t['underlying'], 'ik': t['instrument_key']} for t in targets]}")

        for tgt in targets:
            underlying = tgt["underlying"]
            ik = tgt["instrument_key"]
            print(f"\n=== {underlying} ({ik}) ===")
            candles = await fetch_daily_history(ik, token=token, days=1100)
            print(f"  fetched {len(candles)} candles")
            if not candles:
                continue
            written = await upsert_daily_candles(underlying, ik, candles, session)
            # Sanity — query back what we just stored.
            r = await session.execute(
                text(
                    """
                    SELECT count(*) AS bars,
                           MIN(time)::date AS earliest,
                           MAX(time)::date AS latest
                    FROM underlying_spot_candles
                    WHERE underlying = :u AND interval = 'day'
                    """
                ),
                {"u": underlying},
            )
            row = r.mappings().fetchone() or {}
            print(
                f"  wrote {written} | DB total: bars={row.get('bars')} "
                f"earliest={row.get('earliest')} latest={row.get('latest')}"
            )


if __name__ == "__main__":
    asyncio.run(main())
