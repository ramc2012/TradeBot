"""Session-close writer for `gann_watchlist_snapshots`.

Deliberately NOT wired into the 60-second paper-agent loop. The lane is now a
DAILY lane: the bar only changes once a session, so re-deriving 225 instruments
every minute would buy nothing and cost ~36x today's DB read load on a
Postgres that was OOM-killed twice on 2026-07-20. One bounded 30-minute read
per instrument per session is the affordable shape, and it is what makes the
wide universe possible at all.

Runs the SAME code path the live lane runs — ``GannTPDeltaService._snapshot``
— so the persisted regime/conviction/setup_state cannot drift from what the
agent decided on.

  docker run --rm --name gann-watchlist --network tradebot_default --memory=1200m \
    -v /opt/TradeBot/backend:/app -w /app tradebot-backend \
    python gann_tp_delta/watchlist_runner.py

Env:
  GANN_WATCHLIST_DSN       asyncpg DSN
  GANN_WATCHLIST_SESSIONS  daily bars per instrument (default from config)
  GANN_WATCHLIST_CLASSES   comma list of index,stock,commodity
  GANN_WATCHLIST_LIMIT     cap instruments (smoke runs)
  GANN_WATCHLIST_DRYRUN    "1" to compute and print without writing
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

import asyncpg

from gann_tp_delta.config import clone_default_config
from gann_tp_delta.daily_data import fetch_daily_frame
from gann_tp_delta.universe import class_counts, resolve_universe
from gann_tp_delta.watchlist import compute_watchlist_row, upsert_params, UPSERT_SQL

DSN = os.environ.get("GANN_WATCHLIST_DSN", "postgresql://nomadcurie:nomadcurie@db:5432/nomadcurie")
CLASSES = tuple(
    item.strip()
    for item in os.environ.get("GANN_WATCHLIST_CLASSES", "index,stock,commodity").split(",")
    if item.strip()
)
LIMIT = int(os.environ.get("GANN_WATCHLIST_LIMIT", "0"))
DRYRUN = os.environ.get("GANN_WATCHLIST_DRYRUN", "0") == "1"


async def load_prominent_cycle_keys(connection: Any, underlying: str, run_id: str | None) -> list[str]:
    """Cycles demonstrably prominent for THIS instrument, per the mapper.

    Returns [] when nothing survived — which, on the 2026-07-20 run, is every
    instrument. The caller treats [] as "no prominence filter", so the
    watchlist still reports a next projected turn, labelled `unranked`.
    """
    if not run_id:
        return []
    rows = await connection.fetch(
        """
        SELECT cycle_key FROM gann_cycle_prominence
        WHERE run_id = $1 AND underlying = $2 AND arm = 'genuine' AND status = 'PROMINENT'
        ORDER BY oos_hit_rate DESC NULLS LAST
        """,
        str(run_id),
        str(underlying).upper(),
    )
    return [str(row["cycle_key"]) for row in rows]


async def main() -> int:
    config = clone_default_config()
    sessions = int(
        os.environ.get("GANN_WATCHLIST_SESSIONS")
        or config["paper_agent"]["lookback_sessions"]
    )
    run_id = (config.get("time_cycles") or {}).get("prominence_run_id")

    from gann_tp_delta.service import GannTPDeltaService

    service = GannTPDeltaService(config)
    connection = await asyncpg.connect(DSN)
    written = failed = 0
    started = time.monotonic()
    try:
        members = await resolve_universe(connection, include_classes=CLASSES)
        if LIMIT > 0:
            members = members[:LIMIT]
        print(
            f"[gann-watchlist] universe={len(members)} {class_counts(members)} sessions={sessions}",
            flush=True,
        )
        for position, member in enumerate(members, start=1):
            try:
                daily = await fetch_daily_frame(connection, member.underlying, sessions=sessions)
                feature_frame = service.store.build_feature_frame(
                    daily, "1day", lookback_sessions=sessions, underlying=member.underlying
                )
                signal = None
                if not feature_frame.empty:
                    snapshot = service._snapshot(
                        feature_frame,
                        underlying=member.underlying,
                        timeframe="1day",
                        anchor_mode="auto_pivot",
                        h_mode=str(config["scaling"]["default_h_mode"]),
                        manual_h=None,
                    )
                    signal = snapshot.get("signal")
                prominent = await load_prominent_cycle_keys(connection, member.underlying, run_id)
                row = compute_watchlist_row(
                    underlying=member.underlying,
                    instrument_class=member.instrument_class,
                    daily_frame=feature_frame if not feature_frame.empty else daily,
                    config=config,
                    signal=signal,
                    prominent_cycle_keys=prominent,
                )
                if DRYRUN:
                    print(json.dumps(row.as_dict(), default=str), flush=True)
                else:
                    await connection.execute(UPSERT_SQL, *upsert_params(row))
                written += 1
                del daily, feature_frame
            except Exception as exc:  # keep going — one bad symbol is not a run failure
                failed += 1
                print(f"[gann-watchlist] {member.underlying} FAILED: {exc}", flush=True)
            if position % 25 == 0:
                print(f"[gann-watchlist] {position}/{len(members)}", flush=True)
    finally:
        await connection.close()
    elapsed = time.monotonic() - started
    print(
        f"[gann-watchlist] wrote={written} failed={failed} elapsed={elapsed:.1f}s "
        f"({elapsed / max(written, 1):.2f}s per instrument)",
        flush=True,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(asyncio.run(main()))
