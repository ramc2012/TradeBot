"""Pull daily frames ONCE to local parquet, so the re-test never touches PG again.

PG politeness: reuses the shipped ``gann_tp_delta.daily_data.fetch_daily_frame``,
which bounds ``time`` directly with literal UTC timestamps and is issued
per-instrument.  One connection, one query per symbol, frame released after
write.

  docker run --rm --network tradebot_default --memory=1200m \
    -v <repo>/backend:/app -v <scratch>:/scratch -w /app tradebot-backend \
    python gann_tp_delta/research/practice/fetch_frames.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import asyncpg

from gann_tp_delta.daily_data import fetch_daily_frame
from gann_tp_delta.universe import class_counts, resolve_universe

DSN = os.environ.get("GANN_CYCLE_DSN", "postgresql://nomadcurie:nomadcurie@db:5432/nomadcurie")
SESSIONS = int(os.environ.get("GANN_CYCLE_SESSIONS", "1300"))
OUT = os.environ.get("GANN_PRACTICE_OUT", "/scratch/frames")


async def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    connection = await asyncpg.connect(DSN)
    manifest: list[dict[str, object]] = []
    try:
        members = await resolve_universe(connection)
        print(f"[fetch] universe={len(members)} {class_counts(members)}", flush=True)
        for position, member in enumerate(members, start=1):
            frame = await fetch_daily_frame(connection, member.underlying, sessions=SESSIONS)
            bars = len(frame.index)
            if bars < 120:
                del frame
                continue
            path = os.path.join(OUT, f"{member.underlying}.parquet")
            frame.to_parquet(path, index=False)
            manifest.append(
                {
                    "underlying": member.underlying,
                    "instrument_class": member.instrument_class,
                    "bars": bars,
                    "start": str(frame["time"].iloc[0].date()),
                    "end": str(frame["time"].iloc[-1].date()),
                    "span_days": (frame["time"].iloc[-1].date() - frame["time"].iloc[0].date()).days,
                    "path": path,
                }
            )
            del frame
            if position % 25 == 0:
                print(f"[fetch] {position}/{len(members)}", flush=True)
    finally:
        await connection.close()
    with open(os.path.join(OUT, "manifest.json"), "w") as handle:
        json.dump(manifest, handle, indent=2)
    print(f"[fetch] wrote {len(manifest)} frames -> {OUT}", flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(asyncio.run(main()))
