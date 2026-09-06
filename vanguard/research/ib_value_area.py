"""MP selection done properly: initiative vs responsive, not IB width.

ib_picker.py applied the large-IB finding as a selector and it was inert --
rank IC +0.014, t +0.17, positive in 47% of months, i.e. a coin flip. That is
the right answer to the wrong feature. IB WIDTH is a volatility measure; it says
how much room the auction used, not who is in control.

What Market Profile actually claims drives continuation is LOCATION relative to
the prior auction's accepted value:

    value area   the price band holding 70% of the period's volume. Built here
                 from daily bars by spreading each session's volume uniformly
                 across its high-low range into price bins -- an approximation,
                 since true MP needs intraday TPOs, but an unbiased one.
    POC          the single busiest price: where the auction spent most effort.
    INITIATIVE   this month opening ABOVE the prior month's value area high --
                 buyers paying up beyond accepted value, which is the MP
                 definition of initiative buying and the thing said to continue.
    RESPONSIVE   opening back INSIDE prior value: the auction is balanced, and
                 MP expects rotation, not trend.

FEATURES ALL MEASURED AT THE IB CLOSE so nothing is hindsight:
    vs_prior_vah  (IB close - prior VAH) / prior VA width   [initiative]
    vs_prior_poc  (IB close - prior POC) / prior VA width   [location]
    ib_close_pos  where the IB close sits inside its own IB range  [strength]
    ib_width      the inert one, carried along as a control
    prior_ret     prior month return -- plain momentum, as a reference so a
                  "working" MP feature can be checked against the cheap thing

Each is scored the same way: monthly cross-sectional rank IC against realised
rest-of-month return, with a t across months. A feature that cannot beat
prior_ret is not worth the plumbing.

    python vanguard/research/ib_value_area.py
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
from research.overnight_intraday import SESSION_SQL, decompose  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
UNIVERSE = ("BANKNIFTY",) + BANKS
IB_SESSIONS = 3
TOP_N = 3
BINS = 60
VA_SHARE = 0.70
FEATURES = ["vs_prior_vah", "vs_prior_poc", "ib_close_pos", "ib_width", "prior_ret"]


def value_area(g: pd.DataFrame) -> tuple[float, float, float]:
    """POC, VAL, VAH from daily bars, volume spread across each bar's range."""
    lo, hi = g["low"].min(), g["high"].max()
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return np.nan, np.nan, np.nan
    edges = np.linspace(lo, hi, BINS + 1)
    centres = (edges[:-1] + edges[1:]) / 2
    vol = np.zeros(BINS)
    for r in g.itertuples():
        if not np.isfinite(r.high) or not np.isfinite(r.low) or r.high < r.low:
            continue
        v = float(r.volume) if np.isfinite(r.volume) else 0.0
        # bins this session's range touches, volume shared equally among them
        first, last = np.searchsorted(edges, (r.low, r.high)) - (1, 1)
        first, last = max(int(first), 0), min(max(int(last), 0), BINS - 1)
        vol[first:last + 1] += v / (last - first + 1)
    total = vol.sum()
    if total <= 0:
        return np.nan, np.nan, np.nan
    poc = int(vol.argmax())
    lo_i = hi_i = poc
    held = vol[poc]
    # expand from the POC toward whichever side holds more volume, MP's rule
    while held < VA_SHARE * total and (lo_i > 0 or hi_i < BINS - 1):
        below = vol[lo_i - 1] if lo_i > 0 else -1.0
        above = vol[hi_i + 1] if hi_i < BINS - 1 else -1.0
        if above >= below:
            hi_i += 1
            held += above
        else:
            lo_i -= 1
            held += below
    return float(centres[poc]), float(centres[lo_i]), float(centres[hi_i])


