"""Is open_vs_prev_vah ranking the MOVE, or only picking WHICH SIDE breaks?

THE SUSPICION. On the bank universe open_vs_prev_vah scored a session-demeaned
rank IC of +0.051 (t+2.99) against ret_3d, the only location feature to clear
the noise floor. But ret_3d is signed so positive = moved in the break
direction, and the two sides do not have the same ret_3d: UP breaks average
+0.10%, DOWN breaks -0.35%. A name that opens above the prior value area is the
obvious candidate to break UP. So the feature may be earning its IC purely by
sorting the up-breakers to the top of a session -- which is a statement about
DIRECTION, already known at entry, not about DISTANCE, which is what a buyer of
the option is actually paying for.

FOUR TESTS, in the order that settles it:

  1. SPLIT BY SIDE. Recompute every feature's rank IC demeaned within
     (session, side). Inside one side the mix argument is gone: everything left
     is genuine ranking of the move. A feature that survives in BOTH sides is
     real. One that only works pooled was the side mix.

  2. THE MIRROR (aligned_X = side * X). For a PE the whole geometry flips:
     "above the prior value area" for a CE is "below it" for a PE. If location
     is really a continuation feature, the mirrored version should rank the move
     pooled across both sides. This is the CE/PE-split logic the owner asked for
     -- and note it is algebraically the same claim as test 1 requiring IC_up>0
     and IC_down<0, so the two must agree.

  3. DOES LOCATION PREDICT THE SIDE AT ALL? Binned P(side==1) and a
     per-session rank correlation of the feature against the side itself. If it
     does, the confound is confirmed present; whether it is the WHOLE story is
     what test 1 answers.

  4. IS THE SIDE WORTH KNOWING? Session-demeaned up-minus-down ret_3d, paired
     within sessions that contain both. If the up/down gap does not survive its
     own t-test, then even a perfect side forecast buys nothing.

Method rules enforced throughout: demeaned by session (and by side where the
point is to remove it), t computed ACROSS SESSIONS with one observation per
session, split-half and drop-2-best on every headline, moves in percent.

    docker exec nomadcurie_vanguard_cycle \
        python /vanguard/research/mp_side_confound.py --lookback-days 700
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
from research.banknifty_rotation import BANKS  # noqa: E402
from research.mp_profile import FWD_SESSIONS, dsn, load  # noqa: E402

BANK_UNIVERSE = ("BANKNIFTY",) + BANKS
FEATURES = ["ib_vs_atr", "ib_width", "atr20", "break_frac", "gap",
            "open_vs_prev_vah", "open_vs_prev_poc", "ib_vs_prev_poc"]
# the ones whose sign only means something relative to the break direction
LOCATION = ["gap", "open_vs_prev_vah", "open_vs_prev_poc", "ib_vs_prev_poc"]
TARGETS = ["mfe_total", f"ret_{FWD_SESSIONS}d"]
MIN_GROUP = 5          # names needed in a (session, side) cell to rank anything
MIN_SESSIONS = 30      # sessions needed before a t is worth printing


# ----------------------------------------------------------------- statistics
def t_of(x: pd.Series) -> float:
    x = pd.Series(x).dropna()
    if len(x) < 3 or x.std(ddof=1) == 0:
        return np.nan
    return x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))


def drop2(x: pd.Series) -> tuple[float, float]:
    """Mean and t after removing the two sessions most favourable to the claim."""
    x = pd.Series(x).dropna().sort_values()
    if len(x) < 6:
        return np.nan, np.nan
    keep = x.iloc[:-2] if x.mean() >= 0 else x.iloc[2:]
    return keep.mean(), t_of(keep)


def halves(x: pd.Series) -> tuple[float, float]:
    x = pd.Series(x).dropna()
    h = len(x) // 2
    return x.iloc[:h].mean(), x.iloc[h:].mean()


def ic_by_session(d: pd.DataFrame, feature: str, target: str,
                  min_n: int = MIN_GROUP) -> pd.Series:
    """One Spearman IC per session, computed inside whatever d already is.

    Passing a single-side slice makes the demeaning (session, side) rather than
    session -- which is exactly the control this study is about.
    """
    out = {}
    for dt, g in d.groupby("dt", sort=True):
        g = g[[feature, target]].dropna()
        if len(g) < min_n or g[feature].nunique() < 2 or g[target].nunique() < 2:
            continue
        ic = g[feature].corr(g[target], method="spearman")
        if pd.notna(ic):
            out[dt] = ic
    return pd.Series(out, dtype=float).sort_index()


def edge_by_session(d: pd.DataFrame, feature: str, target: str, k: int,
                    min_n: int = MIN_GROUP) -> pd.Series:
    """Top-k names' target minus the session mean, in RETURN units (not ranks)."""
    out = {}
    for dt, g in d.groupby("dt", sort=True):
        g = g[[feature, target]].dropna()
        if len(g) < min_n or g[feature].nunique() < 2:
            continue
        out[dt] = g.nlargest(k, feature)[target].mean() - g[target].mean()
    return pd.Series(out, dtype=float).sort_index()


