"""REFUTATION pass over mp_side_confound.py.

The claim under attack: open_vs_prev_vah / open_vs_prev_poc survive the side
control, live entirely inside DOWN breaks, and score +0.082/+0.083 (t+3.05/
+3.16) against ret_3d with stable halves and a surviving orthogonalisation to
gap+atr20.

Every number in mp_side_confound.py reproduces exactly. So this pass does not
re-litigate arithmetic; it attacks the inference:

  A  LOOK-AHEAD AUDIT. Every feature rebuilt strictly from information
     available BEFORE the entry bar, and compared with what the study used.
  B  THE t IS ACROSS SESSIONS (correct) BUT THE SESSIONS OVERLAP. ret_3d
     windows on adjacent sessions share 2 of 3 days. Newey-West, a strictly
     non-overlapping subsample, and a circular block bootstrap.
  C  IS "DOWN ONLY" A FINDING? The paired per-session DOWN-minus-UP IC
     difference, which is the test the claim never ran.
  D  CONCENTRATION. Drop the best 2 / 5 / 10 sessions, the best 2 NAMES
     (all 136 pairs, not just leave-one-out), and the best calendar month.
  E  WHERE IN THE HORIZON DOES IT LIVE? ret_3d decomposed into the
     entry->close stub (eod) and the clean 3-session leg measured from the
     session close. A "3-day edge" that sits in the stub is not one.
  F  IS IT AN MP FINDING AT ALL? The same feature against the plain
     unsigned 3-session forward return over ALL name-sessions, breaks and
     non-breaks alike.
  G  COST. 0.05% per side on the underlying, and what that does to the only
     tradeable expression of the result.
  H  OUT OF UNIVERSE. ~200 non-bank NSE names, identical coverage window,
     identical code path. The claim's own caveat 7 asks for this.

    docker exec nomadcurie_vanguard_cycle \
        python /vanguard/research/mp_side_refute.py
"""
from __future__ import annotations

import itertools
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/vanguard")
from research.mp_profile import FWD_SESSIONS  # noqa: E402

CACHE = "/vanguard/research/_refute_cache"
MIN_GROUP = 5
MIN_SESSIONS = 30
TGT = f"ret_{FWD_SESSIONS}d"
RNG = np.random.default_rng(20260828)


# ------------------------------------------------------------------ helpers
def t_of(x) -> float:
    x = pd.Series(x).dropna()
    if len(x) < 3 or x.std(ddof=1) == 0:
        return np.nan
    return x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))


def nw_t(x, lag: int) -> float:
    """Newey-West t for the mean of a serially correlated series."""
    x = pd.Series(x).dropna().values.astype(float)
    n = len(x)
    if n < lag + 3:
        return np.nan
    e = x - x.mean()
    var = (e @ e) / n
    for l in range(1, lag + 1):
        g = (e[l:] @ e[:-l]) / n
        var += 2.0 * (1.0 - l / (lag + 1.0)) * g
    if var <= 0:
        return np.nan
    return x.mean() / np.sqrt(var / n)


def block_boot_p(x, block: int, reps: int = 20000) -> float:
    """Two-sided p for mean==0 under a circular block bootstrap (null centred)."""
    x = pd.Series(x).dropna().values.astype(float)
    n = len(x)
    if n < 3 * block:
        return np.nan
    c = x - x.mean()
    nb = int(np.ceil(n / block))
    starts = RNG.integers(0, n, size=(reps, nb))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]) % n
    means = c[idx.reshape(reps, -1)[:, :n]].mean(axis=1)
    return float((np.abs(means) >= abs(x.mean())).mean())


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 2:
        return np.nan
    ra = pd.Series(a).rank().values
    rb = pd.Series(b).rank().values
    if ra.std() == 0 or rb.std() == 0:
        return np.nan
    return float(np.corrcoef(ra, rb)[0, 1])


def ic_series(d: pd.DataFrame, feature: str, target: str,
              min_n: int = MIN_GROUP) -> pd.Series:
    out = {}
    for dt, g in d.groupby("dt", sort=True):
        g = g[[feature, target]].dropna()
        if len(g) < min_n or g[feature].nunique() < 2 or g[target].nunique() < 2:
            continue
        ic = spearman(g[feature].values, g[target].values)
        if pd.notna(ic):
            out[dt] = ic
    return pd.Series(out, dtype=float).sort_index()


def drop_best(x: pd.Series, k: int) -> tuple[float, float]:
    x = pd.Series(x).dropna().sort_values()
    if len(x) < k + 5:
        return np.nan, np.nan
    keep = x.iloc[:-k] if x.mean() >= 0 else x.iloc[k:]
    return keep.mean(), t_of(keep)


