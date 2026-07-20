"""Positive control + detection limit for the cycle-prominence harness.

A null result is only evidence if the instrument that produced it can detect
the thing it was looking for.  This module plants a KNOWN calendar cycle in a
synthetic series and asks, through the identical scoring path, how strong and
how PUNCTUAL that cycle has to be before the harness sees it.

Three knobs are swept:
  * ``jitter`` — how many calendar days the realised turn is displaced from the
    exact projected grid.  This is the decisive one.
  * ``amp``    — the size of the cyclical component relative to noise.
  * ``shape``  — a sharp V (cusp exactly on the grid) versus a smooth sinusoid.
    A smooth extremum plus noise locates its own turning point with an error of
    order sqrt(noise/curvature), which is many sessions wide; a cusp does not.

Run:
  docker run --rm -e PYTHONPATH=/app -v <repo>/backend:/app -v <scratch>:/scratch \
    -w /app tradebot-backend python gann_tp_delta/research/practice/detection_limit.py
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd

from gann_tp_delta.cycles import CycleDef
from gann_tp_delta.research.practice.practice_harness import (
    ArmSpec,
    anchors_all_pivots,
    anchors_major,
    score_instrument,
    tol_fixed_sessions,
    tol_flat_calendar3,
    tol_scaled_calendar,
    turns_pivot5_median,
)

OUT = os.environ.get("GANN_PRACTICE_RESULTS", "/scratch/results")
PROBES = (30, 45, 60, 90, 120, 135, 144, 180, 270, 360)


def planted_series(
    *,
    sessions: int = 1250,
    cycle: int = 90,
    amp: float = 0.06,
    noise: float = 0.006,
    jitter: int = 0,
    shape: str = "cusp",
    seed: int = 7,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2021-06-21", periods=sessions)
    ordinals = np.array([d.toordinal() for d in dates], dtype=float)
    t = ordinals - ordinals[0]
    if jitter:
        # displace each half-cycle's cusp by an independent uniform draw
        segment = np.floor(t / (cycle / 2.0)).astype(int)
        offsets = rng.integers(-jitter, jitter + 1, size=segment.max() + 2)
        t = t + offsets[segment]
    phase = (t % cycle) / cycle
    if shape == "cusp":
        wave = np.where(phase < 0.5, 4 * phase - 1, 3 - 4 * phase)
    else:
        wave = np.sin(2 * np.pi * phase)
    walk = np.cumsum(rng.normal(0, noise, sessions))
    close = 20000 * np.exp(walk) * (1 + amp * wave)
    high = close * (1 + np.abs(rng.normal(0, noise / 2, sessions)))
    low = close * (1 - np.abs(rng.normal(0, noise / 2, sessions)))
    return pd.DataFrame(
        {"time": dates, "open": close, "high": high, "low": low, "close": close,
         "volume": 0.0, "oi": 0.0}
    )


SPECS = {
    "A0_fixed_sessions_pm3": ArmSpec("x", anchors_all_pivots, turns_pivot5_median, tol_fixed_sessions, 1),
    "A1_calendar_pm3": ArmSpec("x", anchors_all_pivots, turns_pivot5_median, tol_flat_calendar3, 1),
    "A2_scaled_calendar": ArmSpec("x", anchors_all_pivots, turns_pivot5_median, tol_scaled_calendar, 1),
    "A3_major_anchors": ArmSpec("x", anchors_major, turns_pivot5_median, tol_scaled_calendar, 4),
    "A6_repeats": ArmSpec("x", anchors_all_pivots, turns_pivot5_median, tol_scaled_calendar, 4),
}


def probe(frame: pd.DataFrame, cycle: int, spec: ArmSpec) -> dict:
    cycles = [CycleDef(f"probe_{d}", "probe", d, f"probe {d}", "positive control") for d in PROBES]
    cells = score_instrument(spec, "SYNTH", "synthetic", frame, cycles, placebo=False, seed=1)
    scored = [c for c in cells if c.p_value is not None]
    if not scored:
        return {"target_p": None, "target_n": 0, "rank_of_target": None, "note": "no cell testable"}
    target = next((c for c in scored if c.cycle_days == cycle), None)
    order = sorted(scored, key=lambda c: c.p_value)
    return {
        "target_p": (float(target.p_value) if target else None),
        "target_n": (int(target.n_obs) if target else 0),
        "target_lift": (float(target.lift) if target else None),
        "rank_of_target": (order.index(target) + 1 if target else None),
        "scored_cells": len(scored),
        "best_cycle": int(order[0].cycle_days),
        "best_p": float(order[0].p_value),
    }


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    results: list[dict] = []
    for shape in ("cusp", "sine"):
        for amp in (0.04, 0.06, 0.10):
            for jitter in (0, 2, 4, 7, 12, 20):
                for cycle in (90, 144):
                    frame = planted_series(cycle=cycle, amp=amp, jitter=jitter, shape=shape)
                    for name, spec in SPECS.items():
                        row = {"shape": shape, "amp": amp, "jitter_days": jitter,
                               "planted_cycle": cycle, "arm": name}
                        row.update(probe(frame, cycle, spec))
                        results.append(row)
    frame = pd.DataFrame(results)
    frame.to_parquet(os.path.join(OUT, "detection_limit.parquet"), index=False)

    print("=== DETECTION OF A PLANTED CYCLE (target p-value; '-' = target untestable) ===")
    for shape in ("cusp", "sine"):
        for cycle in (90, 144):
            block = frame[frame["shape"].eq(shape) & frame["planted_cycle"].eq(cycle)]
            pivot = block.pivot_table(index="arm", columns=["amp", "jitter_days"],
                                      values="target_p", aggfunc="first")
            print(f"\n-- shape={shape} planted_cycle={cycle}d")
            print(pivot.map(lambda v: "-" if pd.isna(v) else f"{v:.2g}").to_string())
    with open(os.path.join(OUT, "detection_limit.json"), "w") as handle:
        json.dump(results, handle, indent=2, default=str)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