def ic_row(label: str, ics: pd.Series, edges: pd.Series, k: int) -> None:
    if len(ics) < MIN_SESSIONS:
        print(f"   {label:<22}{'too few sessions':>52}")
        return
    a, b = halves(ics)
    d2m, d2t = drop2(ics)
    star = " *" if abs(t_of(ics)) >= 2 else ""
    print(f"   {label:<22}{len(ics):>6}{ics.mean():>+8.3f}{t_of(ics):>+7.2f}"
          f"{a:>+8.3f}{b:>+8.3f}{d2m:>+8.3f}{d2t:>+7.2f}"
          f"{edges.mean() * 100:>+9.2f}{t_of(edges):>+7.2f}{star}")


def ic_header(title: str, k: int) -> None:
    print(f"\n{title}")
    print(f"   {'feature':<22}{'sess':>6}{'IC':>8}{'t':>7}{'IC 1h':>8}"
          f"{'IC 2h':>8}{'drop2':>8}{'t':>7}{f'top{k} pp':>9}{'t':>7}")


# ------------------------------------------------------------------ sections
def reproduce(b: pd.DataFrame) -> None:
    """The pooled baseline, so the side-split numbers have something to move from."""
    print("\n" + "=" * 100)
    print("0. POOLED BASELINE (both sides together, demeaned by SESSION only)")
    print("   This is the number under suspicion. Reproduced here so the split is")
    print("   read against the same data, not against the memo.")
    print("=" * 100)
    for target in TARGETS:
        ic_header(f"target = {target}   (all breaks, n={b[target].notna().sum():,})", 3)
        for f in FEATURES:
            d = b.dropna(subset=[f, target])
            ic_row(f, ic_by_session(d, f, target, 6),
                   edge_by_session(d, f, target, 3, 6), 3)


def by_side(b: pd.DataFrame) -> None:
    print("\n" + "=" * 100)
    print("1. SPLIT BY SIDE -- demeaned within (session, side)")
    print("   Inside one side the feature can no longer earn IC by sorting")
    print("   up-breakers above down-breakers. Whatever is left is real ranking.")
    print("=" * 100)
    for target in TARGETS:
        for side, name in ((1, "UP breaks (CE)"), (-1, "DOWN breaks (PE)")):
            s = b[b["side"] == side]
            ic_header(f"target = {target}   {name}   n={s[target].notna().sum():,}", 2)
            for f in FEATURES:
                d = s.dropna(subset=[f, target])
                ic_row(f, ic_by_session(d, f, target),
                       edge_by_session(d, f, target, 2), 2)
        # both sides, one observation per session = mean of the two within-side ICs
        ic_header(f"target = {target}   BOTH SIDES, within-side IC averaged "
                  f"per session", 2)
        for f in FEATURES:
            up = ic_by_session(b[b["side"] == 1].dropna(subset=[f, target]), f, target)
            dn = ic_by_session(b[b["side"] == -1].dropna(subset=[f, target]), f, target)
            comb = pd.concat([up, dn], axis=1).mean(axis=1).dropna()
            eup = edge_by_session(b[b["side"] == 1].dropna(subset=[f, target]),
                                  f, target, 2)
            edn = edge_by_session(b[b["side"] == -1].dropna(subset=[f, target]),
                                  f, target, 2)
            ecomb = pd.concat([eup, edn], axis=1).mean(axis=1).dropna()
            ic_row(f, comb, ecomb, 2)


