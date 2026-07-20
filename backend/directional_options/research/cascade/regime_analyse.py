"""(1) REGIME DURATION — does consolidation dominate?

Reads ./data/regime/daily.parquet, applies the a-priori definitions in regime_defs.py and
prints every number the report quotes.  No fitting, no selection: the two
definitions and their declared sensitivity grids are all that is computed.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

from regime_defs import (ADX_TREND, THETA_MULT, add_causal_features, all_swings,
                    label_adx_regime)

HERE = os.path.dirname(os.path.abspath(__file__))
DAILY = os.path.join(HERE, "data", "regime", "daily.parquet")
CLASSES = ["index", "commodity", "stock"]
pd.set_option("display.width", 200)


def hdr(t: str) -> None:
    print("\n" + "=" * 78 + "\n" + t + "\n" + "=" * 78)


def runs(lab: pd.Series) -> pd.DataFrame:
    """Consecutive-label run lengths for one instrument's ordered labels."""
    s = lab.dropna()
    if s.empty:
        return pd.DataFrame(columns=["label", "length", "start", "end"])
    grp = (s != s.shift()).cumsum()
    out = s.groupby(grp).agg(label="first", length="size")
    idx = s.groupby(grp).apply(lambda x: (x.index[0], x.index[-1]))
    out["start"] = [a for a, _ in idx]
    out["end"] = [b for _, b in idx]
    return out.reset_index(drop=True)


def q(x: pd.Series) -> dict:
    x = x.dropna()
    if x.empty:
        return {}
    return {
        "n": int(len(x)), "mean": round(float(x.mean()), 2),
        "p25": round(float(x.quantile(.25)), 2), "median": round(float(x.median()), 2),
        "p75": round(float(x.quantile(.75)), 2), "p90": round(float(x.quantile(.90)), 2),
        "max": round(float(x.max()), 2),
    }


