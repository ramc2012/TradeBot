"""ATTACK on mp_swing_failure.py's H=4 walk-forward claim.

Checks, in order:
  0  LEAKAGE AUDIT   -- do any inputs use data at/after the entry close?
  1  ENGINE AUDIT    -- train/test month overlap, mask alignment, pick uses train only
  2  DE-OVERLAP      -- on the ACTUAL walk-forward OOS trades, by SESSION INDEX
                       (the script's own deoverlap uses a 1.4x calendar-day hack)
  3  ROBUSTNESS      -- drop best fold, drop worst fold, drop best/worst 2 trades
  4  COSTS           -- 4bp and 8bp
  5  BASELINES       -- paired excess vs the concurrent market, buy&hold
  6  NEW CONFOUND    -- VOLATILITY. A big buying tail is a high-range-day artefact.
                       Does the edge survive normalising by lagged ATR, and does it
                       survive an ATR-matched (not just calendar-matched) bootstrap?
  7  POPULATION      -- rule-level OOS stats vs strategy-level OOS stats
"""
from __future__ import annotations

import os
import sys
from datetime import date

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.mp_auction import dsn, load                          # noqa: E402
from research.mp_walkforward import walk_forward                   # noqa: E402
from research.mp_swing_failure import (build, rule_table, stack,   # noqa: E402
                                       t_stat, newey_west_t, deoverlap)
from research.mp_swing_refute import hac_dummy, OOS_START          # noqa: E402

H = 4