def side_neutral_target(b: pd.DataFrame) -> None:
    """Keep every break in one pool, but strip the side's own mean out of the target.

    This is the direct decomposition: same n, same session demeaning, only the
    up/down level difference is gone. Whatever the pooled IC loses here is what
    it was borrowing from the side mix.
    """
    print("\n" + "=" * 100)
    print("1b. POOLED, BUT WITH THE SIDE'S MEAN REMOVED FROM THE TARGET")
    print("    target := target - mean(target | session, side). Same rows as the")
    print("    baseline; only the up-vs-down level gap is deleted.")
    print("=" * 100)
    d = b.copy()
    for target in TARGETS:
        col = f"{target}_sn"
        d[col] = d[target] - d.groupby(["dt", "side"])[target].transform("mean")
        ic_header(f"target = {target} (side-neutralised)   n={d[col].notna().sum():,}", 3)
        for f in FEATURES:
            dd = d.dropna(subset=[f, col])
            ic_row(f, ic_by_session(dd, f, col, 6),
                   edge_by_session(dd, f, col, 3, 6), 3)


def aligned(b: pd.DataFrame) -> None:
    print("\n" + "=" * 100)
    print("2. THE MIRROR:  aligned_X = side * X")
    print("   For a PE, opening BELOW the prior value area is the same statement")
    print("   that opening ABOVE it makes for a CE. If location is a continuation")
    print("   feature the mirrored version should rank the move pooled.")
    print("=" * 100)
    d = b.copy()
    cols = []
    for f in LOCATION:
        d[f"aligned_{f}"] = d["side"] * d[f]
        cols.append(f"aligned_{f}")
    for target in TARGETS:
        ic_header(f"target = {target}   all breaks pooled, demeaned by session", 3)
        for f in cols:
            dd = d.dropna(subset=[f, target])
            ic_row(f, ic_by_session(dd, f, target, 6),
                   edge_by_session(dd, f, target, 3, 6), 3)
    print("\n   Read with section 1: aligned IC > 0 requires IC_up > 0 AND IC_down < 0.")
    print("   If section 1 shows the same sign on both sides, the mirror CANNOT work.")


def predicts_side(s: pd.DataFrame) -> None:
    print("\n" + "=" * 100)
    print("3. DOES LOCATION PREDICT WHICH SIDE BREAKS?")
    print("=" * 100)
    n = len(s)
    up, dn, no = (s["side"] == 1).mean(), (s["side"] == -1).mean(), (s["side"] == 0).mean()
    print(f"   base rates over {n:,} sessions:  UP {up * 100:.1f}%   "
          f"DOWN {dn * 100:.1f}%   never {no * 100:.1f}%")
    b = s[s["side"] != 0]
    print(f"   conditional on breaking (n={len(b):,}):  P(side==+1) = "
          f"{(b['side'] == 1).mean() * 100:.1f}%")

    for f in ("open_vs_prev_vah", "open_vs_prev_poc", "gap", "ib_vs_prev_poc"):
        d = b.dropna(subset=[f]).copy()
        # within-session rank so the bins are a cross-sectional statement, not a
        # calendar one (all 17 names gap together on a macro morning)
        d["rk"] = d.groupby("dt")[f].rank(pct=True)
        q = pd.qcut(d["rk"], 5, labels=False, duplicates="drop")
        raw = pd.qcut(d[f], 5, labels=False, duplicates="drop")
        print(f"\n   {f}")
        print(f"      {'quintile':<12}{'n':>7}{'P(up) by session-rank':>24}"
              f"{'P(up) by raw value':>22}{'median value':>15}")
        for i in sorted(pd.Series(q).dropna().unique()):
            m, mr = q == i, raw == i
            print(f"      Q{int(i) + 1:<11}{int(m.sum()):>7}"
                  f"{(d.loc[m, 'side'] == 1).mean() * 100:>23.1f}%"
                  f"{(d.loc[mr, 'side'] == 1).mean() * 100:>21.1f}%"
                  f"{d.loc[m, f].median():>15.2f}")
        ics = ic_by_session(d, f, "side", 6)
        a, h2 = halves(ics)
        d2m, d2t = drop2(ics)
        print(f"      per-session rank IC vs side: {ics.mean():+.3f} "
              f"(t{t_of(ics):+.2f}, {len(ics)} sessions; halves {a:+.3f}/{h2:+.3f}; "
              f"drop2 {d2m:+.3f} t{d2t:+.2f})")


