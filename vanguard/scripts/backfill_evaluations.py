"""Journal M6's per-symbol evaluation for every historical bar.

`candidate_evaluations` only starts accumulating from the moment migration 006
lands, but every input it reads is already sitting in the feature tables for
months back. Replaying M6 over those bars costs nothing (it is a read of
existing rows) and is the difference between a cross-sectional IC study with
one session behind it and one with the whole healthy window.

It writes ONLY the journal — never tickets. A ticket is a decision the lane
made at a moment in time; manufacturing a backdated one would put rows in
`tickets` that no live pass ever emitted, which is exactly the kind of
retro-fabrication the paper book already got burned by.

    python vanguard/scripts/backfill_evaluations.py --sessions 5
    python vanguard/scripts/backfill_evaluations.py --start 2026-05-25 --end 2026-07-28
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fusion.m6_select import evaluate_bar, funnel_counts, persist_evaluations  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"

# Only bars on NSE's own :15/:45 grid. The off-grid rows in `timing` belong to
# instruments trading a different session and carry ~5 symbols against an NSE
# bar's ~210; journaling them would put a 5-symbol "cross-section" into a study
# whose entire premise is cross-sectional width.
BARS_SQL = """
    SELECT DISTINCT ts FROM timing
    WHERE ts >= %(start)s AND ts < %(end)s
      AND EXTRACT(minute FROM ts AT TIME ZONE 'Asia/Kolkata') IN (15, 45)
      AND (ts AT TIME ZONE 'Asia/Kolkata')::time BETWEEN TIME '09:15' AND TIME '15:15'
    ORDER BY ts
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=5,
                        help="how many calendar days back from --end to replay")
    parser.add_argument("--start", type=date.fromisoformat, default=None)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    connection = psycopg2.connect(args.dsn)
    connection.autocommit = True
    try:
        end = args.end or date.today()
        start = args.start or (end - timedelta(days=args.sessions))
        with connection.cursor() as cursor:
            cursor.execute(BARS_SQL, {"start": start, "end": end + timedelta(days=1)})
            bars = [row[0] for row in cursor.fetchall()]
        if not bars:
            print(f"no on-grid timing bars between {start} and {end} — nothing to replay")
            return 0
        print(f"replaying {len(bars)} bars from {bars[0]} to {bars[-1]}")

        total = 0
        for index, ts in enumerate(bars, start=1):
            evaluations = evaluate_bar(connection, ts)
            written = persist_evaluations(connection, evaluations)
            total += written
            survivors = sum(1 for e in evaluations if e.survived)
            if index % 5 == 0 or index == len(bars) or survivors:
                stages = {s["leg"]: s for s in funnel_counts(evaluations)}
                biggest = max(
                    (s for s in stages.values() if s.get("lost_here")),
                    key=lambda s: s["lost_here"], default=None,
                )
                print(f"  [{index:>3}/{len(bars)}] {ts}  {written:>4} symbols  "
                      f"survivors={survivors}"
                      + (f"  biggest killer: {biggest['leg']} (-{biggest['lost_here']})"
                         if biggest else ""))
        print(f"\njournaled {total:,} symbol-bar evaluations")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
