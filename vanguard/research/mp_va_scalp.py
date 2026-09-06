"""INTRADAY VALUE-AREA SCALPS on BANKNIFTY 30m bars, decided by walk-forward.

THE QUESTION. Market Profile's intraday folklore is a small set of rules about
the value area: the 80% rule, POC reversion, fading a rejected probe of the
value edge, trading the break of it, and reacting to the prior session's POC /
VAH / VAL. Each is specific enough to be falsified. This file states TWENTY
named rules up front, then hands the whole set to the anchored walk-forward so
the choice among them is made on data the score never sees.

THE CANDIDATE SET IS FIXED AT 20 AND IS DECLARED HERE, before any result:

  FAMILY 1 -- THE 80% RULE (3 rules)
    Session's first bar closes OUTSIDE the prior session value area, then price
    is back INSIDE for two consecutive bars. Entry at the close of the second
    inside bar; the claim is a traverse to the far edge of that value area.
      r80_long    opened below prior value, came back in  -> long  (target py_vah)
      r80_short   opened above prior value, came back in  -> short (target py_val)
      r80_both    either, signed by the open's side
    The traverse rate itself is reported separately -- that is the literal 80%
    claim, and it is measured whether or not the trade makes money.

  FAMILY 2 -- POC REVERSION (5 rules)
    dist_poc is the close's distance from the DEVELOPING POC in percent. Three
    fixed thresholds, chosen before looking and never tuned inside a fold.
      poc_rev_25 / poc_rev_50 / poc_rev_75   fade: long if below by more than
                                             X, short if above by more than X
      poc_rev_50_long / poc_rev_50_short     the 0.50 threshold, one side only

  FAMILY 3 -- VALUE-EDGE FADE (3 rules)
    The bar's high pokes above dev_vah but the bar CLOSES back inside -> short.
    The bar's low pokes below dev_val but the bar CLOSES back inside -> long.
      edge_fade_short / edge_fade_long / edge_fade_both

  FAMILY 4 -- VALUE-EDGE BREAK (3 rules)
    The mirror. The FIRST bar of the session whose close is outside the
    developing value area, traded in the direction of the break.
      edge_break_long / edge_break_short / edge_break_both

  FAMILY 5 -- PRIOR-VALUE REFERENCE (6 rules)
    The session's FIRST touch of the prior session's POC / VAH / VAL (the bar
    trades through the level), with the approach direction taken from the
    previous bar's close. Faded and followed are separate rules.
      pv_poc_fade / pv_poc_follow / pv_vah_fade / pv_vah_follow
      pv_val_fade / pv_val_follow

CONVENTIONS, applied uniformly and stated so they are not mistaken for choices
made after the fact:
  * Entry is at the CLOSE of the qualifying bar, and only bars 2..10 of the 13
    are eligible, so there is always time left for the trade to work.
  * ONE entry per session per rule. Where the rule says "first" (families 4 and
    5) the first occurrence is found over the WHOLE session and then required to
    land in bars 2..10 -- a level first touched at bar 1 does not become a bar-4
    signal. Elsewhere it is the first qualifying bar inside the window.
  * Two exits are reported for every rule: the SESSION CLOSE (r_eod) and a fixed
    TWO-BAR hold, so the holding period is measured rather than assumed.
  * Costs: 4bp round trip subtracted from every trade. Reported separately.

Walk-forward: 18 months train, 6 months test, anchored, min 12 training trades,
selection by in-sample mean. Only the concatenated out-of-sample stretches are
scored. STABILITY (share of folds where the pick did not change) is reported,
because a winner that reshuffles every fold has found noise.

    docker exec nomadcurie_vanguard_cycle python /vanguard/research/mp_va_scalp.py
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.mp_auction import dsn  # noqa: E402
from research.mp_intraday import load_intraday  # noqa: E402
from research.mp_walkforward import HEADER, report, walk_forward  # noqa: E402

COST = 0.04          # 4bp round trip, in percent
BAR_LO, BAR_HI = 2, 10
POC_THRESHOLDS = (0.25, 0.50, 0.75)

FAMILY = {
    "80% rule": ["r80_long", "r80_short", "r80_both"],
    "POC reversion": ["poc_rev_25", "poc_rev_50", "poc_rev_75",
                      "poc_rev_50_long", "poc_rev_50_short"],
    "value-edge fade": ["edge_fade_short", "edge_fade_long", "edge_fade_both"],
    "value-edge break": ["edge_break_long", "edge_break_short", "edge_break_both"],
    "prior value": ["pv_poc_fade", "pv_poc_follow", "pv_vah_fade",
                    "pv_vah_follow", "pv_val_fade", "pv_val_follow"],
}


def t_of(x: pd.Series) -> float:
    x = x.dropna()
    if len(x) < 5 or x.std(ddof=1) == 0:
        return np.nan
    return x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))


def prep(f: pd.DataFrame) -> pd.DataFrame:
    """Per-bar helpers: the 2-bar forward close, the previous close, the side
    the session opened on relative to the prior value area."""
    f = f.sort_values(["underlying", "dt", "bar"]).reset_index(drop=True)
    g = f.groupby(["underlying", "dt"], sort=False)
    last_close = g["close"].transform("last")
    f["close_f2"] = g["close"].shift(-2)
    f["close_f2"] = f["close_f2"].fillna(last_close)      # never binds for bars<=10
    f["r2"] = (f["close_f2"] / f["close"] - 1) * 100
    f["prev_close"] = g["close"].shift(1)
    f["prev_biv"] = g["back_in_value"].shift(1).fillna(False).astype(bool)

    b0 = f[f["bar"] == 0][["underlying", "dt", "close", "py_val", "py_vah"]].copy()
    side = np.where(b0["close"] < b0["py_val"], 1,
                    np.where(b0["close"] > b0["py_vah"], -1, 0))
    b0 = b0[["underlying", "dt"]].assign(open_side=side)
    return f.merge(b0, on=["underlying", "dt"], how="left")


class RuleBook:
    """Collects {name: (entry mask, side array)} for a frame."""

    def __init__(self, f: pd.DataFrame):
        self.f = f
        self.elig = f["bar"].between(BAR_LO, BAR_HI)
        self.rules: dict[str, tuple[pd.Series, np.ndarray]] = {}

    def _first(self, cond: pd.Series, whole_session: bool) -> pd.Series:
        """One entry per session. whole_session=True finds the first occurrence
        over ALL bars and then demands it fall inside the eligible window."""
        c = cond.fillna(False).astype(bool)
        if not whole_session:
            c = c & self.elig
        hits = self.f.loc[c]
        if not len(hits):
            return pd.Series(False, index=self.f.index)
        first_idx = hits.groupby(["underlying", "dt"], sort=False).head(1).index
        out = pd.Series(False, index=self.f.index)
        out.loc[first_idx] = True
        if whole_session:
            out = out & self.elig
        return out

    def add(self, name, cond, side, whole_session=False) -> None:
        mask = self._first(cond, whole_session)
        s = np.asarray(side)
        if s.ndim == 0:
            s = np.full(len(self.f), float(s))
        self.rules[name] = (mask, s)


def build_rules(f: pd.DataFrame) -> RuleBook:
    rb = RuleBook(f)

    # ---- FAMILY 1: the 80% rule ------------------------------------------
    cond80 = (f["opened_outside_py"] & f["back_in_value"] & f["prev_biv"]
              & f["open_side"].ne(0) & f["open_side"].notna())
    os_ = f["open_side"].fillna(0).values
    rb.add("r80_long", cond80 & f["open_side"].eq(1), 1.0)
    rb.add("r80_short", cond80 & f["open_side"].eq(-1), -1.0)
    rb.add("r80_both", cond80, os_)

    # ---- FAMILY 2: POC reversion -----------------------------------------
    d = f["dist_poc"]
    fade_side = -np.sign(d.fillna(0).values)
    for x in POC_THRESHOLDS:
        rb.add(f"poc_rev_{int(x * 100)}", d.abs() > x, fade_side)
    rb.add("poc_rev_50_long", d < -0.50, 1.0)
    rb.add("poc_rev_50_short", d > 0.50, -1.0)

    # ---- FAMILY 3: value-edge fade (rejected probe) ----------------------
    fade_s = (f["high"] > f["dev_vah"]) & (f["close"] <= f["dev_vah"])
    fade_l = (f["low"] < f["dev_val"]) & (f["close"] >= f["dev_val"])
    rb.add("edge_fade_short", fade_s, -1.0)
    rb.add("edge_fade_long", fade_l, 1.0)
    both = fade_s ^ fade_l                      # drop the ambiguous both-sides bar
    rb.add("edge_fade_both", both, np.where(fade_s.values, -1.0, 1.0))

    # ---- FAMILY 4: value-edge break (first close outside developing VA) ---
    brk_up = f["close"] > f["dev_vah"]
    brk_dn = f["close"] < f["dev_val"]
    rb.add("edge_break_long", brk_up, 1.0, whole_session=True)
    rb.add("edge_break_short", brk_dn, -1.0, whole_session=True)
    rb.add("edge_break_both", brk_up | brk_dn,
           np.where(brk_up.values, 1.0, -1.0), whole_session=True)

    # ---- FAMILY 5: first touch of a prior-session reference --------------
    for tag, col in (("poc", "py_poc"), ("vah", "py_vah"), ("val", "py_val")):
        lvl = f[col]
        touch = (f["low"] <= lvl) & (f["high"] >= lvl) & lvl.notna()
        approach = np.sign((f["prev_close"] - lvl).fillna(0).values)
        ok = touch & (approach != 0)
        rb.add(f"pv_{tag}_fade", ok, approach, whole_session=True)
        rb.add(f"pv_{tag}_follow", ok, -approach, whole_session=True)
    return rb


def stack(f: pd.DataFrame, rb: RuleBook) -> pd.DataFrame:
    """One row per (rule, entry). dt is nudged by rule so every dt is unique --
    walk_forward re-sorts by dt and a tie there could permute the alignment."""
    out = []
    for i, (name, (mask, side)) in enumerate(rb.rules.items()):
        sub = f.loc[mask].copy()
        if not len(sub):
            continue
        s = side[mask.values]
        sub["rule"] = name
        sub["side"] = s
        sub["ret_eod"] = s * sub["r_eod"]
        sub["ret_2bar"] = s * sub["r2"]
        sub["ret_eod_net"] = sub["ret_eod"] - COST
        sub["ret_2bar_net"] = sub["ret_2bar"] - COST
        sub["dt"] = pd.to_datetime(sub["ts"]) + pd.to_timedelta(i, unit="ns")
        out.append(sub)
    t = pd.concat(out, ignore_index=True)
    t = t.sort_values("dt", kind="mergesort").reset_index(drop=True)
    assert t["dt"].is_unique, "dt collision would break walk_forward alignment"
    assert t.sort_values("dt").index.equals(t.index), "re-sort is not identity"
    return t


def insample_table(t: pd.DataFrame) -> None:
    print(f"   {'rule':<20}{'n':>6}{'sess/yr':>9}{'EOD mean':>10}{'t':>7}{'win':>6}"
          f"{'2bar mean':>11}{'t':>7}{'win':>6}{'EOD net':>10}")
    for fam, names in FAMILY.items():
        print(f"   -- {fam}")
        for name in names:
            g = t[t["rule"] == name]
            if not len(g):
                print(f"   {name:<20}{0:>6}   (never fires)")
                continue
            a, b = g["ret_eod"].dropna(), g["ret_2bar"].dropna()
            yrs = (t["dt"].max() - t["dt"].min()).days / 365.25
            print(f"   {name:<20}{len(g):>6}{len(g) / yrs:>9.0f}"
                  f"{a.mean():>+10.3f}{t_of(a):>+7.2f}{(a > 0).mean() * 100:>5.0f}%"
                  f"{b.mean():>+11.3f}{t_of(b):>+7.2f}{(b > 0).mean() * 100:>5.0f}%"
                  f"{a.mean() - COST:>+10.3f}")


def eighty_percent_claim(f: pd.DataFrame, rb: RuleBook) -> None:
    """The literal claim: after the return to value, does price TRAVERSE it?"""
    mask, side = rb.rules["r80_both"]
    s = f.loc[mask].copy()
    sd = side[mask.values]
    s["side"] = sd
    hi_reach = s["close"] * (1 + s["mfe_eod"] / 100)
    lo_reach = s["close"] * (1 + s["mae_eod"] / 100)
    traverse = np.where(sd > 0, hi_reach >= s["py_vah"], lo_reach <= s["py_val"])
    s["traverse"] = traverse
    print(f"   setups {len(s)} in {f['dt'].nunique()} sessions "
          f"({len(s) / f['dt'].nunique() * 100:.0f}% of sessions)")
    print(f"   reached the FAR edge of prior value before the close: "
          f"{s['traverse'].mean() * 100:.1f}%   (the claim is 80%)")
    for lbl, sel in (("from below (long)", sd > 0), ("from above (short)", sd < 0)):
        g = s[sel]
        if len(g) < 10:
            continue
        print(f"      {lbl:<20}n={len(g):>4}  traverse {g['traverse'].mean() * 100:>5.1f}%"
              f"   EOD {(g['side'] * g['r_eod']).mean():>+7.3f}%")
    # what does the far edge actually pay, if you could exit exactly there?
    hit = s[s["traverse"]]
    if len(hit):
        tgt = np.where(hit["side"] > 0, hit["py_vah"], hit["py_val"])
        pay = hit["side"] * (tgt / hit["close"] - 1) * 100
        print(f"   on the {len(hit)} traverses the target itself was worth "
              f"{pay.mean():+.3f}% gross ({pay.mean() - COST:+.3f}% net) -- and "
              f"{(1 - s['traverse'].mean()) * 100:.0f}% of setups never got there")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="BANKNIFTY")
    p.add_argument("--years", type=float, default=5.4)
    p.add_argument("--dsn", default=dsn())
    args = p.parse_args()

    start = date.today() - timedelta(days=int(args.years * 365.25))
    conn = psycopg2.connect(args.dsn)
    try:
        raw = load_intraday(conn, [args.symbol], start)
    finally:
        conn.close()

    f = prep(raw)
    f = f[f["py_val"].notna()].reset_index(drop=True)     # need a prior profile
    print(f"{args.symbol}: {len(f):,} bars / {f['dt'].nunique():,} sessions   "
          f"{f['dt'].min().date()} .. {f['dt'].max().date()}")
    elig = f[f["bar"].between(BAR_LO, BAR_HI)]
    print(f"eligible entry bars {BAR_LO}..{BAR_HI}: {len(elig):,}   "
          f"mean r_eod on ALL of them (long, no rule) {elig['r_eod'].mean():+.4f}% "
          f"(t={t_of(elig['r_eod']):+.2f})   "
          f"mean |r_eod| {elig['r_eod'].abs().mean():.3f}%")
    print(f"COST assumption: {COST:.2f}% round trip.\n")

    rb = build_rules(f)
    t = stack(f, rb)
    print(f"CANDIDATE SET: {len(rb.rules)} named rules, declared before any result.")
    print(f"trade rows {len(t):,}  spanning "
          f"{t['dt'].min().date()} .. {t['dt'].max().date()}\n")

    print("IN-SAMPLE, whole history -- CONTEXT ONLY, this is where 20 rules were "
          "looked at at once")
    insample_table(t)

    print("\nTHE 80% RULE, tested literally")
    eighty_percent_claim(f, rb)

    cands = {n: (t["rule"] == n) for n in rb.rules}

    print("\nWALK-FORWARD, anchored, 18m train / 6m test, min 12 training trades")
    print("all 20 rules in one pool -- the honest position, since nothing told us "
          "which family to prefer beforehand")
    print(HEADER)
    for lbl, col in (("ALL 20 -> EOD exit", "ret_eod"),
                     ("ALL 20 -> EOD exit, net 4bp", "ret_eod_net"),
                     ("ALL 20 -> 2-bar exit", "ret_2bar"),
                     ("ALL 20 -> 2-bar exit, net 4bp", "ret_2bar_net")):
        report(lbl, walk_forward(t, cands, col))

    print("\n   per-family pools (each family was declared up front, so each is a "
          "legitimate pre-specified subset -- but picking the best family AFTER "
          "seeing these is not)")
    print(HEADER)
    for fam, names in FAMILY.items():
        sub = {n: cands[n] for n in names if n in cands}
        report(f"{fam} -> EOD", walk_forward(t, sub, "ret_eod"))
        report(f"{fam} -> EOD net", walk_forward(t, sub, "ret_eod_net"))

    print("\n   robustness: rolling (non-anchored) training window, and a longer "
          "24m train")
    print(HEADER)
    report("ALL 20 EOD, rolling 18/6", walk_forward(t, cands, "ret_eod", anchored=False))
    report("ALL 20 EOD, anchored 24/6", walk_forward(t, cands, "ret_eod", train_m=24))
    report("ALL 20 2bar, rolling 18/6", walk_forward(t, cands, "ret_2bar", anchored=False))

    res = walk_forward(t, cands, "ret_eod")
    if "error" not in res:
        print("\nFOLD BY FOLD (headline run, EOD exit, gross)")
        fo = res["folds"]
        print(f"   {'test from':<12}{'rule picked':<20}{'train mean':>12}"
              f"{'n test':>8}{'test mean':>12}")
        for _, r in fo.iterrows():
            tm = f"{r['test_mean']:+.3f}" if pd.notna(r["test_mean"]) else "-"
            print(f"   {r['fold_start']:<12}{r['rule']:<20}"
                  f"{r['train_mean']:>+12.3f}{int(r['n_test']):>8}{tm:>12}")
        print(f"   stability {res['stability'] * 100:.0f}%  "
              f"({res['switches']} switches over {len(res['picks'])} folds, "
              f"{len(set(res['picks']))} distinct rules ever chosen)")
        o = res["oos"]
        print("\n   OOS by calendar year")
        for y, g in o.groupby(o["dt"].dt.year):
            r = g["ret_eod"]
            print(f"      {y}  n={len(r):>4}  mean {r.mean():>+7.3f}%  "
                  f"t={t_of(r):>+6.2f}  win {(r > 0).mean() * 100:>3.0f}%")

    return 0


if __name__ == "__main__":
    sys.exit(main())
