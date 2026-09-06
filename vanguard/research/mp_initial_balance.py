"""Market Profile Initial Balance on a MONTHLY profile — does a break run 3x IB?

THE HYPOTHESIS (owner): even a SMALL initial balance, once broken, delivers
roughly a 3x-IB move on the monthly timeframe. Tested first on a deliberately
small universe -- NIFTY, BANKNIFTY and the bank constituents -- before any
expansion.

MP TERMS, mapped from the day session to the month:
    IB          the Initial Balance: on a daily profile the first hour, i.e.
                the first 2 of 13 half-hour periods, ~15% of the session. The
                monthly analogue is the first IB_SESSIONS of the month's
                sessions; 3 of ~21 is the same ~15% share. 1 and 5 are reported
                alongside so the choice is visible rather than assumed.
    IB high/low the extremes of that opening period
    break       a CLOSE beyond the IB extreme, not merely a touch -- an
                auction accepts a level by closing there, and a wick through
                is exactly the failed probe MP treats as rejection
    extension   how far price ran BEYOND the broken extreme, expressed in IB
                RANGES. This is the number the 3x claim is about.

WHAT MAKES OR BREAKS THE CLAIM. Extension in IB multiples is mechanically
inflated when the IB is small -- dividing by a small denominator. So a "small IB
gives 3x" result is uninformative on its own, and the extension is ALSO reported
in PERCENT so the two can be told apart. If small IBs give a big multiple but
the same percentage move, the multiple is an artefact of the denominator, not a
tradeable edge.

Both directions are measured separately, and the failure rate -- breaks that do
not extend at all -- is reported, since a mean extension conditioned on success
would describe only the trades that worked.

    python vanguard/research/mp_initial_balance.py
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
UNIVERSE = ("NIFTY", "BANKNIFTY") + BANKS
IB_CHOICES = (1, 3, 5)


def month_profiles(spot: pd.DataFrame, ib_sessions: int) -> pd.DataFrame:
    spot = spot.sort_values(["underlying", "dt"]).copy()
    spot["dt"] = pd.to_datetime(spot["dt"])
    spot["mo"] = spot["dt"].dt.to_period("M")
    rows = []
    for (name, mo), g in spot.groupby(["underlying", "mo"], sort=False):
        g = g.reset_index(drop=True)
        if len(g) < ib_sessions + 5:          # need a month, not a stub
            continue
        ib = g.iloc[:ib_sessions]
        rest = g.iloc[ib_sessions:]
        ib_hi, ib_lo = ib["high"].max(), ib["low"].min()
        ib_range = ib_hi - ib_lo
        if ib_range <= 0:
            continue
        ref = ib["close_last"].iloc[-1]

        # A break is a CLOSE beyond the extreme; find the first one each way.
        up = rest[rest["close_last"] > ib_hi]
        dn = rest[rest["close_last"] < ib_lo]
        first_up = up.index[0] if len(up) else None
        first_dn = dn.index[0] if len(dn) else None

        rec = {"underlying": name, "mo": mo, "ib_range": ib_range,
               "ib_pct": ib_range / ref, "sessions": len(g),
               "ib_hi": ib_hi, "ib_lo": ib_lo}

        # Whichever side broke FIRST is the one that was tradeable.
        if first_up is not None and (first_dn is None or first_up < first_dn):
            after = rest.loc[first_up:]
            rec.update(side="up", broke=True,
                       ext_mult=(after["high"].max() - ib_hi) / ib_range,
                       ext_pct=(after["high"].max() - ib_hi) / ref,
                       adverse_mult=(ib_hi - after["low"].min()) / ib_range,
                       bars_left=len(after))
        elif first_dn is not None:
            after = rest.loc[first_dn:]
            rec.update(side="down", broke=True,
                       ext_mult=(ib_lo - after["low"].min()) / ib_range,
                       ext_pct=(ib_lo - after["low"].min()) / ref,
                       adverse_mult=(after["high"].max() - ib_lo) / ib_range,
                       bars_left=len(after))
        else:
            rec.update(side="none", broke=False, ext_mult=np.nan,
                       ext_pct=np.nan, adverse_mult=np.nan, bars_left=0)
        rows.append(rec)
    return pd.DataFrame(rows)


def describe(label: str, d: pd.DataFrame) -> None:
    if len(d) < 15:
        print(f"  {label:<34}{len(d):>6}  (too few)")
        return
    e = d["ext_mult"]
    print(f"  {label:<34}{len(d):>6}{e.mean():>9.2f}{e.median():>9.2f}"
          f"{(e >= 1).mean() * 100:>8.0f}{(e >= 2).mean() * 100:>8.0f}"
          f"{(e >= 3).mean() * 100:>8.0f}{d['ext_pct'].median() * 100:>9.1f}"
          f"{d['adverse_mult'].median():>9.2f}")


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
    print(f"universe = NIFTY, BANKNIFTY + {len(BANKS)} banks   "
          f"names present={spot['underlying'].nunique()}")
    print(f"window {spot['dt'].min()} .. {spot['dt'].max()}")

    for ib_n in IB_CHOICES:
        prof = month_profiles(spot, ib_n)
        if prof.empty:
            continue
        broke = prof[prof["broke"]]
        print(f"\n=== IB = first {ib_n} session(s) of the month ===")
        print(f"  monthly profiles={len(prof):,}   broke IB={len(broke):,} "
              f"({len(broke) / len(prof) * 100:.0f}%)   "
              f"median IB width={prof['ib_pct'].median() * 100:.1f}% of price")
        print(f"  {'cohort':<34}{'n':>6}{'mean x':>9}{'med x':>9}"
              f"{'>=1x':>8}{'>=2x':>8}{'>=3x':>8}{'med ext%':>9}{'adverse':>9}")
        describe("ALL breaks", broke)
        for s in ("up", "down"):
            describe(f"{s}-side breaks", broke[broke["side"] == s])
        # THE CLAIM: does a SMALL IB give a bigger multiple -- and is it real?
        q = broke["ib_pct"].quantile([0.33, 0.67])
        describe("SMALL IB (bottom third)", broke[broke["ib_pct"] <= q.iloc[0]])
        describe("LARGE IB (top third)", broke[broke["ib_pct"] >= q.iloc[1]])

    print("\n  'adverse' = how far price went back through the broken extreme,\n"
          "  in IB ranges, before the extension completed — the heat taken.\n"
          "  'med ext%' is the same extension in PERCENT: if SMALL IB shows a\n"
          "  bigger multiple but a similar percent, the multiple is the small\n"
          "  denominator, not an edge.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
