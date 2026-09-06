"""SWING FAMILY: value migration + multi-timeframe location, walk-forward only.

THE QUESTION. Everything in the swing family is entered at a session CLOSE on
state that is fully known at 15:15 -- value migration since yesterday, the day's
own value area, and the prior completed WEEK and MONTH value areas -- and held
for a fixed number of sessions close-to-close. Does any of it survive being
chosen out-of-sample?

WHY LONG AND SHORT COMPETE IN ONE ENGINE. mp_walkforward.walk_forward picks the
best candidate by in-sample mean of ONE return column. Running it twice, once on
+cc and once on -cc, and then reporting whichever looked better is exactly the
mining this project is trying to stop. So the frame is STACKED: every session
appears twice, once as a LONG row carrying ret=+cc_h and once as a SHORT row
carrying ret=-cc_h. A long rule masks only long rows, a short rule only short
rows, and the engine is free to prefer a short in one fold and a long in the
next. The short rows are stamped one hour later than the long rows so that the
engine's internal sort_values("dt") is deterministic -- with duplicate keys it
is not, and a reshuffle there would silently misalign every mask.

THE CANDIDATE SET IS FIXED AT 24 NAMED RULES, declared before any result was
seen, 12 long and 12 short:

  LONG                                SHORT (mirror)
   1 below day+week+month value       13 above day+week+month value
   2 higher_outside x2                14 lower_outside x2
   3 higher_outside x2 & close>VAH    15 lower_outside x2 & close<VAL
   4 lower_outside x2 (contrarian)    16 higher_outside x2 (contrarian)
   5 poc_migration>0 x2               17 poc_migration<0 x2
   6 poc_migration>0 x3               18 poc_migration<0 x3
   7 close > prior WEEK high          19 close < prior WEEK low
   8 close > pw high & close>VAH      20 close < pw low & close<VAL
   9 close < pw low (contrarian)      21 close > pw high (contrarian)
  10 re-enter prior MONTH VA from     22 re-enter prior MONTH VA from
     below                               above
  11 va_overlap<0.25 & POC up         23 va_overlap<0.25 & POC down
  12 that & close>VAH                 24 that & close<VAL

Rule 1 is the incumbent -- the only contrarian signal that has replicated -- and
it is in the set to be beaten, not to be protected.

A SECOND, ALSO PRE-DECLARED RUN adds a 25th candidate, ALWAYS_LONG (every
session, long). If the engine prefers it, the honest reading is that the signal
rules add nothing over simply being in BANKNIFTY, and that is the whole point of
the buy-and-hold control this file also prints.

HORIZON IS CHOSEN BEFORE LOOKING, NOT AFTER. h=4 sessions is the headline
because the established multi-timeframe result in this project is stated at 3-4
sessions and because 4 sessions of BANKNIFTY carries roughly 1.4% of standard
deviation against a 4bp round trip -- a 2-session hold is meaningfully closer to
the cost floor. h=2 and h=8 are run as robustness and are labelled as such.

OVERLAP IS THE MAIN STATISTICAL HAZARD and is reported three ways: the engine's
naive t, a Newey-West t at lag h-1, and a greedy NON-OVERLAPPING subset of the
out-of-sample trades (take a trade, then skip every entry within h sessions).

    docker exec nomadcurie_vanguard_cycle python /vanguard/research/mp_swing_wf.py
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.mp_auction import dsn  # noqa: E402
from research.mp_multi_tf import load_mtf, targets  # noqa: E402
from research.mp_walkforward import HEADER, report, walk_forward  # noqa: E402

COST_PCT = 0.04          # 4bp round trip on BANKNIFTY futures
HORIZONS = (2, 4, 8)
HEADLINE_H = 4


# ----------------------------------------------------------------- statistics
def nw_t(x: np.ndarray, lag: int) -> float:
    """Newey-West t on the mean, lag chosen for an h-session overlap."""
    x = np.asarray(x, float)
    n = len(x)
    if n < 5:
        return np.nan
    mu = x.mean()
    e = x - mu
    s = float((e * e).sum() / n)
    for j in range(1, min(lag, n - 1) + 1):
        g = float((e[j:] * e[:-j]).sum() / n)
        s += 2.0 * (1.0 - j / (lag + 1.0)) * g
    if s <= 0:
        return np.nan
    return mu / np.sqrt(s / n)


def plain_t(x: pd.Series) -> float:
    x = x.dropna()
    if len(x) < 3 or x.std(ddof=1) == 0:
        return np.nan
    return x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))


def non_overlapping(oos: pd.DataFrame, sess_idx: dict, h: int, ret_col: str):
    """Greedy: keep a trade, then skip every entry inside its holding period."""
    kept, last = [], -10**9
    for _, row in oos.sort_values("dt").iterrows():
        i = sess_idx.get(pd.Timestamp(row["dt"]).normalize())
        if i is None or i - last < h:
            continue
        kept.append(row[ret_col])
        last = i
    return pd.Series(kept, dtype=float)


# ------------------------------------------------------------------- the rules
def build_state(d: pd.DataFrame) -> pd.DataFrame:
    """Every boolean the candidate set needs. All known at the 15:15 close."""
    d = d.sort_values("dt").reset_index(drop=True).copy()
    c = d["close"]

    d["d_below"] = c < d["val"]
    # d_above already supplied by load_mtf (close > vah)

    hi_out = d["value_shift"].eq("higher_outside")
    lo_out = d["value_shift"].eq("lower_outside")
    d["hi_out2"] = hi_out & hi_out.shift(1).fillna(False)
    d["lo_out2"] = lo_out & lo_out.shift(1).fillna(False)

    pm = d["poc_migration"]
    up, dn = pm > 0, pm < 0
    d["pm_up2"] = up & up.shift(1).fillna(False)
    d["pm_up3"] = d["pm_up2"] & up.shift(2).fillna(False)
    d["pm_dn2"] = dn & dn.shift(1).fillna(False)
    d["pm_dn3"] = d["pm_dn2"] & dn.shift(2).fillna(False)

    d["above_pwh"] = c > d["w_hi"]
    d["below_pwl"] = c < d["w_lo"]

    # monthly 80%-rule analogue: outside the prior month's value yesterday,
    # back inside it today, with the SAME monthly profile on both days
    same_m = d["m_val"].eq(d["m_val"].shift(1))
    inside_m = (c >= d["m_val"]) & (c <= d["m_vah"])
    d["m_reentry_up"] = same_m & inside_m & (c.shift(1) < d["m_val"])
    d["m_reentry_dn"] = same_m & inside_m & (c.shift(1) > d["m_vah"])

    ovl_low = d["va_overlap"] < 0.25
    d["ovl_up"] = ovl_low & (d["poc"] > d["prev_poc"])
    d["ovl_dn"] = ovl_low & (d["poc"] < d["prev_poc"])

    for col in ("d_below", "d_above", "hi_out2", "lo_out2", "pm_up2", "pm_up3",
                "pm_dn2", "pm_dn3", "above_pwh", "below_pwl", "m_reentry_up",
                "m_reentry_dn", "ovl_up", "ovl_dn", "w_below", "w_above",
                "m_below", "m_above"):
        d[col] = d[col].fillna(False).astype(bool)
    return d


def rule_defs(d: pd.DataFrame) -> dict:
    """The 24 named rules as {name: (side, mask)}. Fixed. Declared up front."""
    c, vah, val = d["close"], d["vah"], d["val"]
    above_va, below_va = c > vah, c < val
    return {
        # ---------------- LONG ----------------
        "L01 below day+week+month VA": (+1, d["d_below"] & d["w_below"] & d["m_below"]),
        "L02 higher_outside x2": (+1, d["hi_out2"]),
        "L03 higher_outside x2 & >VAH": (+1, d["hi_out2"] & above_va),
        "L04 lower_outside x2 (contra)": (+1, d["lo_out2"]),
        "L05 poc migration up x2": (+1, d["pm_up2"]),
        "L06 poc migration up x3": (+1, d["pm_up3"]),
        "L07 close > prior week high": (+1, d["above_pwh"]),
        "L08 > pw high & >VAH": (+1, d["above_pwh"] & above_va),
        "L09 < pw low (contra)": (+1, d["below_pwl"]),
        "L10 month VA re-entry up": (+1, d["m_reentry_up"]),
        "L11 va_overlap<.25 & POC up": (+1, d["ovl_up"]),
        "L12 that & >VAH": (+1, d["ovl_up"] & above_va),
        # ---------------- SHORT ---------------
        "S13 above day+week+month VA": (-1, d["d_above"] & d["w_above"] & d["m_above"]),
        "S14 lower_outside x2": (-1, d["lo_out2"]),
        "S15 lower_outside x2 & <VAL": (-1, d["lo_out2"] & below_va),
        "S16 higher_outside x2 (contra)": (-1, d["hi_out2"]),
        "S17 poc migration down x2": (-1, d["pm_dn2"]),
        "S18 poc migration down x3": (-1, d["pm_dn3"]),
        "S19 close < prior week low": (-1, d["below_pwl"]),
        "S20 < pw low & <VAL": (-1, d["below_pwl"] & below_va),
        "S21 > pw high (contra)": (-1, d["above_pwh"]),
        "S22 month VA re-entry down": (-1, d["m_reentry_dn"]),
        "S23 va_overlap<.25 & POC dn": (-1, d["ovl_dn"]),
        "S24 that & <VAL": (-1, d["ovl_dn"] & below_va),
    }


def stack(d: pd.DataFrame, h: int) -> pd.DataFrame:
    """Long rows carry +cc_h, short rows -cc_h and a +1h dt so the sort is total."""
    lo = d.copy()
    lo["side"], lo["ret"] = +1, d[f"cc{h}"]
    sh = d.copy()
    sh["side"], sh["ret"] = -1, -d[f"cc{h}"]
    sh["dt"] = sh["dt"] + pd.Timedelta(hours=1)
    s = pd.concat([lo, sh], ignore_index=True)
    s = s.sort_values("dt", kind="mergesort").reset_index(drop=True)
    s["uid"] = np.arange(len(s))
    # the engine re-sorts internally; prove that sort is the identity here
    chk = s.sort_values("dt").reset_index(drop=True)
    assert (chk["uid"].values == s["uid"].values).all(), "dt sort is not stable"
    return s


def candidates(s: pd.DataFrame, defs: dict) -> dict:
    """Lift the daily masks onto the stacked frame, gated by side."""
    out = {}
    for name, (side, mask) in defs.items():
        sel = (s["side"] == side).values
        assert sel.sum() == len(mask)
        lifted = pd.Series(False, index=s.index)
        # rows of one side stay in dt order, so they are 1:1 with the daily frame
        lifted.loc[sel] = mask.values
        out[name] = lifted
    return out


# ------------------------------------------------------------------- reporting
def in_sample_table(d: pd.DataFrame, defs: dict, h: int) -> None:
    print(f"\nIN-SAMPLE CONTEXT ONLY (full 2021-2026, h={h} sessions close-to-close)")
    print(f"   {'rule':<34}{'n':>6}{'mean %':>9}{'win':>6}{'t':>7}{'net %':>8}")
    base = d[f"cc{h}"].dropna()
    print(f"   {'-- every session, long':<34}{len(base):>6}{base.mean():>+9.3f}"
          f"{(base > 0).mean()*100:>5.0f}%{plain_t(base):>+7.2f}"
          f"{base.mean()-COST_PCT:>+8.3f}")
    for name, (side, mask) in defs.items():
        r = (side * d.loc[mask, f"cc{h}"]).dropna()
        if len(r) < 5:
            print(f"   {name:<34}{len(r):>6}{'':>9}{'':>6}{'':>7}{'':>8}")
            continue
        print(f"   {name:<34}{len(r):>6}{r.mean():>+9.3f}{(r > 0).mean()*100:>5.0f}%"
              f"{plain_t(r):>+7.2f}{r.mean()-COST_PCT:>+8.3f}")


def benchmark(d: pd.DataFrame, res: dict, h: int, label: str) -> None:
    """Buy-and-hold BANKNIFTY over exactly the out-of-sample months."""
    oos_dt = pd.to_datetime(res["oos"]["dt"]).dt.normalize()
    lo, hi = oos_dt.min(), oos_dt.max()
    d = d.copy()
    d["day"] = pd.to_datetime(d["dt"]).dt.normalize()
    win = d[(d["day"] >= lo) & (d["day"] <= hi + pd.Timedelta(days=h * 3))]
    if win.empty:
        return
    bh = win["close"].iloc[-1] / win["close"].iloc[0] - 1.0
    per = win[f"cc{h}"].dropna()
    n_sess = len(win)
    r = res["oos"][res["ret_col"]] if "ret_col" in res else res["oos"].iloc[:, 0]
    nonov = res["nonov"]
    print(f"\n   {label}: OOS window {lo.date()} .. {hi.date()}  ({n_sess} sessions)")
    print(f"     buy & hold BANKNIFTY, same window          {bh*100:>+8.2f}%")
    print(f"     strategy total, {res['n']:>4} trades of {h}d          "
          f"{res['total']*100:>+8.2f}%   (net of {COST_PCT}% "
          f"{((1 + (r - COST_PCT)/100).prod() - 1)*100:>+7.2f}%)")
    print(f"     strategy exposure (trade-days / sessions)  "
          f"{res['n']*h/max(n_sess,1):>8.2f}x")
    print(f"     average {h}-session hold, any session       "
          f"{per.mean():>+8.3f}%  t {plain_t(per):>+5.2f}   n {len(per)}")
    print(f"     strategy mean per trade                    {res['mean']:>+8.3f}%"
          f"  t {res['t']:>+5.2f}   net {res['mean']-COST_PCT:>+7.3f}%")
    print(f"     Newey-West t (lag {h-1}) on the OOS trades   "
          f"{res['nw']:>+8.2f}")
    if len(nonov) > 2:
        print(f"     NON-OVERLAPPING subset                     "
              f"{nonov.mean():>+8.3f}%  t {plain_t(nonov):>+5.2f}   n {len(nonov)}")


def enrich(res: dict, sess_idx: dict, h: int) -> dict:
    if "error" in res:
        return res
    res["ret_col"] = "ret"
    res["nw"] = nw_t(res["oos"]["ret"].values, max(h - 1, 1))
    res["nonov"] = non_overlapping(res["oos"], sess_idx, h, "ret")
    return res


# ------------------------------------------------------------------------ main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=dsn())
    ap.add_argument("--symbol", default="BANKNIFTY")
    ap.add_argument("--train", type=int, default=18)
    ap.add_argument("--test", type=int, default=6)
    args = ap.parse_args()

    conn = psycopg2.connect(args.dsn)
    try:
        d = load_mtf(conn, [args.symbol], date(2021, 1, 1))
    finally:
        conn.close()

    d["dt"] = pd.to_datetime(d["dt"])
    d = targets(d, horizons=HORIZONS)
    d = build_state(d)
    d = d[d["m_val"].notna() & d["w_val"].notna()].reset_index(drop=True)
    defs = rule_defs(d)
    sess_idx = {pd.Timestamp(t).normalize(): i for i, t in enumerate(d["dt"])}

    print("=" * 108)
    print(f"SWING FAMILY -- value migration + multi-timeframe location -- {args.symbol}")
    print("=" * 108)
    print(f"sessions {len(d):,}   {d['dt'].min().date()} .. {d['dt'].max().date()}"
          f"   candidate set: {len(defs)} named rules (12 long / 12 short), fixed up front")
    print(f"walk-forward: anchored, train {args.train}m, test {args.test}m, "
          f"min 12 train trades; costs {COST_PCT}% round trip")
    print("horizon h=4 declared the headline BEFORE running; h=2 and h=8 are robustness")

    in_sample_table(d, defs, HEADLINE_H)

    print("\n" + "=" * 108)
    print("OUT-OF-SAMPLE WALK-FORWARD  (the only thing the conclusion may rest on)")
    print("=" * 108)
    print(HEADER)
    results = {}
    for h in HORIZONS:
        s = stack(d, h)
        cand = candidates(s, defs)
        res = enrich(walk_forward(s, cand, "ret", train_m=args.train,
                                  test_m=args.test, anchored=True), sess_idx, h)
        tag = "HEADLINE" if h == HEADLINE_H else "robust  "
        report(f"h={h} 24 signal rules [{tag}]", res)
        results[("signal", h)] = res

        cand2 = dict(cand)
        cand2["CTRL always long"] = (s["side"] == +1)
        res2 = enrich(walk_forward(s, cand2, "ret", train_m=args.train,
                                   test_m=args.test, anchored=True), sess_idx, h)
        report(f"h={h} + ALWAYS-LONG control", res2)
        results[("ctrl", h)] = res2

    print("\n" + "=" * 108)
    print("THE BENCHMARK THAT DECIDES IT: buy-and-hold over the SAME OOS months")
    print("=" * 108)
    for h in HORIZONS:
        for kind, lab in (("signal", "24 signal rules"), ("ctrl", "+ always-long control")):
            r = results[(kind, h)]
            if "error" not in r:
                benchmark(d, r, h, f"h={h}  {lab}")

    print("\n" + "=" * 108)
    print(f"FOLD DETAIL -- headline h={HEADLINE_H}, 24 signal rules")
    print("=" * 108)
    res = results[("signal", HEADLINE_H)]
    if "error" not in res:
        f = res["folds"]
        print(f"   {'test window':<12}{'rule chosen':<34}{'train mean':>11}"
              f"{'n test':>8}{'test mean':>11}")
        for _, r in f.iterrows():
            print(f"   {r['fold_start']:<12}{r['rule']:<34}{r['train_mean']:>+11.3f}"
                  f"{int(r['n_test']):>8}{r['test_mean']:>+11.3f}")
        print(f"\n   distinct rules chosen {len(set(res['picks']))} of {len(defs)}"
              f"   switches {res['switches']}/{max(len(res['picks'])-1,0)}"
              f"   STABILITY {res['stability']*100:.0f}%")
        print("   stability = share of fold boundaries where the winner did NOT change.")

    print("\n" + "=" * 108)
    print(f"FOLD DETAIL -- headline h={HEADLINE_H}, WITH the always-long control")
    print("=" * 108)
    res = results[("ctrl", HEADLINE_H)]
    if "error" not in res:
        for _, r in res["folds"].iterrows():
            print(f"   {r['fold_start']:<12}{r['rule']:<34}{r['train_mean']:>+11.3f}"
                  f"{int(r['n_test']):>8}{r['test_mean']:>+11.3f}")
        print(f"\n   STABILITY {res['stability']*100:.0f}%   "
              f"distinct rules {len(set(res['picks']))}")


if __name__ == "__main__":
    main()
