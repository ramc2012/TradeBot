"""Per-signal trade ledger for the BANKNIFTY rotation call.

One row per RS-RSI cross: what fired, what was picked, how long it was held, and
what each expression returned. Aggregates hide which signals carried the result
and which were disasters -- with ~21 events in 18 months the ledger IS the
evidence, and a mean over 21 trades is a summary of something small enough to
just read.

PER SIGNAL:
    signal      the session BANKNIFTY/NIFTY's RS-RSI crossed above its own MA
    pick        the bank with the strongest stock/BANKNIFTY momentum that day
    hold        sessions from the cross to the opposite cross (RS-RSI back
                below its MA), capped at MAX_HOLD
    spot %      the picked stock's own return over that hold -- the futures leg
                (stock futures absent from this DB; excludes carry/basis)
    stock CE %  its ATM call, same contract, bought at the cross and held
    BN CE %     the BANKNIFTY ATM call over the same window -- the "just trade
                the index option" alternative

    python vanguard/research/banknifty_ledger.py
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
from research.atm_tail_study import clean, load, pick_atm  # noqa: E402
from research.banknifty_rotation import BANKS, level1, level2  # noqa: E402
from research.overnight_intraday import SESSION_SQL, decompose  # noqa: E402

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
MAX_HOLD = 30


def leg_return(daily: pd.DataFrame, row: pd.Series, exit_idx: int) -> float:
    """Return of ONE contract held from its entry row to `exit_idx`."""
    hit = daily[(daily["underlying"] == row["underlying"])
                & (daily["expiry"] == row["expiry"])
                & (daily["strike"] == row["strike"])
                & (daily["side"] == row["side"])
                & (daily["idx"] == exit_idx)]
    if hit.empty or not row["premium"]:
        return np.nan
    return float(hit.iloc[0]["premium"]) / float(row["premium"]) - 1.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=560)
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    start = date.today() - timedelta(days=args.lookback_days)
    connection = psycopg2.connect(args.dsn)
    try:
        spot_raw = pd.read_sql(SESSION_SQL, connection, params={"start": start})
        daily = load(connection, start)
    finally:
        connection.close()

    spot = decompose(spot_raw)
    l1, l2 = level1(spot), level2(spot)
    atm = pick_atm(clean(daily))
    for f in (l1, l2, atm, spot, daily):
        f["dt"] = pd.to_datetime(f["dt"]).dt.date

    cal = {d: i for i, d in enumerate(sorted(spot["dt"].unique()))}
    inv = {i: d for d, i in cal.items()}
    daily["idx"] = daily["dt"].map(cal)
    atm["idx"] = atm["dt"].map(cal)
    spot_px = spot.set_index(["underlying", "dt"])["close_last"]

    ups = sorted(l1[l1["cross_up"] == True]["dt"])                  # noqa: E712
    downs = sorted(l1[l1["cross_dn"] == True]["dt"])                # noqa: E712

    print(f"window {spot['dt'].min()} .. {spot['dt'].max()}   "
          f"cross-up signals={len(ups)}   cross-down={len(downs)}")
    print("\nPER-SIGNAL LEDGER. 'spot' is the futures stand-in (no stock futures "
          "in this DB).\nCE legs hold ONE contract from the cross to the exit.\n")

    rows = []
    for n, sd in enumerate(ups, 1):
        ei = cal[sd]
        nxt = [cal[d] for d in downs if cal[d] > ei]
        xi = min(nxt[0], ei + MAX_HOLD) if nxt else min(ei + MAX_HOLD, max(inv))
        xd = inv.get(xi)
        if xd is None:
            continue
        hold = xi - ei

        cands = l2[(l2["dt"] == sd) & (l2["underlying"].isin(BANKS))]
        if cands.empty:
            continue
        pick = cands.sort_values("rs_mom", ascending=False).iloc[0]["underlying"]

        entry = atm[(atm["dt"] == sd) & (atm["underlying"] == pick)
                    & (atm["side"] == "CE")]
        bn = atm[(atm["dt"] == sd) & (atm["underlying"] == "BANKNIFTY")
                 & (atm["side"] == "CE")]

        try:
            sp = float(spot_px.loc[(pick, xd)]) / float(spot_px.loc[(pick, sd)]) - 1.0
        except KeyError:
            sp = np.nan
        ce = leg_return(daily, entry.iloc[0], xi) if len(entry) else np.nan
        bnce = leg_return(daily, bn.iloc[0], xi) if len(bn) else np.nan
        dte = int(entry.iloc[0]["dte"]) if len(entry) else -1

        rows.append({"#": n, "signal": str(sd), "pick": pick, "hold": hold,
                     "exit": str(xd), "spot": sp, "stock_ce": ce, "bn_ce": bnce,
                     "dte": dte})
        continue

    led = pd.DataFrame(rows)
    for c in ("spot", "stock_ce", "bn_ce"):
        led[c] = (led[c] * 100).round(1)
    print(led.to_string(index=False, na_rep="-"))

    print(f"\nmedian hold = {led['hold'].median():.0f} sessions   signals={len(led)}")
    for label, col in (("spot/futures", "spot"), ("stock CE", "stock_ce"),
                       ("BANKNIFTY CE", "bn_ce")):
        s = led[col].dropna()
        if s.empty:
            continue
        print(f"    {label:<16} n={len(s):<3} mean={s.mean():+7.1f}%  "
              f"median={s.median():+7.1f}%  win={(s > 0).mean() * 100:4.0f}%  "
              f"best={s.max():+.0f}%  worst={s.min():+.0f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
