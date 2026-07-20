"""(VERIFY 2c) Re-run the pyramid economics on the REPAIRED (untruncated) option
tape and diff it against the shipped result.

Contract SELECTION is unaffected by the repair: selection happens at the 15:15
snapshot inside a |moneyness| <= 6% band, well inside the 8% extraction wall,
so the same contracts are picked from both tapes.  Only the PRICE PATH differs.
That makes this a clean controlled diff: any change in P&L is attributable to
the missing bars alone.

Outputs data/ver_repair.parquet with one row per (band, arm, family, episode)
carrying both the shipped roc_base and the repaired roc_base.
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

DATA = os.path.join(HERE, "data")
IST = harness.IST


def full_tape() -> pd.DataFrame:
    f = pd.read_parquet(os.path.join(DATA, "ver_fulltape.parquet"))
    f["session"] = (f["time"] + IST).dt.date
    f["expiry"] = pd.to_datetime(f["expiry"]).dt.date
    f = f.dropna(subset=["expiry", "strike", "option_type", "instrument_key"])
    ex = f[["underlying", "expiry"]].drop_duplicates()
    ex["ym"] = pd.to_datetime(ex["expiry"]).dt.to_period("M")
    last = ex.groupby(["underlying", "ym"])["expiry"].max().rename("le").reset_index()
    ex = ex.merge(last, on=["underlying", "ym"])
    ex["is_monthly"] = ex["expiry"] == ex["le"]
    return f.merge(ex[["underlying", "expiry", "is_monthly"]],
                   on=["underlying", "expiry"], how="left")


def main() -> None:
    intra, daily = rc.load()
    bars = rc.Bars(intra, daily)
    P.rc_first = bars.first_bar
    D = mat_run.daily_arrays(daily)
    epi = mat_run.build_episodes(intra, daily, bars)
    epi = epi[epi["family"].isin(P.FAMS)].copy()
    sess_map = {(r.underlying, r.session): int(r.sidx)
                for r in daily[["underlying", "session", "sidx"]].itertuples()}

    old = P.load_opt_cached()
    new = full_tape()

    rows = []
    for band in harness.MNY_BANDS:
        sel_o = harness.build_selection(old, harness.MNY_BANDS[band])
        sel_n = harness.build_selection(new, harness.MNY_BANDS[band])
        cmap_o = P.contract_map(sel_o, sess_map)
        cmap_n = P.contract_map(sel_n, sess_map)
        same = sum(1 for k, v in cmap_o.items()
                   if cmap_n.get(k, (None,))[0] == v[0])
        print(f"[{band}] selection rows old={len(cmap_o)} new={len(cmap_n)} "
              f"identical_contract={same}", flush=True)
        ser_o = P.opt_series(old, {v[0] for v in cmap_o.values()})
        ser_n = P.opt_series(new, {v[0] for v in cmap_o.values()})
        # LIQUIDITY STRESS. Some recovered deep-ITM bars are untraded prints
        # (volume 0, price unchanged all session). Dropping them makes prem()
        # fall back to the last bar that actually TRADED, i.e. a last-traded-
        # price fill instead of a phantom quote.
        ser_l = P.opt_series(new[new["volume"] > 0], {v[0] for v in cmap_o.values()})
        for arm in P.ARMS:
            for ep in epi.itertuples():
                B = bars.u.get(ep.underlying)
                d = D.get(ep.underlying)
                if B is None or d is None:
                    continue
                ro = P.simulate(B, d, ep, cmap_o, ser_o, arm, P.PRIMARY_RULE)
                rn = P.simulate(B, d, ep, cmap_o, ser_n, arm, P.PRIMARY_RULE)
                if ro is None or rn is None:
                    continue
                rl = P.simulate(B, d, ep, cmap_o, ser_n, arm, P.PRIMARY_RULE,
                                lag_bars=1)
                rq = P.simulate(B, d, ep, cmap_o, ser_l, arm, P.PRIMARY_RULE)
                rows.append({"band": band, "arm": arm, "family": ep.family,
                             "underlying": ep.underlying, "mkt": ep.mkt,
                             "side": int(ep.side), "quarter": ep.quarter,
                             "entry_time": ep.entry_time, "s2": int(ep.s2),
                             "roc_old": ro["roc_base"], "roc_new": rn["roc_base"],
                             "roc_new_opt": rn["roc_optimistic"],
                             "roc_new_pess": rn["roc_pessimistic"],
                             "pnl_new": rn["pnl_base"],
                             "gross_new": rn["pnl_optimistic"] +
                             0.006 * rn["units"] * P.UNIT,
                             "units": rn["units"], "fills": rn["fills"],
                             "roc_new_lag1": rl["roc_base"] if rl else np.nan,
                             "roc_liq": rq["roc_base"] if rq else np.nan})
        print("  band done, rows", len(rows), flush=True)
    out = pd.DataFrame(rows)
    out.to_parquet(os.path.join(DATA, "ver_repair.parquet"))
    print("wrote", len(out))


if __name__ == "__main__":
    main()
