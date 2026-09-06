"""What does a big run look like BEFORE it starts? An event study.

THE CRITICISM THAT PROMPTED THIS (owner): the monthly winners did not begin
running on the 1st of the month -- they start slowly in the PREVIOUS month. A
calendar grid is arbitrary relative to the move, so a run beginning on the 18th
is split across two buckets and looks mediocre in both. That is very likely why
monthly_pick_v2.py found 16 features and 1 significant: the target was chopped,
not the features useless.

So this drops the calendar entirely and aligns on the RUN ITSELF.

    run       a name whose forward RUN_WINDOW-session return exceeds RUN_MIN
    t = 0     the session the run starts, i.e. the first day of that window
    study     every feature from t-40 to t+20, averaged across runs

CONTROL GROUP IS THE POINT. "Runners have rising volume before they run" is
worthless if every stock does. Each runner is compared against the SAME
features measured on non-runner name-days drawn from the same sessions, so the
market-wide backdrop cancels and only the difference survives.

OVERLAP IS SUPPRESSED. One 60% move would otherwise register as ~20 separate
"runs" on consecutive days and dominate the average. After a run is taken, that
name is skipped for RUN_WINDOW sessions.

NOTE ON LOOK-AHEAD: identifying t=0 uses forward returns, deliberately -- this
is a DESCRIPTIVE study of what setups look like, not a backtest. Nothing here is
tradeable until a feature measured at t<0 separates runners from controls, which
is exactly what the final table tests.

    python vanguard/research/run_anatomy.py
    python vanguard/research/run_anatomy.py --run-min 0.30 --run-window 15
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.monthly_pick_v2 import INDICES, daily_features  # noqa: E402
from research.overnight_intraday import SESSION_SQL, decompose  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
PRE, POST = 40, 20
PROFILE = ["px_vs_ema50", "ema_trend", "bb_pctb", "bb_width", "atr_pct",
           "atr_expand", "rvol", "vol_trend", "rsi_14", "box_height",
           "darvas_in_box"]


def find_runs(frame: pd.DataFrame, window: int, threshold: float) -> pd.DataFrame:
    runs = []
    for name, g in frame.groupby("underlying", sort=False):
        g = g.reset_index(drop=True)
        fwd = g["close_last"].shift(-window) / g["close_last"] - 1.0
        i, n = 0, len(g)
        while i < n:
            if pd.notna(fwd.iloc[i]) and fwd.iloc[i] >= threshold:
                runs.append({"underlying": name, "i": i, "dt": g["dt"].iloc[i],
                             "ret": fwd.iloc[i]})
                i += window          # suppress overlap
            else:
                i += 1
    return pd.DataFrame(runs)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=900)
    parser.add_argument("--run-window", type=int, default=20)
    parser.add_argument("--run-min", type=float, default=0.25)
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    start = date.today() - timedelta(days=args.lookback_days)
    connection = psycopg2.connect(args.dsn)
    try:
        spot_raw = pd.read_sql(SESSION_SQL, connection, params={"start": start})
    finally:
        connection.close()

    feat = daily_features(decompose(spot_raw))
    feat = feat[~feat["underlying"].isin(INDICES)].sort_values(["underlying", "dt"])
    feat = feat.reset_index(drop=True)
    feat["row"] = feat.groupby("underlying").cumcount()

    runs = find_runs(feat, args.run_window, args.run_min)
    if runs.empty:
        print("no runs found at this threshold")
        return 1
    print(f"window {feat['dt'].min().date()} .. {feat['dt'].max().date()}   "
          f"names={feat['underlying'].nunique()}")
    print(f"RUNS: forward {args.run_window}-session return >= {args.run_min * 100:.0f}%  "
          f"-> {len(runs):,} runs across {runs['underlying'].nunique()} names")
    print(f"      median run size {runs['ret'].median() * 100:.1f}%   "
          f"mean {runs['ret'].mean() * 100:.1f}%")

    idx = feat.set_index(["underlying", "row"])
    # ── event profile: features at each offset around the run start ────────
    rows = []
    for r in runs.itertuples():
        for off in range(-PRE, POST + 1):
            try:
                rec = idx.loc[(r.underlying, r.i + off)]
            except KeyError:
                continue
            rows.append({"off": off, **{c: rec[c] for c in PROFILE if c in rec}})
    prof = pd.DataFrame(rows).groupby("off").mean()

    # ── control: same features on all name-days that are NOT near a run ────
    near = set()
    for r in runs.itertuples():
        for off in range(-PRE, POST + 1):
            near.add((r.underlying, r.i + off))
    ctrl_mask = ~pd.MultiIndex.from_arrays(
        [feat["underlying"], feat["row"]]).isin(near)
    ctrl = feat[ctrl_mask][PROFILE].mean()

    print(f"\nFEATURE PATH AROUND THE RUN (control = {int(ctrl_mask.sum()):,} "
          f"non-run name-days)")
    offs = [-40, -30, -20, -15, -10, -5, -2, 0, 5, 10, 20]
    print(f"  {'feature':<15}" + "".join(f"{('t' + str(o)):>8}" for o in offs)
          + f"{'control':>9}")
    for c in PROFILE:
        if c not in prof:
            continue
        line = "".join(f"{prof[c].get(o, np.nan):>8.2f}" for o in offs)
        print(f"  {c:<15}{line}{ctrl[c]:>9.2f}")

    # ── the tradeable question: does t-1 separate runners from controls? ────
    print("\nSEPARATION AT t-1 (the last session BEFORE the run starts)")
    print(f"  {'feature':<15}{'runners':>10}{'control':>10}{'gap':>9}{'gap/sd':>9}")
    pre1 = []
    for r in runs.itertuples():
        try:
            pre1.append(idx.loc[(r.underlying, r.i - 1)])
        except KeyError:
            continue
    pre1 = pd.DataFrame(pre1)
    ctrl_df = feat[ctrl_mask]
    for c in PROFILE:
        if c not in pre1:
            continue
        a, b = pre1[c].dropna(), ctrl_df[c].dropna()
        if len(a) < 50 or b.std() == 0:
            continue
        gap = a.mean() - b.mean()
        print(f"  {c:<15}{a.mean():>10.3f}{b.mean():>10.3f}{gap:>+9.3f}"
              f"{gap / b.std():>+9.2f}")
    print("\n  gap/sd is the separation in control standard deviations — the only\n"
          "  column that matters, since the raw gap depends on each feature's units.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