def build(spot: pd.DataFrame) -> pd.DataFrame:
    spot = spot.sort_values(["underlying", "dt"]).copy()
    spot["dt"] = pd.to_datetime(spot["dt"])
    spot["mo"] = spot["dt"].dt.to_period("M")
    rows = []
    for name, gn in spot.groupby("underlying", sort=False):
        months = [(mo, g.reset_index(drop=True)) for mo, g in gn.groupby("mo", sort=True)]
        for k in range(1, len(months)):
            mo, g = months[k]
            prev_mo, prev = months[k - 1]
            if len(g) < IB_SESSIONS + 5 or len(prev) < 10:
                continue
            if (mo - prev_mo).n != 1:          # a gap in coverage, not a month
                continue
            poc, val, vah = value_area(prev)
            ib = g.iloc[:IB_SESSIONS]
            ib_hi, ib_lo = ib["high"].max(), ib["low"].min()
            ref = ib["close_last"].iloc[-1]
            if ref <= 0 or ib_hi <= ib_lo or not np.isfinite(vah):
                continue
            va_w = max(vah - val, 1e-9)
            rest = g.iloc[IB_SESSIONS:]
            up = rest[rest["close_last"] > ib_hi]
            rows.append({
                "underlying": name, "mo": mo,
                "vs_prior_vah": (ref - vah) / va_w,
                "vs_prior_poc": (ref - poc) / va_w,
                "ib_close_pos": (ref - ib_lo) / (ib_hi - ib_lo),
                "ib_width": (ib_hi - ib_lo) / ref,
                "prior_ret": prev["close_last"].iloc[-1] / prev["close_last"].iloc[0] - 1.0,
                "rest_ret": g["close_last"].iloc[-1] / ref - 1.0,
                "broke_up": len(up) > 0,
            })
    return pd.DataFrame(rows)


