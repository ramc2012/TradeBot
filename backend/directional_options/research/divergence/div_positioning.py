"""(D, element f) OPTION-PARTICIPANT POSITIONING, and the strike-coverage audit.

Honest limit stated UP FRONT (from the case study): stock chains in our store
are ATM-TRACKER only -- roughly 4-5 strikes per underlying per week -- IV and
greeks are ~1% populated for stock options, and OI is partially populated. So
this element is measurable ONLY as OI/volume on near-ATM strikes, and even that
is thin. Nothing is substituted for it: what cannot be measured is reported as
a data gap.

Measured at the 15:15 IST snapshot of the session BEFORE entry (causal):
  oi_ce, oi_pe      total OI across the monthly chain we hold
  d_oi_ce_5         5-session change in CE OI (build > 0 / unwind < 0)
  d_oi_pe_5         same for PE
  pcr_oi            PE OI / CE OI
  vol_oi_ce         CE volume / CE OI (turnover intensity)
Each is then tested for CONDITIONING power on the spot outcome, in deciles and
by cluster bootstrap of the top vs bottom tercile.

Also reported: STRIKE-BAND COVERAGE -- the share of episodes for which each
moneyness band could actually be filled from our store. A band that is only
fillable for a minority of names is a SELECTED subsample, and any result on it
must be read as such.
"""
from __future__ import annotations

import glob
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "cascade"))
sys.path.insert(0, os.path.join(HERE, "..", "setups_2d3d"))

import div_build as B  # noqa: E402
import div_defs as D  # noqa: E402
import run_cascade as rc  # noqa: E402

IST = pd.Timedelta(hours=5, minutes=30)


def load_all() -> pd.DataFrame:
    p = os.path.join(DATA, "optsnap.parquet")
    if os.path.exists(p):
        return pd.read_parquet(p)
    frames = []
    for f in sorted(glob.glob(os.path.join(DATA, "optfull_*.csv"))):
        d = pd.read_csv(f, usecols=["time", "underlying", "expiry", "strike", "option_type",
                                    "close", "volume", "oi", "iv", "delta",
                                    "underlying_price", "instrument_key", "source"])
        d["time"] = pd.to_datetime(d["time"], utc=True)
        d = d[(d["time"].dt.hour * 60 + d["time"].dt.minute) == 585]
        frames.append(d)
    o = pd.concat(frames, ignore_index=True)
    o["session"] = (o["time"] + IST).dt.date
    o["expiry"] = pd.to_datetime(o["expiry"]).dt.date
    o["src_rank"] = (o["source"] != "upstox").astype(int)
    o = (o.sort_values(["instrument_key", "time", "src_rank"])
           .drop_duplicates(["instrument_key", "time"], keep="first"))
    o.to_parquet(p, index=False)
    return o


