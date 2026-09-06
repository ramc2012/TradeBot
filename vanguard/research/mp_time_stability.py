"""Is the IB-break structure stable IN TIME, or a 2025-26 artefact?

WHY. Every number in the bank study rests on 16 bank stocks whose 30m history
begins 2025-03 -- roughly 18 months, one regime. NIFTY, BANKNIFTY, FINNIFTY and
SENSEX carry 30m bars from 2021-06-21 (1,290 sessions each), which spans the
2021 top, the 2022 bear leg, the 2023-24 bull run, and the 2024-25 drawdown.
That is the only out-of-sample-in-time this dataset can offer, so it is the one
worth spending.

WHAT IS TESTED
  1. Per calendar year: break rate up/down/never, median IB width, median MFE
     intraday, MFE 3d, MAE 3d, ret_3d, P(mfe_total >= 2%).
  2. The DOWN-BREAK BIAS (banks: 43.0% down vs 35.8% up). Is it in the index
     history or is it the recent window? Per-session statistic
     d = (n_down - n_up) / n_names, t across SESSIONS, per year and overall.
  3. The UP-BREAK's ret_3d ADVANTAGE (banks: +0.10% up vs -0.35% down). Tested
     WITHIN SESSION -- on sessions where both an up-break and a down-break
     exist, diff = mean(ret_3d | up) - mean(ret_3d | down), t across sessions.
     A raw up-vs-down gap across four indices that move as one would mostly
     measure which days each side happened to fall on.
  4. Bull stretch vs drawdown stretch, chosen from NIFTY's own equity curve
     (deepest peak-to-trough, largest trough-to-peak) rather than by eye.

METHOD RULES OBSERVED
  - every t-stat is across sessions, one observation per session, never across
    the four correlated index-days
  - split-half (first half vs second half of the window by session date) and
    drop-2-best on both headline claims
  - everything in PERCENT, never in IB multiples
  - indices carry zero volume in this table, so no volume field is touched:
    the TPO profile (poc/val/vah) is used, never vpoc/vval/vvah

    docker exec nomadcurie_vanguard_cycle \
        python /vanguard/research/mp_time_stability.py
"""
from __future__ import annotations

import argparse
import os
import sys
import warnings
from datetime import date, timedelta

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.mp_profile import dsn, load  # noqa: E402

warnings.filterwarnings("ignore")

INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]
PCT = 100.0


# ---------------------------------------------------------------- statistics

def t_of(x: pd.Series | np.ndarray) -> float:
    """t of the mean, computed on whatever one-observation-per-session series
    is handed in. Never call this on name-days."""
    x = pd.Series(x).dropna()
    if len(x) < 3 or x.std(ddof=1) == 0:
        return np.nan
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x))))


def summarise(x: pd.Series) -> tuple[float, float, int]:
    x = pd.Series(x).dropna()
    return (float(x.mean()) if len(x) else np.nan, t_of(x), len(x))


def split_half(x: pd.Series) -> tuple[tuple, tuple]:
    """First half vs second half of a session-indexed series, by date order."""
    x = pd.Series(x).dropna().sort_index()
    mid = len(x) // 2
    return summarise(x.iloc[:mid]), summarise(x.iloc[mid:])


def drop_two_best(x: pd.Series) -> tuple[float, float, int]:
    """Remove the two sessions that most flatter the claim (the two largest
    values when the claim is a positive mean, the two smallest when negative),
    and recompute. A number that only lives while its best two days do is dead."""
    x = pd.Series(x).dropna()
    if len(x) < 5:
        return np.nan, np.nan, len(x)
    order = x.sort_values(ascending=(x.mean() < 0))
    return summarise(order.iloc[:-2])


def nonoverlap_t(x: pd.Series, stride: int = 4) -> float:
    """ret_3d windows of consecutive sessions overlap, which inflates any t.
    Recompute on `stride` interleaved subsamples, each internally
    non-overlapping, and return the median t. A claim that only survives on
    the overlapping series was borrowing significance from itself."""
    x = pd.Series(x).dropna().sort_index()
    ts = [t_of(x.iloc[p::stride]) for p in range(stride)]
    ts = [v for v in ts if np.isfinite(v)]
    return float(np.median(ts)) if ts else np.nan


