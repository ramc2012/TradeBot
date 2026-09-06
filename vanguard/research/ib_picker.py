"""Pick monthly winners in the BANKNIFTY group using the large-IB break rule.

mp_initial_balance.py found that a LARGE initial balance, broken to the UPSIDE,
extends about twice as far in percent as a small one (6.2% vs 3.0% median) for
similar heat -- the "small IB gives 3x" folklore being a denominator artefact.
This applies that as a live selection rule and shows it against the truth.

THE RULE, decided entirely at the IB close so nothing is hindsight:
    1. after IB_SESSIONS of the month, rank the group by IB WIDTH (% of price)
    2. take the widest TOP_N as candidates
    3. enter on the first CLOSE above the candidate's IB high
    4. exit at month end

REPORTED PER MONTH: the three names that actually rose most from the IB close,
and the three the rule chose -- with the picks' realised returns and their true
rank, so a pick landing 14th of 17 is visible rather than averaged away.

Universe is BANKNIFTY plus its constituent banks, INDEX INCLUDED, as asked. With
17 names a top-3 slice is ~18% of the group, so the benchmark to beat is the
group mean, not zero -- and it is printed on every row.

    python vanguard/research/ib_picker.py
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


def build(spot: pd.DataFrame) -> pd.DataFrame:
    spot = spot.sort_values(["underlying", "dt"]).copy()
    spot["dt"] = pd.to_datetime(spot["dt"])
    spot["mo"] = spot["dt"].dt.to_period("M")
    rows = []
    for (name, mo), g in spot.groupby(["underlying", "mo"], sort=False):
        g = g.reset_index(drop=True)
        if len(g) < IB_SESSIONS + 5:
            continue
        ib, rest = g.iloc[:IB_SESSIONS], g.iloc[IB_SESSIONS:]
        ib_hi, ib_lo = ib["high"].max(), ib["low"].min()
        ref = ib["close_last"].iloc[-1]
        if ref <= 0 or ib_hi <= ib_lo:
            continue
        up = rest[rest["close_last"] > ib_hi]
        broke_up = len(up) > 0
        entry = float(up["close_last"].iloc[0]) if broke_up else np.nan
        rows.append({
            "underlying": name, "mo": mo,
            "ib_width": (ib_hi - ib_lo) / ref,
            # what holding from the IB close to month end would have made --
            # the decision is taken at the IB close, so this is the honest
            # denominator for "did the pick work"
            "rest_ret": g["close_last"].iloc[-1] / ref - 1.0,
            "broke_up": broke_up,
            # and what entering ON the break would have made
            "break_ret": (g["close_last"].iloc[-1] / entry - 1.0) if broke_up else np.nan,
        })
    return pd.DataFrame(rows)


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

    print(f"universe = BANKNIFTY + {len(BANKS)} banks (index included)   "
          f"names={prof['underlying'].nunique()}")
    print(f"rule: rank by IB width after {IB_SESSIONS} sessions, take widest "
          f"{TOP_N}, enter on close above IB high, exit month end\n")
    print(f"{'month':<9}  {'TOP 3 REAL WINNERS (rest-of-month %)':<44}"
          f"{'PICKED BY IB-WIDTH RULE (ret / rank)':<46}{'grp':>6}")

    stats = []
    for mo, g in prof.groupby("mo"):
        if len(g) < 8:
            continue
        g = g.copy()
        g["rank"] = g["rest_ret"].rank(ascending=False)
        real = g.nlargest(3, "rest_ret")
        picks = g.nlargest(TOP_N, "ib_width")
        grp = g["rest_ret"].mean()

        real_s = "  ".join(f"{r.underlying[:10]}:{r.rest_ret * 100:+.1f}"
                           for r in real.itertuples())
        pick_s = "  ".join(
            f"{r.underlying[:10]}:{r.rest_ret * 100:+.1f}"
            f"({int(r.rank)}/{len(g)}{'' if r.broke_up else ',nb'})"
            for r in picks.itertuples())
        print(f"{str(mo):<9}  {real_s:<44}{pick_s:<46}{grp * 100:>+6.1f}")

        stats.append({"mo": mo, "picked": picks["rest_ret"].mean(),
                      "picked_broke": picks[picks["broke_up"]]["break_ret"].mean(),
                      "grp": grp, "best": real["rest_ret"].iloc[0],
                      "mean_rank": picks["rank"].mean(), "n": len(g)})

    s = pd.DataFrame(stats)
    print(f"\nSUMMARY over {len(s)} months  (nb = never broke IB high)")
    print(f"  picked top-{TOP_N}, held from IB close   mean={s['picked'].mean() * 100:+6.2f}%"
          f"   median={s['picked'].median() * 100:+6.2f}%"
          f"   beat group in {(s['picked'] > s['grp']).mean() * 100:.0f}% of months")
    bk = s["picked_broke"].dropna()
    print(f"  picked, entered ON the break        mean={bk.mean() * 100:+6.2f}%"
          f"   median={bk.median() * 100:+6.2f}%   months={len(bk)}")
    print(f"  group average (benchmark)           mean={s['grp'].mean() * 100:+6.2f}%"
          f"   median={s['grp'].median() * 100:+6.2f}%")
    print(f"  best name in group (ceiling)        mean={s['best'].mean() * 100:+6.2f}%")
    print(f"  average true rank of the picks      {s['mean_rank'].mean():.1f} of "
          f"{s['n'].mean():.0f}   (random would be {(s['n'].mean() + 1) / 2:.1f})")

    # ── ROBUSTNESS ────────────────────────────────────────────────────────
    # Every headline this session that skipped these has collapsed under them.
    d = (s["picked"] - s["grp"]).dropna()          # paired: same month, same market
    t = d.mean() / (d.std(ddof=1) / np.sqrt(len(d))) if d.std(ddof=1) > 0 else np.nan
    half = len(s) // 2
    print(f"\nROBUSTNESS of the picked-minus-group edge ({len(d)} paired months)")
    print(f"  edge                mean={d.mean() * 100:+6.2f}%   t={t:+.2f}"
          f"   (needs |t|>2 to be worth anything)")
    print(f"  drop the best month mean={d.drop(d.idxmax()).mean() * 100:+6.2f}%")
    print(f"  drop best 2 months  mean={d.drop(d.nlargest(2).index).mean() * 100:+6.2f}%")
    print(f"  first half          mean={d.iloc[:half].mean() * 100:+6.2f}%"
          f"      second half mean={d.iloc[half:].mean() * 100:+6.2f}%")

    # The decisive test: does IB width carry ANY cross-sectional rank
    # information? Spearman of ib_width against realised return, per month.
    # This is independent of which slice size is chosen, so it cannot be
    # rescued by tuning TOP_N.
    ics = [g["ib_width"].corr(g["rest_ret"], method="spearman")
           for _, g in prof.groupby("mo") if len(g) >= 8]
    ics = pd.Series([i for i in ics if pd.notna(i)])
    tic = ics.mean() / (ics.std(ddof=1) / np.sqrt(len(ics))) if len(ics) > 2 else np.nan
    print(f"\n  rank IC of IB width vs realised return: mean={ics.mean():+.3f}"
          f"   t={tic:+.2f}   months={len(ics)}   positive in "
          f"{(ics > 0).mean() * 100:.0f}%")

    # And the mirror, so "large IB" is not being credited for a coin flip:
    # if the WIDEST 3 and the NARROWEST 3 perform the same, width is inert.
    narrow = [g.nsmallest(TOP_N, "ib_width")["rest_ret"].mean()
              for _, g in prof.groupby("mo") if len(g) >= 8]
    print(f"  narrowest-{TOP_N} instead of widest:     mean="
          f"{np.mean(narrow) * 100:+6.2f}%   vs widest {s['picked'].mean() * 100:+.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