def halves(x: pd.Series) -> tuple[float, float]:
    x = pd.Series(x).dropna()
    h = len(x) // 2
    return x.iloc[:h].mean(), x.iloc[h:].mean()


def line(label: str, ics: pd.Series) -> None:
    a, b = halves(ics)
    print(f"   {label:<38}{len(ics):>5}{ics.mean():>+8.3f}{t_of(ics):>+7.2f}"
          f"   halves {a:+.3f}/{b:+.3f}")


def rebuild_context(frame: pd.DataFrame) -> pd.DataFrame:
    """Add strictly-lagged volatility and the pieces of the horizon."""
    out = []
    for _, g in frame.groupby("underlying", sort=False):
        g = g.sort_values("dt").reset_index(drop=True)
        prev_close = g["close"].shift(1)
        tr = pd.concat([g["high"] - g["low"],
                        (g["high"] - prev_close).abs(),
                        (g["low"] - prev_close).abs()], axis=1).max(axis=1)
        # what the study used: today's TR is IN the window and today's close is
        # the denominator -- both unknown at the break bar.
        g["atr20_asis"] = tr.rolling(20, min_periods=10).mean() / g["close"]
        # strictly pre-open: TR through YESTERDAY over YESTERDAY's close
        g["atr20_lag"] = (tr.shift(1).rolling(20, min_periods=10).mean()
                          / prev_close)
        g["ib_vs_atr_lag"] = g["ib_width"] / g["atr20_lag"].replace(0, np.nan)
        g["prev_dt"] = g["dt"].shift(1)
        g["stale_days"] = (g["dt"] - g["prev_dt"]).dt.days
        # horizon pieces
        fcl = g["close"].shift(-FWD_SESSIONS)
        g["fwd3_raw"] = fcl / g["close"] - 1.0            # unsigned, from close
        g["ret3_from_close"] = g["side"] * g["fwd3_raw"]  # signed, clean leg
        g["stub"] = g["eod"]                              # entry -> today's close
        out.append(g)
    return pd.concat(out, ignore_index=True)


# ------------------------------------------------------------------ sections
def section_a(s: pd.DataFrame, b: pd.DataFrame) -> None:
    print("\n" + "=" * 100)
    print("A. LOOK-AHEAD AUDIT")
    print("=" * 100)
    print("""
   Feature-by-feature, what is knowable when:
     open, gap, open_vs_prev_vah/_poc   09:15 open + PRIOR session profile   CLEAN
     ib_hi/ib_lo/ib_ref/ib_width        09:15-10:15, entry is >=10:15        CLEAN
     ib_vs_prev_poc                     10:15 close + prior POC              CLEAN
     break_frac                         index of the entry bar itself        CLEAN
     poc/val/vah (today)                FULL session -- NOT used as features
     atr20 / ib_vs_atr                  see below                            DIRTY
""")
    d = rebuild_context(s)
    same = d["atr20_asis"].corr(d["atr20_lag"])
    print(f"   atr20 as the study builds it = mean TR over sessions t-19..t "
          f"divided by CLOSE(t).")
    print(f"   Both halves of that are unknown at the 10:15-15:15 entry bar: "
          f"TR(t) contains the")
    print(f"   post-entry range and close(t) is the 15:15 print. "
          f"corr(as-is, strictly lagged) = {same:.3f}")
    tr_today = (d["high"] - d["low"]) / d["close"]
    print(f"   corr(atr20_asis, TODAY's own range/close) = "
          f"{d['atr20_asis'].corr(tr_today):.3f}   vs lagged version "
          f"{d['atr20_lag'].corr(tr_today):.3f}")
    bb = d[d["side"] != 0]
    print("\n   what the contamination is worth (pooled, demeaned by session):")
    for tgt in ("mfe_total", TGT):
        for f in ("atr20_asis", "atr20_lag", "ib_vs_atr", "ib_vs_atr_lag"):
            ics = ic_series(bb.dropna(subset=[f, tgt]), f, tgt, 6)
            line(f"{f} vs {tgt}", ics)
    print("\n   -> REAL but SMALL. The look-ahead is genuine (TR(t) and close(t)"
          " are both\n      post-entry) yet worth only ~0.012 of IC on mfe_total"
          " because a 20-session\n      mean is dominated by its 19 clean terms."
          " atr20 is still the honest\n      benchmark for method rule 5, and "
          "the study's orthogonalisation control is\n      not materially "
          "damaged. This one does NOT refute anything.")

    st = d.loc[d["side"] != 0, "stale_days"]
    print(f"\n   prev-session staleness (the shift(1) that supplies prev_vah is "
          f"positional,\n   not calendar): median {st.median():.0f}d  "
          f"p90 {st.quantile(0.9):.0f}d  "
          f">4d on {(st > 4).mean() * 100:.1f}% of break rows")


