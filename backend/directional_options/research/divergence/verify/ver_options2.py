"""VERIFY-D1 (fix): rebuild the option strike grid on the REPAIRED tape.

Identical pricing / selection / exit logic to div_options.py -- the ONLY change
is the tape: no `underlying_price IS NOT NULL` predicate, and the spot is
joined from our own 30m spot panel instead of read off the option row. Every
comparison in div_options.py is recomputed so the two can be diffed cell by
cell.
"""
from __future__ import annotations
import glob, os, sys
import numpy as np, pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DIV = os.path.abspath(os.path.join(HERE, ".."))
DATA = os.path.join(DIV, "data")
sys.path.insert(0, DIV)
sys.path.insert(0, os.path.join(DIV, "..", "cascade"))
sys.path.insert(0, os.path.join(DIV, "..", "setups_2d3d"))

import div_build as B
import div_options as O
import run_cascade as rc

IST = pd.Timedelta(hours=5, minutes=30)


def load_opt2(intra: pd.DataFrame) -> pd.DataFrame:
    p = os.path.join(DATA, "optfull2.parquet")
    if os.path.exists(p):
        return pd.read_parquet(p)
    frames = [pd.read_csv(f) for f in sorted(glob.glob(os.path.join(DATA, "opt2_*.csv")))]
    o = pd.concat(frames, ignore_index=True)
    o["time"] = pd.to_datetime(o["time"], utc=True)
    o["mins"] = o["time"].dt.hour * 60 + o["time"].dt.minute
    o = o[(o["mins"] >= 225) & (o["mins"] <= 585)]
    o["session"] = (o["time"] + IST).dt.date
    o["expiry"] = pd.to_datetime(o["expiry"]).dt.date
    o = o.dropna(subset=["expiry", "strike", "option_type", "instrument_key"])
    # ---- THE FIX: spot from our own 30m panel, not from the option row ----
    sp = intra[["underlying", "time", "close"]].rename(columns={"close": "spot_panel"})
    o = o.merge(sp, on=["underlying", "time"], how="left")
    o["underlying_price"] = o["underlying_price"].where(
        o["underlying_price"].notna() & (o["underlying_price"] > 0), o["spot_panel"])
    o = o[o["underlying_price"].notna() & (o["underlying_price"] > 0)].copy()
    o["src_rank"] = (o["source"] != "upstox").astype(int)
    o = (o.sort_values(["instrument_key", "time", "src_rank"])
           .drop_duplicates(["instrument_key", "time"], keep="first"))
    ex = o[["underlying", "expiry"]].drop_duplicates()
    ex["ym"] = pd.to_datetime(ex["expiry"]).dt.to_period("M")
    last = ex.groupby(["underlying", "ym"])["expiry"].max().rename("le").reset_index()
    ex = ex.merge(last, on=["underlying", "ym"])
    ex["is_monthly"] = ex["expiry"] == ex["le"]
    o = o.merge(ex[["underlying", "expiry", "is_monthly"]], on=["underlying", "expiry"])
    o.to_parquet(p, index=False)
    return o