def stat_block(label: str, x: pd.Series, unit: str = "pp") -> None:
    m, t, n = summarise(x)
    (m1, t1, n1), (m2, t2, n2) = split_half(x)
    dm, dt, dn = drop_two_best(x)
    print(f"  {label:<34s} mean {m:+7.3f}{unit}  t {t:+6.2f}  n {n:5d}")
    print(f"    split-half   H1 {m1:+7.3f} (t {t1:+5.2f}, n {n1})"
          f"   H2 {m2:+7.3f} (t {t2:+5.2f}, n {n2})")
    print(f"    drop-2-best  {dm:+7.3f} (t {dt:+5.2f}, n {dn})"
          f"   non-overlap t {nonoverlap_t(x):+5.2f}")


# ------------------------------------------------- single-name two-sample test
# Four indices are, for this purpose, one asset: on a day the market rises they
# all break up together, so a "cross-section" of them is not one. The honest
# high-power test is therefore per NAME, across sessions -- each observation is
# a distinct session of a single asset, so nothing is double-counted.

def two_sample(up: pd.Series, dn: pd.Series) -> tuple[float, float, int, int]:
    up, dn = up.dropna(), dn.dropna()
    if len(up) < 3 or len(dn) < 3:
        return np.nan, np.nan, len(up), len(dn)
    diff = up.mean() - dn.mean()
    se = np.sqrt(up.var(ddof=1) / len(up) + dn.var(ddof=1) / len(dn))
    return float(diff), (float(diff / se) if se > 0 else np.nan), len(up), len(dn)


def _drop2(up: pd.Series, dn: pd.Series) -> tuple[float, float, int, int]:
    """Remove the two single sessions contributing most to the difference."""
    up, dn = up.dropna(), dn.dropna()
    if len(up) < 5 or len(dn) < 5:
        return np.nan, np.nan, len(up), len(dn)
    d0 = up.mean() - dn.mean()
    contrib = pd.concat([up / len(up), -dn / len(dn)])
    tag = pd.Series(["u"] * len(up) + ["d"] * len(dn))
    idx = np.argsort(contrib.values)
    kill = idx[-2:] if d0 > 0 else idx[:2]
    ku = [i for i in kill if tag.iloc[i] == "u"]
    kd = [i for i in kill if tag.iloc[i] == "d"]
    up2 = up.drop(up.index[ku]) if ku else up
    dn2 = dn.drop(dn.index[[i - len(up) for i in kd]]) if kd else dn
    return two_sample(up2, dn2)


def contrast(label: str, a: pd.Series, b: pd.Series,
             na: str, nb: str, unit: str = "") -> None:
    """Did the statistic actually CHANGE between two stretches of time? A
    number that is significant in one era and not in the other has not been
    shown to differ -- this tests the difference itself."""
    d, t, n1, n2 = two_sample(pd.Series(a).dropna(), pd.Series(b).dropna())
    print(f"  {label:<34s} {na} {pd.Series(a).mean():+.3f}{unit}  vs"
          f"  {nb} {pd.Series(b).mean():+.3f}{unit}"
          f"   difference {d:+.3f}, Welch t {t:+.2f} (n {n1}/{n2})")