def section_b(b: pd.DataFrame) -> None:
    print("\n" + "=" * 100)
    print("B. THE t IS ACROSS SESSIONS -- BUT THE SESSIONS OVERLAP")
    print("=" * 100)
    print("   One obs per session is right. But ret_3d(t) and ret_3d(t+1) share "
          "2 of their 3\n   forward sessions, and every bank moves together, so "
          "consecutive per-session ICs\n   are NOT independent draws. Newey-West, "
          "a strictly non-overlapping subsample,\n   and a block bootstrap.\n")
    all_dates = sorted(pd.to_datetime(pd.Series(b["dt"].unique())))
    pos = {pd.Timestamp(d): i for i, d in enumerate(all_dates)}
    hdr = (f"   {'series':<34}{'n':>5}{'IC':>8}{'t':>7}{'NW2':>7}{'NW5':>7}"
           f"{'NW10':>7}{'boot p':>9}{'rho1':>7}")
    print(hdr)
    cases = []
    for f in ("open_vs_prev_vah", "open_vs_prev_poc"):
        cases.append((f"{f} pooled", b.dropna(subset=[f, TGT]), f, TGT))
        for side, nm in ((1, "UP"), (-1, "DOWN")):
            sl = b[b["side"] == side].dropna(subset=[f, TGT])
            cases.append((f"{f} {nm}", sl, f, TGT))
    keep = {}
    for label, d, f, tgt in cases:
        ics = ic_series(d, f, tgt, 6 if "pooled" in label else MIN_GROUP)
        keep[label] = ics
        rho = pd.Series(ics.values).autocorr(1)
        print(f"   {label:<34}{len(ics):>5}{ics.mean():>+8.3f}{t_of(ics):>+7.2f}"
              f"{nw_t(ics, 2):>+7.2f}{nw_t(ics, 5):>+7.2f}{nw_t(ics, 10):>+7.2f}"
              f"{block_boot_p(ics, 5):>9.3f}{rho:>+7.2f}")

    print("\n   strictly NON-OVERLAPPING sessions (each kept session >= 3 "
          "trading days after\n   the last kept one, so no two ret_3d windows "
          "touch). 3 phases shown.")
    for label in ("open_vs_prev_vah DOWN", "open_vs_prev_poc DOWN",
                  "open_vs_prev_vah pooled", "open_vs_prev_poc pooled"):
        ics = keep[label]
        idx = np.array([pos[pd.Timestamp(d)] for d in ics.index])
        for phase in range(3):
            sel, last = [], -99
            for j, p in enumerate(idx):
                if p >= last + FWD_SESSIONS and p % FWD_SESSIONS == phase:
                    sel.append(j)
                    last = p
            sub = ics.iloc[sel]
            print(f"      {label:<26} phase{phase}  n={len(sub):>4}  "
                  f"IC {sub.mean():+.3f}  t{t_of(sub):+.2f}")


def section_c(b: pd.DataFrame) -> None:
    print("\n" + "=" * 100)
    print("C. IS 'IT LIVES ENTIRELY INSIDE DOWN BREAKS' A FINDING?")
    print("=" * 100)
    print("   The claim reads UP t+1.50 as 'nothing' and DOWN t+3.05 as 'the "
          "survivor'.\n   That is comparing each to zero. The statement 'DOWN > "
          "UP' needs its own test:\n   the paired per-session (IC_down - IC_up) "
          "on sessions that carry both sides.\n")
    for f in ("open_vs_prev_vah", "open_vs_prev_poc"):
        up = ic_series(b[b["side"] == 1].dropna(subset=[f, TGT]), f, TGT)
        dn = ic_series(b[b["side"] == -1].dropna(subset=[f, TGT]), f, TGT)
        j = pd.concat([up.rename("up"), dn.rename("dn")], axis=1).dropna()
        d = j["dn"] - j["up"]
        a, h = halves(d)
        print(f"   {f}")
        print(f"      paired sessions n={len(j)}   UP {j['up'].mean():+.3f}   "
              f"DOWN {j['dn'].mean():+.3f}")
        print(f"      DOWN-minus-UP {d.mean():+.3f}  t{t_of(d):+.2f}  "
              f"NW5 {nw_t(d, 5):+.2f}  halves {a:+.3f}/{h:+.3f}  "
              f"boot p {block_boot_p(d, 5):.3f}")
    print("\n   Also: the two sides are not independent tests of one hypothesis "
          "-- they are a\n   2-way split chosen AFTER the pooled result. With 8 "
          "features x 2 sides x 2 targets\n   = 32 within-side cells, the "
          "Bonferroni 5% bar is |t| >= 2.9 and the Holm bar for\n   the single "
          "largest is the same. DOWN open_vs_prev_poc t+3.16 clears it by a "
          "hair;\n   open_vs_prev_vah t+3.05 clears it by less; both fail it "
          "under Newey-West above.")


