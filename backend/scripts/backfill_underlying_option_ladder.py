"""Ladder backfill for option_premium_candles for ONE underlying.

Companion to ``backfill_session_premium_gaps.py``. That script derives its
contract universe from rows that already exist in ``option_premium_candles``,
so it cannot repair an underlying whose rows were *quarantined* (there is
nothing left to seed the universe from) or whose Upstox contract catalog is
empty.

This driver instead builds the strike ladder deterministically around the
underlying's own spot and re-drives the SAME OptionHistoryService broker
primitives (``_fetch_broker_candles`` / ``_persist_broker_candles``) that
``load_candles(allow_broker_refresh=True)`` uses, under CLASS_BULK.

Written for the 2026-07-20 M&M/MARUTI option-store quarantine.

    docker exec nomadcurie_backend python scripts/backfill_underlying_option_ladder.py \
        --underlying M&M --expiry 2026-07-28 \
        --from-date 2026-07-15 --to-date 2026-07-17 \
        --step 20 --span 6 --interval 30minute
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from brokers.rate_limiter import CLASS_BULK, broker_class  # noqa: E402
from db.database import AsyncSessionLocal  # noqa: E402
from market_data.option_history import OptionHistoryService, interval_minutes  # noqa: E402
from market_data.option_subscription_manager import (  # noqa: E402
    _build_fyers_monthly_option_symbol,
)


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


async def _spot(underlying: str, on: date) -> float | None:
    """External anchor: the underlying's OWN spot, broker-sourced only."""
    async with AsyncSessionLocal() as session:
        row = (await session.execute(text(
            """
            SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY close) AS px
            FROM underlying_spot_candles
            WHERE underlying = :u AND time >= :d AND time < :nd
              AND source <> 'live_tick' AND close > 0
            """
        ), {"u": underlying, "d": on, "nd": on + timedelta(days=1)})).one()
    return float(row.px) if row.px is not None else None


async def _count(underlying: str, d0: date, d1: date, interval: str) -> int:
    async with AsyncSessionLocal() as session:
        row = (await session.execute(text(
            """
            SELECT count(*) AS n FROM option_premium_candles
            WHERE underlying = :u AND interval = :i
              AND time >= :d0 AND time < :d1
            """
        ), {"u": underlying, "i": interval, "d0": d0, "d1": d1 + timedelta(days=1)})).one()
    return int(row.n)


async def _one(svc: OptionHistoryService, *, underlying: str, expiry: date,
               strike: float, option_type: str, key: str, interval: str,
               from_date: date, to_date: date) -> tuple[str, int]:
    agg = interval_minutes(interval) if interval in {"3minute", "5minute", "15minute"} else None
    with broker_class(CLASS_BULK):
        if agg is not None:
            rows = await svc._fetch_broker_candles(
                instrument_key=key, from_date=from_date, to_date=to_date, interval="1minute",
            )
            rows = svc._aggregate_rows(rows, agg) if rows else []
        else:
            rows = await svc._fetch_broker_candles(
                instrument_key=key, from_date=from_date, to_date=to_date, interval=interval,
            )
    if not rows:
        return "empty", 0
    await svc._persist_broker_candles(
        rows=rows,
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        instrument_key=key,
        interval=interval,
        already_in_db=set(),
    )
    return "ok", len(rows)


async def _amain(a: argparse.Namespace) -> int:
    spot = a.spot or await _spot(a.underlying, a.to_date)
    if not spot:
        print(f"NO SPOT ANCHOR for {a.underlying} on {a.to_date} — refusing to guess a ladder.")
        return 2
    atm = round(spot / a.step) * a.step
    strikes = [atm + i * a.step for i in range(-a.span, a.span + 1)]
    print(f"{a.underlying} spot~{spot:.2f} atm={atm} ladder={strikes[0]}..{strikes[-1]} step={a.step}")

    legs: list[tuple[float, str, str]] = []
    for k in strikes:
        for ot in ("CE", "PE"):
            sym = _build_fyers_monthly_option_symbol(a.underlying, a.expiry.isoformat(), k, ot)
            if sym:
                legs.append((float(k), ot, sym))

    before = await _count(a.underlying, a.from_date, a.to_date, a.interval)
    print(f"[{a.interval}] before={before} rows over {a.from_date}..{a.to_date}; {len(legs)} legs")

    sem = asyncio.Semaphore(a.concurrency)
    stats: dict[str, int] = defaultdict(int)
    failures: list[str] = []

    async def run(strike: float, ot: str, key: str) -> None:
        async with sem:
            try:
                status, n = await _one(
                    svc, underlying=a.underlying, expiry=a.expiry, strike=strike,
                    option_type=ot, key=key, interval=a.interval,
                    from_date=a.from_date, to_date=a.to_date + timedelta(days=1),
                )
                stats[status] += 1
                stats["broker_rows"] += n
            except Exception as exc:  # noqa: BLE001 - never abort for one leg
                stats["error"] += 1
                failures.append(f"{key}: {type(exc).__name__}: {exc}")

    svc = OptionHistoryService()
    await asyncio.gather(*(run(s, ot, k) for s, ot, k in legs))

    after = await _count(a.underlying, a.from_date, a.to_date, a.interval)
    print(f"[{a.interval}] after={after} rows (+{after - before})  "
          f"ok={stats['ok']} empty={stats['empty']} error={stats['error']} "
          f"broker_rows={stats['broker_rows']}")
    for f in failures[:20]:
        print(f"  FAIL {f}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--underlying", required=True)
    p.add_argument("--expiry", type=_parse_date, required=True)
    p.add_argument("--from-date", type=_parse_date, required=True, dest="from_date")
    p.add_argument("--to-date", type=_parse_date, required=True, dest="to_date")
    p.add_argument("--interval", default="30minute")
    p.add_argument("--step", type=float, default=20.0, help="strike step for the ladder")
    p.add_argument("--span", type=int, default=6, help="strikes each side of ATM")
    p.add_argument("--spot", type=float, default=None,
                   help="override the spot anchor (default: median non-live_tick spot close)")
    p.add_argument("--concurrency", type=int, default=3)
    return asyncio.run(_amain(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
