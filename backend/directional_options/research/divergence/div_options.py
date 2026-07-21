"""(D) OPTION-LEVEL ECONOMICS ACROSS A STRIKE GRID.

The question the owner actually needs answered: given a setup that fires, WHICH
CONTRACT do you buy, and what does the payoff distribution look like -- an OTM
lottery with a 15% hit rate and a 600% winner, or a 55% ITM grinder?

DESIGN (all fixed before measuring)
  vehicle        monthly expiry only. near = first monthly with DTE >= 8 at
                 entry; far = the next monthly after that.
  strike grid    target moneyness K/S-1 in {+6%, +3%, 0, -3%, -6%} (CE), the
                 nearest available strike inside +-2.5% of the target. A band
                 that cannot be filled is COUNTED AS MISSING, never silently
                 replaced by a nearer strike -- that substitution is how the
                 +-8% bug produced its inverted headline.
  selection      from the 15:15 IST snapshot of the session BEFORE entry, so
                 the contract is known before the entry session opens.
  entry          the OPEN of the first 30m bar of the entry session.
  exit           the SAME 30m bar at which the spot triple barrier resolved
                 (target +2 ATR / stop -1 ATR / 15-session cutoff), and never
                 later than expiry - 2 calendar days.
  costs          ../setups_2d3d/harness.py COST_RT (0.6 / 1.6 / 4.0 % of
                 premium round trip) plus the owner's assumed 8%. SPREADS ARE
                 NOT IN OUR DATA -- every cost number here is ASSUMED.

THE STALE-ITM PROBLEM (the case study's central data finding)
  Our stock chains are ATM-TRACKER only. When the underlying runs, the tape of
  the contract that won simply STOPS -- in the PNB case 100% of the strikes
  that finished ITM had no tape on the exit date. Reading the exit off the tape
  would therefore delete the winners a second time, through the data instead of
  through the code. So the exit is MODELLED:
     * implied vol is inverted from the ENTRY premium (Black-Scholes, r=0,
       q=0), which is the only vol we have -- stock option IV is populated on
       ~1% of rows;
     * the exit premium is that same vol re-priced at the exit spot and exit
       DTE, floored at intrinsic;
     * where a real exit quote EXISTS, both are reported and the disagreement
       is quantified, so the model can be judged rather than trusted.
  The stale-exit rate is reported BY OUTCOME, which is the proof requested.
"""
from __future__ import annotations

import glob
import math
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
BANDS = {"OTM6": 0.06, "OTM3": 0.03, "ATM": 0.0, "ITM3": -0.03, "ITM6": -0.06}
BAND_TOL = 0.025
DTE_MIN = 8
EXIT_BUFFER_DAYS = 2
COSTS = {"optimistic": 0.006, "base": 0.016, "pessimistic": 0.040, "owner_8pct": 0.080}
PRICED_ARMS = ("cross_div", "cross_divany", "cross_divany_hl", "cross", "ctrl_random")


# ==========================================================================
# Black-Scholes (r = 0, q = 0) -- calls only, this study is long-CE
# ==========================================================================

def _nd(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call(S: float, K: float, T: float, v: float) -> float:
    if T <= 0 or v <= 0:
        return max(0.0, S - K)
    sq = v * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * v * v * T) / sq
    return S * _nd(d1) - K * _nd(d1 - sq)


def implied_vol(price: float, S: float, K: float, T: float) -> float:
    """Bisection on [1%, 400%]. Returns nan when the price is at/below intrinsic
    (nothing to imply) or above the no-arbitrage cap."""
    intr = max(0.0, S - K)
    if not (np.isfinite(price) and np.isfinite(S) and np.isfinite(K)) or T <= 0:
        return float("nan")
    if price <= intr + 1e-9 or price >= S:
        return float("nan")
    lo, hi = 0.01, 4.0
    if bs_call(S, K, T, hi) < price:
        return float("nan")
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if bs_call(S, K, T, mid) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# ==========================================================================
# tape
# ==========================================================================