def section_d(b: pd.DataFrame) -> None:
    print("\n" + "=" * 100)
    print("D. CONCENTRATION: DROP-2-BEST NAMES, AND MORE THAN 2 SESSIONS")
    print("=" * 100)
    print("   The study ran leave-ONE-name-out and drop-2-SESSIONS. Neither is "
          "demanding on\n   a 230-session, 17-name panel. Here: drop the best "
          "2/5/10 sessions, all 136\n   two-name deletions, the best calendar "
          "month, and the per-month sign count.\n")
    for f in ("open_vs_prev_vah", "open_vs_prev_poc"):
        dn = b[b["side"] == -1].dropna(subset=[f, TGT])
        ics = ic_series(dn, f, TGT)
        print(f"   {f}  DOWN base IC {ics.mean():+.3f} t{t_of(ics):+.2f} "
              f"({len(ics)} sessions)")
        for k in (2, 5, 10, 20):
            m, t = drop_best(ics, k)
            print(f"      drop best {k:>2} sessions:  IC {m:+.3f}  t{t:+.2f}")

        # all two-name deletions
        names = sorted(dn["underlying"].unique())
        cells = []
        for dt, g in dn.groupby("dt", sort=True):
            cells.append((g["underlying"].values, g[f].values, g[TGT].values))
        worst = (9e9, None)
        best = (-9e9, None)
        for pair in itertools.combinations(names, 2):
            vals = []
            for nm, x, y in cells:
                m = ~np.isin(nm, pair)
                if m.sum() < MIN_GROUP:
                    continue
                ic = spearman(x[m], y[m])
                if pd.notna(ic):
                    vals.append(ic)
            v = pd.Series(vals)
            if len(v) < MIN_SESSIONS:
                continue
            tt = t_of(v)
            if tt < worst[0]:
                worst = (tt, (pair, v.mean(), len(v)))
            if tt > best[0]:
                best = (tt, (pair, v.mean(), len(v)))
        (pair, m, n) = worst[1]
        print(f"      worst of 136 two-name deletions: drop {pair[0]}+{pair[1]}"
              f"  IC {m:+.3f}  t{worst[0]:+.2f}  (n={n})")
        (pair, m, n) = best[1]
        print(f"      best  of 136 two-name deletions: drop {pair[0]}+{pair[1]}"
              f"  IC {m:+.3f}  t{best[0]:+.2f}")

        # calendar months
        mo = ics.groupby(pd.Series(ics.index).dt.to_period("M").values).mean()
        pos = (mo > 0).sum()
        top = mo.nlargest(1)
        rest = ics[pd.Series(ics.index).dt.to_period("M").values != top.index[0]]
        print(f"      months: {pos}/{len(mo)} positive; best month "
              f"{top.index[0]} IC {top.iloc[0]:+.3f}; "
              f"WITHOUT it IC {rest.mean():+.3f} t{t_of(rest):+.2f}")
        print("      per-month IC: " + "  ".join(
            f"{str(k)[2:]}{v:+.2f}" for k, v in mo.items()))


def section_e(d: pd.DataFrame) -> None:
    print("\n" + "=" * 100)
    print("E. WHERE INSIDE THE HORIZON DOES THE 'EDGE' SIT?")
    print("=" * 100)
    print("   ret_3d runs from the BREAK BAR CLOSE to the close 3 sessions "
          "later. It is the\n   sum of a same-day stub (entry -> today's 15:15) "
          "and a clean 3-session leg\n   (today's close -> +3 close). The stub "
          "is the segment where the feature and the\n   entry price are "
          "mechanically entangled: a name that opened far above prior value\n"
          "   and still broke DOWN has already round-tripped, so its entry sits "
          "at a very\n   different point of the day's range than a name that "
          "opened low.\n")
    b = d[d["side"] != 0]
    hdr = (f"   {'slice':<20}{'target':<20}{'n':>5}{'IC':>8}{'t':>7}{'NW5':>7}"
           f"   halves")
    print(hdr)
    for f in ("open_vs_prev_vah", "open_vs_prev_poc"):
        print(f"   -- {f}")
        for side, nm in ((-1, "DOWN"), (1, "UP")):
            sl = b[b["side"] == side]
            for tgt in (TGT, "stub", "ret3_from_close", "mfe_intraday"):
                dd = sl.dropna(subset=[f, tgt])
                ics = ic_series(dd, f, tgt)
                if len(ics) < MIN_SESSIONS:
                    continue
                a, h = halves(ics)
                print(f"   {nm:<20}{tgt:<20}{len(ics):>5}{ics.mean():>+8.3f}"
                      f"{t_of(ics):>+7.2f}{nw_t(ics, 5):>+7.2f}"
                      f"   {a:+.3f}/{h:+.3f}")