def side_worth_knowing(b: pd.DataFrame) -> None:
    print("\n" + "=" * 100)
    print("4. IS THE SIDE ITSELF WORTH KNOWING?  up minus down, paired by session")
    print("=" * 100)
    for target in TARGETS + ["mfe_intraday", f"mae_{FWD_SESSIONS}d"]:
        rows = {}
        for dt, g in b.groupby("dt", sort=True):
            u = g.loc[g["side"] == 1, target].dropna()
            v = g.loc[g["side"] == -1, target].dropna()
            if len(u) >= 2 and len(v) >= 2:
                rows[dt] = u.mean() - v.mean()
        diff = pd.Series(rows, dtype=float).sort_index()
        if len(diff) < MIN_SESSIONS:
            continue
        a, h2 = halves(diff)
        d2m, d2t = drop2(diff)
        u_all = b.loc[b["side"] == 1, target]
        v_all = b.loc[b["side"] == -1, target]
        print(f"\n   {target}")
        print(f"      pooled mean   UP {u_all.mean() * 100:+.2f}%   "
              f"DOWN {v_all.mean() * 100:+.2f}%   "
              f"median UP {u_all.median() * 100:+.2f}%  DOWN {v_all.median() * 100:+.2f}%")
        print(f"      paired up-minus-down, {len(diff)} sessions: "
              f"{diff.mean() * 100:+.3f}pp  t{t_of(diff):+.2f}  "
              f"halves {a * 100:+.3f}/{h2 * 100:+.3f}  "
              f"drop2 {d2m * 100:+.3f}pp t{d2t:+.2f}")


