"""Arm-vs-its-own-placebo comparison, plus a planted-cycle positive control.

Two questions this answers that the per-arm FDR table cannot:

1. Every arm shows a small NEGATIVE mean lift in BOTH the genuine and the
   placebo arm.  That is a property of the pipeline (projections sit at a fixed
   lag from clustered anchors, and turns cluster), not a property of Gann's
   numbers.  So "hit rate vs empirical null" is the wrong contrast; the right
   one is genuine lift vs PLACEBO lift, which cancels the offset.  Reported
   with a Welch t-statistic and a per-instrument paired mean.

2. A null is uninterpretable unless the harness can detect a real cycle.  The
   positive control plants a genuine periodic turn structure in a synthetic
   series and runs it through the identical scoring path.
"""
from __future__ import annotations

import json
import math
import os
import sys

import numpy as np
import pandas as pd

from gann_tp_delta.cycles import CycleDef
from gann_tp_delta.research.practice.practice_harness import (
    ArmSpec,
    anchors_all_pivots,
    finalise,
    score_instrument,
    tol_fixed_sessions,
    tol_scaled_calendar,
    turns_pivot5_median,
)

RESULTS = os.environ.get("GANN_PRACTICE_RESULTS", "/scratch/results")


def welch(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    if len(a) < 3 or len(b) < 3:
        return float("nan"), float("nan")
    va, vb = a.var(ddof=1) / len(a), b.var(ddof=1) / len(b)
    t = (a.mean() - b.mean()) / math.sqrt(va + vb)
    df = (va + vb) ** 2 / (va**2 / (len(a) - 1) + vb**2 / (len(b) - 1))
    # normal approximation to the two-sided p (df is large in every arm here)
    p = math.erfc(abs(t) / math.sqrt(2.0))
    return t, p


def compare(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for arm, block in frame.groupby("arm"):
        g = block[(~block.is_placebo) & block.lift.notna()]
        p = block[(block.is_placebo) & block.lift.notna()]
        if g.empty or p.empty:
            continue
        t, pv = welch(g.lift.to_numpy(), p.lift.to_numpy())
        # per-instrument paired mean lift, so cycle-composition differences
        # between the two arms cannot drive the contrast
        gm = g.groupby("underlying").lift.mean()
        pm = p.groupby("underlying").lift.mean()
        joined = pd.concat([gm.rename("g"), pm.rename("p")], axis=1).dropna()
        diff = (joined.g - joined.p) if not joined.empty else pd.Series(dtype=float)
        tp, pp = (welch(joined.g.to_numpy(), joined.p.to_numpy()) if len(joined) > 3 else (float("nan"),) * 2)
        rows.append(
            {
                "arm": arm,
                "genuine_cells": len(g),
                "placebo_cells": len(p),
                "genuine_mean_lift": g.lift.mean(),
                "placebo_mean_lift": p.lift.mean(),
                "delta_lift": g.lift.mean() - p.lift.mean(),
                "welch_t": t,
                "welch_p": pv,
                "instruments_paired": len(joined),
                "paired_mean_delta": float(diff.mean()) if len(diff) else float("nan"),
                "paired_frac_positive": float((diff > 0).mean()) if len(diff) else float("nan"),
                "genuine_best_p": g.p_value.min(),
                "placebo_best_p": p.p_value.min(),
                "genuine_prominent": int((g.status == "PROMINENT").sum()),
                "placebo_prominent": int((p.status == "PROMINENT").sum()),
            }
        )
    return pd.DataFrame(rows).sort_values("arm")


# ── positive control ───────────────────────────────────────────────────────


def synthetic_frame(sessions: int = 1250, cycle: int = 90, strength: float = 0.9, seed: int = 7) -> pd.DataFrame:
    """Random walk with a PLANTED turn every ``cycle`` CALENDAR days.

    At each planted date the drift sign flips, so a genuine confirmed swing
    pivot forms there with probability ~``strength``.  Everything else is noise.
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2021-06-21", periods=sessions)
    ordinals = np.array([d.toordinal() for d in dates])
    origin = ordinals[0]
    sign = np.ones(sessions)
    current = 1.0
    last_flip = origin
    for i in range(sessions):
        if ordinals[i] - last_flip >= cycle and rng.random() < strength:
            current = -current
            last_flip = last_flip + cycle * ((ordinals[i] - last_flip) // cycle)
        sign[i] = current
    noise = rng.normal(0, 0.008, sessions)
    drift = sign * 0.004
    close = 20000 * np.exp(np.cumsum(drift + noise))
    high = close * (1 + np.abs(rng.normal(0, 0.004, sessions)))
    low = close * (1 - np.abs(rng.normal(0, 0.004, sessions)))
    return pd.DataFrame(
        {"time": dates, "open": close, "high": high, "low": low, "close": close,
         "volume": 0.0, "oi": 0.0}
    )


def positive_control() -> dict:
    out = {}
    for cycle_days in (90, 144):
        frame = synthetic_frame(cycle=cycle_days)
        cycles = [
            CycleDef(f"probe_{d}", "probe", d, f"probe {d}", "positive control")
            for d in (30, 45, 60, 90, 120, 135, 144, 180, 270, 360)
        ]
        for spec in (
            ArmSpec("ctrl_shipped", anchors_all_pivots, turns_pivot5_median, tol_fixed_sessions, 1),
            ArmSpec("ctrl_scaled", anchors_all_pivots, turns_pivot5_median, tol_scaled_calendar, 1),
        ):
            cells = score_instrument(
                spec, f"SYNTH{cycle_days}", "synthetic", frame, cycles, placebo=False, seed=1,
            )
            finalise(cells)
            table = [
                {
                    "cycle_days": c.cycle_days, "n": c.n_obs, "hit": c.hit_rate,
                    "null": c.null_rate, "lift": c.lift, "p": c.p_value,
                    "q": c.p_fdr, "status": c.status,
                }
                for c in cells if c.p_value is not None
            ]
            out[f"planted{cycle_days}|{spec.name}"] = table
    return out


def main() -> int:
    cells = pd.read_parquet(os.path.join(RESULTS, "cells.parquet"))
    table = compare(cells)
    print("=== ARM vs ITS OWN PLACEBO (lift = hit rate - empirical null) ===")
    print(table.round(5).to_string(index=False))

    print("\n=== POWER: median non-overlapping observations by class x cycle band ===")
    t = cells[(~cells.is_placebo) & (cells.arm == "A2_scaled_tolerance")].copy()
    t["band"] = pd.cut(t.cycle_days, [0, 45, 90, 180, 400],
                       labels=["<=45d", "46-90d", "91-180d", "181-400d"])
    power = t.groupby(["instrument_class", "band"], observed=True).agg(
        cells=("n_obs", "size"), median_obs=("n_obs", "median"),
        max_obs=("n_obs", "max"), pct_scored=("p_value", lambda s: float(s.notna().mean())),
    )
    print(power.round(3).to_string())

    print("\n=== POSITIVE CONTROL: planted cycle through the identical path ===")
    control = positive_control()
    for key, rows in control.items():
        print(f"-- {key}")
        print(pd.DataFrame(rows).round(4).to_string(index=False))

    payload = {
        "arm_vs_placebo": json.loads(table.to_json(orient="records")),
        "positive_control": control,
    }
    with open(os.path.join(RESULTS, "analysis.json"), "w") as handle:
        json.dump(payload, handle, indent=2, default=str)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