def score(prof: pd.DataFrame, feature: str) -> dict:
    """Monthly rank IC, plus what a top-TOP_N slice on this feature earned."""
    ics, edges = [], []
    for _, g in prof.groupby("mo"):
        if len(g) < 8:
            continue
        ic = g[feature].corr(g["rest_ret"], method="spearman")
        if pd.notna(ic):
            ics.append(ic)
        picks = g.nlargest(TOP_N, feature)
        edges.append(picks["rest_ret"].mean() - g["rest_ret"].mean())
    ics, edges = pd.Series(ics), pd.Series(edges)
    def t(x):
        return x.mean() / (x.std(ddof=1) / np.sqrt(len(x))) if len(x) > 2 and x.std(ddof=1) > 0 else np.nan
    return {"ic": ics.mean(), "t_ic": t(ics), "pos": (ics > 0).mean(),
            "edge": edges.mean(), "t_edge": t(edges),
            "drop2": edges.drop(edges.nlargest(2).index).mean(), "n": len(ics)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=1100)
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    start = date.today() - timedelta(days=args.lookback_days)
    connection = psycopg2.connect(args.dsn)
    try:
        raw = pd.read_sql(SESSION_SQL, connection, params={"start": start})
    finally:
        connection.close()
    spot = decompose(raw)
    spot = spot[spot["underlying"].isin(UNIVERSE)]
    prof = build(spot)
    if prof.empty:
        print("no profiles built")
        return 1

    print(f"universe = BANKNIFTY + {len(BANKS)} banks (index included)   "
          f"names={prof['underlying'].nunique()}   profiles={len(prof)}")
    print(f"prior-month value area = {VA_SHARE:.0%} of volume over {BINS} price bins\n")

    print(f"  {'feature':<16}{'rank IC':>9}{'t(IC)':>8}{'IC>0':>7}"
          f"{'top3 edge':>11}{'t':>7}{'drop2':>9}{'months':>8}")
    ranked = []
    for f in FEATURES:
        s = score(prof, f)
        ranked.append((f, s))
        star = " *" if abs(s["t_ic"]) >= 2 else ""
        print(f"  {f:<16}{s['ic']:>+9.3f}{s['t_ic']:>+8.2f}{s['pos'] * 100:>6.0f}%"
              f"{s['edge'] * 100:>+11.2f}{s['t_edge']:>+7.2f}"
              f"{s['drop2'] * 100:>+9.2f}{s['n']:>8}{star}")

    # MP's own categorical read, which is what the theory actually states.
    print("\nINITIATIVE vs RESPONSIVE (MP's categorical claim)")
    print(f"  {'cohort':<34}{'n':>6}{'mean':>9}{'median':>9}{'win%':>7}")
    cohorts = [
        ("IB closes ABOVE prior VAH", prof["vs_prior_vah"] > 0),
        ("IB closes inside prior value", (prof["vs_prior_vah"] <= 0)
         & (prof["vs_prior_poc"] > -1.0)),
        ("IB closes BELOW prior value", prof["vs_prior_poc"] <= -1.0),
    ]
    for label, mask in cohorts:
        d = prof[mask]["rest_ret"].dropna()
        if len(d) < 15:
            print(f"  {label:<34}{len(d):>6}  (too few)")
            continue
        print(f"  {label:<34}{len(d):>6}{d.mean() * 100:>+9.2f}"
              f"{d.median() * 100:>+9.2f}{(d > 0).mean() * 100:>6.0f}%")
    base = prof["rest_ret"].dropna()
    print(f"  {'ALL profiles (baseline)':<34}{len(base):>6}{base.mean() * 100:>+9.2f}"
          f"{base.median() * 100:>+9.2f}{(base > 0).mean() * 100:>6.0f}%")

    # These 17 names move together, so a raw cohort gap mostly measures WHICH
    # MONTHS the cohort was crowded into -- names close above prior value in
    # strong months, and in strong months everything rises. Demeaning by month
    # strips the market out and leaves only the selection.
    prof["excess"] = prof["rest_ret"] - prof.groupby("mo")["rest_ret"].transform("mean")
    print("\n  same cohorts, DEMEANED BY MONTH (market factor removed)")
    print(f"  {'cohort':<34}{'n':>6}{'excess':>9}{'t':>8}  (t across months)")
    for label, mask in cohorts:
        d = prof[mask]
        if len(d) < 15:
            continue
        # one observation per month = that month's mean excess for the cohort,
        # so correlated names inside a month cannot inflate the count
        per_month = d.groupby("mo")["excess"].mean().dropna()
        t = (per_month.mean() / (per_month.std(ddof=1) / np.sqrt(len(per_month)))
             if len(per_month) > 2 and per_month.std(ddof=1) > 0 else np.nan)
        print(f"  {label:<34}{len(d):>6}{per_month.mean() * 100:>+9.2f}{t:>+8.2f}"
              f"   months={len(per_month)}")

    # Ledger on whichever feature scored best, so the picks are inspectable.
    best = max(ranked, key=lambda kv: abs(kv[1]["t_ic"]) if pd.notna(kv[1]["t_ic"]) else -1)[0]
    print(f"\nMONTHLY LEDGER using the strongest feature: {best}")
    print(f"{'month':<9}  {'TOP 3 REAL WINNERS':<44}{'PICKED (ret / rank)':<46}{'grp':>6}")
    for mo, g in prof.groupby("mo"):
        if len(g) < 8:
            continue
        g = g.copy()
        g["rank"] = g["rest_ret"].rank(ascending=False)
        real = g.nlargest(3, "rest_ret")
        picks = g.nlargest(TOP_N, best)
        real_s = "  ".join(f"{r.underlying[:10]}:{r.rest_ret * 100:+.1f}" for r in real.itertuples())
        pick_s = "  ".join(f"{r.underlying[:10]}:{r.rest_ret * 100:+.1f}"
                           f"({int(r.rank)}/{len(g)}{'' if r.broke_up else ',nb'})"
                           for r in picks.itertuples())
        print(f"{str(mo):<9}  {real_s:<44}{pick_s:<46}{g['rest_ret'].mean() * 100:>+6.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
