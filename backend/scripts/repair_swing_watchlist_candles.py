"""Repair the exact option candles the Vanguard swing watchlist needs.

The session-wide tool (backfill_session_premium_gaps.py) walks the whole
contract universe, which is thousands of broker calls. This one repairs only
the 10-20 exact contracts on a swing watchlist run -- the ones whose 14:45 IST
entry bar and subsequent marks the desk is actually waiting on.

Why it is needed at all: the `upstox_chain` sweep works through 30-minute bars
sequentially and runs roughly 45-60 minutes behind, so the day's last two bars
are still being written after the close. Anything that interrupts the backend
in that window (a deploy, a crash) leaves the entry bar permanently thin --
observed 2026-09-04, when 18 of 20 watchlist contracts had no candle at all
after 14:45.

Uses the same broker-fetch/persist primitives as the session tool, under
CLASS_BULK, so it is hard-capped at 25% of the broker budget.

    docker exec nomadcurie_backend python scripts/repair_swing_watchlist_candles.py \
        --session 2026-09-04 --interval 30minute
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loguru import logger  # noqa: E402
from sqlalchemy import text  # noqa: E402

from brokers.rate_limiter import CLASS_BULK, broker_class  # noqa: E402
from db.database import AsyncSessionLocal  # noqa: E402
from market_data.option_history import OptionHistoryService, interval_minutes  # noqa: E402


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


async def _contracts(session_date: date) -> list[dict]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(text(
            """SELECT symbol underlying, option_type, strike::float strike, expiry,
                      instrument instrument_key, rank, side_rank
               FROM vanguard_swing_watchlist_items
               WHERE source_session = :session
               ORDER BY option_type, side_rank NULLS LAST, rank"""
        ), {"session": session_date})
        return [dict(row) for row in rows.mappings().all()]


async def _one(svc: OptionHistoryService, contract: dict, *, interval: str,
               from_date: date, to_date: date) -> tuple[str, int]:
    key = contract["instrument_key"]
    if not key:
        return "no_key", 0
    aggregate = interval_minutes(interval) if interval in {"3minute", "5minute", "15minute"} else None
    with broker_class(CLASS_BULK):
        if aggregate is not None:
            rows = await svc._fetch_broker_candles(
                instrument_key=key, from_date=from_date, to_date=to_date, interval="1minute")
            rows = svc._aggregate_rows(rows, aggregate) if rows else []
        else:
            rows = await svc._fetch_broker_candles(
                instrument_key=key, from_date=from_date, to_date=to_date, interval=interval)
    if not rows:
        return "empty", 0
    await svc._persist_broker_candles(
        rows=rows, underlying=contract["underlying"], expiry=contract["expiry"],
        strike=contract["strike"], option_type=contract["option_type"],
        instrument_key=key, interval=interval, already_in_db=set(),
    )
    return "ok", len(rows)


async def _amain(args: argparse.Namespace) -> int:
    contracts = await _contracts(args.session)
    if not contracts:
        logger.warning(f"no swing watchlist items for {args.session}")
        return 1
    svc = OptionHistoryService()
    # The fetch window must INCLUDE today for the intraday endpoint to be used;
    # /historical-candle alone excludes the current session entirely.
    from_date = args.session - timedelta(days=args.fetch_back)
    to_date = args.session
    tally: dict[str, int] = {}
    fetched = 0
    for contract in contracts:
        status, count = await _one(
            svc, contract, interval=args.interval, from_date=from_date, to_date=to_date)
        tally[status] = tally.get(status, 0) + 1
        fetched += count
        logger.info(f"  {contract['underlying']:<12} {contract['option_type']} "
                    f"{contract['strike']:<10} {status} ({count})")
    print(f"[swing-candle repair] {args.session} {args.interval}: "
          f"{dict(sorted(tally.items()))} broker_rows={fetched}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", type=_parse_date, required=True,
                        help="swing watchlist source_session to repair")
    parser.add_argument("--interval", default="30minute")
    parser.add_argument("--fetch-back", type=int, default=2, dest="fetch_back")
    return asyncio.run(_amain(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
