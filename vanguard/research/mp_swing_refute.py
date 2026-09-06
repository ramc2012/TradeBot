"""Refutation of the H=4 walk-forward winner. Six ways to kill tail_low_long.

The walk-forward picked "9 tail_low_long" in 6 of 8 folds (stability 86%) and
returned +0.349%/trade OOS on 165 trades, t +2.27. Before that is called an edge
it has to survive the tests that have already killed several headline numbers in
this project. The rule is LONG-ONLY, so the first and most dangerous explanation
is simply that BANKNIFTY went up.

  A. EXCESS OVER BEING LONG ANYWAY. Over the identical OOS window every session
     held 4 sessions returned +0.147%. A dummy regression with HAC errors asks
     whether the rule's return is different from that, not from zero.
  B. CALENDAR-MATCHED BOOTSTRAP. Replace each signal with a random session drawn
     from the SAME MONTH and re-measure, 20,000 times. This holds the market
     regime constant and asks only whether the SIGNAL DATES were special.
  C. MEAN-REVERSION CONFOUND. A big buying tail forms after a sell-off. Control
     for the trailing 5-session return and see whether the tail still adds.
  D. YEAR BY YEAR. An edge concentrated in one year is a regime, not a rule.
  E. PROTOCOL SENSITIVITY. Rolling instead of anchored; 12/24-month training;
     3-month test windows. The candidate set is unchanged; only the protocol
     moves. If the headline needs one particular split it is not a finding.
  F. REPLICATION ON NIFTY. Same 22 rules, same protocol, different index.

    docker exec nomadcurie_vanguard_cycle python /vanguard/research/mp_swing_refute.py
"""
from __future__ import annotations

import os
import sys
from datetime import date

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.mp_auction import dsn, load                        # noqa: E402
from research.mp_walkforward import HEADER, report, walk_forward  # noqa: E402
from research.mp_swing_failure import (build, rule_table, stack,  # noqa: E402
                                       newey_west_t, t_stat, COST_PCT)

H = 4
OOS_START = pd.Timestamp("2022-12-01")     # first OOS month of the 18/6 protocol
RULE = "9 tail_low_long"


def hac_dummy(y: np.ndarray, x: np.ndarray, lag: int) -> tuple[float, float]:
    """OLS y ~ 1 + x with Bartlett HAC. Returns (beta, t) for the dummy."""
    X = np.column_stack([np.ones(len(x)), x.astype(float)])
    b, *_ = np.linalg.lstsq(X, y, rcond=None)
    e = y - X @ b
    n = len(y)
    XtX_inv = np.linalg.pinv(X.T @ X)
    S = (X * e[:, None]).T @ (X * e[:, None])
    for l in range(1, min(lag, n - 1) + 1):
        w = 1.0 - l / (lag + 1.0)
        A = (X[l:] * e[l:, None]).T @ (X[:-l] * e[:-l, None])
        S += w * (A + A.T)
    V = XtX_inv @ S @ XtX_inv
    return float(b[1]), float(b[1] / np.sqrt(V[1, 1]))


