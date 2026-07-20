"""Run the practice-implied arms off cached parquet frames. No DB access.

  docker run --rm -e PYTHONPATH=/app -v <repo>/backend:/app -v <scratch>:/scratch \
    -w /app tradebot-backend python gann_tp_delta/research/practice/run_practice_arms.py
"""
from __future__ import annotations

import json
import os
import random
import sys
from collections import defaultdict
from typing import Any

import numpy as np
import pandas as pd

from gann_tp_delta.cycle_prominence import binom_sf, placebo_cycles
from gann_tp_delta.cycles import CycleDef, all_cycles
from gann_tp_delta.research.practice.practice_harness import (
    FDR_Q,
    MAX_NULL_COVERAGE,
    MIN_OBSERVATIONS,
    ArmSpec,
    Cell,
    anchors_all_pivots,
    anchors_extremes,
    anchors_major,
    finalise,
    null_coverage,
    project,
    score_instrument,
    thin,
    tol_fixed_sessions,
    tol_flat_calendar3,
    tol_scaled_calendar,
    turns_bar_reversal,
    turns_pivot5_median,
)

FRAMES = os.environ.get("GANN_PRACTICE_FRAMES", "/scratch/frames")
OUT = os.environ.get("GANN_PRACTICE_RESULTS", "/scratch/results")

#: All cycles that are testable IN PRINCIPLE (<= 400 calendar days).  The
#: shipped runner additionally required days * 20 <= history span, which on a
#: 1,855-day index history capped the tested set at 92 days and on a 479-day
#: stock history admitted nothing at all.  That gate confuses "20 repetitions
#: of the cycle" with "20 independent observations": observations come from
#: many ANCHORS, not only from repetitions of one count.  Here the empirical
#: non-overlap count is the only gate, so the 120/135/144/180/270/360-day
#: cycles — the ones practitioners actually name — get scored.
CYCLE_SET = [c for c in all_cycles() if c.testable]

ARMS: list[ArmSpec] = [
    ArmSpec(
        "A0_shipped_replica", anchors_all_pivots, turns_pivot5_median,
        tol_fixed_sessions, repeats=1,
        note="shipped configuration, on the widened cycle set",
    ),
    ArmSpec(
        "A1_calendar_unit", anchors_all_pivots, turns_pivot5_median,
        tol_flat_calendar3, repeats=1,
        note="same tolerance magnitude, CALENDAR days (Gann's stated unit)",
    ),
    ArmSpec(
        "A2_scaled_tolerance", anchors_all_pivots, turns_pivot5_median,
        tol_scaled_calendar, repeats=1,
        note="+/-1 short, +/-3 intermediate, +/-7 long (published convention)",
    ),
    ArmSpec(
        "A3_major_anchors", anchors_major, turns_pivot5_median,
        tol_scaled_calendar, repeats=4,
        note="only significant 41-session extremes, top 40% magnitude, repeats 1..4",
    ),
    ArmSpec(
        "A4_extreme_anchors", anchors_extremes, turns_pivot5_median,
        tol_scaled_calendar, repeats=8,
        note="running all-time high/low anchors only, repeats 1..8",
    ),
    ArmSpec(
        "A5_bar_reversal_turn", anchors_all_pivots, turns_bar_reversal,
        tol_scaled_calendar, repeats=1,
        note="practitioner 2-bar reversal turn definition",
    ),
    ArmSpec(
        "A6_repeats", anchors_all_pivots, turns_pivot5_median,
        tol_scaled_calendar, repeats=4,
        note="a cycle repeats from its anchor: 1c, 2c, 3c, 4c",
    ),
    ArmSpec(
        "A7_price_confluence", anchors_all_pivots, turns_pivot5_median,
        tol_scaled_calendar, repeats=1, confluence=True,
        note="cycle date AND price within 1 ATR of a Square-of-Nine level from the same anchor",
    ),
    ArmSpec(
        "A8_major_confluence", anchors_major, turns_pivot5_median,
        tol_scaled_calendar, repeats=4, confluence=True,
        note="significant anchors AND price-level confluence — the full documented construct",
    ),
]


def load_manifest() -> list[dict[str, Any]]:
    with open(os.path.join(FRAMES, "manifest.json")) as handle:
        return json.load(handle)


# ── convergence-box arm (instrument-level, not per-cycle) ──────────────────


