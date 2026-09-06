"""Multi-day swings from AUCTION FAILURE and DAY-TYPE STRUCTURE. Walk-forward.

THE QUESTION. Market Profile's most-taught reversal and repair signals are all
END-OF-SESSION facts: a failed auction (probed beyond the prior extreme, closed
back inside), a poor high or poor low (an unfinished extreme the auction "must"
return to repair), a large tail (excess), and the Dalton day types. None of them
can predict the session that produced them -- they are only known at 15:15. So
they are tested here the ONLY way they can honestly be tested: entry at the
close of session t, exit at the close of session t+H, H fixed.

HORIZON, CHOSEN UP FRONT AND FOR STATED REASONS. H = 4 sessions.
  - It is the house horizon: mp_multi_tf.targets already defines up4/dn4/cc4,
    and the one replicated finding in this project (below day AND week AND month
    value -> P(up 2%) 48% vs 28% base) is a 4-session statement.
  - H = 2 sits on top of a known contaminant: a strong close predicts the NEXT
    OPEN at +0.175%/trade and that edge is SPENT BY 09:15, with the following
    intraday leg NEGATIVE. At H = 2 an overnight artefact is a large share of
    the measured return; by H = 4 it is noise around a real swing.
  - H = 8 puts ~8 overlapping windows on every trade in a 1,250-session sample
    and leaves too few independent observations to say anything.
  H = 2 and H = 8 are computed and printed FOR CONTEXT ONLY. The conclusion
  rests on the H = 4 walk-forward, declared before any number was seen.

THE CANDIDATE SET IS FIXED AT 22 NAMED RULES, defined below, six families, each
family traded in BOTH directions so that the walk-forward is choosing a sign
rather than being handed one. No rule was added after seeing a result, and no
threshold is tuned inside a fold. The only threshold anywhere is the "large
tail" definition, and it is a TRAILING PERCENTILE (top quintile of the last 250
sessions, self-inclusive and therefore causal), not a fitted number.

  FAILED AUCTION      1 fail_high_short   2 fail_low_long
                      3 fail_high_long    4 fail_low_short
  POOR EXTREME        5 poor_high_long    6 poor_low_short     (repair: trade
                      7 poor_high_short   8 poor_low_long       toward it)
  TAIL / EXCESS       9 tail_low_long    10 tail_high_short    (continuation
                     11 tail_low_short   12 tail_high_long      away / back in)
  DAY TYPE           13 trend_cont       14 trend_rev
                     15 nx_cont          16 nx_rev             (neutral_extreme)
                     17 dd_cont          18 dd_rev             (double_dist)
  NEUTRAL DAY        19 neutral_cont     20 neutral_rev
  BALANCE-THEN-BREAK 21 btb_cont         22 btb_fade

WHAT "cont" AND "rev" MEAN. For the day-type families the day itself has no
inherent side, so direction is taken from where the session CLOSED in its own
range: cont = long if close_pos >= 0.5 else short; rev = the opposite. This is
known at 15:15 like everything else in the family.

HONESTY MACHINERY BEYOND THE WALK-FORWARD.
  - Overlap. At H = 4 consecutive signals share up to three sessions, so the
    naive t on the concatenated trades is inflated. Reported alongside it: a
    Newey-West t at lag H-1 and a DE-OVERLAPPED t on the greedy subset of trades
    spaced at least H sessions apart.
  - Costs. BANKNIFTY futures round trip ~4bp. Every mean is also shown net.
  - Baselines over the SAME out-of-sample months: buy-and-hold total return, and
    the mean H-session return of EVERY session (the "just be long" null).

    docker exec nomadcurie_vanguard_cycle python /vanguard/research/mp_swing_failure.py
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
from research.mp_auction import dsn, load                      # noqa: E402
from research.mp_walkforward import HEADER, report, walk_forward  # noqa: E402

COST_PCT = 0.04              # 4bp round trip, in percent
HORIZONS = (2, 4, 8)
HEADLINE_H = 4               # declared before any result was seen
TAIL_WINDOW = 250
TAIL_Q = 0.80


# ---------------------------------------------------------------- statistics
def t_stat(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < 5 or x.std(ddof=1) == 0:
        return np.nan
    return x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))


def newey_west_t(x: np.ndarray, lag: int) -> float:
    """t on the mean with a Bartlett HAC variance -- the honest t under overlap."""
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 10:
        return np.nan
    e = x - x.mean()
    var = (e @ e) / n
    for l in range(1, min(lag, n - 1) + 1):
        cov = (e[l:] @ e[:-l]) / n
        var += 2.0 * (1.0 - l / (lag + 1.0)) * cov
    if var <= 0:
        return np.nan
    return x.mean() / np.sqrt(var / n)


def deoverlap(dates: pd.Series, rets: pd.Series, gap_days: int) -> np.ndarray:
    """Greedy subset of trades whose entries are >= gap_days calendar-ish apart.

    Sessions, not calendar days, would be exact; entries are unique session
    dates so a gap in trading days is recovered by ranking the dates."""
    order = np.argsort(dates.values)
    d = dates.values[order]
    r = rets.values[order]
    kept, last = [], None
    for i in range(len(d)):
        if last is None or (d[i] - last) / np.timedelta64(1, "D") >= gap_days * 1.4:
            kept.append(r[i])
            last = d[i]
    return np.asarray(kept, dtype=float)


# ------------------------------------------------------------------ features
def build(s: pd.DataFrame) -> pd.DataFrame:
    """One row per session, with every rule's firing flag and signed returns."""
    d = s.sort_values("dt").reset_index(drop=True).copy()

    for h in HORIZONS:
        d[f"long{h}"] = (d["close"].shift(-h) / d["close"] - 1.0) * 100.0

    # LARGE TAIL: top quintile of the trailing 250 sessions, self-inclusive.
    # (tail_h at t is known at 15:15 on t and the window is strictly past, so
    #  this leaks nothing; a fixed percent threshold would be a fitted number.)
    for side in ("high", "low"):
        col = f"tail_{side}"
        rank = (d[col].rolling(TAIL_WINDOW, min_periods=60)
                .apply(lambda w: (w[-1] > w[:-1]).mean(), raw=True))
        d[f"big_tail_{side}"] = (d[col] > 0) & (rank >= TAIL_Q)

    d["up_close"] = d["close_pos"] >= 0.5           # where the day closed
    dt_ = d["day_type"]
    d["is_trend"] = dt_ == "trend"
    d["is_nx"] = dt_ == "neutral_extreme"
    d["is_dd"] = dt_ == "double_distribution"
    d["is_neutral"] = dt_ == "neutral"

    # BALANCE-THEN-BREAK: >= 2 consecutive balanced sessions, then a close
    # outside the PRIOR session's value area.
    bal = dt_.isin(["normal", "normal_variation"])
    two_bal = bal.shift(1).fillna(False) & bal.shift(2).fillna(False)
    d["btb_up"] = two_bal & (d["close"] > d["prev_vah"])
    d["btb_dn"] = two_bal & (d["close"] < d["prev_val"])
    return d