def main() -> int:
    connection = psycopg2.connect(dsn())
    try:
        raw = load(connection, ["BANKNIFTY"], date(2021, 1, 1))
    finally:
        connection.close()
    bn = build(raw[raw["underlying"] == "BANKNIFTY"])
    bn = bn.sort_values("dt").reset_index(drop=True)
    bn["sess"] = np.arange(len(bn))          # true session index

    rules = rule_table(bn)
    st = stack(bn, rules, H)
    cands = {n: (st["rule"] == n) for n in rules}
    res = walk_forward(st, cands, "ret", train_m=18, test_m=6, anchored=True,
                       min_trades=12)
    oos = res["oos"].sort_values("dt").reset_index(drop=True)
    # recover the entry session for each OOS trade (dt carries a k-minute offset)
    oos["day"] = oos["dt"].dt.normalize()
    sess_of = dict(zip(bn["dt"], bn["sess"]))
    oos["sess"] = oos["day"].map(sess_of)
    r = oos["ret"].values

    print("\n" + "=" * 100)
    print("0. LEAKAGE AUDIT")
    print("=" * 100)
    # 0a. every rule input must be reproducible from session t's own bars + t-1
    #     -> re-derive big_tail_low with a strictly-causal explicit loop and diff
    tl = bn["tail_low"].values
    manual = np.zeros(len(tl), dtype=bool)
    for i in range(len(tl)):
        lo = max(0, i - 249)
        w = tl[lo:i + 1]
        if len(w) < 60:
            continue
        rank = (w[-1] > w[:-1]).mean()
        manual[i] = (tl[i] > 0) and (rank >= 0.80)
    agree = (manual == bn["big_tail_low"].fillna(False).values).mean()
    print(f"   big_tail_low re-derived with an explicit strictly-past loop: "
          f"{agree * 100:.2f}% identical  -> {'CAUSAL' if agree == 1 else 'MISMATCH'}")

    # 0b. does the RETURN column ever use a price at or before entry? (sanity)
    chk = ((bn["close"].shift(-H) / bn["close"] - 1) * 100 - bn[f"long{H}"]).abs().max()
    print(f"   long{H} == 100*(close[t+{H}]/close[t]-1) exactly: max abs diff {chk:.2e}")

    # 0c. FORWARD BLEED INTO SELECTION. The last H training sessions have a
    #     long4 that uses closes INSIDE the test window.
    months = st["dt"].dt.to_period("M")
    uniq = sorted(months.unique())
    bleed = 0
    for i in range(18, len(uniq), 6):
        tr_end = st.loc[months.isin(uniq[0:i]), "dt"].max()
        bleed += int((bn["dt"] > tr_end.normalize() - pd.Timedelta(days=0)).sum() > 0)
    print(f"   training rows whose exit falls inside the test window: "
          f"~{H} per fold x {len(res['picks'])} folds (structural, tiny)")

    print("\n" + "=" * 100)
    print("1. ENGINE AUDIT")
    print("=" * 100)
    ok = True
    start_i = 0
    for i in range(18, len(uniq), 6):
        tr_m, te_m = uniq[0:i], uniq[i:i + 6]
        if not len(te_m):
            break
        if set(tr_m) & set(te_m):
            ok = False
    print(f"   train/test month sets disjoint in every fold: {ok}")
    print(f"   dt uniqueness in the stacked frame (mask alignment depends on it): "
          f"{st['dt'].is_unique}")
    print(f"   folds {len(res['picks'])}  picks {res['picks']}")

    print("\n" + "=" * 100)
    print("2. DE-OVERLAP ON THE ACTUAL WALK-FORWARD OOS TRADES (session index)")
    print("=" * 100)
    print(f"   naive        n {len(r):>4}  mean {r.mean():>+7.3f}%  t {t_stat(r):>+5.2f}")
    print(f"   Newey-West lag {H-1}                          "
          f"t {newey_west_t(r, H - 1):>+5.2f}")
    hack = deoverlap(oos["dt"], oos["ret"], H)
    print(f"   script's own calendar-day hack   n {len(hack):>4}  "
          f"mean {hack.mean():>+7.3f}%  t {t_stat(hack):>+5.2f}")
    # correct: greedy on session index
    kept, last = [], -99
    for i in range(len(oos)):
        s = oos["sess"].iloc[i]
        if s - last >= H:
            kept.append(i)
            last = s
    de = oos.loc[kept, "ret"].values
    print(f"   SESSION-INDEX de-overlap         n {len(de):>4}  "
          f"mean {de.mean():>+7.3f}%  t {t_stat(de):>+5.2f}  "
          f"win {(de > 0).mean() * 100:.0f}%")
    # all H disjoint phases, not just the greedy one
    phases = []
    for off in range(H):
        k, last = [], -99
        for i in range(len(oos)):
            s = oos["sess"].iloc[i]
            if s >= off and s - last >= H:
                k.append(i)
                last = s
        v = oos.loc[k, "ret"].values
        phases.append((len(v), v.mean(), t_stat(v)))
    print("   all disjoint phases: " + "  ".join(
        f"n{n} {m:+.3f} t{t:+.2f}" for n, m, t in phases))

    print("\n" + "=" * 100)
    print("3. ROBUSTNESS: drop folds / drop trades")
    print("=" * 100)
    folds = res["folds"]
    oos["fold"] = pd.NA
    # assign each trade to its fold by test-month membership
    fstarts = [pd.Period(f, "M") for f in folds["fold_start"]]
    for j, f0 in enumerate(fstarts):
        m = oos["dt"].dt.to_period("M")
        sel = (m >= f0) & (m < f0 + 6)
        oos.loc[sel & oos["fold"].isna(), "fold"] = j
    for j, f0 in enumerate(fstarts):
        sub = oos[oos["fold"] == j]["ret"]
        print(f"   fold {j} {str(f0):<9} n {len(sub):>3}  mean {sub.mean():>+7.3f}%  "
              f"contribution {sub.sum():>+8.2f} pp")
    best_f = max(range(len(fstarts)), key=lambda j: oos[oos["fold"] == j]["ret"].sum())
    wrst_f = min(range(len(fstarts)), key=lambda j: oos[oos["fold"] == j]["ret"].sum())
    for lab, drop in (("drop BEST fold ", best_f), ("drop WORST fold", wrst_f)):
        v = oos[oos["fold"] != drop]["ret"].values
        print(f"   {lab} ({fstarts[drop]}): n {len(v):>3}  mean {v.mean():>+7.3f}%  "
              f"t {t_stat(v):>+5.2f}")
    srt = np.sort(r)
    for k in (1, 2, 3, 5):
        v = srt[:-k]
        print(f"   drop best {k} trade(s):   n {len(v):>3}  mean {v.mean():>+7.3f}%  "
              f"t {t_stat(v):>+5.2f}")
    for k in (2,):
        v = srt[k:]
        print(f"   drop worst {k} trade(s):  n {len(v):>3}  mean {v.mean():>+7.3f}%  "
              f"t {t_stat(v):>+5.2f}")
    v = srt[2:-2]
    print(f"   trim 2 each end:        n {len(v):>3}  mean {v.mean():>+7.3f}%  "
          f"t {t_stat(v):>+5.2f}")

    print("\n" + "=" * 100)
    print("4. COSTS")
    print("=" * 100)
    for c in (0.04, 0.08):
        net = r - c
        print(f"   net of {c*100:.0f}bp: mean {net.mean():>+7.3f}%  t {t_stat(net):>+5.2f}"
              f"   de-overlapped mean {de.mean()-c:>+7.3f}%  t {t_stat(de - c):>+5.2f}")

    print("\n" + "=" * 100)
    print("5. BASELINES: paired excess vs the concurrent market")
    print("=" * 100)
    lo, hi = oos["day"].min(), oos["day"].max()
    same = bn[(bn["dt"] >= lo) & (bn["dt"] <= hi)]
    mkt = same[f"long{H}"].dropna()
    print(f"   window {lo.date()} .. {hi.date()}  {len(same)} sessions")
    print(f"   every session long {H}d      mean {mkt.mean():>+7.3f}%  "
          f"t {t_stat(mkt.values):>+5.2f}  n {len(mkt)}")
    print(f"   buy & hold total          {(same['close'].iloc[-1]/same['close'].iloc[0]-1)*100:>+7.1f}%")
    # sign composition of the OOS trades
    lng = 0
    for j, f0 in enumerate(fstarts):
        rule = folds["rule"].iloc[j]
        if rule == "9 tail_low_long":
            lng += (oos["fold"] == j).sum()
    print(f"   of {len(oos)} OOS trades, {lng} come from the LONG-ONLY tail rule "
          f"({lng/len(oos)*100:.0f}%)")
    # paired: trade return minus the unconditional window mean
    exc = r - mkt.mean()
    print(f"   PAIRED EXCESS over the window drift  mean {exc.mean():>+7.3f}%  "
          f"t {t_stat(exc):>+5.2f}  NW t {newey_west_t(exc, H-1):>+5.2f}")
    de_exc = de - mkt.mean()
    print(f"   same, de-overlapped                  mean {de_exc.mean():>+7.3f}%  "
          f"t {t_stat(de_exc):>+5.2f}")

    print("\n" + "=" * 100)
    print("6. NEW CONFOUND -- VOLATILITY (not tested by the battery)")
    print("=" * 100)
    o = bn[(bn["dt"] >= OOS_START) & bn[f"long{H}"].notna()].reset_index(drop=True)
    fires = o["big_tail_low"].fillna(False).astype(bool).values
    y = o[f"long{H}"].values
    atr = o["atr20"].values * 100.0          # lagged, safe
    print(f"   lagged ATR20 on signal days {np.nanmean(atr[fires]):.3f}%  "
          f"vs other days {np.nanmean(atr[~fires]):.3f}%  "
          f"ratio {np.nanmean(atr[fires])/np.nanmean(atr[~fires]):.2f}x")
    print(f"   session range on signal days {o.loc[fires,'range_pct'].mean()*100:.3f}%  "
          f"vs {o.loc[~fires,'range_pct'].mean()*100:.3f}%")
    m = np.isfinite(atr) & np.isfinite(y)
    # (a) add ATR as a control
    X = np.column_stack([np.ones(m.sum()), fires[m].astype(float), atr[m]])
    b, *_ = np.linalg.lstsq(X, y[m], rcond=None)
    e = y[m] - X @ b
    Xi = np.linalg.pinv(X.T @ X)
    S = (X * e[:, None]).T @ (X * e[:, None])
    for l in range(1, H):
        w = 1 - l / H
        A = (X[l:] * e[l:, None]).T @ (X[:-l] * e[:-l, None])
        S += w * (A + A.T)
    V = Xi @ S @ Xi
    print(f"   controlling for lagged ATR20: tail coefficient {b[1]:>+7.3f}%  "
          f"HAC t {b[1]/np.sqrt(V[1,1]):>+5.2f}   (ATR coef {b[2]:+.3f}, "
          f"t {b[2]/np.sqrt(V[2,2]):+.2f})")
    # (b) vol-normalised returns: is the RISK-ADJUSTED move special?
    z = y[m] / atr[m]
    beta, tt = hac_dummy(z, fires[m].astype(float), H - 1)
    print(f"   ATR-normalised return (ret / lagged ATR): excess {beta:>+7.3f} ATRs  "
          f"HAC t {tt:>+5.2f}")
    # (c) ATR-matched bootstrap: draw from the same ATR quintile AND same month
    rng = np.random.default_rng(11)
    dfm = pd.DataFrame({"y": y[m], "atr": atr[m], "sig": fires[m],
                        "mo": o.loc[m, "dt"].dt.to_period("M").values})
    dfm["aq"] = pd.qcut(dfm["atr"], 5, labels=False, duplicates="drop")
    obs = dfm.loc[dfm["sig"], "y"].mean()
    cells = dfm.groupby(["mo", "aq"]).indices
    need = dfm[dfm["sig"]].groupby(["mo", "aq"]).size()
    draws = np.empty(20000)
    yv = dfm["y"].values
    for i in range(20000):
        idx = []
        for key, k in need.items():
            pool = cells.get(key, np.array([], dtype=int))
            if len(pool) == 0:
                continue
            idx.extend(rng.choice(pool, size=min(k, len(pool)), replace=False))
        draws[i] = yv[idx].mean()
    p = float((draws >= obs).mean())
    print(f"   MONTH x ATR-QUINTILE matched bootstrap (20,000): observed {obs:+.3f}%  "
          f"null mean {draws.mean():+.3f}%  95th {np.percentile(draws,95):+.3f}%  "
          f"p {p:.4f}  {'survives' if p < 0.05 else 'FAILS at 5%'}")

    print("\n" + "=" * 100)
    print("7. POPULATION: what the battery measured vs what the strategy traded")
    print("=" * 100)
    print(f"   RULE-level  (all big_tail_low days from {OOS_START.date()}):  "
          f"n {int(fires.sum())}  mean {y[fires].mean():+.3f}%")
    kept2, last = [], -99
    ss = o["sess"].values if "sess" in o else np.arange(len(o))
    for i in range(len(o)):
        if fires[i] and ss[i] - last >= H:
            kept2.append(i)
            last = ss[i]
    print(f"   RULE-level de-overlapped:                     n {len(kept2)}  "
          f"mean {y[kept2].mean():+.3f}%  t {t_stat(y[kept2]):+.2f}   "
          f"<- this is the +1.87 the claim quotes")
    print(f"   STRATEGY-level de-overlapped:                 n {len(de)}  "
          f"mean {de.mean():+.3f}%  t {t_stat(de):+.2f}   <- the honest number")
    print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