def load_opt() -> pd.DataFrame:
    p = os.path.join(DATA, "optfull.parquet")
    if os.path.exists(p):
        return pd.read_parquet(p)
    frames = []
    for f in sorted(glob.glob(os.path.join(DATA, "optfull_*.csv"))):
        frames.append(pd.read_csv(f))
    o = pd.concat(frames, ignore_index=True)
    o["time"] = pd.to_datetime(o["time"], utc=True)
    o["mins"] = o["time"].dt.hour * 60 + o["time"].dt.minute
    o = o[(o["mins"] >= 225) & (o["mins"] <= 585)]
    o["session"] = (o["time"] + IST).dt.date
    o["expiry"] = pd.to_datetime(o["expiry"]).dt.date
    o = o.dropna(subset=["expiry", "strike", "option_type", "instrument_key"])
    o = o[o["option_type"] == "CE"].copy()
    # source-preference dedupe: upstox is the canonical writer for stock chains
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


# ==========================================================================
def main() -> None:
    daily, intra = B.load_panel()
    e = B.build_daily_elements(daily)
    bars = rc.Bars(intra, daily)
    ep = pd.read_parquet(os.path.join(DATA, "episodes.parquet"))
    ep = ep[ep["arm"].isin(PRICED_ARMS)].copy()
    o = load_opt()
    print("option tape (CE, no moneyness band):", len(o), "rows,",
          o["underlying"].nunique(), "underlyings", flush=True)
    banded = (np.abs(o["strike"] - o["underlying_price"])
              <= 0.08 * o["underlying_price"]).mean()
    print(f"rows the +-8% band would have kept: {banded:.2%}  "
          f"-> it deletes {1-banded:.2%} of the CE tape", flush=True)

    sess_of = {(r.underlying, int(r.sidx)): r.session for r in e.itertuples()}
    # 15:15 selection snapshot, indexed by (underlying, session)
    snap = o[(o["mins"] == 585) & (o["is_monthly"])]
    snap_by = {k: g for k, g in snap.groupby(["underlying", "session"], sort=False)}
    # per-contract tape
    tape = {k: g.sort_values("time") for k, g in o.groupby("instrument_key", sort=False)}

    rows = []
    miss = {"no_snapshot": 0, "no_expiry": 0, "no_strike": 0, "no_entry_bar": 0}
    for r in ep.itertuples():
        u = r.underlying
        sel_sess = sess_of.get((u, int(r.sidx_entry) - 1))
        ent_sess = sess_of.get((u, int(r.sidx_entry)))
        if sel_sess is None or ent_sess is None:
            miss["no_snapshot"] += 1
            continue
        sn = snap_by.get((u, sel_sess))
        if sn is None or sn.empty:
            miss["no_snapshot"] += 1
            continue
        S_sel = float(sn["underlying_price"].iloc[0])
        exps = sorted({x for x in sn["expiry"].unique()
                       if (x - ent_sess).days >= DTE_MIN})
        if not exps:
            miss["no_expiry"] += 1
            continue
        # exit bar: the 30m bar at which the SPOT barrier resolved
        Bu = bars.u[u]
        pos0 = bars.first_bar[(u, int(r.sidx_entry))]
        pos_x = pos0 + int(r.bars)
        t_exit = pd.Timestamp(Bu["time"][pos_x])
        S_exit = float(Bu["close"][pos_x])
        exit_sess = (t_exit + IST).date()
        for ei, exp in enumerate(exps[:2]):
            leg = "near" if ei == 0 else "far"
            avail = sn[sn["expiry"] == exp]
            if avail.empty:
                continue
            mny = avail["strike"].to_numpy(float) / S_sel - 1.0
            for band, tgt in BANDS.items():
                j = int(np.argmin(np.abs(mny - tgt)))
                if abs(mny[j] - tgt) > BAND_TOL:
                    miss["no_strike"] += 1
                    continue
                row_sel = avail.iloc[j]
                ik = row_sel["instrument_key"]
                K = float(row_sel["strike"])
                g = tape.get(ik)
                if g is None:
                    miss["no_entry_bar"] += 1
                    continue
                gt = g[g["session"] == ent_sess]
                if gt.empty:
                    miss["no_entry_bar"] += 1
                    continue
                e0 = gt.iloc[0]
                S_in = float(e0["underlying_price"])
                prem_in = max(float(e0["open"]), max(0.0, S_in - K))    # no-arb floor
                if prem_in <= 0:
                    continue
                # exit no later than expiry - EXIT_BUFFER_DAYS: walk BACK from
                # the barrier bar to the last 30m bar whose IST session date is
                # inside the cap. Never walks forward, so it cannot see ahead.
                cap_date = exp - pd.Timedelta(days=EXIT_BUFFER_DAYS)
                cap_date = cap_date.date() if hasattr(cap_date, "date") else cap_date
                pos_x2 = pos_x
                while pos_x2 > pos0 and (
                        pd.Timestamp(Bu["time"][pos_x2]) + IST).date() > cap_date:
                    pos_x2 -= 1
                t_x2 = pd.Timestamp(Bu["time"][pos_x2])
                S_x2 = float(Bu["close"][pos_x2])
                T_in = max((pd.Timestamp(exp) - pd.Timestamp(ent_sess)).days, 1) / 365.0
                T_out = max((exp - (t_x2 + IST).date()).days, 0) / 365.0
                v = implied_vol(prem_in, S_in, K, T_in)
                prem_model = (max(bs_call(S_x2, K, T_out, v), max(0.0, S_x2 - K))
                              if np.isfinite(v) else max(0.0, S_x2 - K))
                gx = g[g["time"] == t_x2]
                prem_tape = float(gx["close"].iloc[0]) if not gx.empty else np.nan
                if np.isfinite(prem_tape):
                    prem_tape = max(prem_tape, max(0.0, S_x2 - K))
                rows.append({
                    "arm": r.arm, "underlying": u, "mkt": r.mkt, "quarter": r.quarter,
                    "session_entry": ent_sess, "band": band, "leg": leg,
                    "strike": K, "expiry": exp, "instrument_key": ik,
                    "mny_sel": float(mny[j]), "dte_entry": (exp - ent_sess).days,
                    "S_in": S_in, "S_out": S_x2, "spot_ret": S_x2 / S_in - 1.0,
                    "prem_in": prem_in, "prem_model": prem_model, "prem_tape": prem_tape,
                    "iv_in": v, "stale_exit": int(not np.isfinite(prem_tape)),
                    "hit": r.hit, "large": r.large, "term_atr": r.term_atr,
                })
    t = pd.DataFrame(rows)
    t["gross_model"] = t["prem_model"] / t["prem_in"] - 1.0
    t["gross_tape"] = t["prem_tape"] / t["prem_in"] - 1.0
    for k, c in COSTS.items():
        t["net_" + k] = t["gross_model"] - c
    t.to_parquet(os.path.join(DATA, "opt_trades.parquet"), index=False)
    print("\nselection misses:", miss)
    print("priced legs:", len(t), " episodes covered:",
          t.groupby("arm")["session_entry"].size().to_dict())

    # ---------------- stale-exit proof --------------------------------------
    print("\n" + "=" * 78)
    print("STALE-EXIT RATE BY OUTCOME (the +-8%-band failure, reproduced in data)")
    print("=" * 78)
    print(t.groupby(["band", "large"])["stale_exit"].agg(["size", "mean"])
          .rename(columns={"mean": "stale_rate"}).to_string(
              float_format=lambda v: f"{v:.3f}"))
    print("\nby final moneyness of the contract at exit:")
    t["fin_mny"] = t["strike"] / t["S_out"] - 1.0
    t["fin_bucket"] = pd.cut(t["fin_mny"], [-1, -0.06, -0.02, 0.02, 0.06, 1],
                             labels=["deep ITM", "ITM", "near ATM", "OTM", "deep OTM"])
    print(t.groupby("fin_bucket", observed=True)["stale_exit"].agg(["size", "mean"])
          .rename(columns={"mean": "stale_rate"}).to_string(
              float_format=lambda v: f"{v:.3f}"))
    both = t.dropna(subset=["gross_tape"])
    if len(both):
        d = both["gross_model"] - both["gross_tape"]
        print(f"\nmodel vs tape where BOTH exist (n={len(both)}): "
              f"median diff {d.median():+.4f}  mean {d.mean():+.4f}  "
              f"p05 {d.quantile(.05):+.3f} p95 {d.quantile(.95):+.3f}")

    # ---------------- payoff distribution -----------------------------------
    print("\n" + "=" * 78)
    print("PAYOFF DISTRIBUTION BY ARM x STRIKE BAND x EXPIRY  (net of 1.6% base cost)")
    print("=" * 78)
    def dist(g: pd.DataFrame) -> pd.Series:
        x = g["net_base"]
        return pd.Series({
            "n": len(x), "hit_rate": float((x > 0).mean()),
            "mean": x.mean(), "median": x.median(),
            "p10": x.quantile(.10), "p25": x.quantile(.25), "p75": x.quantile(.75),
            "p90": x.quantile(.90), "p95": x.quantile(.95), "max": x.max(),
            "iv_med": g["iv_in"].median(), "dte_med": g["dte_entry"].median(),
            "stale": g["stale_exit"].mean(),
        })
    for arm in PRICED_ARMS:
        s = t[t["arm"] == arm]
        if s.empty:
            continue
        print(f"\n--- {arm}  (n legs {len(s)})")
        out = s.groupby(["leg", "band"], observed=True).apply(dist, include_groups=False)
        print(out.to_string(float_format=lambda v: f"{v:.4f}"))

    print("\n" + "=" * 78)
    print("COST SENSITIVITY (near-expiry legs only, mean net return)")
    print("=" * 78)
    n = t[t["leg"] == "near"]
    piv = n.pivot_table(index=["arm", "band"], values=["net_" + k for k in COSTS],
                        aggfunc="mean")
    print(piv.to_string(float_format=lambda v: f"{v:.4f}"))

    print("\n" + "=" * 78)
    print("SETUP vs CONTROL AT OPTION LEVEL (near, base cost, cluster bootstrap)")
    print("=" * 78)
    res = []
    for arm in ("cross_div", "cross_divany", "cross_divany_hl", "cross"):
        for band in BANDS:
            sub = n[(n["arm"].isin([arm, "ctrl_random"])) & (n["band"] == band)]
            if sub["arm"].nunique() < 2:
                continue
            r = rc.cluster_boot_diff(sub, "net_base", (sub["arm"] == arm).to_numpy(),
                                     (sub["arm"] == "ctrl_random").to_numpy())
            r.update({"arm": arm, "band": band})
            res.append(r)
    rdf = pd.DataFrame(res)
    if not rdf.empty:
        rdf["q_bh"] = rc.bh(list(rdf["p"]))
        rdf["p_bonf"] = np.minimum(1.0, rdf["p"] * len(rdf))
        print(rdf[["arm", "band", "n_a", "n_b", "mean_a", "mean_b", "diff", "lo", "hi",
                   "p", "p_bonf", "q_bh"]].to_string(
            index=False, float_format=lambda v: f"{v:.4f}"))
        rdf.to_csv(os.path.join(DATA, "opt_tests.csv"), index=False)

    print("\n" + "=" * 78)
    print("EX-TOP-3 AND PER-QUARTER (near ATM/OTM3, the two headline vehicles)")
    print("=" * 78)
    for arm in ("cross_divany", "cross_div"):
        for band in ("ATM", "OTM3", "OTM6", "ITM3", "ITM6"):
            s = n[(n["arm"] == arm) & (n["band"] == band)].sort_values(
                "net_base", ascending=False)
            if len(s) < 10:
                continue
            ex = s.iloc[3:]
            print(f"{arm:14s} {band:5s} n={len(s):4d} mean={s['net_base'].mean():+.4f} "
                  f"ex-top3={ex['net_base'].mean():+.4f} "
                  f"median={s['net_base'].median():+.4f} "
                  f"hit={float((s['net_base']>0).mean()):.3f} "
                  f"PNB legs={int((s['underlying']=='PNB').sum())} "
                  f"top3={', '.join(f'{r.underlying} {r.net_base:+.1%}' for r in s.head(3).itertuples())}")
    for arm in ("cross_divany",):
        for band in ("ATM", "OTM3"):
            s = n[(n["arm"] == arm) & (n["band"] == band)]
            print(f"\n--- {arm} {band} per quarter")
            print(s.groupby("quarter")["net_base"].agg(
                ["size", "mean", "median"]).to_string(float_format=lambda v: f"{v:.4f}"))


if __name__ == "__main__":
    main()
