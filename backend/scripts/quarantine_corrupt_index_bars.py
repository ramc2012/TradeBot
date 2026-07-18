"""One-time quarantine of cross-symbol-contaminated index spot bars.

Why delete, not clamp
---------------------
The documented Fyers WS ``topic_id``→symbol misresolution wrote whole foreign
frames (BANKNIFTY ~57.8k, REALTY ~928, half-value ~48545) under an index's
``underlying`` on 07-09/07-14/07-15, before the tick fences shipped 07-16. Many
of the poisoned NIFTY/30minute rows have a corrupt ``open`` and/or ``close`` in
addition to high/low — for those, per-leg reconstruction (e.g.
``high = max(open, close)``) would *fabricate* prices. The honest, bounded
repair is to DELETE the provably out-of-band rows and let the chart gap over the
handful of missing bars (aggregation already skips gaps). A delete is reversible
via broker backfill; a clamp is not.

Predicate
---------
A row is a quarantine candidate iff it is a guarded index underlying and ANY of
its O/H/L/C legs fails ``index_band_guard.passes`` once the ±20% prior-session
reference has been seeded from history. That reference is what rejects the
``48545`` close (inside the wide absolute band, outside ±20% of ~24000) as well
as the obvious 57k / 928 legs. Valid ~24000 rows cannot match.

Scope — WHY the ``--since-days`` window matters
-----------------------------------------------
The ±20% reference is the *current* prior-session level. It is only a valid
sanity band for **recent** rows: a legit NIFTY bar from 2021 (~15000) or a 2024
BANKNIFTY bar (~48000) is >20% away from today's level and would be
false-flagged by a full-history scan (a naive scan flags ~38k legit historical
bars). The documented contamination is confined to 2026-07-08..07-15, so the
scan is scoped to the last ``--since-days`` (default 30) days, where the current
reference genuinely applies and only the real cross-symbol swaps match.

Safety
------
* Market-closed operation; no writer races these rows.
* Every candidate row is logged (time + O/H/L/C) before deletion.
* ``--dry-run`` (default) prints candidates and deletes nothing. Pass
  ``--apply`` to perform the scoped DELETE.
* Idempotent — a second run finds nothing.

Usage
-----
    python -m scripts.quarantine_corrupt_index_bars              # dry-run
    python -m scripts.quarantine_corrupt_index_bars --apply      # delete
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import text  # noqa: E402

from db.database import AsyncSessionLocal  # noqa: E402
from market_data import index_band_guard  # noqa: E402

# Native timeframes the chart serves + the 1minute base they aggregate from.
_INTERVALS = ("1minute", "3minute", "5minute", "15minute", "30minute", "60minute")


async def _quarantine(apply: bool, since_days: int) -> int:
    seeded = await index_band_guard.refresh_reference_closes(AsyncSessionLocal)
    print(
        f"[quarantine] seeded {seeded} prior-session reference closes; "
        f"scanning the last {since_days} days"
    )

    total_candidates = 0
    total_deleted = 0
    async with AsyncSessionLocal() as session:
        for underlying, app_symbol in index_band_guard._UNDERLYING_TO_APP.items():
            for interval in _INTERVALS:
                result = await session.execute(
                    text(
                        """
                        SELECT time, open, high, low, close
                        FROM underlying_spot_candles
                        WHERE underlying = :underlying
                          AND interval = :interval
                          AND time >= NOW() - make_interval(days => :since_days)
                        ORDER BY time ASC
                        """
                    ),
                    {"underlying": underlying, "interval": interval, "since_days": since_days},
                )
                bad_times = []
                for row in result.fetchall():
                    r = dict(row._mapping)
                    legs = (r.get("open"), r.get("high"), r.get("low"), r.get("close"))
                    if not index_band_guard.check_ohlc(app_symbol, *legs):
                        bad_times.append(r["time"])
                        print(
                            f"[quarantine] CANDIDATE {underlying}/{interval} "
                            f"t={r['time']} o={r.get('open')} h={r.get('high')} "
                            f"l={r.get('low')} c={r.get('close')}"
                        )
                if not bad_times:
                    continue
                total_candidates += len(bad_times)
                if apply:
                    del_result = await session.execute(
                        text(
                            """
                            DELETE FROM underlying_spot_candles
                            WHERE underlying = :underlying
                              AND interval = :interval
                              AND time = ANY(:times)
                            """
                        ),
                        {
                            "underlying": underlying,
                            "interval": interval,
                            "times": bad_times,
                        },
                    )
                    deleted = del_result.rowcount or 0
                    total_deleted += deleted
                    print(
                        f"[quarantine] DELETED {deleted} rows "
                        f"{underlying}/{interval}"
                    )
        if apply:
            await session.commit()

    print(
        f"[quarantine] done — candidates={total_candidates} "
        f"deleted={total_deleted} apply={apply}"
    )
    return total_candidates


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the DELETE. Without it the script is a dry-run.",
    )
    parser.add_argument(
        "--since-days",
        type=int,
        default=30,
        help="Only scan rows newer than this many days (default 30). The ±20%% "
        "reference is only valid near-term — a wider window false-flags legit "
        "historical bars.",
    )
    args = parser.parse_args()
    asyncio.run(_quarantine(apply=args.apply, since_days=args.since_days))


if __name__ == "__main__":
    main()
