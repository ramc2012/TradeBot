#!/usr/bin/env python
"""Backfill `preopen_spot_snapshots` from the WS tick tape.

Zero broker calls: every value comes from `market_ticks` and
`underlying_spot_candles` rows already in Postgres. Safe to run at any time,
including inside a session.

Sessions MUST be processed oldest → newest, because each session's relative
volume baseline is the median of that name's OWN prior rows in this very table.
The script enforces that order regardless of the order dates are supplied in.

Usage
  python -m scripts.backfill_preopen_spot_snapshots --from 2026-07-13 --to 2026-07-24
  python -m scripts.backfill_preopen_spot_snapshots --discover           # auto-find sessions
  python -m scripts.backfill_preopen_spot_snapshots --date 2026-07-24 --universe session_catalog
  python -m scripts.backfill_preopen_spot_snapshots --discover --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from db.database import AsyncSessionLocal  # noqa: E402
from market_data.preopen_spot import (  # noqa: E402
    IST,
    build_session_snapshot,
    preopen_window_utc,
)


async def discover_sessions(start: date, end: date) -> list[date]:
    """Sessions in [start, end] that have ANY pre-open frame on the tape.

    One bounded scan per session — `time` is always bound directly with literal
    UTC instants, never wrapped in a function.
    """
    found: list[date] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            s, e = preopen_window_utc(day)
            async with AsyncSessionLocal() as session:
                n = (
                    await session.execute(
                        text(
                            """
                            SELECT count(*) FROM market_ticks
                             WHERE time >= :s AND time < :e
                            """
                        ),
                        {"s": s, "e": e},
                    )
                ).scalar_one()
            if int(n or 0) > 0:
                found.append(day)
                print(f"  {day}  {int(n):>7,} pre-open frames")
            else:
                print(f"  {day}  ------- no pre-open frames (skipped)")
        day += timedelta(days=1)
    return found


async def tape_start() -> date:
    async with AsyncSessionLocal() as session:
        row = (await session.execute(text("SELECT min(time) FROM market_ticks"))).scalar()
    if row is None:
        return datetime.now(IST).date()
    return row.astimezone(IST).date()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="single session YYYY-MM-DD")
    ap.add_argument("--from", dest="frm", help="range start YYYY-MM-DD")
    ap.add_argument("--to", dest="to", help="range end YYYY-MM-DD")
    ap.add_argument(
        "--discover",
        action="store_true",
        help="scan from the first tick in the tape to today and backfill every "
             "session that actually has pre-open frames",
    )
    ap.add_argument(
        "--universe",
        choices=("ticks_only", "session_catalog"),
        default="ticks_only",
        help="ticks_only (default, for past sessions: never judges a past "
             "session against today's catalog) | session_catalog (writes a row "
             "for every catalog name, recording absence explicitly)",
    )
    ap.add_argument("--dry-run", action="store_true", help="compute but do not write")
    args = ap.parse_args()

    if args.discover:
        start = await tape_start()
        end = datetime.now(IST).date()
        print(f"Scanning {start} .. {end} for pre-open frames")
        sessions = await discover_sessions(start, end)
    elif args.date:
        sessions = [date.fromisoformat(args.date)]
    elif args.frm and args.to:
        s, e = date.fromisoformat(args.frm), date.fromisoformat(args.to)
        sessions = [s + timedelta(days=i) for i in range((e - s).days + 1)]
        sessions = [d for d in sessions if d.weekday() < 5]
    else:
        ap.error("give --date, --from/--to, or --discover")
        return 2

    # Ascending is not cosmetic: the volume baseline reads prior rows of this
    # same table, so out-of-order processing would let a session borrow a
    # baseline built from its own future.
    sessions = sorted(set(sessions))

    results = []
    for day in sessions:
        summary = await build_session_snapshot(
            day, universe_source=args.universe, persist=not args.dry_run
        )
        results.append(summary)
        print(
            f"{day}  rows={summary['rows']:>4}  written={summary['written']:>4}  "
            f"statuses={summary['by_data_status']}  states={summary['by_activeness_state']}"
        )
        for hit in summary["active_top"][:5]:
            print(
                f"        ACTIVE  {hit['underlying']:<14} score={hit['score']}  "
                f"gap={hit['gap_pct']}%  reasons={hit['reasons']}"
            )

    print("\n=== SUMMARY ===")
    print(json.dumps(
        [
            {
                k: v for k, v in r.items()
                if k not in {"active_top", "mcx_excluded_reason"}
            }
            for r in results
        ],
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