def convergence_arm(
    underlying: str,
    instrument_class: str,
    frame: pd.DataFrame,
    cycles: list[CycleDef],
    *,
    placebo: bool,
    coverage_target: float,
) -> Cell | None:
    """The 3-4-tools-converge doctrine: score only dates where enough DISTINCT
    (anchor, cycle) projections land in the same tolerance window.

    Practitioners say "3 or 4 tools converging".  On this grid a >=3 threshold
    covers essentially the whole calendar, so the threshold is chosen by a
    PRE-DECLARED rule that touches no outcome data: the smallest agreement
    count whose projected coverage falls at or below ``coverage_target`` of the
    in-sample calendar.  The identical rule is applied to the placebo, so the
    two arms are matched on coverage rather than on threshold.
    """
    n_sessions = len(frame.index)
    if n_sessions < 150:
        return None
    session_dates = [pd.Timestamp(v).date() for v in frame["time"]]
    ordinals = np.array([d.toordinal() for d in session_dates])
    anchors = anchors_all_pivots(frame)
    turn_list = turns_pivot5_median(frame)
    if not anchors or not turn_list:
        return None
    turn_set = set(turn_list)
    votes = np.zeros(n_sessions, dtype=np.int32)
    for cycle in cycles:
        for p in project(anchors, int(cycle.days), session_dates, ordinals,
                         tolerance=tol_scaled_calendar, repeats=4):
            votes[p.lo : p.hi + 1] += 1
    split = int(n_sessions * 0.70)
    if split <= 0:
        return None
    min_agree = None
    for candidate in range(1, int(votes.max()) + 2):
        coverage = float((votes[:split] >= candidate).mean())
        if coverage <= coverage_target:
            min_agree = candidate
            break
    if min_agree is None:
        return None
    proj_coverage = float((votes[:split] >= min_agree).mean())
    dense = np.flatnonzero(votes >= int(min_agree))
    if dense.size == 0:
        return None
    # collapse contiguous runs to their centre, then enforce disjoint +/-3 windows
    runs: list[int] = []
    start = dense[0]
    prev = dense[0]
    for idx in dense[1:]:
        if idx != prev + 1:
            runs.append(int((start + prev) // 2))
            start = idx
        prev = idx
    runs.append(int((start + prev) // 2))
    half = 2
    centres: list[int] = []
    last = -10**9
    for c in runs:
        if c - half > last:
            centres.append(c)
            last = c + half

    cell = Cell(
        arm=f"A9_convergence_cov{int(coverage_target * 100)}", underlying=underlying,
        instrument_class=instrument_class, cycle_key=f"converge_ge{min_agree}",
        family="convergence", cycle_days=0, is_placebo=placebo,
        agree_threshold=int(min_agree), proj_coverage=proj_coverage, tol_half_width=half,
    )

    def region(lo: int, hi: int) -> tuple[int, int, float, float]:
        sel = [c for c in centres if lo <= c < hi]
        hits = sum(1 for c in sel if any(i in turn_set for i in range(c - half, c + half + 1)))
        rt = [t for t in turn_list if lo <= t < hi]
        return len(sel), hits, (hits / len(sel) if sel else 0.0), null_coverage(rt, lo, hi, half)

    n, hits, rate, null = region(0, split)
    cell.n_obs, cell.hits, cell.null_rate = n, hits, null
    if n < MIN_OBSERVATIONS:
        cell.status = "UNTESTABLE"
        cell.reason = f"{n} disjoint convergence dates in-sample < {MIN_OBSERVATIONS}"
        return cell
    if null > MAX_NULL_COVERAGE:
        cell.status = "UNTESTABLE_BY_COVERAGE"
        cell.reason = f"null coverage {null:.2f} > {MAX_NULL_COVERAGE}"
        return cell
    cell.hit_rate, cell.lift = rate, rate - null
    cell.p_value = binom_sf(hits, n, null)
    mid = split // 2
    e1n, _e1h, e1r, e1null = region(0, mid)
    e2n, _e2h, e2r, e2null = region(mid, split)
    cell.era1_n, cell.era2_n = e1n, e2n
    cell.era_stable = bool(e1n >= 8 and e2n >= 8 and e1r > e1null and e2r > e2null)
    on, oh, orate, onull = region(split, n_sessions)
    cell.oos_n, cell.oos_hits, cell.oos_null_rate = on, oh, onull
    if on >= 8:
        cell.oos_hit_rate = orate
        cell.oos_p = binom_sf(oh, on, onull)
        cell.oos_confirms = bool(orate > onull and cell.oos_p <= 0.10)
    cell.status = "TESTED_NOT_PROMINENT"
    return cell


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    manifest = load_manifest()
    print(f"[practice] frames={len(manifest)} cycles={len(CYCLE_SET)}", flush=True)

    rows: list[dict[str, Any]] = []
    by_family: dict[tuple[str, bool], list[Cell]] = defaultdict(list)

    for position, entry in enumerate(manifest, start=1):
        underlying = entry["underlying"]
        klass = entry["instrument_class"]
        frame = pd.read_parquet(entry["path"])
        seed = abs(hash(underlying)) % (2**31)
        controls = placebo_cycles(len(CYCLE_SET), seed=seed)
        for spec in ARMS:
            for placebo, cycles in ((False, CYCLE_SET), (True, controls)):
                cells = score_instrument(
                    spec, underlying, klass, frame, cycles, placebo=placebo, seed=seed,
                )
                by_family[(spec.name, placebo)].extend(cells)
        for coverage_target in (0.50, 0.25, 0.10):
            for placebo, cycles in ((False, CYCLE_SET), (True, controls)):
                cell = convergence_arm(
                    underlying, klass, frame, cycles, placebo=placebo,
                    coverage_target=coverage_target,
                )
                if cell is not None:
                    by_family[(cell.arm, placebo)].append(cell)
        del frame
        if position % 25 == 0:
            print(f"[practice] {position}/{len(manifest)}", flush=True)

    report: dict[str, Any] = {"arms": {}}
    for (arm, placebo), cells in sorted(by_family.items()):
        stats = finalise(cells, q=FDR_Q)
        key = f"{arm}|{'placebo' if placebo else 'genuine'}"
        counts = {
            "cells": len(cells),
            "untestable_n": sum(1 for c in cells if c.status == "UNTESTABLE"),
            "untestable_coverage": sum(1 for c in cells if c.status == "UNTESTABLE_BY_COVERAGE"),
            "tested": sum(1 for c in cells if c.p_value is not None),
            "era_stable": sum(1 for c in cells if c.era_stable),
            "oos_confirms": sum(1 for c in cells if c.oos_confirms),
            "prominent": sum(1 for c in cells if c.status == "PROMINENT"),
        }
        report["arms"][key] = {**counts, **stats}
        rows.extend(c.row() for c in cells)

    frame = pd.DataFrame(rows)
    frame.to_parquet(os.path.join(OUT, "cells.parquet"), index=False)
    with open(os.path.join(OUT, "arm_report.json"), "w") as handle:
        json.dump(report, handle, indent=2, default=str)
    print(json.dumps(report, indent=2, default=str), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
