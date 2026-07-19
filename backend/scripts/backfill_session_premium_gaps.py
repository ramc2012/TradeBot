"""Session-gap backfill for option_premium_candles.

Re-drives the EXISTING OptionHistoryService broker-fetch + persist primitives
(`_fetch_broker_candles` / `_persist_broker_candles` — the same path
`load_candles(allow_broker_refresh=True)` uses) for every contract that is
under-covered on a given session date. Written for the 2026-07-17 Upstox
chain-400 storm, which collapsed the S1 ATM watchlist from ~112 underlyings
to 5 and left option premium candles ~59% (30minute) / ~43% (3minute) short.

Everything runs under CLASS_BULK so it is hard-capped at 25% of the broker
budget and yields to any live CRITICAL work.

    docker exec nomadcurie_backend python scripts/backfill_session_premium_gaps.py \
        --date 2026-07-17 --interval 30minute --expected 13
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loguru import logger  # noqa: E402
from sqlalchemy import text  # noqa: E402

from brokers.rate_limiter import CLASS_BULK, broker_class  # noqa: E402
from db.database import AsyncSessionLocal  # noqa: E402
from market_data.option_history import OptionHistoryService, interval_minutes  # noqa: E402


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


async def _targets(target: date, interval: str, expected: int, ref_days: int,
                   ref_interval: str | None = None) -> list[dict]:
    """Contracts seen in the reference window that are short on `target`."""
    ref_from = target - timedelta(days=ref_days)
    ref_clause = "AND interval = :ref_interval" if ref_interval else ""
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text(
            f"""
            WITH universe AS (
                SELECT DISTINCT underlying, expiry, strike, option_type, instrument_key
                FROM option_premium_candles
                WHERE time >= :ref_from AND time < :ref_to
                  AND instrument_key IS NOT NULL AND instrument_key <> ''
                  {ref_clause}
            ),
            have AS (
                SELECT instrument_key, count(*) AS n
                FROM option_premium_candles
                WHERE time >= :target AND time < :next_day AND interval = :interval
                GROUP BY 1
            )
            SELECT u.underlying, u.expiry, u.strike, u.option_type, u.instrument_key,
                   COALESCE(h.n, 0) AS have_bars
            FROM universe u
            LEFT JOIN have h ON h.instrument_key = u.instrument_key
            WHERE COALESCE(h.n, 0) < :expected
            ORDER BY COALESCE(h.n, 0) ASC, u.underlying
            """
        ), {
            "ref_from": ref_from,
            "ref_to": target + timedelta(days=1),
            "target": target,
            "next_day": target + timedelta(days=1),
            "interval": interval,
            "expected": expected,
            **({"ref_interval": ref_interval} if ref_interval else {}),
        })).fetchall()
    return [
        {
            "underlying": r.underlying,
            "expiry": r.expiry,
            "strike": float(r.strike),
            "option_type": r.option_type,
            "instrument_key": r.instrument_key,
            "have": r.have_bars,
        }
        for r in rows
    ]


async def _count(target: date, interval: str) -> tuple[int, int]:
    async with AsyncSessionLocal() as session:
        row = (await session.execute(text(
            """
            SELECT count(*) AS rows, count(DISTINCT underlying) AS unds
            FROM option_premium_candles
            WHERE time >= :d AND time < :nd AND interval = :interval
            """
        ), {"d": target, "nd": target + timedelta(days=1), "interval": interval})).one()
    return int(row.rows), int(row.unds)


async def _one(svc: OptionHistoryService, c: dict, *, interval: str,
               from_date: date, to_date: date) -> tuple[str, int]:
    key = c["instrument_key"]
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
        underlying=c["underlying"],
        expiry=c["expiry"],
        strike=c["strike"],
        option_type=c["option_type"],
        instrument_key=key,
        interval=interval,
        already_in_db=set(),
    )
    return "ok", len(rows)


async def _amain(a: argparse.Namespace) -> int:
    svc = OptionHistoryService()
    targets = await _targets(a.date, a.interval, a.expected, a.ref_days, a.ref_interval)
    if a.limit:
        targets = targets[: a.limit]
    before_rows, before_unds = await _count(a.date, a.interval)
    print(f"[{a.interval}] {a.date}: before={before_rows} rows / {before_unds} underlyings; "
          f"{len(targets)} contracts short of {a.expected} bars")

    from_date = a.date - timedelta(days=a.fetch_back)
    to_date = a.to_date
    sem = asyncio.Semaphore(a.concurrency)
    stats: dict[str, int] = defaultdict(int)
    failures: list[str] = []
    done = 0

    async def run(c: dict) -> None:
        nonlocal done
        async with sem:
            try:
                status, n = await _one(svc, c, interval=a.interval,
                                       from_date=from_date, to_date=to_date)
                stats[status] += 1
                stats["broker_rows"] += n
            except Exception as exc:  # never abort the run for one symbol
                stats["error"] += 1
                failures.append(f"{c['underlying']} {c['expiry']} {c['strike']}{c['option_type']} "
                                f"{c['instrument_key']}: {type(exc).__name__}: {exc}")
            done += 1
            if done % 100 == 0:
                print(f"  ...{done}/{len(targets)}  ok={stats['ok']} empty={stats['empty']} "
                      f"err={stats['error']}", flush=True)

    await asyncio.gather(*(run(c) for c in targets))

    after_rows, after_unds = await _count(a.date, a.interval)
    print(f"[{a.interval}] {a.date}: after={after_rows} rows / {after_unds} underlyings  "
          f"(+{after_rows - before_rows} rows, +{after_unds - before_unds} underlyings)")
    print(f"  contracts ok={stats['ok']} empty={stats['empty']} error={stats['error']}  "
          f"broker_rows_fetched={stats['broker_rows']}")
    for f in failures[:25]:
        print(f"  FAIL {f}")
    if len(failures) > 25:
        print(f"  ... and {len(failures) - 25} more failures")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--date", type=_parse_date, required=True, help="session date to repair (UTC/IST date)")
    p.add_argument("--interval", default="30minute")
    p.add_argument("--expected", type=int, default=13, help="full-session bar count for the interval")
    p.add_argument("--ref-days", type=int, default=2, dest="ref_days",
                   help="days before --date to harvest the contract universe from")
    p.add_argument("--ref-interval", default=None, dest="ref_interval",
                   help="restrict the contract universe to keys that already had rows at this "
                        "interval in the reference window — keeps the run on genuinely tracked "
                        "ATM legs instead of every strike ever written")
    p.add_argument("--fetch-back", type=int, default=4, dest="fetch_back",
                   help="broker fetch window start = date - fetch_back days")
    p.add_argument("--to-date", type=_parse_date, default=None, dest="to_date",
                   help="broker fetch window end (default date+1; keep < today to avoid the intraday call)")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--limit", type=int, default=0)
    a = p.parse_args()
    if a.to_date is None:
        a.to_date = a.date + timedelta(days=1)
    return asyncio.run(_amain(a))


if __name__ == "__main__":
    raise SystemExit(main())