def main() -> None:
    d = pd.read_parquet(DAILY)
    d["session"] = pd.to_datetime(d["session"])
    d["year"] = d["session"].dt.year

    hdr("DATA")
    print(d.groupby("mclass").agg(instruments=("underlying", "nunique"),
                                  sessions=("sidx", "size"),
                                  first=("session", "min"), last=("session", "max")))
    per_inst = d.groupby(["mclass", "series_id"]).size()
    print("\nsessions per instrument (median by class):")
    print(per_inst.groupby("mclass").median())
    bad = d[d["raw_ret"].abs() > 0.20]
    print(f"\nsuspect sessions |ret|>20%: {len(bad)} of {len(d)} "
          f"({100*len(bad)/len(d):.3f}%) -> {bad.groupby('mclass').size().to_dict()}")
    print(bad[["underlying", "session", "raw_ret"]].to_string(index=False))

    d = add_causal_features(d).sort_values(["series_id", "sidx"]).reset_index(drop=True)
    d["regime"] = label_adx_regime(d, ADX_TREND)

    # ---------------------------------------------------------------- (1a)
    hdr("TIME IN EACH REGIME — causal Wilder ADX(14) >= 25")
    lab = d.dropna(subset=["regime"])
    tab = (lab.groupby(["mclass", "regime"]).size()
           .unstack(fill_value=0))
    tab["pct_trending"] = 100 * tab["trending"] / tab.sum(axis=1)
    print(tab)
    print("\nper-instrument %% trending — distribution within class:")
    pi = (lab.groupby(["mclass", "series_id"])["regime"]
          .apply(lambda s: 100 * (s == "trending").mean()))
    print(pi.groupby("mclass").describe()[["count", "mean", "min", "25%", "50%", "75%", "max"]].round(1))

    print("\nDECLARED sensitivity grid (ADX threshold), %% of sessions trending:")
    for th in (20, 25, 30):
        r = label_adx_regime(d, th).dropna()
        row = d.loc[r.index].groupby("mclass").apply(
            lambda g: 100 * (r.loc[g.index] == "trending").mean())
        print(f"  ADX>={th}: " + "  ".join(f"{k}={v:.1f}%" for k, v in row.items()))

    hdr("TIME IN EACH REGIME — causal cross-check, efficiency ratio(20) >= 0.5")
    e = d.dropna(subset=["er20"])
    print("median ER20 by class:", e.groupby("mclass")["er20"].median().round(3).to_dict())
    for cut in (0.3, 0.5, 0.7):
        row = e.groupby("mclass")["er20"].apply(lambda s: 100 * (s >= cut).mean())
        print(f"  ER>={cut}: " + "  ".join(f"{k}={v:.1f}%" for k, v in row.items()))
    both = e.assign(adxT=e["adx"] >= ADX_TREND, erT=e["er20"] >= 0.5)
    print("\nagreement of the two causal lenses (% of sessions):")
    print((100 * both.groupby("mclass")
           .apply(lambda g: pd.Series({
               "both_trend": (g.adxT & g.erT).mean(),
               "adx_only": (g.adxT & ~g.erT).mean(),
               "er_only": (~g.adxT & g.erT).mean(),
               "both_chop": (~g.adxT & ~g.erT).mean()}))).round(1))

    # ---------------------------------------------------------------- (1b)
    hdr("REGIME RUN LENGTHS (sessions) — how long does a move last once started")
    allruns = []
    for (mc, u), g in d.groupby(["mclass", "series_id"]):
        r = runs(g.set_index("sidx")["regime"])
        r["mclass"], r["series_id"] = mc, u
        allruns.append(r)
    R = pd.concat(allruns, ignore_index=True)
    for mc in CLASSES:
        for labname in ("trending", "consolidating"):
            s = R[(R.mclass == mc) & (R.label == labname)]["length"]
            print(f"{mc:10s} {labname:14s}", q(s))
    print("\nshare of trending runs lasting >= k sessions:")
    for mc in CLASSES:
        s = R[(R.mclass == mc) & (R.label == "trending")]["length"]
        print(f"  {mc:10s} " + "  ".join(
            f">={k}: {100*(s>=k).mean():.0f}%" for k in (2, 3, 5, 10, 20)))

    # size of the ADX trending runs (close-to-close over the run)
    hdr("MOVE SIZE — close-to-close % over each causal ADX trending run")
    px = d.set_index(["series_id", "sidx"])["c"]
    tr = R[R.label == "trending"].copy()
    st = px.reindex(pd.MultiIndex.from_arrays([tr.series_id, tr.start])).to_numpy()
    en = px.reindex(pd.MultiIndex.from_arrays([tr.series_id, tr.end])).to_numpy()
    tr["move_pct"] = 100 * (en / st - 1.0)
    tr["abs_pct"] = tr["move_pct"].abs()
    for mc in CLASSES:
        s = tr[tr.mclass == mc]
        print(f"{mc:10s} signed", q(s["move_pct"]))
        print(f"{mc:10s} abs   ", q(s["abs_pct"]))

    # ---------------------------------------------------------------- (1c)
    hdr(f"SWING DECOMPOSITION — directional change, theta = {THETA_MULT} x median daily TR%")
    SW = all_swings(d, THETA_MULT)
    th = (d.groupby(["mclass", "series_id"])
          .apply(lambda g: 100 * THETA_MULT * ((g.h - g.l) / g.c).median()))
    print("theta (%) by class:", th.groupby("mclass").median().round(2).to_dict())
    Q = SW[SW.qualifies]
    for mc in CLASSES:
        s = Q[Q.mclass == mc]
        print(f"\n{mc}: qualifying moves n={len(s)} "
              f"({len(s)/max(1,d[d.mclass==mc].series_id.nunique()):.1f} per instrument)")
        print("   duration (sessions):", q(s["duration"]))
        print("   size (%)           :", q(100 * s["abs_size"]))
        print("   up/down            :", s.direction.value_counts().to_dict())

    hdr("MOVE VELOCITY — net % per session, qualifying swings")
    Q = Q.copy()
    Q["velocity"] = 100 * Q["abs_size"] / Q["duration"].clip(lower=1)
    for mc in CLASSES:
        s = Q[Q.mclass == mc]
        print(f"{mc:10s} all       ", q(s["velocity"]))
        big = s[s["abs_size"] >= s["abs_size"].quantile(0.75)]
        print(f"{mc:10s} top-quartile size", q(big["velocity"]),
              f"  duration median {big['duration'].median():.0f}")

    hdr("ARE BIG MOVES FAST OR LONG?  size vs duration")
    from scipy.stats import spearmanr
    for mc in CLASSES:
        s = Q[Q.mclass == mc]
        rho, p = spearmanr(s["abs_size"], s["duration"])
        rv, pv = spearmanr(s["abs_size"], s["velocity"])
        print(f"{mc:10s} spearman(size,duration)={rho:+.3f} (p={p:.1e})   "
              f"spearman(size,velocity)={rv:+.3f} (p={pv:.1e})   n={len(s)}")
        dec = s.assign(dq=pd.qcut(s["abs_size"], 4, labels=["Q1", "Q2", "Q3", "Q4"]))
        print("   by size quartile:",
              dec.groupby("dq", observed=True).apply(
                  lambda g: f"dur={g['duration'].median():.0f} vel={g['velocity'].median():.2f}"
              ).to_dict())

    # session coverage: fraction of sessions that sit inside a qualifying move
    cov = {}
    for mc in CLASSES:
        tot = int(d[d.mclass == mc].groupby("series_id").size().sum())
        inside = int(Q[Q.mclass == mc]["duration"].sum())
        cov[mc] = (inside, tot, 100 * inside / tot)
    print("\nsessions inside a qualifying move / total sessions:")
    for mc, (i, t, p) in cov.items():
        print(f"  {mc:10s} {i:7d} / {t:7d} = {p:5.1f}%   -> consolidation {100-p:5.1f}%")

    print("\nDECLARED sensitivity grid (theta multiplier), % of sessions inside a move:")
    for mult in (2.0, 3.0, 4.0):
        S = all_swings(d, mult)
        S = S[S.qualifies]
        line = []
        for mc in CLASSES:
            tot = int(d[d.mclass == mc].groupby("series_id").size().sum())
            line.append(f"{mc}={100*S[S.mclass==mc]['duration'].sum()/tot:.1f}%")
        print(f"  theta={mult}x: " + "  ".join(line))

    # ---------------------------------------------------------------- (1d)
    hdr("CONCENTRATION — how much of the year's travel comes from the top N moves")
    d["abslog"] = np.log1p(d["ret"].fillna(0)).abs()
    travel = d.groupby(["mclass", "series_id", "year"])["abslog"].sum().rename("travel")
    sess = d.groupby(["mclass", "series_id", "year"]).size().rename("sessions")
    base = pd.concat([travel, sess], axis=1).reset_index()
    Q2 = Q.copy()
    Q2["year"] = pd.to_datetime(Q2["end_session"]).dt.year
    Q2["logsize"] = np.log1p(Q2["size"]).abs()
    rows = []
    for (mc, u, yr), g in Q2.groupby(["mclass", "series_id", "year"]):
        b = base[(base.series_id == u) & (base.year == yr)]
        if b.empty or b["sessions"].iloc[0] < 100:   # need a near-full year
            continue
        g = g.sort_values("logsize", ascending=False)
        tv, ns = float(b["travel"].iloc[0]), int(b["sessions"].iloc[0])
        swing_tv = float(g["logsize"].sum())
        r = {"mclass": mc, "series_id": u, "year": yr, "sessions": ns, "n_moves": len(g)}
        for N in (1, 3, 5, 10):
            top = g.head(N)
            r[f"top{N}_travel_share"] = 100 * top["logsize"].sum() / tv
            r[f"top{N}_swing_share"] = 100 * top["logsize"].sum() / swing_tv if swing_tv else np.nan
            r[f"top{N}_session_share"] = 100 * top["duration"].sum() / ns
        rows.append(r)
    C = pd.DataFrame(rows)
    print(f"instrument-years with >=100 sessions: {len(C)}")
    for mc in CLASSES:
        s = C[C.mclass == mc]
        if s.empty:
            continue
        print(f"\n{mc}  (n={len(s)} instrument-years)")
        for N in (1, 3, 5, 10):
            print(f"   top{N:2d}: share of ALL daily travel {s[f'top{N}_travel_share'].median():5.1f}%"
                  f" | share of net SWING travel {s[f'top{N}_swing_share'].median():5.1f}%"
                  f" | sessions occupied {s[f'top{N}_session_share'].median():5.1f}%"
                  f" | travel-per-time ratio {s[f'top{N}_swing_share'].median()/max(1e-9, s[f'top{N}_session_share'].median()):.2f}")
    print("\nper-year detail (class medians):")
    print(C.groupby(["mclass", "year"])[["top3_travel_share", "top3_session_share",
                                         "top5_travel_share", "top5_session_share",
                                         "n_moves", "sessions"]].median().round(1))

    # ---------------------------------------------------------------- verdict inputs
    hdr("VERDICT INPUTS")
    for mc in CLASSES:
        adx_trend = 100 * (lab[lab.mclass == mc]["regime"] == "trending").mean()
        move_cov = cov[mc][2]
        s = Q[Q.mclass == mc]
        print(f"{mc:10s} ADX-trending {adx_trend:5.1f}% | inside-move {move_cov:5.1f}% | "
              f"median move {100*s['abs_size'].median():4.1f}% over "
              f"{s['duration'].median():.0f} sessions | "
              f"moves/instrument/yr "
              f"{len(s)/d[d.mclass==mc].series_id.nunique()/ (d[d.mclass==mc].groupby('series_id').size().median()/250):.1f}")


if __name__ == "__main__":
    main()
