"""(VERIFY, defect 3) SESSION-INDEX HOLES.

Every horizon in this series is counted in SESSION INDEX, not calendar time:
the label window is s0+10 sessions, HOLD_CAP is 10 sessions, the stage-2 window
is 3 sessions.  The spot table has gaps for a handful of names — CUMMINSIND has
no session at all between 2025-09-23 (sidx 39) and 2026-03-23 (sidx 40), a
181-day hole.  For an episode entered at sidx 33 the "10-session" outcome is
therefore resolved against prices SIX MONTHS later, and the move looks
enormous (MFE 9.0 ATR) purely because the clock jumped.

`simulate()` already drops episodes whose tape is TRUNCATED, but a hole is not
a truncation: sidx s0+10 exists, so the episode is kept.

This script measures how many episodes are affected and re-runs the headline
without them.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "setups_2d3d"))

import run_cascade as rc  # noqa: E402
import ver_stats as VS  # noqa: E402

DATA = os.path.join(HERE, "data")
MAX_SPAN_DAYS = 25          # 10 trading sessions is <= ~16 calendar days
OUT = []


def p(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    OUT.append(s)


def main() -> None:
    intra, daily = rc.load()
    d = daily.sort_values(["underlying", "sidx"]).copy()
    d["dt"] = pd.to_datetime(d["session"].astype(str))
    d["gap"] = d.groupby("underlying")["dt"].diff().dt.days

    p("=" * 78)
    p("DEFECT 3 — session-index holes: a 'N-session' horizon that spans months")
    p("=" * 78)
    holes = d[d["gap"] > 7]
    p(f"sessions preceded by a >7 calendar-day hole: {len(holes)} "
      f"across {holes['underlying'].nunique()} underlyings of "
      f"{d['underlying'].nunique()}")
    p(holes[["underlying", "session", "sidx", "gap"]].to_string(index=False))

    # calendar span of each episode's 10-session window
    span = {}
    for u, g in d.groupby("underlying", sort=False):
        dt = g["dt"].to_numpy()
        sx = g["sidx"].to_numpy()
        for i, s in enumerate(sx):
            j = min(i + 10, len(dt) - 1)
            span[(u, int(s))] = (dt[j] - dt[i]) / np.timedelta64(1, "D")

    t = pd.read_parquet(os.path.join(DATA, "ver_repair.parquet"))
    ep = pd.read_parquet(os.path.join(DATA, "mat_episodes.parquet"))
    # rebuild s0 from entry_time via the daily session map
    smap = {(r.underlying, pd.Timestamp(str(r.session))): int(r.sidx)
            for r in d[["underlying", "session", "sidx"]].itertuples()}
    et = pd.to_datetime(t["entry_time"], utc=True) + pd.Timedelta(hours=5, minutes=30)
    t["_s0"] = [smap.get((u, pd.Timestamp(x.date())), -1)
                for u, x in zip(t["underlying"], et)]
    t["span"] = [span.get((u, s), np.nan) for u, s in zip(t["underlying"], t["_s0"])]
    t["holed"] = t["span"] > MAX_SPAN_DAYS

    p(f"\nepisode-trades whose 10-session window spans > {MAX_SPAN_DAYS} calendar "
      f"days: {int(t['holed'].sum())} / {len(t)} ({t['holed'].mean():.3%})")
    if t["holed"].any():
        h = t[t["holed"]]
        p(h.groupby(["underlying"])["span"].agg(["size", "max"]).to_string())
        p(f"  their mean roc_new {h['roc_new'].mean():+.4f} vs clean "
          f"{t[~t['holed']]['roc_new'].mean():+.4f}")

    p("\nHEADLINE WITH HOLED EPISODES REMOVED (repaired tape, base cost)")
    p(f"{'band':11s} {'arm':11s} {'family':12s} {'n':>5s} {'all':>9s} "
      f"{'clean':>9s} {'n_clean':>7s}")
    for band in ("deep_itm", "slight_itm"):
        for arm in ("pyramid", "fixed_t1", "fixed_hold"):
            for fam in ("s1_primary", "ctrl_long"):
                g = t[(t.band == band) & (t.arm == arm) & (t.family == fam)]
                c = g[~g["holed"]]
                if len(g) < 20:
                    continue
                p(f"{band:11s} {arm:11s} {fam:12s} {len(g):5d} "
                  f"{g['roc_new'].mean():+9.4f} {c['roc_new'].mean():+9.4f} "
                  f"{len(c):7d}")

    p("\nSIGNAL vs CONTROL on the CLEAN subset (episode-clustered bootstrap)")
    for band in ("deep_itm", "slight_itm"):
        for arm in ("pyramid", "fixed_t1"):
            a = t[(t.band == band) & (t.arm == arm) & (t.family == "s1_primary")
                  & (~t["holed"])]
            b = t[(t.band == band) & (t.arm == arm) & (t.family == "ctrl_long")
                  & (~t["holed"])]
            if len(a) < 20 or len(b) < 20:
                continue
            dd, lo, hi, pv = VS.boot_diff(a["roc_new"].to_numpy(),
                                          a["underlying"].to_numpy(),
                                          b["roc_new"].to_numpy(),
                                          b["underlying"].to_numpy())
            p(f"  {band:11s} {arm:11s} signal - ctrl_long {dd:+.4f} "
              f"[{lo:+.4f},{hi:+.4f}] p={pv:.4f}")

    with open(os.path.join(HERE, "ver_holes.txt"), "w") as fh:
        fh.write("\n".join(OUT) + "\n")


if __name__ == "__main__":
    main()