def headline_robustness(b: pd.DataFrame) -> None:
    """Everything that could still kill the surviving within-side result.

    Split-half and drop-2-best are already on every row above. What is not is
    (a) the payoff in percent rather than in rank units, (b) one dominant name
    carrying it, and (c) the feature being nothing but the gap, or nothing but
    trailing volatility, wearing a value-area costume.
    """
    print("\n" + "=" * 100)
    print("5. ROBUSTNESS ON THE PART THAT SURVIVED")
    print("=" * 100)
    target = f"ret_{FWD_SESSIONS}d"

    for f in ("open_vs_prev_vah", "open_vs_prev_poc"):
        print(f"\n   {f}: {target} in PERCENT by within-(session,side) quintile")
        print("      'dem' = the same number minus its (session, side) mean, which is")
        print("      the part selection can claim; the raw level is the side's, not the")
        print("      feature's. A LOSING raw level is still a loss however it ranks.")
        print(f"      {'quintile':<10}{'UP n':>7}{'UP raw':>9}{'UP dem':>9}"
              f"{'DOWN n':>9}{'DOWN raw':>11}{'DOWN dem':>10}{'DOWN MFE':>11}"
              f"{'MFE dem':>10}")
        d = b.dropna(subset=[f, target]).copy()
        d["rk"] = d.groupby(["dt", "side"])[f].rank(pct=True)
        d = d[d.groupby(["dt", "side"])[f].transform("size") >= MIN_GROUP]
        for c in (target, "mfe_total"):
            d[f"dem_{c}"] = d[c] - d.groupby(["dt", "side"])[c].transform("mean")
        d["q"] = pd.qcut(d["rk"], 5, labels=False, duplicates="drop")
        for i in sorted(d["q"].dropna().unique()):
            u = d[(d["q"] == i) & (d["side"] == 1)]
            v = d[(d["q"] == i) & (d["side"] == -1)]
            print(f"      Q{int(i) + 1:<9}{len(u):>7}{u[target].mean() * 100:>8.2f}%"
                  f"{u[f'dem_{target}'].mean() * 100:>+9.2f}"
                  f"{len(v):>9}{v[target].mean() * 100:>10.2f}%"
                  f"{v[f'dem_{target}'].mean() * 100:>+10.2f}"
                  f"{v['mfe_total'].mean() * 100:>10.2f}%"
                  f"{v['dem_mfe_total'].mean() * 100:>+10.2f}")

        # (b) leave one name out, on the side where it survived
        dn = b[b["side"] == -1].dropna(subset=[f, target])
        base = ic_by_session(dn, f, target)
        print(f"      DOWN-break leave-one-name-out (base IC {base.mean():+.3f} "
              f"t{t_of(base):+.2f}):")
        worst = []
        for name in sorted(dn["underlying"].unique()):
            ics = ic_by_session(dn[dn["underlying"] != name], f, target)
            if len(ics) >= MIN_SESSIONS:
                worst.append((t_of(ics), ics.mean(), name))
        worst.sort()
        for t, m, name in worst[:3]:
            print(f"         drop {name:<12} IC {m:+.3f}  t{t:+.2f}")
        print(f"         (worst of {len(worst)} single-name deletions shown; "
              f"the result must survive all of them)")

        # (c) is it just the gap, or just volatility?
        d2 = b[b["side"] != 0].dropna(subset=[f, target, "gap", "atr20"]).copy()
        grp = ["dt", "side"]
        for c in (f, "gap", "atr20"):
            d2[f"r_{c}"] = d2.groupby(grp)[c].rank(pct=True)
        d2 = d2[d2.groupby(grp)[f].transform("size") >= MIN_GROUP]
        x = np.column_stack([np.ones(len(d2)), d2["r_gap"], d2["r_atr20"]])
        beta, *_ = np.linalg.lstsq(x, d2[f"r_{f}"].values, rcond=None)
        d2["resid"] = d2[f"r_{f}"].values - x @ beta
        for side, label in ((1, "UP"), (-1, "DOWN")):
            ics = ic_by_session(d2[d2["side"] == side], "resid", target)
            if len(ics) < MIN_SESSIONS:
                continue
            a, h2 = halves(ics)
            d2m, d2t = drop2(ics)
            print(f"      {label:<5} orthogonalised to gap+atr20 ranks: "
                  f"IC {ics.mean():+.3f} t{t_of(ics):+.2f}  "
                  f"halves {a:+.3f}/{h2:+.3f}  drop2 {d2m:+.3f} t{d2t:+.2f}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--lookback-days", type=int, default=700)
    p.add_argument("--dsn", default=dsn())
    args = p.parse_args()

    start = date.today() - timedelta(days=args.lookback_days)
    conn = psycopg2.connect(args.dsn)
    try:
        s = load(conn, list(BANK_UNIVERSE), start)
    finally:
        conn.close()
    if s.empty:
        print("no sessions built")
        return 1

    b = s[s["side"] != 0].copy()
    print(f"universe=banks  names={s['underlying'].nunique()}  "
          f"window {s['dt'].min().date()} .. {s['dt'].max().date()}  "
          f"name-sessions={len(s):,}  breaks={len(b):,}  "
          f"dates={s['dt'].nunique():,}")
    print("all ICs are Spearman, computed inside a session (and inside a side where")
    print("stated), one observation per session; t is across those sessions.")

    reproduce(b)
    by_side(b)
    side_neutral_target(b)
    aligned(b)
    predicts_side(s)
    side_worth_knowing(b)
    headline_robustness(b)
    return 0


if __name__ == "__main__":
    sys.exit(main())