def main() -> int:
    connection = psycopg2.connect(dsn())
    try:
        raw = load(connection, ["BANKNIFTY", "NIFTY"], date(2021, 1, 1))
    finally:
        connection.close()

    bn = build(raw[raw["underlying"] == "BANKNIFTY"])
    oos = bn[(bn["dt"] >= OOS_START) & bn[f"long{H}"].notna()].copy()
    fires = oos["big_tail_low"].fillna(False).astype(bool)
    y = oos[f"long{H}"].values
    print(f"\nBANKNIFTY OOS window {oos['dt'].min().date()} .. "
          f"{oos['dt'].max().date()}   {len(oos)} sessions, "
          f"{int(fires.sum())} signal days ({fires.mean() * 100:.1f}%)")

    # ---------------------------------------------------------------- A
    print("\n" + "=" * 100)
    print("A. IS IT MORE THAN BEING LONG? dummy regression, HAC lag 3")
    print("=" * 100)
    base = y[~fires.values].mean()
    sig = y[fires.values].mean()
    beta, t = hac_dummy(y, fires.values, H - 1)
    print(f"   mean 4-session return, NO signal      {base:>+8.3f}%  "
          f"n {int((~fires).sum())}")
    print(f"   mean 4-session return, SIGNAL         {sig:>+8.3f}%  "
          f"n {int(fires.sum())}")
    print(f"   EXCESS attributable to the signal     {beta:>+8.3f}%   "
          f"HAC t {t:>+5.2f}")
    print(f"   net of {COST_PCT:.2f}% cost the signal day earns "
          f"{sig - COST_PCT:+.3f}% vs {base:+.3f}% for doing nothing special")

    # ---------------------------------------------------------------- B
    print("\n" + "=" * 100)
    print("B. CALENDAR-MATCHED BOOTSTRAP: same months, random days, 20,000 draws")
    print("=" * 100)
    rng = np.random.default_rng(7)
    o = oos.reset_index(drop=True)
    month = o["dt"].dt.to_period("M")
    by_month = {m: np.flatnonzero((month == m).values) for m in month.unique()}
    sig_months = month[fires.values].value_counts()
    draws = np.empty(20000)
    for i in range(20000):
        idx = []
        for m, k in sig_months.items():
            pool = by_month[m]
            idx.extend(rng.choice(pool, size=min(k, len(pool)), replace=False))
        draws[i] = o.loc[idx, f"long{H}"].mean()
    p = float((draws >= sig).mean())
    print(f"   observed signal mean                  {sig:>+8.3f}%")
    print(f"   calendar-matched null  mean {draws.mean():+.3f}%  "
          f"sd {draws.std():.3f}  95th pct {np.percentile(draws, 95):+.3f}%")
    print(f"   p(random same-month days >= observed) {p:>8.4f}"
          f"   {'SURVIVES' if p < 0.05 else 'FAILS at 5%'}")

    # ---------------------------------------------------------------- C
    print("\n" + "=" * 100)
    print("C. MEAN-REVERSION CONFOUND: is the tail just 'the market fell'?")
    print("=" * 100)
    o["ret5"] = (o["close"] / o["close"].shift(5) - 1.0) * 100.0
    m = o["ret5"].notna()
    print(f"   trailing 5-session return on signal days  "
          f"{o.loc[m & fires.reset_index(drop=True), 'ret5'].mean():>+7.3f}%")
    print(f"   trailing 5-session return on other days   "
          f"{o.loc[m & ~fires.reset_index(drop=True), 'ret5'].mean():>+7.3f}%")
    X = np.column_stack([fires.values[m.values].astype(float),
                         o.loc[m, "ret5"].values])
    yy = o.loc[m, f"long{H}"].values
    Xf = np.column_stack([np.ones(len(yy)), X])
    b, *_ = np.linalg.lstsq(Xf, yy, rcond=None)
    e = yy - Xf @ b
    XtX_inv = np.linalg.pinv(Xf.T @ Xf)
    S = (Xf * e[:, None]).T @ (Xf * e[:, None])
    for l in range(1, H):
        w = 1.0 - l / H
        A = (Xf[l:] * e[l:, None]).T @ (Xf[:-l] * e[:-l, None])
        S += w * (A + A.T)
    V = XtX_inv @ S @ XtX_inv
    print(f"   controlling for ret5, tail_low coefficient {b[1]:>+7.3f}%  "
          f"HAC t {b[1] / np.sqrt(V[1, 1]):>+5.2f}")
    print(f"   (ret5 coefficient {b[2]:>+7.4f}, HAC t "
          f"{b[2] / np.sqrt(V[2, 2]):>+5.2f})")

    # ---------------------------------------------------------------- D
    print("\n" + "=" * 100)
    print("D. YEAR BY YEAR, signal days only (OOS window)")
    print("=" * 100)
    sd = o[fires.values]
    print(f"   {'year':<8}{'n':>5}{'mean %':>10}{'t':>7}{'win':>7}"
          f"{'   market same-year 4d mean':<28}")
    for yr, g in sd.groupby(sd["dt"].dt.year):
        allg = o[o["dt"].dt.year == yr][f"long{H}"]
        r = g[f"long{H}"]
        print(f"   {yr:<8}{len(r):>5}{r.mean():>+10.3f}{t_stat(r.values):>+7.2f}"
              f"{(r > 0).mean() * 100:>6.0f}%{allg.mean():>+18.3f}%")

    # ---------------------------------------------------------------- E
    print("\n" + "=" * 100)
    print("E. PROTOCOL SENSITIVITY -- same 22 candidates, protocol varied")
    print("=" * 100)
    rules = rule_table(bn)
    st = stack(bn, rules, H)
    cands = {n: (st["rule"] == n) for n in rules}
    print(HEADER)
    for label, kw in [("anchored 18/6 (headline)", dict(train_m=18, test_m=6, anchored=True)),
                      ("anchored 24/6", dict(train_m=24, test_m=6, anchored=True)),
                      ("anchored 12/6", dict(train_m=12, test_m=6, anchored=True)),
                      ("anchored 18/3", dict(train_m=18, test_m=3, anchored=True)),
                      ("ROLLING  18/6", dict(train_m=18, test_m=6, anchored=False)),
                      ("ROLLING  24/6", dict(train_m=24, test_m=6, anchored=False))]:
        res = walk_forward(st, cands, "ret", min_trades=12, **kw)
        report(label, res)
        if "picks" in res:
            print(f"      picks: {' '.join(res['picks'])}")

    # ---------------------------------------------------------------- F
    print("\n" + "=" * 100)
    print("F. REPLICATION ON NIFTY -- same 22 rules, same anchored 18/6 protocol")
    print("=" * 100)
    nf = build(raw[raw["underlying"] == "NIFTY"])
    nrules = rule_table(nf)
    nst = stack(nf, nrules, H)
    ncands = {n: (nst["rule"] == n) for n in nrules}
    nres = walk_forward(nst, ncands, "ret", train_m=18, test_m=6, anchored=True,
                        min_trades=12)
    print(HEADER)
    report("NIFTY walk-forward H=4", nres)
    if "picks" in nres:
        print(f"      picks: {' '.join(nres['picks'])}")
    nf_oos = nf[(nf["dt"] >= OOS_START) & nf[f"long{H}"].notna()]
    nfires = nf_oos["big_tail_low"].fillna(False).astype(bool)
    yn = nf_oos[f"long{H}"].values
    bn_, tn_ = hac_dummy(yn, nfires.values, H - 1)
    print(f"\n   NIFTY tail_low_long, OOS window only: signal mean "
          f"{yn[nfires.values].mean():+.3f}% (n {int(nfires.sum())}) vs "
          f"no-signal {yn[~nfires.values].mean():+.3f}%")
    print(f"   NIFTY excess {bn_:+.3f}%  HAC t {tn_:+.2f}   "
          f"{'replicates' if tn_ > 1.5 else 'DOES NOT replicate'}")

    print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
