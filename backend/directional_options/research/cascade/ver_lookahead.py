"""(VERIFY 1) END-TO-END lookahead test.

The shipped test_cascade_causality.py proves prefix invariance of the INDICATOR
FILTERS on synthetic data. That is necessary but not sufficient: it never runs
the real pipeline, and its one path test perturbs bars BEFORE the entry (past
independence), which is not the lookahead direction.

This test cuts the ENTIRE dataset at a date T, rebuilds everything from the
truncated data, and asserts that for every episode whose stage-2 window and
label horizon close well before T:

  1. the stage-1 episode SET is identical,
  2. the stage-2 confirm flag and its lag are identical,
  3. the "sustained large move" label is identical,
  4. the option contract chosen for each tranche is identical,
  5. the entry premium of each tranche is identical.

If anything downstream peeked at a bar after its own decision point, at least
one of these must change when the future is deleted.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "setups_2d3d"))

import harness  # noqa: E402
import mat_run  # noqa: E402
import pyr_run as P  # noqa: E402
import run_cascade as rc  # noqa: E402

CUT = pd.Timestamp("2026-01-05", tz="UTC")
# an episode is comparable only if its own 3-session stage-2 window and
# 10-session label horizon both close before the cut, with slack
SAFE = pd.Timestamp("2025-11-15", tz="UTC")
OUT = []


def p(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    OUT.append(s)


def build(intra, daily, opt):
    bars = rc.Bars(intra, daily)
    P.rc_first = bars.first_bar
    epi = mat_run.build_episodes(intra, daily, bars)
    epi = epi[epi["family"] == "s1_primary"].copy()
    sess_map = {(r.underlying, r.session): int(r.sidx)
                for r in daily[["underlying", "session", "sidx"]].itertuples()}
    sel = harness.build_selection(opt, harness.MNY_BANDS["deep_itm"])
    cmap = P.contract_map(sel, sess_map)
    ser = P.opt_series(opt, {v[0] for v in cmap.values()})

    rows = []
    for ep in epi.itertuples():
        B = bars.u.get(ep.underlying)
        if B is None:
            continue
        con = cmap.get((ep.underlying, int(ep.side), int(ep.s0)))
        px = np.nan
        if con and con[0] in ser:
            px = P.prem(ser[con[0]], B["time"][ep.bar], "open")
        c2, px2 = None, np.nan
        if bool(ep.s2):
            e2 = bars.first_bar.get((ep.underlying, int(ep.s2_sidx) + 1))
            if e2 is not None:
                c2 = cmap.get((ep.underlying, int(ep.side), int(B["sidx"][e2])))
                if c2 and c2[0] in ser:
                    px2 = P.prem(ser[c2[0]], B["time"][e2], "open")
        ps = rc.path_stats(B, ep.bar, int(ep.side), float(ep.atr_abs),
                           int(ep.s0) + rc.LARGE_HORIZON_SESSIONS)
        rows.append({"key": f"{ep.underlying}|{ep.entry_time}|{ep.side}",
                     "s2": int(ep.s2),
                     "s2_lag": int(ep.s2_sidx) - int(ep.s0) if ep.s2 else -1,
                     "large": int(ps["large"]) if ps else -1,
                     "atr": round(float(ep.atr_abs), 10),
                     "con1": con[0] if con else None, "px1": px,
                     "con2": c2[0] if c2 else None, "px2": px2,
                     "entry_time": ep.entry_time})
    return pd.DataFrame(rows)


def main() -> None:
    intra, daily = rc.load()
    opt = P.load_opt_cached()
    p("=" * 78)
    p("VERIFY 1 — end-to-end lookahead: delete the future, rebuild, compare")
    p("=" * 78)
    p(f"cut = {CUT.date()}   comparable window = entries before {SAFE.date()}")

    full = build(intra, daily, opt)

    ci = intra[intra["time"] < CUT].copy()
    cd = daily[pd.to_datetime(daily["session"].astype(str)) < CUT.tz_localize(None)].copy()
    cd["sidx"] = cd.groupby("underlying").cumcount()
    co = opt[opt["time"] < CUT].copy()
    p(f"truncated: intra {len(intra)}->{len(ci)}  daily {len(daily)}->{len(cd)}  "
      f"opt {len(opt)}->{len(co)}")

    cut = build(ci, cd, co)

    a = full[full["entry_time"] < SAFE].set_index("key").sort_index()
    b = cut[cut["entry_time"] < SAFE].set_index("key").sort_index()
    p(f"\nepisodes in comparable window: full={len(a)}  after-cut={len(b)}")
    only_full = sorted(set(a.index) - set(b.index))
    only_cut = sorted(set(b.index) - set(a.index))
    p(f"  episodes only in the FULL run : {len(only_full)}  {only_full[:5]}")
    p(f"  episodes only in the CUT run  : {len(only_cut)}  {only_cut[:5]}")
    ok = set(a.index) & set(b.index)
    a, b = a.loc[sorted(ok)], b.loc[sorted(ok)]

    fails = 0
    for col in ("s2", "s2_lag", "large", "con1", "con2"):
        diff = (a[col].astype(str) != b[col].astype(str)).sum()
        p(f"  [{'PASS' if diff == 0 else 'FAIL'}] {col:8s} mismatches {diff} / {len(a)}")
        fails += diff
    for col in ("px1", "px2", "atr"):
        x, y = a[col].to_numpy(float), b[col].to_numpy(float)
        m = np.isfinite(x) | np.isfinite(y)
        bad = int((~np.isclose(x[m], y[m], rtol=1e-12, atol=1e-12,
                               equal_nan=True)).sum())
        p(f"  [{'PASS' if bad == 0 else 'FAIL'}] {col:8s} mismatches {bad} / {int(m.sum())}")
        fails += bad
    p(f"\n  episode-set mismatches: {len(only_full) + len(only_cut)}")
    fails += len(only_full) + len(only_cut)

    p("\nDISCRIMINATION CHECK — the same test must FAIL on a deliberately "
      "lookahead-contaminated variant:")
    ci2 = intra.copy()
    # contaminate: let the 30m ADX see 1 bar into the future
    ci2["m_adx14"] = ci2.groupby("underlying")["m_adx14"].shift(-1)
    ci2 = ci2[ci2["time"] < CUT]
    bad_build = build(ci2, cd, co)
    b2 = bad_build[bad_build["entry_time"] < SAFE].set_index("key")
    p(f"  contaminated episode set differs from full by "
      f"{len(set(a.index) ^ set(b2.index))} keys "
      f"({'PASS - the test can detect lookahead' if set(a.index) ^ set(b2.index) else 'FAIL - test is blind'})")

    p(f"\nVERDICT: {'PASS — no lookahead detected' if fails == 0 else f'FAIL — {fails} mismatches'}")
    with open(os.path.join(HERE, "ver_lookahead.txt"), "w") as fh:
        fh.write("\n".join(OUT) + "\n")


if __name__ == "__main__":
    main()