def main() -> None:
    o = load_all()
    o["mny"] = o["strike"] / o["underlying_price"] - 1.0
    stock = ~o["underlying"].isin(B.INDEX)
    print("=" * 78)
    print("DATA AVAILABILITY (15:15 IST snapshots, whole extraction)")
    print("=" * 78)
    for nm, m in (("stocks", stock), ("indices", ~stock)):
        s = o[m]
        print(f"{nm:8s} rows={len(s):8d}  oi non-null {s['oi'].notna().mean():.1%}  "
              f"oi>0 {(s['oi'].fillna(0) > 0).mean():.1%}  "
              f"iv non-null {s['iv'].notna().mean():.1%}  "
              f"delta non-null {s['delta'].notna().mean():.1%}  "
              f"volume>0 {(s['volume'].fillna(0) > 0).mean():.1%}")
    ss = o[stock]
    per = ss.groupby(["underlying", "session"])["strike"].nunique()
    print(f"\ndistinct stock strikes per underlying-session: median "
          f"{per.median():.0f}  p10 {per.quantile(.1):.0f}  p90 {per.quantile(.9):.0f}")
    span = ss.groupby(["underlying", "session"])["mny"].agg(["min", "max"])
    print(f"moneyness span held per underlying-session: median low "
          f"{span['min'].median():+.1%}  median high {span['max'].median():+.1%}")

    # ---------------- strike-band coverage --------------------------------
    t = pd.read_parquet(os.path.join(DATA, "opt_trades.parquet"))
    ep = pd.read_parquet(os.path.join(DATA, "episodes.parquet"))
    print("\n" + "=" * 78)
    print("STRIKE-BAND COVERAGE  (share of episodes each band could be filled)")
    print("=" * 78)
    for arm in t["arm"].unique():
        n_ep = int((ep["arm"] == arm).sum())
        sub = t[(t["arm"] == arm) & (t["leg"] == "near")]
        cov = sub.groupby("band")["underlying"].size() / max(n_ep, 1)
        print(f"{arm:16s} episodes={n_ep:5d}  " +
              "  ".join(f"{b} {cov.get(b, 0):.0%}" for b in
                        ("ITM6", "ITM3", "ATM", "OTM3", "OTM6")))
    print("\nA band below ~40% coverage is a SELECTED subsample of names whose "
          "\nstrike ladder happens to be wide in our ATM-tracker store; treat any "
          "\nITM6/OTM6 result as conditional on that selection.")

    # ---------------- positioning features --------------------------------
    print("\n" + "=" * 78)
    print("POSITIONING FEATURES AT THE SELECTION SNAPSHOT")
    print("=" * 78)
    near = o[o["mny"].abs() <= 0.05].copy()
    agg = (near.groupby(["underlying", "session", "option_type"])
                .agg(oi=("oi", "sum"), vol=("volume", "sum")).reset_index())
    piv = agg.pivot_table(index=["underlying", "session"], columns="option_type",
                          values=["oi", "vol"]).reset_index()
    piv.columns = ["underlying", "session"] + [f"{a}_{b}" for a, b in piv.columns[2:]]
    piv = piv.sort_values(["underlying", "session"])
    for c in ("oi_CE", "oi_PE"):
        if c in piv:
            piv["d5_" + c] = piv.groupby("underlying")[c].pct_change(5)
    piv["pcr_oi"] = piv.get("oi_PE") / piv.get("oi_CE")
    piv["vol_oi_ce"] = piv.get("vol_CE") / piv.get("oi_CE")

    daily, intra = B.load_panel()
    e = B.build_daily_elements(daily)
    sess_of = {(r.underlying, int(r.sidx)): r.session for r in e.itertuples()}
    feats = piv.set_index(["underlying", "session"])

    ep = ep[ep["arm"].isin(["cross", "cross_div", "cross_divany", "ctrl_random"])].copy()
    ep["sel_session"] = [sess_of.get((u, s - 1)) for u, s in
                         zip(ep["underlying"], ep["sidx_entry"])]
    idx = pd.MultiIndex.from_arrays([ep["underlying"], ep["sel_session"]])
    for c in ("d5_oi_CE", "d5_oi_PE", "pcr_oi", "vol_oi_ce", "oi_CE"):
        if c in feats.columns:
            ep[c] = feats[c].reindex(idx).to_numpy()

    print("\ncoverage of the positioning features on setup episodes:")
    for arm in ("cross", "cross_divany", "cross_div"):
        s = ep[ep["arm"] == arm]
        print(f"  {arm:14s} n={len(s):5d}  " + "  ".join(
            f"{c} {s[c].notna().mean():.0%}" for c in
            ("oi_CE", "d5_oi_CE", "d5_oi_PE", "pcr_oi", "vol_oi_ce") if c in s))

    print("\nCONDITIONING TEST (widest arm = `cross`, quintiles of each feature)")
    base = ep[ep["arm"] == "cross"]
    res = []
    for c in ("d5_oi_CE", "d5_oi_PE", "pcr_oi", "vol_oi_ce"):
        if c not in base:
            continue
        d = base.dropna(subset=[c]).copy()
        if len(d) < 100:
            print(f"  {c}: n={len(d)} too few to test -- DATA GAP")
            continue
        d["q"] = pd.qcut(d[c].rank(method="first"), 5, labels=False)
        g = d.groupby("q").agg(n=("large", "size"), p=("large", "mean"),
                               term=("term_atr", "mean"), val=(c, "median"))
        rho = d[[c, "term_atr"]].corr(method="spearman").iloc[0, 1]
        print(f"\n  {c}  n={len(d)}  spearman(x, term_atr)={rho:+.3f}")
        print(g.to_string(float_format=lambda v: f"{v:.4f}"))
        hi = (d["q"] == 4).to_numpy()
        lo = (d["q"] == 0).to_numpy()
        r = rc.cluster_boot_diff(d, "term_atr", hi, lo)
        r.update({"feature": c})
        res.append(r)
        print(f"   top-vs-bottom quintile term_atr diff {r['diff']:+.4f} "
              f"[{r['lo']:+.3f},{r['hi']:+.3f}] p={r['p']:.4f}")
    if res:
        rdf = pd.DataFrame(res)
        rdf["q_bh"] = rc.bh(list(rdf["p"]))
        rdf.to_csv(os.path.join(DATA, "positioning_tests.csv"), index=False)
        print("\nBH across the positioning grid:")
        print(rdf[["feature", "n_a", "n_b", "mean_a", "mean_b", "diff", "p", "q_bh"]]
              .to_string(index=False, float_format=lambda v: f"{v:.4f}"))


if __name__ == "__main__":
    main()
