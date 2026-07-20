"""Offline Gann time-cycle prominence mapper.

Runs OFF the database in an ISOLATED process — never inside the prod backend
container.  Same operational discipline as ``tune_sweep.py``: ONE asyncpg
connection, no app/broker bootstrap, per-instrument bounded reads, each daily
frame released before the next is loaded.

  docker run -d --name gann-cycles --network tradebot_default --memory=1200m \
    -v /opt/TradeBot/backend:/app -w /app tradebot-backend \
    python gann_tp_delta/run_cycle_mapping.py
  docker logs -f gann-cycles

Env:
  GANN_CYCLE_DSN        asyncpg DSN (default: the in-network prod DSN)
  GANN_CYCLE_SESSIONS   daily bars per instrument (default 1300)
  GANN_CYCLE_LIMIT      cap instruments, for a smoke run (default: no cap)
  GANN_CYCLE_CLASSES    comma list of index,stock,commodity
  GANN_CYCLE_PERSIST    "1" to write gann_cycle_prominence (default "0")
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import asyncpg

from gann_tp_delta.cycle_prominence import (
    FDR_Q,
    MIN_OBSERVATIONS,
    finalise,
    placebo_cycles,
    prominence_summary,
    ranking,
    score_instrument,
)
from gann_tp_delta.cycles import testable_cycles, untestable_cycles
from gann_tp_delta.daily_data import fetch_daily_frame
from gann_tp_delta.universe import class_counts, resolve_universe

DSN = os.environ.get("GANN_CYCLE_DSN", "postgresql://nomadcurie:nomadcurie@db:5432/nomadcurie")
SESSIONS = int(os.environ.get("GANN_CYCLE_SESSIONS", "1300"))
LIMIT = int(os.environ.get("GANN_CYCLE_LIMIT", "0"))
CLASSES = tuple(
    item.strip() for item in os.environ.get("GANN_CYCLE_CLASSES", "index,stock,commodity").split(",") if item.strip()
)
PERSIST = os.environ.get("GANN_CYCLE_PERSIST", "0") == "1"

_INSERT = """
INSERT INTO gann_cycle_prominence (
    run_id, underlying, cycle_key, family, cycle_days, arm, status,
    untestable_reason, is_observations, is_hits, is_hit_rate, null_rate, lift,
    p_value, p_value_fdr, fdr_significant, era1_observations, era1_hit_rate,
    era2_observations, era2_hit_rate, era_stable, oos_observations, oos_hits,
    oos_hit_rate, oos_null_rate, oos_p_value, oos_confirms,
    median_turn_magnitude_pct, history_sessions, history_start, history_end
) VALUES (
    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,
    $21,$22,$23,$24,$25,$26,$27,$28,$29,$30,$31
)
ON CONFLICT (run_id, underlying, arm, cycle_key) DO NOTHING
"""


async def main() -> int:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    connection = await asyncpg.connect(DSN)
    try:
        members = await resolve_universe(connection, include_classes=CLASSES)
        if LIMIT > 0:
            members = members[:LIMIT]
        print(f"[gann-cycles] run_id={run_id} universe={len(members)} {class_counts(members)}", flush=True)

        genuine_scores = []
        placebo_scores = []
        history: dict[str, tuple[int, object, object]] = {}
        skipped: list[tuple[str, str]] = []

        for position, member in enumerate(members, start=1):
            frame = await fetch_daily_frame(connection, member.underlying, sessions=SESSIONS)
            bars = len(frame.index)
            if bars < 120:
                skipped.append((member.underlying, f"only {bars} daily bars"))
                del frame
                continue
            start_date = frame["time"].iloc[0].date()
            end_date = frame["time"].iloc[-1].date()
            span_days = (end_date - start_date).days
            history[member.underlying] = (bars, start_date, end_date)

            cycles = testable_cycles(span_days, MIN_OBSERVATIONS)
            if not cycles:
                skipped.append((member.underlying, f"no cycle is testable over {span_days} calendar days"))
                del frame
                continue
            controls = placebo_cycles(len(cycles), seed=abs(hash(member.underlying)) % (2**31))

            genuine_scores.extend(score_instrument(member.underlying, frame, cycles))
            placebo_scores.extend(score_instrument(member.underlying, frame, controls, placebo=True))
            del frame
            if position % 25 == 0:
                print(f"[gann-cycles] {position}/{len(members)} scored", flush=True)

        finalise(genuine_scores, placebo_scores, q=FDR_Q)
        summary = prominence_summary(genuine_scores, placebo_scores)
        print("[gann-cycles] SUMMARY " + json.dumps(summary, indent=2), flush=True)

        prominent = [s for s in genuine_scores if s.status == "PROMINENT"]
        print(f"[gann-cycles] prominent cells: {len(prominent)}", flush=True)
        for score in sorted(prominent, key=lambda s: -( (s.oos_hit_rate or 0) - (s.oos_null_rate or 0) ))[:60]:
            print(
                f"  {score.underlying:14s} {score.cycle_key:14s} {score.family:16s} "
                f"d={score.cycle_days:4d} n={score.is_observations:3d} hit={score.is_hit_rate:.3f} "
                f"null={score.null_rate:.3f} p={score.p_value:.4f} q={score.p_value_fdr:.4f} "
                f"oos={score.oos_hit_rate:.3f}/{score.oos_null_rate:.3f}",
                flush=True,
            )

        if skipped:
            print(f"[gann-cycles] skipped {len(skipped)}: {skipped[:15]}", flush=True)

        if PERSIST:
            written = 0
            for arm_name, arm in (("genuine", genuine_scores), ("placebo", placebo_scores)):
                for score in arm:
                    bars, start_date, end_date = history.get(score.underlying, (None, None, None))
                    await connection.execute(
                        _INSERT,
                        run_id, score.underlying, score.cycle_key, score.family, score.cycle_days,
                        arm_name, score.status, score.untestable_reason,
                        score.is_observations, score.is_hits, score.is_hit_rate, score.null_rate,
                        score.lift, score.p_value, score.p_value_fdr, score.fdr_significant,
                        score.era1_observations, score.era1_hit_rate,
                        score.era2_observations, score.era2_hit_rate, score.era_stable,
                        score.oos_observations, score.oos_hits, score.oos_hit_rate,
                        score.oos_null_rate, score.oos_p_value, score.oos_confirms,
                        score.median_turn_magnitude_pct, bars, start_date, end_date,
                    )
                    written += 1
            print(f"[gann-cycles] persisted {written} rows under run_id={run_id}", flush=True)
        else:
            print("[gann-cycles] PERSIST disabled (set GANN_CYCLE_PERSIST=1 to write)", flush=True)
    finally:
        await connection.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(asyncio.run(main()))