def two_sample_block(label: str, b: pd.DataFrame, col: str) -> None:
    """Up-break vs down-break on `col`, for ONE name, with the full audit."""
    b = b[(b["side"] != 0) & b[col].notna()].sort_values("dt")
    up = b[b["side"] == 1].set_index("dt")[col] * PCT
    dn = b[b["side"] == -1].set_index("dt")[col] * PCT
    d, t, nu, nd = two_sample(up, dn)
    print(f"  {label:<34s} up-down {d:+7.3f}pp  Welch t {t:+6.2f}"
          f"  n_up {nu:4d} n_dn {nd:4d}")
    cut = b["dt"].iloc[len(b) // 2]
    for tag, sel in (("H1", b["dt"] < cut), ("H2", b["dt"] >= cut)):
        h = b[sel]
        d1, t1, u1, l1 = two_sample(h[h["side"] == 1].set_index("dt")[col] * PCT,
                                    h[h["side"] == -1].set_index("dt")[col] * PCT)
        print(f"    split-half {tag}  {d1:+7.3f} (t {t1:+5.2f},"
              f" n_up {u1}, n_dn {l1})")
    d2, t2, u2, l2 = _drop2(up, dn)
    print(f"    drop-2-best   {d2:+7.3f} (t {t2:+5.2f}, n_up {u2}, n_dn {l2})")


# ------------------------------------------------------------ session tables

def per_session_break_bias(s: pd.DataFrame) -> pd.Series:
    """d = (n_down - n_up) / n_names, one number per calendar session."""
    g = s.groupby("dt")["side"]
    return ((g.apply(lambda v: (v == -1).sum()) - g.apply(lambda v: (v == 1).sum()))
            / g.size())


def per_session_up_minus_down(s: pd.DataFrame, col: str) -> pd.Series:
    """Within-session paired difference on `col`: up-breaks minus down-breaks,
    only on sessions that contain at least one of each. This is the demeaning
    the method rules demand -- both legs sit in the same day."""
    b = s[(s["side"] != 0) & s[col].notna()]
    up = b[b["side"] == 1].groupby("dt")[col].mean()
    dn = b[b["side"] == -1].groupby("dt")[col].mean()
    j = pd.concat({"up": up, "dn": dn}, axis=1).dropna()
    return (j["up"] - j["dn"]) * PCT


def cohort_row(s: pd.DataFrame) -> dict:
    """The headline table for one slice, all of it in percent."""
    n = len(s)
    b = s[s["side"] != 0]
    up, dn = s[s["side"] == 1], s[s["side"] == -1]
    med = lambda f, c: float(f[c].median() * PCT) if len(f) and f[c].notna().any() else np.nan
    hit = lambda f: (float((f["mfe_total"] >= 0.02).mean() * PCT)
                     if len(f) and f["mfe_total"].notna().any() else np.nan)
    # METHOD RULE 5: volatility is the benchmark. An index is calmer than a
    # single stock, so raw percents cannot be compared to the bank baseline --
    # the /atr columns can.
    rat = lambda f, c: (float((f[c] / f["atr20"]).median())
                        if len(f) and f[c].notna().any() else np.nan)
    return {
        "sessions": n,
        "up%": len(up) / n * PCT if n else np.nan,
        "down%": len(dn) / n * PCT if n else np.nan,
        "never%": (n - len(b)) / n * PCT if n else np.nan,
        "ib_w%": med(s, "ib_width"),
        "mfe_id%": med(b, "mfe_intraday"),
        "mfe3d%": med(b, "mfe_3d"),
        "mae3d%": med(b, "mae_3d"),
        "ret3d%": med(b, "ret_3d"),
        "P(mfe>=2%)": hit(b),
        "ret3d_up%": med(up, "ret_3d"),
        "ret3d_dn%": med(dn, "ret_3d"),
        "atr20%": med(s, "atr20"),
        "ibw/atr": rat(s, "ib_width"),
        "mfe3d/atr": rat(b, "mfe_3d"),
        "mae3d/atr": rat(b, "mae_3d"),
    }


def table(s: pd.DataFrame, key: str, order=None) -> pd.DataFrame:
    rows = {}
    keys = order if order is not None else sorted(s[key].unique())
    for k in keys:
        sub = s[s[key] == k]
        if len(sub):
            rows[k] = cohort_row(sub)
    return pd.DataFrame(rows).T


def show(df: pd.DataFrame, title: str) -> None:
    print(f"\n{title}")
    print(df.to_string(float_format=lambda v: f"{v:7.2f}"))


# ------------------------------------------------------------------ regimes

ADVANCE_LEN = 250          # ~one year of sessions
DD_PULLBACK = -0.02
DD_DEEP = -0.07


def nifty_close(s: pd.DataFrame) -> pd.Series:
    return (s[s["underlying"] == "NIFTY"].sort_values("dt")
            .set_index("dt")["close"].dropna())


def drawdown_legs(n: pd.Series, top: int = 3) -> list[tuple]:
    """Every peak -> trough decline in NIFTY, deepest first. Data-chosen, so
    no episode is picked because it flatters or spoils a result."""
    dd = n / n.cummax() - 1.0
    legs, run = [], []
    for d, v in dd.items():
        if v < 0:
            run.append(d)
        elif run:
            legs.append(run)
            run = []
    if run:
        legs.append(run)
    out = []
    for run in legs:
        seg = dd.loc[run[0]:run[-1]]
        trough = seg.idxmin()
        peak = n.loc[:run[0]].idxmax()
        out.append((peak, trough, float(seg.min())))
    return sorted(out, key=lambda r: r[2])[:top]


def best_advance(n: pd.Series, length: int = ADVANCE_LEN) -> tuple:
    """The strongest `length`-session stretch: a bull leg of fixed size, so it
    is comparable to a drawdown leg instead of swallowing the whole window."""
    if len(n) <= length:
        return n.index[0], n.index[-1], float(n.iloc[-1] / n.iloc[0] - 1.0)
    g = n.values[length:] / n.values[:-length] - 1.0
    i = int(np.argmax(g))
    return n.index[i], n.index[i + length], float(g[i])


# --------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback-days", type=int, default=1900)
    ap.add_argument("--no-banks", action="store_true")
    a = ap.parse_args()
    start = date.today() - timedelta(days=a.lookback_days)

    bank_frame = None
    with psycopg2.connect(dsn()) as con:
        s = load(con, INDICES, start)
        if not a.no_banks:
            from research.banknifty_rotation import BANKS
            bk = load(con, list(("BANKNIFTY",) + BANKS), date(2024, 9, 1))
            bk = bk[pd.to_datetime(bk["dt"]) >= pd.Timestamp("2024-09-01")]
            print("=" * 92)
            print("0. HARNESS CHECK -- the bank baseline this must reproduce")
            print("=" * 92)
            print("   expected: up 35.8%, down 43.0%, never 21.2%, 6,035 sessions,")
            print("   median IB width 1.25%, MFE 3d 1.54%, MAE 3d 1.62%,"
                  " ret_3d -0.17%, P(>=2%) 41%")
            print("   got:")
            print(pd.DataFrame({"banks 2024-09..": cohort_row(bk)}).T
                  .to_string(float_format=lambda v: f"{v:7.2f}"))
            print("   the /atr columns are how the index numbers below become")
            print("   comparable to these: an index is a calmer asset, so its")
            print("   raw percents are smaller for a reason that is not structure.")
            bank_frame = bk
    s = s.sort_values(["dt", "underlying"]).reset_index(drop=True)
    s["year"] = pd.to_datetime(s["dt"]).dt.year
    # UNSIGNED forward return from the break entry: ret_3d is signed by the
    # break side, so multiplying it back by side recovers the plain
    # close-to-close move. This is the series that separates "the break
    # forecasts direction" from "the market drifts up and the up-break is
    # simply the leg that is long".
    s["fwd_ret"] = s["ret_3d"] * s["side"]

    print("=" * 92)
    print("IB-BREAK STRUCTURE OUT OF SAMPLE IN TIME -- 4 INDICES, 30m bars")
    print("=" * 92)
    print(f"names      {', '.join(INDICES)}")
    print(f"window     {s['dt'].min().date()} .. {s['dt'].max().date()}")
    print(f"rows       {len(s)} name-sessions over {s['dt'].nunique()} calendar sessions")
    print("volume     NOT USED -- indices carry zero volume in this table")
    print(s.groupby(["underlying", "year"]).size().unstack(fill_value=0).to_string())

    # ---------------------------------------------------------------- (1)
    print("\n" + "=" * 92)
    print("1. PER CALENDAR YEAR  (percent throughout; medians over breaks only)")
    print("=" * 92)
    show(table(s, "year"), "all four indices pooled")
    print("\n  2021 starts 06-21 and 2026 ends 08-28; both are part years.")
    print("  The last 3 sessions of the window have no ret_3d/mfe_3d yet.")

    for nm in INDICES:
        show(table(s[s["underlying"] == nm], "year"), f"{nm} alone")

    # ---------------------------------------------------------------- (2)
    print("\n" + "=" * 92)
    print("2. THE DOWN-BREAK BIAS  (banks 2024-09..2026-08: 43.0% down / 35.8% up)")
    print("=" * 92)
    print("   session statistic d = (n_down - n_up)/n_names; d>0 means the day")
    print("   broke down more often than up. t is across SESSIONS.\n")
    bias = per_session_break_bias(s)
    by_year = pd.DataFrame({
        "sessions": bias.groupby(pd.to_datetime(bias.index).year).size(),
        "mean d": bias.groupby(pd.to_datetime(bias.index).year).mean(),
        "t": bias.groupby(pd.to_datetime(bias.index).year).apply(t_of),
    })
    by_year["up%"] = table(s, "year")["up%"]
    by_year["down%"] = table(s, "year")["down%"]
    by_year["never%"] = table(s, "year")["never%"]
    print(by_year.to_string(float_format=lambda v: f"{v:7.3f}"))
    print()
    stat_block("down-bias d, whole window", bias, unit="  ")

    print("\n   HALF-YEAR, to date when the tilt turns on:")
    s["half"] = (pd.to_datetime(s["dt"]).dt.year.astype(str) + "H"
                 + ((pd.to_datetime(s["dt"]).dt.month > 6).astype(int) + 1).astype(str))
    hb = bias.copy()
    hkey = (pd.to_datetime(hb.index).year.astype(str) + "H"
            + ((pd.to_datetime(hb.index).month > 6).astype(int) + 1).astype(str))
    half = pd.DataFrame({
        "sessions": hb.groupby(hkey).size(),
        "up%": table(s, "half")["up%"],
        "down%": table(s, "half")["down%"],
        "never%": table(s, "half")["never%"],
        "mean d": hb.groupby(hkey).mean(),
        "t": hb.groupby(hkey).apply(t_of),
    })
    print(half.to_string(float_format=lambda v: f"{v:7.3f}"))

    print("\n   PER NAME, break rates by era (the bank cohort's window is")
    print("   2024-09..2026-08; if the down tilt is a WINDOW effect rather than")
    print("   a stocks-vs-index effect, the indices must show it too, and only")
    print("   inside that window):")
    era = np.where(pd.to_datetime(s["dt"]) >= pd.Timestamp("2024-09-01"),
                   "bank-window", "pre-2024-09")
    rows = {}
    for nm in INDICES:
        for e in ("pre-2024-09", "bank-window"):
            sub = s[(s["underlying"] == nm) & (era == e)]
            r = cohort_row(sub)
            rows[(nm, e)] = {k: r[k] for k in ("sessions", "up%", "down%", "never%")}
    print(pd.DataFrame(rows).T.to_string(float_format=lambda v: f"{v:7.2f}"))

    # ---------------------------------------------------------------- (3)
    print("\n" + "=" * 92)
    print("3. THE UP-BREAK's ret_3d ADVANTAGE  (banks: up +0.10% vs down -0.35%)")
    print("=" * 92)
    print("   within-session paired difference, up-break mean ret_3d minus")
    print("   down-break mean ret_3d, on sessions holding both. t across sessions.\n")
    d = per_session_up_minus_down(s, "ret_3d")
    yr = pd.to_datetime(d.index).year
    per_yr = pd.DataFrame({
        "paired sessions": d.groupby(yr).size(),
        "mean up-down pp": d.groupby(yr).mean(),
        "t": d.groupby(yr).apply(t_of),
    })
    t3 = table(s, "year")
    per_yr["ret3d_up% med"] = t3["ret3d_up%"]
    per_yr["ret3d_dn% med"] = t3["ret3d_dn%"]
    print(per_yr.to_string(float_format=lambda v: f"{v:8.3f}"))
    print()
    stat_block("up-minus-down ret_3d", d)
    print()
    stat_block("up-minus-down mfe_3d",
               per_session_up_minus_down(s, "mfe_3d"))
    stat_block("up-minus-down mae_3d",
               per_session_up_minus_down(s, "mae_3d"))

    print("\n   MEAN ret_3d by side and year, unpaired (percent). Four indices")
    print("   are one asset for this purpose, so this pools correlated rows and")
    print("   its spread is descriptive only -- the per-name test below is the")
    print("   one that carries a defensible t.")
    br = s[s["side"] != 0].copy()
    br["sd"] = np.where(br["side"] == 1, "up", "down")
    print((br.pivot_table(index="year", columns="sd", values="ret_3d",
                          aggfunc="mean") * PCT
           ).to_string(float_format=lambda v: f"{v:7.3f}"))

    print("\n   PER-NAME two-sample test, one observation per session of a")
    print("   single asset (no double counting, no cross-sectional inflation):")
    for nm in INDICES:
        two_sample_block(f"{nm} ret_3d", s[s["underlying"] == nm], "ret_3d")
    print("\n   per name and year, up-minus-down mean ret_3d in pp"
          " (Welch t in brackets):")
    grid = {}
    for nm in INDICES:
        col = {}
        for y in sorted(s["year"].unique()):
            sub = s[(s["underlying"] == nm) & (s["year"] == y) & (s["side"] != 0)]
            d, t, nu, nd = two_sample(sub[sub["side"] == 1]["ret_3d"] * PCT,
                                      sub[sub["side"] == -1]["ret_3d"] * PCT)
            col[y] = f"{d:+6.2f} ({t:+5.2f})" if np.isfinite(d) else "     --"
        grid[nm] = col
    print(pd.DataFrame(grid).to_string())

    # ---------------------------------------------------------------- (3b)
    print("\n" + "-" * 92)
    print("3b. THE TEST THAT DECIDES IT: does the break DIRECTION forecast")
    print("    direction, or is the up-break simply the leg that happens to be")
    print("    long in a market that drifts up?")
    print("-" * 92)
    print("    fwd_ret = the UNSIGNED 3-session return from the break entry.")
    print("    If the break carries directional information, fwd_ret after an")
    print("    up-break must exceed fwd_ret after a down-break. If it does not,")
    print("    the whole signed 'up-break advantage' is drift, which is free.")
    print("    Note the arithmetic: signed gap = mean(fwd|up) + mean(fwd|dn),")
    print("    so a signed gap is guaranteed whenever the market drifts up.\n")
    for nm in INDICES:
        two_sample_block(f"{nm} fwd_ret", s[s["underlying"] == nm], "fwd_ret")
    print("\n   mean fwd_ret in pp, by name, side and year:")
    fr = s[s["side"] != 0].copy()
    fr["sd"] = np.where(fr["side"] == 1, "after up", "after down")
    print((fr.pivot_table(index="year", columns=["underlying", "sd"],
                          values="fwd_ret", aggfunc="mean") * PCT
           ).to_string(float_format=lambda v: f"{v:6.2f}"))
    if bank_frame is not None:
        print("\n   AND THE BANK COHORT, same test, its own window:")
        bank_frame["fwd_ret"] = bank_frame["ret_3d"] * bank_frame["side"]
        two_sample_block("banks fwd_ret", bank_frame, "fwd_ret")
        bb = bank_frame[bank_frame["side"] != 0]
        contrast("banks fwd_ret means",
                 bb[bb["side"] == 1]["fwd_ret"] * PCT,
                 bb[bb["side"] == -1]["fwd_ret"] * PCT,
                 "after up", "after down", "pp")
        print("\n   THE ARITHMETIC, spelled out for the bank cohort:")
        for nm, f in (("indices", s), ("banks", bank_frame)):
            b = f[f["side"] != 0]
            u = float(b[b["side"] == 1]["fwd_ret"].mean() * PCT)
            d = float(b[b["side"] == -1]["fwd_ret"].mean() * PCT)
            print(f"     {nm:8s} mean fwd_ret after up {u:+.3f}pp,"
                  f" after down {d:+.3f}pp"
                  f"  ->  drift {(u + d) / 2:+.3f}pp,"
                  f" information {u - d:+.3f}pp")
        print("     The SIGNED up-minus-down gap is 2x drift plus 0x")
        print("     information. Drift is free and is not a profile finding.")

    # ---------------------------------------------------------------- (4)
    print("\n" + "=" * 92)
    print("4. BULL STRETCH vs DRAWDOWN STRETCH  (dated off NIFTY session closes)")
    print("=" * 92)
    n = nifty_close(s)
    legs = drawdown_legs(n)
    b0, b1, gain = best_advance(n)
    dt = pd.to_datetime(s["dt"])

    print("   NIFTY's three deepest peak-to-trough declines in the window:")
    for i, (p0, p1, depth) in enumerate(legs, 1):
        print(f"     DD{i}  {p0.date()} .. {p1.date()}"
              f"  {depth*PCT:+6.1f}%  ({(p1-p0).days:4d} calendar days)")
    print(f"   strongest {ADVANCE_LEN}-session advance:"
          f"  {b0.date()} .. {b1.date()}  {gain*PCT:+.1f}%")
    print("   (a fixed-length bull leg, so it is comparable to a drawdown leg")
    print("    instead of covering 3.5 of the 5 years.)")

    s["regime"] = "other"
    for i, (p0, p1, _) in enumerate(legs, 1):
        s.loc[(dt >= p0) & (dt <= p1), "regime"] = f"DD{i}"
    s.loc[(dt >= b0) & (dt <= b1), "regime"] = "bull-leg"
    order = ["bull-leg"] + [f"DD{i}" for i in range(1, len(legs) + 1)] + ["other"]
    show(table(s, "regime", order=order), "named episodes, four indices pooled")

    print("\n   CONTINUOUS regime, by NIFTY's own drawdown from its running")
    print(f"   peak: at-highs > {DD_PULLBACK*PCT:.0f}%,"
          f" pullback {DD_PULLBACK*PCT:.0f}..{DD_DEEP*PCT:.0f}%,"
          f" drawdown < {DD_DEEP*PCT:.0f}%. Every session is classified,")
    print("   so the three buckets partition the window.")
    dd = (n / n.cummax() - 1.0).rename("dd")
    s["dd"] = pd.to_datetime(s["dt"]).map(dd)
    s["bucket"] = np.where(s["dd"] >= DD_PULLBACK, "at-highs",
                  np.where(s["dd"] >= DD_DEEP, "pullback", "drawdown"))
    show(table(s, "bucket", order=["at-highs", "pullback", "drawdown"]),
         "by NIFTY drawdown bucket, four indices pooled")

    for r in ("bull-leg", "DD1"):
        sub = s[s["regime"] == r]
        print(f"\n  --- episode {r} ---")
        stat_block("down-bias d", per_session_break_bias(sub), unit="  ")
        stat_block("up-minus-down ret_3d (paired)",
                   per_session_up_minus_down(sub, "ret_3d"))
        for nm in INDICES:
            two_sample_block(f"{nm} ret_3d", sub[sub["underlying"] == nm], "ret_3d")

    for r in ("at-highs", "pullback", "drawdown"):
        sub = s[s["bucket"] == r]
        print(f"\n  --- bucket {r} ({sub['dt'].nunique()} sessions) ---")
        stat_block("down-bias d", per_session_break_bias(sub), unit="  ")
        for nm in INDICES:
            two_sample_block(f"{nm} ret_3d", sub[sub["underlying"] == nm], "ret_3d")

    print("\n  DID IT ACTUALLY CHANGE? (differences, not two separate t's)")
    for x, y in (("at-highs", "drawdown"), ("at-highs", "pullback")):
        contrast("down-bias d",
                 per_session_break_bias(s[s["bucket"] == x]),
                 per_session_break_bias(s[s["bucket"] == y]), x, y)
    nb = s[s["underlying"] == "NIFTY"]
    for x, y in (("bull-leg", "DD1"),):
        contrast("NIFTY ret_3d, up-breaks",
                 nb[(nb["regime"] == x) & (nb["side"] == 1)]["ret_3d"] * PCT,
                 nb[(nb["regime"] == y) & (nb["side"] == 1)]["ret_3d"] * PCT,
                 x, y, "pp")
        contrast("NIFTY ret_3d, down-breaks",
                 nb[(nb["regime"] == x) & (nb["side"] == -1)]["ret_3d"] * PCT,
                 nb[(nb["regime"] == y) & (nb["side"] == -1)]["ret_3d"] * PCT,
                 x, y, "pp")

    # ------------------------------------------------- bank-window overlap
    print("\n" + "=" * 92)
    print("5. THE INDICES INSIDE THE BANK WINDOW (2024-09..2026-08) vs BEFORE")
    print("=" * 92)
    s["era"] = np.where(dt >= pd.Timestamp("2024-09-01"), "bank-window", "pre-2024-09")
    show(table(s, "era", order=["pre-2024-09", "bank-window"]), "pooled")
    for e in ("pre-2024-09", "bank-window"):
        sub = s[s["era"] == e]
        print(f"\n  --- {e} ---")
        stat_block("down-bias d", per_session_break_bias(sub), unit="  ")
        stat_block("up-minus-down ret_3d (paired)",
                   per_session_up_minus_down(sub, "ret_3d"))
        for nm in INDICES:
            two_sample_block(f"{nm} ret_3d", sub[sub["underlying"] == nm], "ret_3d")

    print("\n  DID IT ACTUALLY CHANGE BETWEEN ERAS?")
    contrast("down-bias d",
             per_session_break_bias(s[s["era"] == "pre-2024-09"]),
             per_session_break_bias(s[s["era"] == "bank-window"]),
             "pre", "bank-win")
    for nm in INDICES:
        e = s[s["underlying"] == nm]
        for sd, tag in ((1, "up-breaks"), (-1, "down-breaks")):
            contrast(f"{nm} ret_3d, {tag}",
                     e[(e["era"] == "pre-2024-09") & (e["side"] == sd)]["ret_3d"] * PCT,
                     e[(e["era"] == "bank-window") & (e["side"] == sd)]["ret_3d"] * PCT,
                     "pre", "bank-win", "pp")


if __name__ == "__main__":
    main()