def price(ep, e, bars, o):
    """Verbatim copy of div_options.main()'s pricing loop."""
    sess_of = {(r.underlying, int(r.sidx)): r.session for r in e.itertuples()}
    snap = o[(o["mins"] == 585) & (o["is_monthly"])]
    snap_by = {k: g for k, g in snap.groupby(["underlying", "session"], sort=False)}
    tape = {k: g.sort_values("time") for k, g in o.groupby("instrument_key", sort=False)}
    rows = []
    for r in ep.itertuples():
        u = r.underlying
        sel_sess = sess_of.get((u, int(r.sidx_entry) - 1))
        ent_sess = sess_of.get((u, int(r.sidx_entry)))
        if sel_sess is None or ent_sess is None:
            continue
        sn = snap_by.get((u, sel_sess))
        if sn is None or sn.empty:
            continue
        S_sel = float(sn["underlying_price"].iloc[0])
        exps = sorted({x for x in sn["expiry"].unique() if (x - ent_sess).days >= O.DTE_MIN})
        if not exps:
            continue
        Bu = bars.u[u]
        pos0 = bars.first_bar[(u, int(r.sidx_entry))]
        pos_x = pos0 + int(r.bars)
        for ei, exp in enumerate(exps[:2]):
            leg = "near" if ei == 0 else "far"
            avail = sn[sn["expiry"] == exp]
            if avail.empty:
                continue
            mny = avail["strike"].to_numpy(float) / S_sel - 1.0
            for band, tgt in O.BANDS.items():
                j = int(np.argmin(np.abs(mny - tgt)))
                if abs(mny[j] - tgt) > O.BAND_TOL:
                    continue
                row_sel = avail.iloc[j]
                ik = row_sel["instrument_key"]; K = float(row_sel["strike"])
                g = tape.get(ik)
                if g is None:
                    continue
                gt = g[g["session"] == ent_sess]
                if gt.empty:
                    continue
                e0 = gt.iloc[0]
                S_in = float(e0["underlying_price"])
                prem_in = max(float(e0["open"]), max(0.0, S_in - K))
                if prem_in <= 0:
                    continue
                cap_date = exp - pd.Timedelta(days=O.EXIT_BUFFER_DAYS)
                cap_date = cap_date.date() if hasattr(cap_date, "date") else cap_date
                pos_x2 = pos_x
                while pos_x2 > pos0 and (pd.Timestamp(Bu["time"][pos_x2]) + IST).date() > cap_date:
                    pos_x2 -= 1
                t_x2 = pd.Timestamp(Bu["time"][pos_x2]); S_x2 = float(Bu["close"][pos_x2])
                T_in = max((pd.Timestamp(exp) - pd.Timestamp(ent_sess)).days, 1) / 365.0
                T_out = max((exp - (t_x2 + IST).date()).days, 0) / 365.0
                v = O.implied_vol(prem_in, S_in, K, T_in)
                prem_model = (max(O.bs_call(S_x2, K, T_out, v), max(0.0, S_x2 - K))
                              if np.isfinite(v) else max(0.0, S_x2 - K))
                gx = g[g["time"] == t_x2]
                prem_tape = float(gx["close"].iloc[0]) if not gx.empty else np.nan
                if np.isfinite(prem_tape):
                    prem_tape = max(prem_tape, max(0.0, S_x2 - K))
                rows.append({"arm": r.arm, "underlying": u, "mkt": r.mkt,
                             "quarter": r.quarter, "session_entry": ent_sess,
                             "band": band, "leg": leg, "strike": K, "expiry": exp,
                             "instrument_key": ik, "mny_sel": float(mny[j]),
                             "dte_entry": (exp - ent_sess).days, "S_in": S_in,
                             "S_out": S_x2, "spot_ret": S_x2 / S_in - 1.0,
                             "prem_in": prem_in, "prem_model": prem_model,
                             "prem_tape": prem_tape, "iv_in": v,
                             "stale_exit": int(not np.isfinite(prem_tape)),
                             "hit": r.hit, "large": r.large, "term_atr": r.term_atr})
    t = pd.DataFrame(rows)
    t["gross_model"] = t["prem_model"] / t["prem_in"] - 1.0
    t["gross_tape"] = t["prem_tape"] / t["prem_in"] - 1.0
    for k, c in O.COSTS.items():
        t["net_" + k] = t["gross_model"] - c
    return t


