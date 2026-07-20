"""(VERIFY 2) Is the OPTION PRICE PATH complete?

The prior pass in this series was wrong because a +-8% moneyness filter applied
at EXTRACTION deleted option bars once spot moved far from the strike, and the
simulator silently fell back to the last surviving bar. A pyramided winner
drives its contract deep ITM, so the winners are exactly the trades whose exit
bar goes missing.

setups_2d3d/extract.py line 53:
    AND abs(strike - underlying_price) <= 0.08 * underlying_price
is a PER-BAR predicate, so the same defect class is present in the tape that
pyr_run.py consumes.

This script re-runs the pyramid simulation with pyr_run.prem() instrumented so
that every price lookup records whether it resolved to the EXACT requested 30m
bar or fell back to a stale earlier bar, and writes one row per trade.
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

CUR: dict = {}
_orig_prem = P.prem


def prem_instrumented(S, t, field):
    i = int(np.searchsorted(S["time"], t))
    exact = i < len(S["time"]) and S["time"][i] == t
    v = _orig_prem(S, t, field)
    if CUR:
        CUR["n"] += 1
        if not exact:
            CUR["stale"] += 1
            j = int(np.searchsorted(S["time"], t, side="right")) - 1
            if j >= 0:
                gap = (pd.Timestamp(t) - pd.Timestamp(S["time"][j])) / pd.Timedelta("1D")
                CUR["max_gap_days"] = max(CUR["max_gap_days"], float(gap))
                CUR["tape_end"] = pd.Timestamp(S["time"][-1])
    return v


P.prem = prem_instrumented


def main() -> None:
    intra, daily = rc.load()
    bars = rc.Bars(intra, daily)
    P.rc_first = bars.first_bar
    D = mat_run.daily_arrays(daily)
    epi = mat_run.build_episodes(intra, daily, bars)
    epi = epi[epi["family"].isin(P.FAMS)].copy()

    opt = P.load_opt_cached()
    sess_map = {(r.underlying, r.session): int(r.sidx)
                for r in daily[["underlying", "session", "sidx"]].itertuples()}

    rows = []
    for band in harness.MNY_BANDS:
        sel = harness.build_selection(opt, harness.MNY_BANDS[band])
        cmap = P.contract_map(sel, sess_map)
        keys = {v[0] for v in cmap.values()}
        ser = P.opt_series(opt, keys)
        # tape metadata per contract: last bar, moneyness at last bar
        meta = {}
        op = opt[opt["instrument_key"].isin(keys)]
        for k, g in op.groupby("instrument_key", sort=False):
            g = g.sort_values("time")
            up = float(g["underlying_price"].iloc[-1])
            st = float(g["strike"].iloc[0])
            meta[k] = {"last_t": pd.Timestamp(g["time"].iloc[-1]),
                       "last_absmny": abs(st - up) / up,
                       "expiry": pd.Timestamp(g["expiry"].iloc[-1]),
                       "nbars": len(g)}
        for arm in ("pyramid", "fixed_t1"):
            for ep in epi.itertuples():
                B = bars.u.get(ep.underlying)
                d = D.get(ep.underlying)
                if B is None or d is None:
                    continue
                CUR.clear()
                CUR.update({"n": 0, "stale": 0, "max_gap_days": 0.0,
                            "tape_end": pd.NaT})
                r = P.simulate(B, d, ep, cmap, ser, arm, P.PRIMARY_RULE)
                if r is None:
                    continue
                # contracts this trade used
                cons = [cmap.get((ep.underlying, int(ep.side), int(ep.s0)))]
                if arm == "pyramid" and bool(ep.s2):
                    e2 = bars.first_bar.get((ep.underlying, int(ep.s2_sidx) + 1))
                    if e2 is not None:
                        cons.append(cmap.get((ep.underlying, int(ep.side),
                                              int(B["sidx"][e2]))))
                m = [meta.get(c[0]) for c in cons if c]
                rows.append({
                    "band": band, "arm": arm, "family": ep.family,
                    "underlying": ep.underlying, "mkt": ep.mkt,
                    "side": int(ep.side), "quarter": ep.quarter,
                    "entry_time": ep.entry_time, "s2": int(ep.s2),
                    "roc_base": r["roc_base"],
                    "lookups": CUR["n"], "stale": CUR["stale"],
                    "max_gap_days": CUR["max_gap_days"],
                    "tape_last_absmny": max([x["last_absmny"] for x in m if x],
                                            default=np.nan),
                    "tape_nbars": min([x["nbars"] for x in m if x], default=np.nan),
                })
        print("band", band, "rows", len(rows), flush=True)
    out = pd.DataFrame(rows)
    out.to_parquet(os.path.join(HERE, "data", "ver_trunc.parquet"))
    print("wrote", len(out))


if __name__ == "__main__":
    main()