def rule_table(d: pd.DataFrame) -> dict:
    """{name: (fires, is_long)} -- is_long may be a Series for signed rules."""
    T = pd.Series(True, index=d.index)
    F = pd.Series(False, index=d.index)
    up, dn = d["up_close"], ~d["up_close"]
    return {
        # 1-4 FAILED AUCTION
        "1 fail_high_short":  (d["failed_high"], F),
        "2 fail_low_long":    (d["failed_low"], T),
        "3 fail_high_long":   (d["failed_high"], T),
        "4 fail_low_short":   (d["failed_low"], F),
        # 5-8 POOR EXTREME (unambiguous: one poor end only)
        "5 poor_high_long":   (d["poor_high"] & ~d["poor_low"], T),
        "6 poor_low_short":   (d["poor_low"] & ~d["poor_high"], F),
        "7 poor_high_short":  (d["poor_high"] & ~d["poor_low"], F),
        "8 poor_low_long":    (d["poor_low"] & ~d["poor_high"], T),
        # 9-12 TAIL / EXCESS
        "9 tail_low_long":    (d["big_tail_low"], T),
        "10 tail_high_short": (d["big_tail_high"], F),
        "11 tail_low_short":  (d["big_tail_low"], F),
        "12 tail_high_long":  (d["big_tail_high"], T),
        # 13-18 DAY TYPE
        "13 trend_cont":      (d["is_trend"], up),
        "14 trend_rev":       (d["is_trend"], dn),
        "15 nx_cont":         (d["is_nx"], up),
        "16 nx_rev":          (d["is_nx"], dn),
        "17 dd_cont":         (d["is_dd"], up),
        "18 dd_rev":          (d["is_dd"], dn),
        # 19-20 NEUTRAL DAY
        "19 neutral_cont":    (d["is_neutral"], up),
        "20 neutral_rev":     (d["is_neutral"], dn),
        # 21-22 BALANCE-THEN-BREAK
        "21 btb_cont":        (d["btb_up"] | d["btb_dn"], d["btb_up"]),
        "22 btb_fade":        (d["btb_up"] | d["btb_dn"], d["btb_dn"]),
    }