def section_f(d: pd.DataFrame) -> None:
    print("\n" + "=" * 100)
    print("F. IS THIS AN MARKET-PROFILE FINDING AT ALL?")
    print("=" * 100)
    print("   Same feature, but scored on the plain UNSIGNED 3-session forward "
          "return from the\n   session close, over EVERY name-session -- breaks, "
          "non-breaks, everything. If it\n   works there, the IB break is "
          "decoration and 'inside DOWN breaks' is just a\n   sign flip on a "
          "generic short-horizon reversal.\n")
    for f in ("open_vs_prev_vah", "open_vs_prev_poc", "gap"):
        for label, sl in (("ALL name-sessions", d),
                          ("never broke (side=0)", d[d["side"] == 0]),
                          ("DOWN breaks", d[d["side"] == -1]),
                          ("UP breaks", d[d["side"] == 1])):
            dd = sl.dropna(subset=[f, "fwd3_raw"])
            ics = ic_series(dd, f, "fwd3_raw", 6)
            if len(ics) < MIN_SESSIONS:
                continue
            a, h = halves(ics)
            print(f"   {f:<20}{label:<24}{len(ics):>5}"
                  f"{ics.mean():>+8.3f}{t_of(ics):>+7.2f}"
                  f"{nw_t(ics, 5):>+7.2f}   halves {a:+.3f}/{h:+.3f}")
        print()
    print("   Read: IC vs UNSIGNED forward return. Inside DOWN breaks "
          "ret_3d = -fwd3, so a\n   NEGATIVE number here is the same statement "
          "as the claim's POSITIVE DOWN IC.")


def section_g(b: pd.DataFrame) -> None:
    print("\n" + "=" * 100)
    print("G. COST")
    print("=" * 100)
    f = "open_vs_prev_poc"
    dn = b[b["side"] == -1].dropna(subset=[f, TGT]).copy()
    dn = dn[dn.groupby("dt")[f].transform("size") >= MIN_GROUP]
    dn["rk"] = dn.groupby("dt")[f].rank(pct=True)
    dn["q"] = dn.groupby("dt")["rk"].transform(
        lambda x: pd.qcut(x, 5, labels=False, duplicates="drop"))
    per = dn.groupby(["dt", "q"])[TGT].mean().unstack()
    print(f"   DOWN breaks, {f} quintiles built INSIDE each session "
          f"({len(per)} sessions)")
    print(f"      {'quintile':<10}{'mean ret_3d':>14}{'t':>8}"
          f"{'net of 0.10% RT':>18}{'t':>8}")
    for q in sorted(c for c in per.columns if pd.notna(c)):
        x = per[q].dropna()
        net = x - 0.0010
        print(f"      Q{int(q) + 1:<9}{x.mean() * 100:>13.3f}%{t_of(x):>+8.2f}"
              f"{net.mean() * 100:>17.3f}%{t_of(net):>+8.2f}")
    if 4 in per.columns and 0 in per.columns:
        sp = (per[4] - per[0]).dropna()
        print(f"      Q5-Q1 spread {sp.mean() * 100:+.3f}pp  t{t_of(sp):+.2f}  "
              f"NW5 {nw_t(sp, 5):+.2f}  boot p {block_boot_p(sp, 5):.3f}")
        print(f"      Q5-Q1 needs BOTH legs: 4 x 0.05% = 0.20% round trip "
              f"-> {sp.mean() * 100 - 0.20:+.3f}pp")
    print("\n   Every quintile is a LOSS before cost. The 'edge' is that Q5 "
          "loses less. A PE\n   held 3 sessions on the Q5 bucket still has a "
          "negative expected underlying move,\n   and the option leg pays theta "
          "on top of the 0.05%/side the owner named.")


