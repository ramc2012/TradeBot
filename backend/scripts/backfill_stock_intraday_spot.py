"""Backfill intraday stock spot candles into underlying_spot_candles via Fyers.

Companion to backfill_stock_daily_ohlc.py (same adapter, same upsert contract)
but for intraday resolutions. Exists because the only LIVE writer of 30-minute
stock spot is data/upstox_research_sync.py with spot_limit=25 — it covers 25 of
~209 F&O names per pass, so most of the stock universe has no 30m tape on any
given session (07-17: 20 of 209 names present).

Rows are written under the catalog's canonical spot_instrument_key (NSE_EQ|INE…)
so they merge with the existing upstox_spot / live_tick rows rather than
forking a second Fyers-symbol keyspace.

    docker exec nomadcurie_backend python scripts/backfill_stock_intraday_spot.py \
        --interval 30minute --from 2026-07-15 --to 2026-07-17
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loguru import logger  # noqa: E402
from sqlalchemy import text  # noqa: E402

from api.routers.auth import ensure_fyers_session, get_active_adapter  # noqa: E402
from brokers.rate_limiter import CLASS_BULK, broker_class  # noqa: E402
from db.database import AsyncSessionLocal  # noqa: E402

SOURCE = "fyers"
RESOLUTION = {"1minute": "1", "3minute": "3", "5minute": "5", "15minute": "15", "30minute": "30"}
RATE_LIMIT_SLEEP = 0.35  # ~3 req/s — well inside Fyers 10/s + 200/min


def _parse_date(v: str) -> date:
    return datetime.strptime(v, "%Y-%m-%d").date()


async def _universe(kind: str) -> list[tuple[str, str, str]]:
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text(
            """
            SELECT symbol, spot_instrument_key
            FROM fo_underlying_catalog
            WHERE kind = :kind
              AND spot_instrument_key IS NOT NULL AND spot_instrument_key <> ''
            ORDER BY symbol
            """
        ), {"kind": kind})).fetchall()
    out = []
    for sym, key in rows:
        key = str(key or "").strip()
        if not key:
            continue
        if key.startswith(("NSE:", "BSE:")):
            fy = key
        else:
            fy = f"NSE:{sym}-EQ"
        out.append((sym, key, fy))
    return out


async def _upsert(symbol: str, instrument_key: str, interval: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    async with AsyncSessionLocal() as session:
        await session.execute(text(
            """
            INSERT INTO underlying_spot_candles
                (time, instrument_key, underlying, interval, open, high, low, close, volume, oi, source)
            VALUES (:time, :instrument_key, :underlying, :interval, :open, :high, :low, :close, :volume, 0, :source)
            ON CONFLICT (instrument_key, interval, "time") DO NOTHING
            """
        ), [
            {
                "time": r["time"], "instrument_key": instrument_key, "underlying": symbol,
                "interval": interval, "open": r["open"], "high": r["high"], "low": r["low"],
                "close": r["close"], "volume": r["volume"], "source": SOURCE,
            }
            for r in rows
        ])
        await session.commit()
    return len(rows)


async def _count(interval: str, frm: date, to: date, kind: str) -> tuple[int, int]:
    async with AsyncSessionLocal() as session:
        row = (await session.execute(text(
            """
            SELECT count(*) AS rows, count(DISTINCT s.underlying) AS names
            FROM underlying_spot_candles s
            JOIN fo_underlying_catalog c ON c.symbol = s.underlying AND c.kind = :kind
            WHERE s.interval = :interval AND s.time >= :frm AND s.time < :to
            """
        ), {"interval": interval, "frm": frm, "to": to + timedelta(days=1), "kind": kind})).one()
    return int(row.rows), int(row.names)


async def _amain(a: argparse.Namespace) -> int:
    resolution = RESOLUTION.get(a.interval)
    if not resolution:
        raise SystemExit(f"unsupported interval {a.interval}")

    universe = await _universe(a.kind)
    if a.symbols:
        wanted = {s.upper() for s in a.symbols}
        universe = [u for u in universe if u[0].upper() in wanted]
    if a.limit:
        universe = universe[: a.limit]

    adapter = get_active_adapter("fyers")
    if adapter is None:
        if not await ensure_fyers_session(force_validate=False):
            raise SystemExit("No live Fyers session")
        adapter = get_active_adapter("fyers")
    if adapter is None:
        raise SystemExit("Fyers adapter unavailable")

    before_rows, before_names = await _count(a.interval, a.from_date, a.to_date, a.kind)
    print(f"[{a.interval}] {a.from_date}..{a.to_date} {a.kind}: "
          f"before={before_rows} rows / {before_names} names; {len(universe)} symbols to fetch")

    ok = failed = total = 0
    failures: list[str] = []
    start = time.time()
    for idx, (symbol, key, fy) in enumerate(universe, start=1):
        try:
            with broker_class(CLASS_BULK):
                raw = await adapter.get_historical_candles(
                    symbol=fy, resolution=resolution,
                    range_from=a.from_date.isoformat(), range_to=a.to_date.isoformat(),
                )
            rows = []
            for r in raw or []:
                try:
                    rows.append({
                        "time": datetime.fromisoformat(str(r["time"]).replace("Z", "+00:00")),
                        "open": float(r["open"]), "high": float(r["high"]),
                        "low": float(r["low"]), "close": float(r["close"]),
                        "volume": int(r.get("volume") or 0),
                    })
                except (TypeError, ValueError, KeyError):
                    continue
            total += await _upsert(symbol, key, a.interval, rows)
            ok += 1
        except Exception as exc:  # one bad symbol never aborts the run
            failed += 1
            failures.append(f"{symbol} ({fy}): {type(exc).__name__}: {exc}")
            logger.warning(f"  [{idx}/{len(universe)}] {symbol} FAILED: {exc}")
        if idx % 50 == 0:
            print(f"  ...{idx}/{len(universe)} ok={ok} failed={failed} "
                  f"fetched={total} elapsed={time.time() - start:.0f}s", flush=True)
        await asyncio.sleep(RATE_LIMIT_SLEEP)

    after_rows, after_names = await _count(a.interval, a.from_date, a.to_date, a.kind)
    print(f"[{a.interval}] after={after_rows} rows / {after_names} names  "
          f"(+{after_rows - before_rows} rows, +{after_names - before_names} names)")
    print(f"  symbols ok={ok} failed={failed} broker_rows={total} elapsed={time.time() - start:.0f}s")
    for f in failures[:25]:
        print(f"  FAIL {f}")
    if len(failures) > 25:
        print(f"  ... and {len(failures) - 25} more failures")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--interval", default="30minute")
    p.add_argument("--from", dest="from_date", type=_parse_date, required=True)
    p.add_argument("--to", dest="to_date", type=_parse_date, required=True)
    p.add_argument("--kind", default="STOCK", help="fo_underlying_catalog.kind (STOCK / INDEX)")
    p.add_argument("--symbols", nargs="*", default=None)
    p.add_argument("--limit", type=int, default=0)
    return asyncio.run(_amain(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