def stack(d: pd.DataFrame, rules: dict, h: int) -> pd.DataFrame:
    """Long format: one row per (rule, firing session), with the SIGNED return.

    walk_forward takes a single ret_col, so direction has to live in the return.
    A per-rule offset of a few MINUTES is added to dt so that every row has a
    unique timestamp -- otherwise the engine's sort_values('dt') (quicksort, not
    stable) could permute ties and silently misalign the candidate masks."""
    out = []
    for k, (name, (fires, is_long)) in enumerate(rules.items()):
        f = fires.fillna(False).astype(bool)
        if not f.any():
            continue
        sub = d.loc[f, ["dt", f"long{h}"]].copy()
        sign = np.where(is_long.loc[f] if hasattr(is_long, "loc") else is_long,
                        1.0, -1.0)
        sub["ret"] = sub[f"long{h}"] * sign
        sub["rule"] = name
        sub["dt"] = sub["dt"] + pd.to_timedelta(k, unit="m")
        out.append(sub[["dt", "rule", "ret"]])
    st = pd.concat(out, ignore_index=True)
    return st.sort_values("dt", kind="mergesort").reset_index(drop=True)


# --------------------------------------------------------------------- main
def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="BANKNIFTY")
    p.add_argument("--train", type=int, default=18)
    p.add_argument("--test", type=int, default=6)
    p.add_argument("--dsn", default=dsn())
    a = p.parse_args()

    connection = psycopg2.connect(a.dsn)
    try:
        s = load(connection, [a.symbol], date(2021, 1, 1))
    finally:
        connection.close()
    s = s[s["underlying"] == a.symbol]
    d = build(s)
    rules = rule_table(d)

    print(f"\n{a.symbol}  {len(d)} sessions  {d['dt'].min().date()} .. "
          f"{d['dt'].max().date()}   candidate set = {len(rules)} named rules")
    print(f"walk-forward: train {a.train}m anchored / test {a.test}m, "
          f"headline horizon H={HEADLINE_H} sessions close-to-close, "
          f"cost {COST_PCT:.2f}%/trade")

    # ---- how often each rule even fires, and its FULL-SAMPLE (in-sample) mean
    print("\n" + "=" * 108)
    print("IN-SAMPLE, FULL PERIOD -- CONTEXT ONLY. This is the number that has "
          "already fooled this project 35 times.")
    print("=" * 108)
    print(f"   {'rule':<20}{'n':>6}{'fires%':>8}" +
          "".join(f"{'H=' + str(h) + ' mean':>12}{'t':>7}" for h in HORIZONS))
    for name, (fires, is_long) in rules.items():
        f = fires.fillna(False).astype(bool)
        cells = ""
        for h in HORIZONS:
            sign = np.where(is_long.loc[f] if hasattr(is_long, "loc") else is_long,
                            1.0, -1.0)
            r = (d.loc[f, f"long{h}"] * sign).dropna()
            cells += (f"{r.mean():>+12.3f}{t_stat(r.values):>+7.2f}"
                      if len(r) >= 10 else f"{'-':>12}{'-':>7}")
        print(f"   {name:<20}{int(f.sum()):>6}{f.mean() * 100:>7.1f}%{cells}")

    # ---- THE HEADLINE: walk-forward, out-of-sample only
    print("\n" + "=" * 108)
    print(f"WALK-FORWARD OUT-OF-SAMPLE (the headline is H={HEADLINE_H})")
    print("=" * 108)
    print(HEADER)
    results = {}
    for h in HORIZONS:
        st = stack(d, rules, h)
        cands = {name: (st["rule"] == name) for name in rules}
        res = walk_forward(st, cands, "ret", train_m=a.train, test_m=a.test,
                           anchored=True, min_trades=12)
        results[h] = res
        tag = "  <-- HEADLINE" if h == HEADLINE_H else ""
        report(f"H={h} sessions{tag}", res)

    res = results[HEADLINE_H]
    if "error" in res:
        print(res["error"])
        return 1

    # ---- what the folds actually chose
    print("\n" + "=" * 108)
    print(f"FOLD-BY-FOLD CHOICES, H={HEADLINE_H}   (stability = share of folds "
          "where the pick did NOT change)")
    print("=" * 108)
    folds = res["folds"]
    print(f"   {'test window':<14}{'rule chosen':<20}{'train mean':>12}"
          f"{'n oos':>7}{'oos mean':>11}")
    for _, r in folds.iterrows():
        print(f"   {r['fold_start']:<14}{r['rule']:<20}{r['train_mean']:>+12.3f}"
              f"{int(r['n_test']):>7}{r['test_mean']:>+11.3f}")
    picks = pd.Series(res["picks"]).value_counts()
    print(f"\n   distinct rules chosen: {len(picks)} of {len(rules)}   "
          f"switches {res['switches']}/{len(res['picks']) - 1}   "
          f"stability {res['stability'] * 100:.0f}%")
    print("   " + "  ".join(f"{k}x{v}" for k, v in picks.items()))

    # ---- overlap-corrected inference on the OOS concatenation
    oos = res["oos"].sort_values("dt")
    r = oos["ret"].values
    nw = newey_west_t(r, HEADLINE_H - 1)
    de = deoverlap(oos["dt"], oos["ret"], HEADLINE_H)
    print("\n" + "=" * 108)
    print("IS THE OUT-OF-SAMPLE t REAL? Overlapping H=4 windows inflate it.")
    print("=" * 108)
    print(f"   naive t on {len(r)} OOS trades                 {res['t']:>+8.2f}")
    print(f"   Newey-West t (Bartlett, lag {HEADLINE_H - 1})                "
          f"{nw:>+8.2f}")
    print(f"   de-overlapped t ({len(de)} spaced trades)          "
          f"{t_stat(de):>+8.2f}   mean {de.mean():>+7.3f}%")

    # ---- costs
    print("\n" + "=" * 108)
    print("NET OF COSTS")
    print("=" * 108)
    net = r - COST_PCT
    eq = (1 + net / 100).cumprod()
    print(f"   gross mean {res['mean']:>+7.3f}%/trade   "
          f"net {net.mean():>+7.3f}%/trade   "
          f"net t {t_stat(net):>+6.2f}   net total {(eq[-1] - 1) * 100:>+7.1f}%")
    print(f"   cost is {COST_PCT / abs(res['mean']) * 100:>.0f}% of the gross edge"
          if res["mean"] != 0 else "")

    # ---- baselines over the SAME out-of-sample months
    lo, hi = oos["dt"].min(), oos["dt"].max()
    same = d[(d["dt"] >= lo.normalize()) & (d["dt"] <= hi.normalize())]
    bh = (same["close"].iloc[-1] / same["close"].iloc[0] - 1.0) * 100.0
    every = same[f"long{HEADLINE_H}"].dropna()
    print("\n" + "=" * 108)
    print(f"BASELINES OVER THE SAME OOS WINDOW  {lo.date()} .. {hi.date()}  "
          f"({len(same)} sessions)")
    print("=" * 108)
    print(f"   buy & hold BANKNIFTY spot                    {bh:>+8.1f}%  total")
    print(f"   every session held {HEADLINE_H} sessions, long        "
          f"{every.mean():>+8.3f}%  per trade  (t {t_stat(every.values):>+5.2f},"
          f" win {(every > 0).mean() * 100:.0f}%, n {len(every)})")
    print(f"   the walk-forward strategy                    "
          f"{res['mean']:>+8.3f}%  per trade  (t {res['t']:>+5.2f}, "
          f"win {res['win'] * 100:.0f}%, n {res['n']})")
    print(f"   strategy total (gross)                       "
          f"{res['total'] * 100:>+8.1f}%  over {res['n']} trades, "
          f"maxDD {res['maxdd'] * 100:.1f}%")

    # ---- per-rule OOS-window in-sample means, to see whether the engine could
    #      have done better with hindsight (a bound on what was available)
    print("\n" + "=" * 108)
    print("WITH HINDSIGHT: each rule's mean over the OOS window alone "
          "(NOT tradeable -- shows what the picker was chasing)")
    print("=" * 108)
    rows = []
    for name, (fires, is_long) in rules.items():
        f = fires.fillna(False).astype(bool) & (d["dt"] >= lo.normalize())
        sign = np.where(is_long.loc[f] if hasattr(is_long, "loc") else is_long,
                        1.0, -1.0)
        rr = (d.loc[f, f"long{HEADLINE_H}"] * sign).dropna()
        if len(rr) >= 12:
            rows.append((name, len(rr), rr.mean(), t_stat(rr.values)))
    rows.sort(key=lambda x: -x[2])
    for name, n, mu, t in rows[:6]:
        print(f"   best  {name:<20}{n:>5}{mu:>+9.3f}{t:>+7.2f}")
    for name, n, mu, t in rows[-3:]:
        print(f"   worst {name:<20}{n:>5}{mu:>+9.3f}{t:>+7.2f}")

    print("\n(in-sample lines above are context; the conclusion is the "
          "out-of-sample block and its stability figure.)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
