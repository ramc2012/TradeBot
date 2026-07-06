"""One-time backfill: stamp broker greeks onto greeks-null index option candles.

Fills the gap left when the Fyers 1-minute greeks writer died on 2026-06-23 by
copying greeks from `option_chain_snapshots` (real broker greeks, already
persisted since 2026-06-20) onto every greeks-null `source in ('fyers','upstox')`
index option bar. Idempotent — only touches NULL iv, so it is safe to re-run and
safe to run alongside the live enrichment daemon.

Run inside the backend container (light; index-band only), or in a sidecar:
    docker exec nomadcurie_backend python scripts/backfill_option_greeks.py \
        --from 2026-06-23 --to 2026-07-07
    # defaults: --from 2026-06-23, --to = tomorrow (UTC), all supported intervals
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from market_data.greeks_enrichment import (  # noqa: E402
    DEFAULT_INTERVALS,
    enrich_option_greeks,
)

UTC = timezone.utc


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


async def _amain(args: argparse.Namespace) -> int:
    since = datetime.combine(args.from_date, datetime.min.time(), tzinfo=UTC)
    until = datetime.combine(args.to_date, datetime.min.time(), tzinfo=UTC)
    intervals = tuple(args.intervals) if args.intervals else DEFAULT_INTERVALS

    print(f"Greeks backfill  {since.date()} → {until.date()}  intervals={intervals}")
    counts = await enrich_option_greeks(since=since, until=until, intervals=intervals)
    total = sum(counts.values())
    for interval, n in counts.items():
        print(f"  {interval:>9}: {n:>8} rows enriched")
    print(f"✓ Done — {total} greeks rows filled")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="from_date", type=_parse_date,
                        default=date(2026, 6, 23), help="start date (YYYY-MM-DD, inclusive, UTC)")
    parser.add_argument("--to", dest="to_date", type=_parse_date,
                        default=(datetime.now(UTC).date() + timedelta(days=1)),
                        help="end date (YYYY-MM-DD, exclusive, UTC); defaults to tomorrow")
    parser.add_argument("--intervals", nargs="*", default=None,
                        help=f"intervals to enrich (default: {' '.join(DEFAULT_INTERVALS)})")
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