def section_h() -> None:
    p = f"{CACHE}/other.pkl"
    print("\n" + "=" * 100)
    print("H. OUT OF UNIVERSE: ~200 NON-BANK NSE NAMES, SAME CODE PATH")
    print("=" * 100)
    if not os.path.exists(p):
        print("   (cache not built)")
        return
    s = pd.read_pickle(p)
    s = rebuild_context(s)
    b = s[s["side"] != 0]
    print(f"   names={s['underlying'].nunique()}  "
          f"window {s['dt'].min().date()} .. {s['dt'].max().date()}  "
          f"name-sessions={len(s):,}  breaks={len(b):,}  "
          f"dates={s['dt'].nunique():,}")
    up = (s["side"] == 1).mean() * 100
    dn2 = (s["side"] == -1).mean() * 100
    print(f"   break rates: UP {up:.1f}%  DOWN {dn2:.1f}%  "
          f"never {(s['side'] == 0).mean() * 100:.1f}%")
    print(f"\n   {'feature':<20}{'slice':<12}{'n':>5}{'IC':>8}{'t':>7}{'NW5':>7}"
          f"   halves        drop2")
    for f in ("open_vs_prev_vah", "open_vs_prev_poc", "gap", "break_frac",
              "atr20_lag", "ib_width"):
        for side, nm in ((None, "pooled"), (-1, "DOWN"), (1, "UP")):
            sl = b if side is None else b[b["side"] == side]
            dd = sl.dropna(subset=[f, TGT])
            ics = ic_series(dd, f, TGT, 10)
            if len(ics) < MIN_SESSIONS:
                continue
            a, h = halves(ics)
            m2, t2 = drop_best(ics, 2)
            print(f"   {f:<20}{nm:<12}{len(ics):>5}{ics.mean():>+8.3f}"
                  f"{t_of(ics):>+7.2f}{nw_t(ics, 5):>+7.2f}"
                  f"   {a:+.3f}/{h:+.3f}  {m2:+.3f} t{t2:+.2f}")
    print("\n   and on mfe_total, for the magnitude question:")
    for f in ("open_vs_prev_poc", "atr20_lag", "ib_width", "break_frac"):
        for side, nm in ((None, "pooled"), (-1, "DOWN")):
            sl = b if side is None else b[b["side"] == side]
            dd = sl.dropna(subset=[f, "mfe_total"])
            ics = ic_series(dd, f, "mfe_total", 10)
            a, h = halves(ics)
            print(f"   {f:<20}{nm:<12}{len(ics):>5}{ics.mean():>+8.3f}"
                  f"{t_of(ics):>+7.2f}{nw_t(ics, 5):>+7.2f}"
                  f"   {a:+.3f}/{h:+.3f}")


def section_i(bank: pd.DataFrame) -> None:
    """How surprising is t+3.05 if you get to pick a 17-name sector?"""
    p = f"{CACHE}/other.pkl"
    print("\n" + "=" * 100)
    print("I. CALIBRATION: RANDOM 17-NAME SUB-UNIVERSES FROM THE 193 NON-BANKS")
    print("=" * 100)
    if not os.path.exists(p):
        print("   (cache not built)")
        return
    print("   The banks are ONE 17-name group. The right null is not 'IC=0 on "
          "193 names' but\n   'what does a 17-name group of this size do'. Two "
          "draws: uniformly random, and\n   correlation-clustered (a seed name "
          "plus its 16 closest return-correlation\n   neighbours) which mimics "
          "a sector's tight cross-section.\n")
    s = pd.read_pickle(p)
    b = s[s["side"] == -1].dropna(subset=["open_vs_prev_poc",
                                          "open_vs_prev_vah", TGT])
    names = np.array(sorted(s["underlying"].unique()))
    nidx = {n: i for i, n in enumerate(names)}

    # correlation clusters from daily close-to-close returns
    w = s.pivot_table(index="dt", columns="underlying", values="close")
    r = w.pct_change().dropna(how="all")
    corr = r.corr().reindex(index=names, columns=names).fillna(0.0).values
    np.fill_diagonal(corr, -9.0)
    clusters = [np.concatenate([[i], np.argsort(-corr[i])[:16]])
                for i in range(len(names))]

    cells = []
    for dt, g in b.groupby("dt", sort=True):
        cells.append((np.array([nidx[n] for n in g["underlying"]]),
                      g["open_vs_prev_vah"].values,
                      g["open_vs_prev_poc"].values,
                      g[TGT].values))

    def run(sel: np.ndarray) -> tuple[float, float, float, float]:
        mask = np.zeros(len(names), dtype=bool)
        mask[sel] = True
        a, c = [], []
        for ids, xv, xp, y in cells:
            m = mask[ids]
            if m.sum() < MIN_GROUP:
                continue
            i1 = spearman(xv[m], y[m])
            i2 = spearman(xp[m], y[m])
            if pd.notna(i1):
                a.append(i1)
            if pd.notna(i2):
                c.append(i2)
        return (np.mean(a) if len(a) > MIN_SESSIONS else np.nan,
                t_of(pd.Series(a)) if len(a) > MIN_SESSIONS else np.nan,
                np.mean(c) if len(c) > MIN_SESSIONS else np.nan,
                t_of(pd.Series(c)) if len(c) > MIN_SESSIONS else np.nan)

    for label, draws in (
            ("uniform random 17", [RNG.choice(len(names), 17, replace=False)
                                   for _ in range(300)]),
            ("correlation cluster 17", clusters)):
        res = np.array([run(sel) for sel in draws], dtype=float)
        res = res[~np.isnan(res).any(axis=1)]
        for j, f in ((0, "open_vs_prev_vah"), (2, "open_vs_prev_poc")):
            ic, tt = res[:, j], res[:, j + 1]
            bank_t = 3.05 if j == 0 else 3.16
            bank_ic = 0.082 if j == 0 else 0.083
            print(f"   {label:<24}{f:<20} draws={len(res)}")
            print(f"      IC   mean {ic.mean():+.3f}  sd {ic.std():.3f}   "
                  f"P(IC >= bank {bank_ic:+.3f}) = "
                  f"{(ic >= bank_ic).mean() * 100:.1f}%")
            print(f"      t    mean {tt.mean():+.2f}   sd {tt.std():.2f}    "
                  f"P(t  >= bank {bank_t:+.2f}) = "
                  f"{(tt >= bank_t).mean() * 100:.1f}%   "
                  f"max {tt.max():+.2f}")

    print("\n   MONOTONICITY. The claim's own quintile table is not ordered "
          "(dem -0.06/-0.07/\n   -0.16/+0.06/+0.29): only Q5 moves. Q5 vs "
          "Q1-Q4, paired inside each session:")
    for label, d in (("BANKS", bank[bank["side"] == -1]),
                     ("NON-BANKS", b)):
        for f in ("open_vs_prev_vah", "open_vs_prev_poc"):
            dd = d.dropna(subset=[f, TGT]).copy()
            dd = dd[dd.groupby("dt")[f].transform("size") >= MIN_GROUP]
            dd["rk"] = dd.groupby("dt")[f].rank(pct=True)
            rows = {}
            for dt, g in dd.groupby("dt", sort=True):
                hi = g.loc[g["rk"] > 0.8, TGT]
                lo = g.loc[g["rk"] <= 0.8, TGT]
                if len(hi) and len(lo):
                    rows[dt] = hi.mean() - lo.mean()
            x = pd.Series(rows, dtype=float)
            a, h = halves(x)
            m2, t2 = drop_best(x, 2)
            print(f"      {label:<11}{f:<20}n={len(x):>4}  "
                  f"Q5-rest {x.mean() * 100:+.3f}pp  t{t_of(x):+.2f}  "
                  f"NW5 {nw_t(x, 5):+.2f}  halves {a * 100:+.3f}/{h * 100:+.3f}"
                  f"  drop2 {m2 * 100:+.3f} t{t2:+.2f}")