def main():
    daily, intra = B.load_panel()
    e = B.build_daily_elements(daily)
    bars = rc.Bars(intra, daily)
    ep = pd.read_parquet(os.path.join(DATA, "episodes.parquet"))
    ep = ep[ep["arm"].isin(O.PRICED_ARMS)].copy()
    o = load_opt2(intra)
    old = pd.read_parquet(os.path.join(DATA, "optfull.parquet"))
    print("=" * 78)
    print("TAPE REPAIR")
    print("=" * 78)
    print(f"old tape (with underlying_price predicate): {len(old):>9,} rows  "
          f"{old['instrument_key'].nunique():>6,} contracts  "
          f"{old['underlying'].nunique()} underlyings")
    print(f"new tape (predicate removed, spot joined) : {len(o):>9,} rows  "
          f"{o['instrument_key'].nunique():>6,} contracts  "
          f"{o['underlying'].nunique()} underlyings")
    print("new tape by source:\n", o.groupby("source").size().to_string())
    banded = (np.abs(o["strike"] - o["underlying_price"]) <= 0.08 * o["underlying_price"]).mean()
    print(f"\n+-8%% band would keep {banded:.2%} of the REPAIRED tape "
          f"-> deletes {1-banded:.2%}")
    # PNB 28-JUL proof
    pnb = o[(o["underlying"] == "PNB") & (o["expiry"].astype(str) == "2026-07-28")]
    pnbo = old[(old["underlying"] == "PNB") & (old["expiry"].astype(str) == "2026-07-28")]
    print(f"\nPNB 2026-07-28 CE rows: old {len(pnbo)}   repaired {len(pnb)}")

    t = price(ep, e, bars, o)
    t.to_parquet(os.path.join(DATA, "opt_trades2.parquet"), index=False)
    told = pd.read_parquet(os.path.join(DATA, "opt_trades.parquet"))
    print(f"\npriced legs: old {len(told)}  repaired {len(t)}")

    print("\n" + "=" * 78)
    print("STALE-EXIT RATE BY OUTCOME (repaired tape)")
    print("=" * 78)
    print(t.groupby(["band", "large"])["stale_exit"].agg(["size", "mean"]).to_string(
        float_format=lambda v: f"{v:.3f}"))
    t["fin_mny"] = t["strike"] / t["S_out"] - 1.0
    t["fin_bucket"] = pd.cut(t["fin_mny"], [-1, -0.06, -0.02, 0.02, 0.06, 1],
                             labels=["deep ITM", "ITM", "near ATM", "OTM", "deep OTM"])
    print("\nby final moneyness:")
    print(t.groupby("fin_bucket", observed=True)["stale_exit"].agg(["size", "mean"]).to_string(
        float_format=lambda v: f"{v:.3f}"))
    both = t.dropna(subset=["gross_tape"])
    d = both["gross_model"] - both["gross_tape"]
    print(f"\nmodel vs tape where BOTH exist (n={len(both)}): median {d.median():+.4f} "
          f"mean {d.mean():+.4f} p05 {d.quantile(.05):+.3f} p95 {d.quantile(.95):+.3f}")
    print(f"iv_in un-invertible (entry print at/below intrinsic) by band:\n"
          f"{t.groupby('band')['iv_in'].apply(lambda s: (~np.isfinite(s)).mean()).to_string(float_format=lambda v: f'{v:.3f}')}")

    def dist(g):
        x = g["net_base"]
        return pd.Series({"n": len(x), "hit": float((x > 0).mean()), "mean": x.mean(),
                          "median": x.median(), "p10": x.quantile(.10), "p75": x.quantile(.75),
                          "p90": x.quantile(.90), "p95": x.quantile(.95), "max": x.max(),
                          "stale": g["stale_exit"].mean()})
    print("\n" + "=" * 78)
    print("PAYOFF DISTRIBUTION, NEAR LEG, net 1.6%  (repaired)")
    print("=" * 78)
    n = t[t["leg"] == "near"]
    for arm in O.PRICED_ARMS:
        s = n[n["arm"] == arm]
        if s.empty:
            continue
        print(f"\n--- {arm} (n={len(s)})")
        print(s.groupby("band", observed=True).apply(dist, include_groups=False)
              .to_string(float_format=lambda v: f"{v:.4f}"))

    print("\n" + "=" * 78)
    print("SETUP vs RANDOM CONTROL, option level (repaired)")
    print("=" * 78)
    res = []
    for arm in ("cross_div", "cross_divany", "cross_divany_hl", "cross"):
        for band in O.BANDS:
            sub = n[(n["arm"].isin([arm, "ctrl_random"])) & (n["band"] == band)]
            if sub["arm"].nunique() < 2:
                continue
            r = rc.cluster_boot_diff(sub, "net_base", (sub["arm"] == arm).to_numpy(),
                                     (sub["arm"] == "ctrl_random").to_numpy())
            r.update({"arm": arm, "band": band}); res.append(r)
    rdf = pd.DataFrame(res)
    rdf["q_bh"] = rc.bh(list(rdf["p"]))
    rdf["p_bonf"] = np.minimum(1.0, rdf["p"] * len(rdf))
    print(rdf[["arm", "band", "n_a", "n_b", "mean_a", "mean_b", "diff", "lo", "hi", "p",
               "p_bonf", "q_bh"]].to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    rdf.to_csv(os.path.join(DATA, "opt_tests2.csv"), index=False)

    print("\n" + "=" * 78)
    print("CONCENTRATION (repaired): ex-top3, ex-PNB, per quarter")
    print("=" * 78)
    for arm in ("cross_div", "cross_divany"):
        for band in ("ITM6", "ITM3", "ATM", "OTM3", "OTM6"):
            s = n[(n["arm"] == arm) & (n["band"] == band)].sort_values("net_base", ascending=False)
            if len(s) < 8:
                continue
            ex = s.iloc[3:]
            nop = s[s["underlying"] != "PNB"]
            print(f"{arm:14s} {band:5s} n={len(s):4d} mean={s['net_base'].mean():+.4f} "
                  f"med={s['net_base'].median():+.4f} hit={float((s['net_base']>0).mean()):.3f} "
                  f"ex3={ex['net_base'].mean():+.4f} exPNB={nop['net_base'].mean():+.4f} "
                  f"(PNB legs {int((s['underlying']=='PNB').sum())}) "
                  f"top3={', '.join(f'{r.underlying} {r.net_base:+.0%}' for r in s.head(3).itertuples())}")
    print("\nper quarter, cross_divany:")
    print(n[n["arm"] == "cross_divany"].groupby(["quarter", "band"], observed=True)["net_base"]
          .agg(["size", "mean", "median"]).to_string(float_format=lambda v: f"{v:.3f}"))
    print("\ncost sensitivity (near, mean net):")
    print(n.pivot_table(index=["arm", "band"], values=["net_" + k for k in O.COSTS],
                        aggfunc="mean").to_string(float_format=lambda v: f"{v:.4f}"))


if __name__ == "__main__":
    main()
