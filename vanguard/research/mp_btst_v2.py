"""BTST v2: a proper MP strong close, on a stock that is actually strong.

WHAT v1 ESTABLISHED. Ranking close_pos against each name's own history fires ~98
times for EVERY name regardless of trend (rank corr with trend +0.069) -- it
cannot distinguish a rising bank from a falling one. Replacing it with the MP
statement of strength, A CLOSE ABOVE THE VALUE AREA HIGH, fixes both halves:
signal count now tracks trend (+0.525) and the edge roughly doubles, +15.9 bp
per night against +8.5, with a higher session-clustered t on half the trades.

WHAT THIS ADDS. The owner's point has a second half: a genuinely strong name
should throw up many good chances and a weak one few. Closing above value is a
statement about TODAY. Whether the STOCK is strong is a separate, and separately
available, fact -- so it is tested as an explicit filter rather than hoped for.

    trend filters, all knowable at the 15:15 close and all lagged:
      close above its own 20-EMA / 50-EMA
      positive trailing 20-session return
      the name in the top half of the 16 banks by trailing 20-session return
        (cross-sectional relative strength, which is what "strong performer"
        actually means in a sector book)

CAUTION CARRIED FORWARD. Nine definitions were already searched in v1, and each
filter here multiplies that. Session-clustered t, both halves and drop-2 are
reported for everything, and the count of combinations tried is printed so the
multiple-testing discount is visible rather than implied.

    python vanguard/research/mp_btst_v2.py
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
from research.mp_auction import dsn, load  # noqa: E402

RANK_WINDOW, MIN_PERIODS = 120, 60


def cluster_t(d: pd.DataFrame, col: str = "gap") -> float:
    d = d[[col, "dt"]].dropna()
    if len(d) < 30:
        return np.nan
    mu = d[col].mean()
    g = (d[col] - mu).groupby(d["dt"]).sum()
    se = np.sqrt((g ** 2).sum()) / len(d)
    return mu / se if se > 0 else np.nan


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=float, default=3.0)
    parser.add_argument("--dsn", default=dsn())
    args = parser.parse_args()

    connection = psycopg2.connect(args.dsn)
    try:
        s = load(connection, list(BANKS),
                 date.today() - timedelta(days=int(args.years * 365.25)))
    finally:
        connection.close()
    s = s.sort_values(["underlying", "dt"]).reset_index(drop=True)
    g = s.groupby("underlying")
    s["ema20"] = g["close"].transform(lambda x: x.ewm(span=20, adjust=False).mean())
    s["ema50"] = g["close"].transform(lambda x: x.ewm(span=50, adjust=False).mean())
    s["ret20"] = g["close"].transform(lambda x: x / x.shift(20) - 1.0)
    s["cp_rank"] = (g["close_pos"]
                    .transform(lambda x: x.rolling(RANK_WINDOW, min_periods=MIN_PERIODS)
                               .apply(lambda w: (w[-1] > w[:-1]).mean(), raw=True)))
    # cross-sectional relative strength: is this name in the stronger half of the
    # sector today? computed within each session, so it is genuinely relative
    s["rs_rank"] = s.groupby("dt")["ret20"].rank(pct=True)
    s = s.dropna(subset=["next_open_ret", "vah", "ret20", "rs_rank"]).reset_index(drop=True)
    s["gap"] = s["next_open_ret"]
    s["above_vah"] = s["close"] > s["vah"]

    trend = (s.groupby("underlying")
             .apply(lambda x: x["close"].iloc[-1] / x["close"].iloc[0] - 1.0))
    print(f"{s['underlying'].nunique()} banks, {s['dt'].nunique()} sessions, "
          f"{s['dt'].min().date()} .. {s['dt'].max().date()}")

    base = s["above_vah"]
    FILTERS = {
        "(none) close above value area": pd.Series(True, index=s.index),
        "+ close > own 20-EMA": s["close"] > s["ema20"],
        "+ close > own 50-EMA": s["close"] > s["ema50"],
        "+ trailing 20d return > 0": s["ret20"] > 0,
        "+ RS: top half of the 16 banks": s["rs_rank"] >= 0.5,
        "+ RS: top third of the 16 banks": s["rs_rank"] >= 2 / 3,
        "+ RS top half AND > 20-EMA": (s["rs_rank"] >= 0.5) & (s["close"] > s["ema20"]),
        "+ RS top third AND > 50-EMA": (s["rs_rank"] >= 2 / 3) & (s["close"] > s["ema50"]),
    }
    print(f"\n1. A STRONG CLOSE ON A STRONG STOCK  (base = close above the value area)")
    print(f"   {'filter':<36}{'trades':>8}{'bp/nt':>8}{'median':>8}{'win':>6}"
          f"{'clus t':>8}{'1st half':>10}{'2nd half':>10}{'drop2':>8}")
    for label, m in FILTERS.items():
        d = s[base & m]
        if len(d) < 120:
            print(f"   {label:<36}{len(d):>8}   (too few)")
            continue
        r = d["gap"]
        h = d["dt"].nunique() // 2
        cut = sorted(d["dt"].unique())[h]
        a = d[d["dt"] < cut]["gap"].mean() * 1e4
        b = d[d["dt"] >= cut]["gap"].mean() * 1e4
        d2 = r.drop(r.nlargest(2).index).mean() * 1e4
        print(f"   {label:<36}{len(d):>8}{r.mean() * 1e4:>+8.1f}{r.median() * 1e4:>+8.1f}"
              f"{(r > 0).mean() * 100:>5.0f}%{cluster_t(d):>+8.2f}{a:>+10.1f}"
              f"{b:>+10.1f}{d2:>+8.1f}")
    print(f"   {len(FILTERS)} filters here, after 12 definitions in v1 — discount "
          f"accordingly.")

    # ── the comparison the owner asked for, per name ────────────────────────
    print(f"\n2. OLD vs NEW, PER NAME (sorted by the name's own buy & hold)")
    old = s["cp_rank"] >= 2 / 3
    new = base & (s["rs_rank"] >= 0.5)
    print(f"   {'name':<13}{'buy&hold%':>11}" +
          f"{'OLD n':>8}{'OLD bp':>8}{'OLD tot%':>10}" +
          f"{'NEW n':>8}{'NEW bp':>8}{'NEW tot%':>10}")
    for name in trend.sort_values(ascending=False).index:
        o = s[old & (s["underlying"] == name)]["gap"]
        w = s[new & (s["underlying"] == name)]["gap"]
        ot = ((1 + o).prod() - 1) * 100 if len(o) else np.nan
        wt = ((1 + w).prod() - 1) * 100 if len(w) else np.nan
        print(f"   {name:<13}{trend[name] * 100:>+11.1f}"
              f"{len(o):>8}{o.mean() * 1e4 if len(o) else np.nan:>+8.1f}{ot:>+10.1f}"
              f"{len(w):>8}{w.mean() * 1e4 if len(w) else np.nan:>+8.1f}{wt:>+10.1f}")
    for label, m in (("OLD (relative close_pos)", old), ("NEW (above VAH + RS top half)", new)):
        d = s[m]
        cnt = d.groupby("underlying").size().reindex(trend.index).fillna(0)
        bp = d.groupby("underlying")["gap"].mean().reindex(trend.index)
        print(f"   {label:<34} corr(trend, signals) "
              f"{trend.corr(cnt, method='spearman'):+.3f}"
              f"   corr(trend, bp) {trend.corr(bp, method='spearman'):+.3f}"
              f"   pooled {d['gap'].mean() * 1e4:+.1f}bp  t {cluster_t(d):+.2f}")

    # ── equity curve for the improved rule ──────────────────────────────────
    print(f"\n3. EQUITY, per-symbol books under the NEW rule (zero cost)")
    print(f"   {'name':<13}{'trades':>8}{'bp/nt':>8}{'median':>8}{'win':>6}"
          f"{'total%':>9}{'maxDD%':>9}")
    tot = []
    for name, d in s[new].groupby("underlying"):
        r = d["gap"]
        if len(r) < 15:
            continue
        eq = (1 + r).cumprod()
        tot.append(eq.iloc[-1] - 1)
        print(f"   {name:<13}{len(r):>8}{r.mean() * 1e4:>+8.1f}{r.median() * 1e4:>+8.1f}"
              f"{(r > 0).mean() * 100:>5.0f}%{(eq.iloc[-1] - 1) * 100:>+9.1f}"
              f"{(eq / eq.cummax() - 1).min() * 100:>+9.1f}")
    print(f"   average book {np.mean(tot) * 100:+.2f}%   "
          f"{sum(1 for x in tot if x > 0)}/{len(tot)} positive")

    out = os.environ.get("V2_OUT")
    if out:
        recs = []
        for name, d in s[new].groupby("underlying"):
            if len(d) < 15:
                continue
            eq = (1 + d["gap"]).cumprod()
            recs.append({"name": name, "dts": ",".join(str(x)[:10] for x in d["dt"]),
                         "eq": ",".join(f"{v:.4f}" for v in eq)})
        pd.DataFrame(recs).to_csv(out, index=False)
        print(f"\ncurves written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