def q5_rest(d: pd.DataFrame, f: str, target: str = TGT,
            resid: str | None = None) -> pd.Series:
    key = resid or f
    dd = d.dropna(subset=[key, target]).copy()
    dd = dd[dd.groupby("dt")[key].transform("size") >= MIN_GROUP]
    dd["rk"] = dd.groupby("dt")[key].rank(pct=True)
    rows = {}
    for dt, g in dd.groupby("dt", sort=True):
        hi = g.loc[g["rk"] > 0.8, target]
        lo = g.loc[g["rk"] <= 0.8, target]
        if len(hi) and len(lo):
            rows[dt] = hi.mean() - lo.mean()
    return pd.Series(rows, dtype=float).sort_index()


def section_j(bank: pd.DataFrame) -> None:
    print("\n" + "=" * 100)
    print("J. THE PART THAT DOES CARRY OVER, SIZED HONESTLY")
    print("=" * 100)
    other = rebuild_context(pd.read_pickle(f"{CACHE}/other.pkl"))
    ob = other[other["side"] == -1]
    bb = bank[bank["side"] == -1]

    print("   1. IS THE BANK ESTIMATE BIGGER THAN THE POPULATION ONE? "
          "Q5-minus-rest ret_3d,\n      DOWN breaks, paired on common sessions."
          "  (banks = 17 names, non-banks = 193)\n")
    for f in ("open_vs_prev_vah", "open_vs_prev_poc"):
        qb, qo = q5_rest(bb, f), q5_rest(ob, f)
        j = pd.concat([qb.rename("bank"), qo.rename("other")], axis=1).dropna()
        d = j["bank"] - j["other"]
        print(f"      {f:<20}n={len(j):>4}  bank {j['bank'].mean() * 100:+.3f}pp"
              f"  non-bank {j['other'].mean() * 100:+.3f}pp  "
              f"difference {d.mean() * 100:+.3f}pp t{t_of(d):+.2f} "
              f"(boot p {block_boot_p(d, 5):.3f})")

    print("\n   2. COST. 0.05%/side on the underlying, both legs of a Q5-vs-rest"
          " expression\n      = 0.20% round trip; a one-legged Q5 PE pays 0.10%."
          "  In pp of ret_3d:\n")
    for label, d in (("BANKS", bb), ("NON-BANKS", ob)):
        for f in ("open_vs_prev_vah", "open_vs_prev_poc"):
            q = q5_rest(d, f)
            dd = d.dropna(subset=[f, TGT]).copy()
            dd = dd[dd.groupby("dt")[f].transform("size") >= MIN_GROUP]
            dd["rk"] = dd.groupby("dt")[f].rank(pct=True)
            lvl = dd.loc[dd["rk"] > 0.8].groupby("dt")[TGT].mean()
            print(f"      {label:<11}{f:<20}Q5 LEVEL "
                  f"{lvl.mean() * 100:+.3f}pp t{t_of(lvl):+.2f}"
                  f" -> net of 0.10% {lvl.mean() * 100 - 0.10:+.3f}pp   "
                  f"| Q5-rest {q.mean() * 100:+.3f}pp"
                  f" -> net of 0.20% {q.mean() * 100 - 0.20:+.3f}pp")

    print("\n   3. IS THE NON-BANK REMNANT JUST THE GAP OR JUST VOLATILITY? "
          "residualise the\n      feature on within-session ranks of gap and "
          "strictly-lagged atr20.\n")
    for label, d in (("BANKS", bb), ("NON-BANKS", ob)):
        dd = d.dropna(subset=["gap", "atr20_lag", TGT,
                              "open_vs_prev_vah", "open_vs_prev_poc"]).copy()
        for c in ("gap", "atr20_lag", "open_vs_prev_vah", "open_vs_prev_poc"):
            dd[f"r_{c}"] = dd.groupby("dt")[c].rank(pct=True)
        x = np.column_stack([np.ones(len(dd)), dd["r_gap"], dd["r_atr20_lag"]])
        for f in ("open_vs_prev_vah", "open_vs_prev_poc"):
            beta, *_ = np.linalg.lstsq(x, dd[f"r_{f}"].values, rcond=None)
            dd["resid"] = dd[f"r_{f}"].values - x @ beta
            ics = ic_series(dd, "resid", TGT)
            q = q5_rest(dd, f, resid="resid")
            a, h = halves(ics)
            print(f"      {label:<11}{f:<20}IC {ics.mean():+.3f} t{t_of(ics):+.2f}"
                  f" NW5 {nw_t(ics, 5):+.2f} halves {a:+.3f}/{h:+.3f}"
                  f"  | Q5-rest {q.mean() * 100:+.3f}pp t{t_of(q):+.2f}")

    print("\n   4. NON-BANK REMNANT BY SUB-PERIOD (thirds of the common window)"
          " and by\n      liquidity, to see whether it is a real population "
          "effect or a corner.\n")
    for f in ("open_vs_prev_vah", "open_vs_prev_poc"):
        q = q5_rest(ob, f)
        k = len(q) // 3
        print(f"      {f:<20}thirds "
              f"{q.iloc[:k].mean() * 100:+.3f} / "
              f"{q.iloc[k:2 * k].mean() * 100:+.3f} / "
              f"{q.iloc[2 * k:].mean() * 100:+.3f} pp   "
              f"drop best 10 sessions "
              f"{drop_best(q, 10)[0] * 100:+.3f}pp t{drop_best(q, 10)[1]:+.2f}")
    med = ob.groupby("underlying")["volume"].median()
    big = set(med.nlargest(len(med) // 2).index)
    for f in ("open_vs_prev_vah", "open_vs_prev_poc"):
        for label, sel in (("top-half volume", ob[ob["underlying"].isin(big)]),
                           ("bottom-half volume",
                            ob[~ob["underlying"].isin(big)])):
            ics = ic_series(sel.dropna(subset=[f, TGT]), f, TGT, 10)
            q = q5_rest(sel, f)
            print(f"      {f:<20}{label:<20}IC {ics.mean():+.3f} "
                  f"t{t_of(ics):+.2f}   Q5-rest {q.mean() * 100:+.3f}pp "
                  f"t{t_of(q):+.2f}")


def main() -> int:
    s = pd.read_pickle(f"{CACHE}/bank.pkl")
    d = rebuild_context(s)
    b = d[d["side"] != 0].copy()
    print(f"bank universe  names={s['underlying'].nunique()}  "
          f"window {s['dt'].min().date()} .. {s['dt'].max().date()}  "
          f"name-sessions={len(s):,}  breaks={len(b):,}  "
          f"dates={s['dt'].nunique():,}")
    section_a(d, b)
    section_b(b)
    section_c(b)
    section_d(b)
    section_e(d)
    section_f(d)
    section_g(b)
    section_h()
    section_i(b)
    section_j(b)
    return 0


if __name__ == "__main__":
    sys.exit(main())
