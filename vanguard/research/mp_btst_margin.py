"""How FAR above value does the close have to be? The knife-edge problem.

FOUND BY INSPECTING SBIN'S SIGNAL LIST. Many nights qualifying as "close above
the value area" clear the VAH by almost nothing -- one close sat 0.001% above it.
The value area high is the upper edge of a 40-bin histogram of the session range,
so a close a hair above it is a bin-boundary artefact, not an auction accepting
higher. Roughly a sixth of all sessions close above VAH, and if a large share of
those are marginal then the signal is mostly measuring rounding.

So: require the close to be MATERIALLY above value, and see whether the edge
strengthens with the margin. Two natural yardsticks, both scale-free:

    margin / value-area width   how far past the edge of value, relative to how
                                wide value itself was that day
    margin / ATR                how far past it in units of the name's own
                                daily range, which is comparable across names

If the edge rises monotonically with the margin, the marginal signals are noise
and should be excluded. If it is flat, the VAH test is doing no work beyond
"close near the high" and the whole profile framing is decoration.

    python vanguard/research/mp_btst_margin.py
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
    s["ret20"] = g["close"].transform(lambda x: x / x.shift(20) - 1.0)
    s["rs_rank"] = s.groupby("dt")["ret20"].rank(pct=True)
    s = s.dropna(subset=["next_open_ret", "vah", "val", "ret20", "rs_rank", "atr20"])
    s = s.reset_index(drop=True)
    s["gap"] = s["next_open_ret"] * 100.0
    s["margin_pct"] = (s["close"] - s["vah"]) / s["close"] * 100.0
    s["margin_va"] = (s["close"] - s["vah"]) / (s["vah"] - s["val"]).replace(0, np.nan)
    s["margin_atr"] = (s["close"] - s["vah"]) / (s["atr20"] * s["close"]).replace(0, np.nan)
    above = s[s["close"] > s["vah"]].copy()

    print(f"{s['underlying'].nunique()} banks, {s['dt'].nunique()} sessions, "
          f"{len(above):,} closes above VAH ({len(above) / len(s) * 100:.1f}% of "
          f"{len(s):,} name-sessions)")
    print(f"\nHOW MARGINAL ARE THEY? distribution of (close - VAH)")
    for lab, col, unit in (("as % of price", "margin_pct", "%"),
                           ("as a fraction of VA width", "margin_va", "x"),
                           ("as a fraction of daily ATR", "margin_atr", "x")):
        q = above[col].quantile([0.10, 0.25, 0.50, 0.75, 0.90])
        print(f"   {lab:<28}" + "  ".join(
            f"p{int(p * 100)}={v:.3f}{unit}" for p, v in q.items()))
    tiny = (above["margin_atr"] < 0.05).mean()
    print(f"   {tiny * 100:.0f}% of them clear the VAH by less than 5% of one day's ATR"
          f" — a bin\n   boundary, not an acceptance.")

    print(f"\n1. DOES THE EDGE RISE WITH THE MARGIN?  (all closes above VAH)")
    print(f"   {'margin bucket (x daily ATR)':<32}{'trades':>8}{'mean %':>9}"
          f"{'median %':>10}{'win':>6}{'clus t':>8}")
    above["b"] = pd.cut(above["margin_atr"],
                        [-0.001, 0.05, 0.15, 0.30, 0.60, 99],
                        labels=["0.00-0.05 (knife edge)", "0.05-0.15", "0.15-0.30",
                                "0.30-0.60", "0.60+"])
    for b, d in above.groupby("b", observed=True):
        r = d["gap"]
        print(f"   {str(b):<32}{len(d):>8}{r.mean():>+9.3f}{r.median():>+10.3f}"
              f"{(r > 0).mean() * 100:>5.0f}%{cluster_t(d):>+8.2f}")

    print(f"\n2. THE RULE WITH A MINIMUM MARGIN  (+ RS top half, as before)")
    print(f"   {'rule':<32}{'trades':>8}{'mean %':>9}{'median %':>10}{'win':>6}"
          f"{'clus t':>8}{'1st half':>10}{'2nd half':>10}")
    rs = s["rs_rank"] >= 0.5
    for lab, m in (("no minimum (current)", s["close"] > s["vah"]),
                   ("margin >= 0.05 ATR", s["margin_atr"] >= 0.05),
                   ("margin >= 0.10 ATR", s["margin_atr"] >= 0.10),
                   ("margin >= 0.20 ATR", s["margin_atr"] >= 0.20),
                   ("margin >= 0.30 ATR", s["margin_atr"] >= 0.30),
                   ("margin >= 0.25 x VA width", s["margin_va"] >= 0.25)):
        d = s[m & rs]
        if len(d) < 80:
            print(f"   {lab:<32}{len(d):>8}   (too few)")
            continue
        r = d["gap"]
        h = d["dt"].nunique() // 2
        cut = sorted(d["dt"].unique())[h]
        print(f"   {lab:<32}{len(d):>8}{r.mean():>+9.3f}{r.median():>+10.3f}"
              f"{(r > 0).mean() * 100:>5.0f}%{cluster_t(d):>+8.2f}"
              f"{d[d['dt'] < cut]['gap'].mean():>+10.3f}"
              f"{d[d['dt'] >= cut]['gap'].mean():>+10.3f}")

    print(f"\n3. IS 'ABOVE VAH' DOING ANYTHING BEYOND 'NEAR THE HIGH'?")
    print(f"   {'cohort':<32}{'trades':>8}{'mean %':>9}{'median %':>10}{'win':>6}{'clus t':>8}")
    near_high = s["close_pos"] >= 0.80
    for lab, m in (("close_pos >= 0.80 only", near_high & ~(s["close"] > s["vah"])),
                   ("above VAH only", (s["close"] > s["vah"]) & ~near_high),
                   ("both", near_high & (s["close"] > s["vah"])),
                   ("above VAH by >=0.20 ATR, both", (s["margin_atr"] >= 0.20) & near_high)):
        d = s[m]
        if len(d) < 80:
            print(f"   {lab:<32}{len(d):>8}   (too few)")
            continue
        r = d["gap"]
        print(f"   {lab:<32}{len(d):>8}{r.mean():>+9.3f}{r.median():>+10.3f}"
              f"{(r > 0).mean() * 100:>5.0f}%{cluster_t(d):>+8.2f}")
    print("   If 'above VAH only' beats 'close_pos>=0.80 only', the profile is adding\n"
          "   information that range location does not carry. If not, it is decoration.")

    print(f"\n4. PER SYMBOL under the best margin rule (>= 0.20 ATR + RS top half)")
    best = s[(s["margin_atr"] >= 0.20) & rs]
    print(f"   {'name':<13}{'trades':>8}{'mean %':>9}{'median %':>10}{'win':>6}{'total %':>9}")
    for name, d in best.groupby("underlying"):
        if len(d) < 8:
            print(f"   {name:<13}{len(d):>8}   (too few)")
            continue
        r = d["gap"]
        print(f"   {name:<13}{len(d):>8}{r.mean():>+9.3f}{r.median():>+10.3f}"
              f"{(r > 0).mean() * 100:>5.0f}%"
              f"{((1 + r / 100).prod() - 1) * 100:>+9.1f}")
    r = best["gap"]
    print(f"   {'POOLED':<13}{len(r):>8}{r.mean():>+9.3f}{r.median():>+10.3f}"
          f"{(r > 0).mean() * 100:>5.0f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
