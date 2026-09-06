"""REFUTATION ATTEMPT on "atr20 ranks the IB-break move" (rank IC +0.234, t+14.2).

Four attacks, run together so the corrected numbers are all on one sample.

  1 LOOK-AHEAD.  research/mp_profile.add_context builds

        prev_close = g["close"].shift(1)
        tr         = max(high-low, |high-prev_close|, |low-prev_close|)
        atr20      = tr.rolling(20, min_periods=10).mean() / g["close"]

      tr at row i is built from row i's OWN high/low, and .rolling(20) at row i
      INCLUDES tr[i]. So atr20 for session t contains 1/20 of session t's own
      true range -- the very quantity that mechanically bounds mfe_intraday,
      which is a component of mfe_total. The divisor is session t's CLOSE, also
      unknown at the 10:15 entry. Both are repaired here:

        atr20_lag = tr.shift(1).rolling(20).mean() / close.shift(1)

      and the two leaks are separated so we can see which one carried the IC.

  2 MECHANICAL ARTEFACT.  mfe_total is a percent move, atr20 a percent range.
      Re-run the IC on mfe_total / atr20_lag (payoff per unit of the volatility
      the option already charges for). Because the same denominator appears in
      the feature, a ratio bias pushes that IC down mechanically, so it is also
      re-run against a DISJOINT normaliser (ATR over t-40..t-21), which controls
      for volatility without sharing the feature's window.

  3 IS ib_vs_atr INERT, OR BADLY DENOMINATED?  ib_width is legitimately known at
      10:15. Try it three other ways that are all prior-data-only: deviation
      from its own trailing 20-session mean, a z-score of that, and a trailing
      120-session percentile within the name's own history.

  4 SELECTION.  side==0 sessions are dropped. If low-vol names break less often,
      the break sample is already conditioned on volatility. Break rate by
      within-session ATR quintile, plus a zero-filled full-sample target
      (no break = no trade = 0 move), which is what a trader actually gets.

Method rules honoured: session Spearman IC (one observation per session),
t across sessions, split-half and drop-2-best on every headline, everything in
percent as well as in ATR units, atr20 treated as the benchmark to beat.

    docker exec nomadcurie_vanguard_cycle python /vanguard/research/atr_refutation.py
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
from research.banknifty_rotation import BANKS  # noqa: E402
from research.mp_profile import FWD_SESSIONS, dsn, load  # noqa: E402

BANK_UNIVERSE = ("BANKNIFTY",) + BANKS
MIN_NAMES = 6

warnings.filterwarnings("ignore")


# ----------------------------------------------------------------- statistics
def t_of(x) -> float:
    x = pd.Series(x).dropna()
    if len(x) < 3 or x.std(ddof=1) == 0:
        return np.nan
    return x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))


def session_ics(d: pd.DataFrame, feature: str, target: str,
                min_names: int = MIN_NAMES) -> pd.DataFrame:
    """One Spearman IC per SESSION, across the names present that session."""
    dd = d.dropna(subset=[feature, target])
    rows = []
    for dt, g in dd.groupby("dt", sort=True):
        if len(g) < min_names:
            continue
        ic = g[feature].corr(g[target], method="spearman")
        if pd.isna(ic):
            continue
        edge = g.nlargest(3, feature)[target].mean() - g[target].mean()
        rows.append((dt, ic, edge, len(g)))
    return pd.DataFrame(rows, columns=["dt", "ic", "edge", "n"])


def ic_line(label: str, ics: pd.DataFrame, edge_scale: float = 100.0) -> str:
    if len(ics) < 30:
        return f"   {label:<24}{'too few sessions':>60}"
    ic, ed = ics["ic"], ics["edge"]
    h = len(ics) // 2
    keep = ics.sort_values("ic").iloc[:-2]["ic"]        # drop the 2 best sessions
    star = " *" if abs(t_of(ic)) >= 2 else "  "
    return (f"   {label:<24}{ic.mean():>+8.3f}{t_of(ic):>+7.2f}"
            f"{(ic > 0).mean() * 100:>6.0f}%"
            f"{ic.iloc[:h].mean():>+8.3f}{ic.iloc[h:].mean():>+8.3f}"
            f"{keep.mean():>+8.3f}{t_of(keep):>+7.2f}"
            f"{ed.mean() * edge_scale:>+9.2f}{t_of(ed):>+7.2f}{star}")


def ic_header(edge_unit: str) -> str:
    return (f"   {'feature':<24}{'IC':>8}{'t':>7}{'IC>0':>7}"
            f"{'1st h':>8}{'2nd h':>8}{'drop2':>8}{'t':>7}"
            f"{'top3 ' + edge_unit:>9}{'t':>7}")


def block(d: pd.DataFrame, features, target: str, title: str,
          edge_scale: float = 100.0, edge_unit: str = "pp") -> None:
    n = d.dropna(subset=[target]).shape[0]
    print(f"\n{title}   target={target}  n={n:,}")
    print(ic_header(edge_unit))
    for f in features:
        if f not in d.columns:
            continue
        print(ic_line(f, session_ics(d, f, target), edge_scale))


# ------------------------------------------------------------------- features
def trailing_pct(x: pd.Series, win: int = 120, minp: int = 40) -> pd.Series:
    """Percentile of today's value within the name's OWN prior `win` sessions."""
    v = x.to_numpy(dtype=float)
    out = np.full(len(v), np.nan)
    for i in range(len(v)):
        if not np.isfinite(v[i]):
            continue
        hist = v[max(0, i - win):i]
        hist = hist[np.isfinite(hist)]
        if len(hist) >= minp:
            out[i] = (hist < v[i]).mean()
    return pd.Series(out, index=x.index)


def add_clean_features(s: pd.DataFrame) -> pd.DataFrame:
    out = []
    for _, g in s.groupby("underlying", sort=False):
        g = g.sort_values("dt").reset_index(drop=True)
        prev_close = g["close"].shift(1)
        tr = pd.concat([g["high"] - g["low"],
                        (g["high"] - prev_close).abs(),
                        (g["low"] - prev_close).abs()], axis=1).max(axis=1)

        # exactly what mp_profile builds -- reproduced so the leak is provable
        g["atr20_leak"] = tr.rolling(20, min_periods=10).mean() / g["close"]
        # honest: numerator over t-20..t-1, divisor is yesterday's close
        g["atr20_lag"] = tr.shift(1).rolling(20, min_periods=10).mean() / prev_close
        # the two leaks separated
        g["atr20_numleak"] = tr.rolling(20, min_periods=10).mean() / prev_close
        g["atr20_denleak"] = tr.shift(1).rolling(20, min_periods=10).mean() / g["close"]
        # the pure look-ahead quantity that the leak smuggles in (5% weight)
        g["tr_today"] = tr / prev_close
        # a normaliser DISJOINT from the feature's own window: t-40 .. t-21
        g["atr_prior"] = (tr.shift(21).rolling(20, min_periods=10).mean()
                          / g["close"].shift(21))

        # ib_width, legitimately known at 10:15, expressed three other ways
        ibw = g["ib_width"]
        roll_mean = ibw.shift(1).rolling(20, min_periods=10).mean()
        roll_std = ibw.shift(1).rolling(20, min_periods=10).std()
        g["ib_dev"] = ibw - roll_mean
        g["ib_z"] = g["ib_dev"] / roll_std.replace(0, np.nan)
        g["ib_pct"] = trailing_pct(ibw)
        g["ib_vs_atr_clean"] = ibw / g["atr20_lag"].replace(0, np.nan)
        out.append(g)
    return pd.concat(out, ignore_index=True)


# --------------------------------------------------------------------- audits
def leak_audit(s: pd.DataFrame) -> None:
    d = s.dropna(subset=["atr20", "atr20_leak", "atr20_lag"])
    print("\n0. LEAK AUDIT -- does atr20 for session t contain session t?")
    print(f"   reproduction of mp_profile.atr20 from its own recipe:"
          f"  max abs diff = {(d['atr20'] - d['atr20_leak']).abs().max():.3e}"
          f"   (identical -> the recipe below IS what shipped)")
    print(f"      atr20 = tr.rolling(20).mean() / close      <- tr[t] is IN the window,"
          f" and the divisor is session t's CLOSE")
    print(f"      the fix: tr.shift(1).rolling(20).mean() / close.shift(1)")
    r = d["atr20_leak"].corr(d["atr20_lag"], method="spearman")
    rel = ((d["atr20_leak"] - d["atr20_lag"]) / d["atr20_lag"])
    print(f"   spearman(atr20_leak, atr20_lag) = {r:.4f}"
          f"   median relative gap {rel.median() * 100:+.2f}%"
          f"   p90 {rel.quantile(0.90) * 100:+.2f}%   p99 {rel.quantile(0.99) * 100:+.2f}%")
    # how much of the WITHIN-SESSION ranking is moved by the leak
    dd = d.copy()
    dd["rk_leak"] = dd.groupby("dt")["atr20_leak"].rank(pct=True)
    dd["rk_lag"] = dd.groupby("dt")["atr20_lag"].rank(pct=True)
    moved = (dd["rk_leak"] - dd["rk_lag"]).abs()
    print(f"   within-session percentile rank moved by the leak:"
          f"  median {moved.median():.3f}  mean {moved.mean():.3f}"
          f"  P(|move|>0.10) {(moved > 0.10).mean() * 100:.1f}%")


def quintiles(b: pd.DataFrame, feature: str, min_names: int = 10) -> None:
    d = b.dropna(subset=[feature, "mfe_total", "atr20_lag"]).copy()
    parts = []
    for _, g in d.groupby("dt"):
        if len(g) < min_names:
            continue
        g = g.copy()
        g["q"] = pd.qcut(g[feature].rank(method="first"), 5, labels=False)
        parts.append(g)
    if not parts:
        print("   (no sessions with enough names)")
        return
    d = pd.concat(parts)
    print(f"\n   within-session quintile of {feature}   (1=lowest vol, 5=highest)")
    print(f"      {'q':<4}{'n':>7}{'atr20_lag':>11}{'med MFE%':>11}"
          f"{'mean MFE%':>11}{'MFE/atr':>10}{'MFE/atrPRI':>12}{'P(>=2%)':>10}")
    for q, g in d.groupby("q"):
        print(f"      {int(q) + 1:<4}{len(g):>7}{g['atr20_lag'].median() * 100:>10.2f}%"
              f"{g['mfe_total'].median() * 100:>10.2f}%"
              f"{g['mfe_total'].mean() * 100:>10.2f}%"
              f"{(g['mfe_total'] / g['atr20_lag']).median():>10.2f}"
              f"{(g['mfe_total'] / g['atr_prior']).median():>12.2f}"
              f"{(g['mfe_total'] >= 0.02).mean() * 100:>9.1f}%")
    print("      MFE/atrPRI uses the DISJOINT t-40..t-21 ATR, so the ranking variable")
    print("      is not in the denominator and no ratio bias can create the tilt.")


def _quint(d: pd.DataFrame, feature: str, label: str) -> pd.DataFrame:
    parts = []
    for _, g in d.dropna(subset=[feature]).groupby("dt"):
        if len(g) < 10:
            continue
        g = g.copy()
        g["q"] = pd.qcut(g[feature].rank(method="first"), 5, labels=False)
        parts.append(g)
    dd = pd.concat(parts)
    print(f"\n   break rate by within-session {label} quintile   "
          f"sessions={dd['dt'].nunique()}")
    print(f"      {'q':<4}{'n':>7}{'atr20_lag':>11}{'ib_width':>10}"
          f"{'broke':>9}{'up':>8}{'down':>8}{'never':>8}")
    for q, g in dd.groupby("q"):
        print(f"      {int(q) + 1:<4}{len(g):>7}{g['atr20_lag'].median() * 100:>10.2f}%"
              f"{g['ib_width'].median() * 100:>9.2f}%"
              f"{(g['side'] != 0).mean() * 100:>8.1f}%"
              f"{(g['side'] == 1).mean() * 100:>7.1f}%"
              f"{(g['side'] == -1).mean() * 100:>7.1f}%"
              f"{(g['side'] == 0).mean() * 100:>7.1f}%")
    return dd


def selection_audit(s: pd.DataFrame) -> None:
    print("\n4. SELECTION -- is the break sample conditioned on volatility?")
    d = s.dropna(subset=["atr20_lag"]).copy()
    d = _quint(d, "atr20_lag", "atr20_lag")
    _quint(s.dropna(subset=["atr20_lag", "ib_pct"]).copy(), "ib_pct",
           "ib_pct (IB width vs the name's own 120-session history)")

    # is the one surviving signal a MOVE signal or just a break-probability one?
    d["broke"] = (d["side"] != 0).astype(float)
    print("\n   IC against the BREAK INDICATOR itself (does the IB get broken at all?)")
    print(ic_header("pp"))
    for f in ("atr20_lag", "ib_width", "ib_pct", "ib_z"):
        print(ic_line(f, session_ics(d, f, "broke")))
    # tradeable full-sample target: no break -> no trade -> zero move
    d["mfe_zero"] = d["mfe_total"].where(d["side"] != 0, 0.0)
    d["mfe_zero_n"] = d["mfe_zero"] / d["atr20_lag"]
    print("\n   zero-filled FULL sample (no break = 0 move), so nothing is selected away")
    print(ic_header("pp"))
    for f in ("atr20_leak", "atr20_lag", "ib_width", "ib_pct"):
        print(ic_line(f, session_ics(d, f, "mfe_zero")))
    print("\n   ...and the same, normalised by atr20_lag (edge in ATR units)")
    print(ic_header("atr"))
    for f in ("atr20_leak", "atr20_lag", "ib_width", "ib_pct"):
        print(ic_line(f, session_ics(d, f, "mfe_zero_n"), edge_scale=1.0))


# ----------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback-days", type=int, default=700)
    ap.add_argument("--dsn", default=dsn())
    args = ap.parse_args()

    start = date.today() - timedelta(days=args.lookback_days)
    conn = psycopg2.connect(args.dsn)
    try:
        s = load(conn, list(BANK_UNIVERSE), start)
    finally:
        conn.close()
    if s.empty:
        print("no sessions built")
        return 1

    s = add_clean_features(s)
    print(f"universe=banks  names={s['underlying'].nunique()}  "
          f"window {s['dt'].min().date()} .. {s['dt'].max().date()}  "
          f"sessions(name-days)={len(s):,}  breaks={(s['side'] != 0).sum():,}")

    leak_audit(s)

    # every comparison on the SAME rows: both ATR variants defined
    b = s[(s["side"] != 0)].dropna(subset=["atr20_leak", "atr20_lag", "atr_prior"]).copy()
    b["mfe_norm"] = b["mfe_total"] / b["atr20_lag"]
    b["mfe_norm_disjoint"] = b["mfe_total"] / b["atr_prior"]
    b["mfe3d_norm"] = b[f"mfe_{FWD_SESSIONS}d"] / b["atr20_lag"]
    b["range_pct"] = (b["high"] - b["low"]) / b["close"]
    print(f"\n   common sample for every table below: {len(b):,} breaks, "
          f"{b['dt'].nunique():,} sessions, {b['underlying'].nunique()} names")

    vol_feats = ["atr20_leak", "atr20_lag", "atr20_numleak", "atr20_denleak", "tr_today"]
    prof_feats = ["ib_width", "ib_vs_atr", "ib_vs_atr_clean", "ib_dev", "ib_z", "ib_pct"]

    block(b, vol_feats + prof_feats, "mfe_total",
          "1. THE HEADLINE, AS SHIPPED AND AS CORRECTED")
    print("   tr_today is the pure look-ahead quantity the leak smuggles in at 1/20 weight.")

    block(b, vol_feats + prof_feats, "range_pct",
          "1b. CONTROL: same features against session t's OWN realised range "
          "(a thing no one can trade)")

    block(b, vol_feats + prof_feats, "mfe_norm",
          "2. MECHANICAL ARTEFACT: move per unit of the volatility already priced",
          edge_scale=1.0, edge_unit="atr")
    print("   NOTE: mfe_norm shares atr20_lag with the feature, so a ratio bias drags")
    print("   its IC negative on its own. The disjoint normaliser below has no such bias.")

    block(b, vol_feats + prof_feats, "mfe_norm_disjoint",
          "2b. SAME, normalised by a DISJOINT ATR (sessions t-40..t-21)",
          edge_scale=1.0, edge_unit="atr")

    block(b, ["atr20_leak", "atr20_lag", "ib_width", "ib_pct"], "mfe_intraday",
          "3a. WHERE THE LEAK LIVES: intraday leg (same session as the leaked range)")
    block(b, ["atr20_leak", "atr20_lag", "ib_width", "ib_pct"], f"mfe_{FWD_SESSIONS}d",
          "3b. ...and the forward leg (later sessions, which the leak cannot touch)")
    block(b, ["atr20_leak", "atr20_lag", "ib_width", "ib_pct"], "mfe3d_norm",
          "3c. forward leg, normalised", edge_scale=1.0, edge_unit="atr")
    block(b, ["atr20_leak", "atr20_lag", "ib_width", "ib_pct"], f"ret_{FWD_SESSIONS}d",
          "3d. the honest hold-to-horizon result")

    print("\n3e. QUINTILE PROFILE (break sample)")
    quintiles(b, "atr20_lag")
    quintiles(b, "atr20_leak")

    selection_audit(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
