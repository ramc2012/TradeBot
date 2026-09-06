"""Proper MP strong-close definitions for BTST, and the bug the owner just found.

TWO CRITICISMS, both correct.

  1. NO MP PROFILE WAS USED ON THE STOCKS. The bank books ranked
     close_pos = (close-low)/(high-low), a crude range location. A close near the
     high of a wide rotational day scores identically to a close above value on a
     trend day, and they are not the same auction. The full TPO apparatus exists
     in mp_auction.py but was only ever run on BANKNIFTY. Closing above the VALUE
     AREA HIGH is the MP statement of strength; being near the day's high is not.

  2. THE SELF-NORMALISATION BUG. "If Federal Bank is a strong performer it should
     have given ample chances of good wins; non-performing banks should have
     given least chances." Exactly -- and the rule cannot do that. Ranking each
     close against THAT NAME'S OWN trailing 120 sessions forces every name to
     signal on roughly a third of nights whatever its trend. A stock in freefall
     still has top-tertile closes. The signal never asked "is this stock strong",
     only "is today strong FOR THIS STOCK". FEDERALBNK rose 64% over the window
     and its book made nothing, which is the symptom.

So this tests ABSOLUTE, PROFILE-BASED definitions against the relative one, and
scores them on the owner's own criterion: a good BTST screen should fire MORE
often on names that are actually rising, and its per-name results should line up
with those names' trends.

    python vanguard/research/mp_btst_candidates.py
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


def cluster_t(d: pd.DataFrame, col: str) -> float:
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
    s["cp_rank"] = (s.groupby("underlying")["close_pos"]
                    .transform(lambda x: x.rolling(RANK_WINDOW, min_periods=MIN_PERIODS)
                               .apply(lambda w: (w[-1] > w[:-1]).mean(), raw=True)))
    s = s.dropna(subset=["cp_rank", "next_open_ret", "vah"]).reset_index(drop=True)
    s["gap"] = s["next_open_ret"]

    # each name's realised trend over the tradeable window -- the thing a strong
    # close ought to be picking up
    trend = (s.groupby("underlying")
             .apply(lambda g: g["close"].iloc[-1] / g["close"].iloc[0] - 1.0)
             .rename("bh"))
    sessions = s["dt"].nunique()
    print(f"{s['underlying'].nunique()} banks, {sessions} sessions, "
          f"{s['dt'].min().date()} .. {s['dt'].max().date()}")

    # ── 1. the bug, demonstrated ────────────────────────────────────────────
    print(f"\n1. THE SELF-NORMALISATION BUG — signals per name vs that name's trend")
    print(f"   {'name':<13}{'buy&hold%':>11}{'RELATIVE signals':>18}{'ABSOLUTE: close>VAH':>21}")
    s["above_vah"] = s["close"] > s["vah"]
    rel = s[s["cp_rank"] >= 2 / 3].groupby("underlying").size()
    ab = s[s["above_vah"]].groupby("underlying").size()
    for name in trend.sort_values(ascending=False).index:
        print(f"   {name:<13}{trend[name] * 100:>+11.1f}{rel.get(name, 0):>18}"
              f"{ab.get(name, 0):>21}")
    r1 = trend.corr(rel.reindex(trend.index).fillna(0), method="spearman")
    r2 = trend.corr(ab.reindex(trend.index).fillna(0), method="spearman")
    print(f"   rank corr(trend, signal count):  RELATIVE {r1:+.3f}   ABSOLUTE {r2:+.3f}")
    print(f"   The relative rule fires ~{rel.mean():.0f} times for EVERY name by "
          f"construction — it\n   cannot tell a rising bank from a falling one. That is "
          f"the defect.")

    # ── 2. MP-based definitions of a strong close ───────────────────────────
    print(f"\n2. WHAT COUNTS AS A STRONG CLOSE — profile definitions vs range location")
    s["above_prev_vah"] = s["close"] > s["prev_vah"]
    s["above_poc"] = s["close"] > s["poc"]
    s["cp80"] = s["close_pos"] >= 0.80
    DEFS = {
        "RELATIVE close_pos rank>=2/3": s["cp_rank"] >= 2 / 3,
        "close_pos >= 0.80 (absolute)": s["cp80"],
        "close ABOVE the value area": s["above_vah"],
        "close above VAH + above POC": s["above_vah"] & s["above_poc"],
        "close above PRIOR VAH (initiative)": s["above_prev_vah"],
        "above VAH + above prior VAH": s["above_vah"] & s["above_prev_vah"],
        "above VAH + value shifted higher": s["above_vah"] & (s["value_shift"] == "higher_outside"),
        "above VAH + poor high (unfinished)": s["above_vah"] & s["poor_high"],
        "above VAH + POC migrated up": s["above_vah"] & (s["poc_migration"] > 0),
        "above VAH + trend day": s["above_vah"] & (s["day_type"] == "trend"),
        "above VAH + cp>=0.80": s["above_vah"] & s["cp80"],
        "above VAH + above prior VAH + cp>=.8": (s["above_vah"] & s["above_prev_vah"]
                                                 & s["cp80"]),
    }
    print(f"   {'definition':<38}{'trades':>8}{'/name':>7}{'bp/nt':>8}{'median':>8}"
          f"{'win':>6}{'clus t':>8}{'corr w/ trend':>15}")
    rows = []
    for label, m in DEFS.items():
        g = s[m]
        if len(g) < 100:
            continue
        r = g["gap"]
        per = g.groupby("underlying")["gap"].mean()
        cw = trend.reindex(per.index).corr(per, method="spearman")
        ct = cluster_t(g, "gap")
        rows.append({"label": label, "n": len(g), "bp": r.mean() * 1e4,
                     "ct": ct, "cw": cw, "m": m})
        print(f"   {label:<38}{len(g):>8}{len(g) / 16:>7.0f}{r.mean() * 1e4:>+8.1f}"
              f"{r.median() * 1e4:>+8.1f}{(r > 0).mean() * 100:>5.0f}%{ct:>+8.2f}"
              f"{cw:>+15.3f}")
    print("   'corr w/ trend' is the owner's test: does the definition earn MORE on the\n"
          "   names that actually rose? A screen that identifies strength should be\n"
          "   positive here. 'clus t' clusters by session, as the banks move together.")

    # ── 3. FEDERALBNK specifically ──────────────────────────────────────────
    print(f"\n3. FEDERALBNK — the test case (buy & hold {trend['FEDERALBNK'] * 100:+.1f}%)")
    f = s[s["underlying"] == "FEDERALBNK"]
    print(f"   {'definition':<38}{'signals':>9}{'bp/nt':>8}{'median':>8}{'win':>6}{'total%':>9}")
    for label, m in DEFS.items():
        g = f[m.reindex(f.index).fillna(False)]
        if len(g) < 10:
            print(f"   {label:<38}{len(g):>9}   (too few)")
            continue
        r = g["gap"]
        print(f"   {label:<38}{len(g):>9}{r.mean() * 1e4:>+8.1f}{r.median() * 1e4:>+8.1f}"
              f"{(r > 0).mean() * 100:>5.0f}%{((1 + r).prod() - 1) * 100:>+9.1f}")

    # ── 4. the best definition, per name ────────────────────────────────────
    best = max([r for r in rows if r["n"] >= 200],
               key=lambda x: (x["cw"] if pd.notna(x["cw"]) else -1))
    print(f"\n4. PER NAME under the definition that best tracks trend: "
          f"{best['label']}")
    g = s[best["m"]]
    print(f"   {'name':<13}{'buy&hold%':>11}{'signals':>9}{'bp/nt':>8}{'median':>8}"
          f"{'win':>6}{'total%':>9}")
    for name in trend.sort_values(ascending=False).index:
        gg = g[g["underlying"] == name]
        if len(gg) < 8:
            print(f"   {name:<13}{trend[name] * 100:>+11.1f}{len(gg):>9}   (too few)")
            continue
        r = gg["gap"]
        print(f"   {name:<13}{trend[name] * 100:>+11.1f}{len(gg):>9}"
              f"{r.mean() * 1e4:>+8.1f}{r.median() * 1e4:>+8.1f}"
              f"{(r > 0).mean() * 100:>5.0f}%{((1 + r).prod() - 1) * 100:>+9.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
