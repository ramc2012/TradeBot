"""Index-only session repair for option_premium_candles.

``backfill_session_premium_gaps.py`` has no underlying filter: on 2026-09-15 it
queued 11,424 contracts (every F&O stock plus indices) to repair a restart hole
that only matters to the index lanes. This drives the same ``_targets`` /
``_one`` primitives, restricted to the index underlyings, for each interval.

    docker exec nomadcurie_backend python scripts/backfill_index_premium_session.py \
        --date 2026-09-15 --intervals 30minute:13,3minute:125
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from backfill_session_premium_gaps import _count, _one, _parse_date, _targets  # noqa: E402
from market_data.option_history import OptionHistoryService  # noqa: E402

INDICES = ("NIFTY", "BANKNIFTY", "SENSEX")


async def _amain(a: argparse.Namespace) -> int:
    svc = OptionHistoryService()
    underlyings = {u.strip().upper() for u in a.underlyings.split(",") if u.strip()}
    for spec in a.intervals.split(","):
        interval, expected = spec.split(":")
        targets = [
            t for t in await _targets(a.date, interval, int(expected), a.ref_days)
            if str(t["underlying"]).upper() in underlyings and int(t["have"]) > 0
        ]
        before, _ = await _count(a.date, interval)
        print(f"[{interval}] {a.date}: {len(targets)} index contracts short of {expected}; rows before={before}")
        sem = asyncio.Semaphore(a.concurrency)
        stats: Counter = Counter()

        async def run(t: dict) -> None:
            async with sem:
                try:
                    status, n = await _one(svc, t, interval=interval, from_date=a.date, to_date=a.date)
                    stats[status] += 1
                    stats["rows"] += n
                except Exception:  # noqa: BLE001 — one contract never aborts the run
                    stats["error"] += 1

        await asyncio.gather(*(run(t) for t in targets))
        after, _ = await _count(a.date, interval)
        print(f"[{interval}] done {dict(stats)}; rows after={after} (+{after - before})")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--date", type=_parse_date, required=True)
    p.add_argument("--intervals", default="30minute:13,3minute:125")
    p.add_argument("--underlyings", default=",".join(INDICES))
    p.add_argument("--ref-days", type=int, default=0, dest="ref_days")
    p.add_argument("--concurrency", type=int, default=4)
    return asyncio.run(_amain(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
