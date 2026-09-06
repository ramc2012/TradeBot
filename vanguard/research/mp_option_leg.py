"""The decisive test: is the two-sided IB break monetisable through CE/PE?

Everything before this measured SPOT. An option trader is not paid in spot. The
break move has to clear what the option cost, and the option was priced by
someone who already knew the name's volatility -- which is exactly why atr20
ranking the move is not, on its own, an edge.

THE TRADE, exactly as the owner framed it:
    up-break   -> buy the ATM CE at the 30m close that accepted the break
    down-break -> buy the ATM PE at the same bar
    exit       -> that session's close, or +1 / +2 / +3 sessions

WHAT MAKES THIS AN HONEST TEST RATHER THAN A FLATTERING ONE:
  BOTH LEGS ARE PRICED AT ENTRY. The ATM CE *and* the ATM PE are resolved on
  every break, so the same event yields the traded leg, the WRONG leg, and the
  straddle. The wrong leg is the control that says whether direction selection
  did any work; the straddle is what the trade costs if it did none.
  ENTRY FILTERS ONLY. Premium floor, ATM distance and liquidity are applied to
  the ENTRY bar. Cleaning the exit leg deleted losses in an earlier study and
  manufactured a winner; the exit is taken as it comes, and missing exits are
  counted and inspected rather than quietly dropped.
  EXPIRY HAS TO OUTLIVE THE HORIZON. Contracts expiring inside the holding
  window are excluded at selection, otherwise the result is gamma noise on a
  dying option rather than the move being tested.
  REALISED vs IMPLIED. The option's own iv at entry says what move was already
  paid for. P&L is one view; realised-minus-implied is the view that says
  whether the edge is in the move or merely in the volatility.

    python vanguard/research/mp_option_leg.py
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.banknifty_rotation import BANKS  # noqa: E402
from research.mp_profile import dsn, load  # noqa: E402

UNIVERSE = ("BANKNIFTY",) + BANKS
MIN_PREMIUM = 1.0
MAX_ATM_DIST = 0.03
MIN_EXPIRY_DAYS = 6           # must outlive a 3-session hold plus a weekend
HORIZONS = (0, 1, 2, 3)       # sessions after the break session (0 = same day)

ENTRY_SQL = """
SELECT DISTINCT ON (e.underlying, e.dt, o.option_type)
       e.underlying, e.dt, e.side, e.entry_spot,
       o.option_type, o.expiry, o.strike,
       o.close AS prem, o.iv, o.delta, o.oi, o.volume,
       o.time_to_expiry_years AS tte
FROM ev e
JOIN option_premium_candles o
  ON o.underlying = e.underlying
 AND o.interval = '30minute'
 AND (o.time AT TIME ZONE 'Asia/Kolkata') = e.ts
 AND o.expiry >= e.dt + %(min_exp)s
 AND o.close >= %(min_prem)s
 AND o.volume > 0
 AND ABS(o.strike - e.entry_spot) / e.entry_spot <= %(max_dist)s
ORDER BY e.underlying, e.dt, o.option_type,
         o.expiry ASC, ABS(o.strike - e.entry_spot) ASC
"""

# Session-closing premium for each resolved contract, over the holding window.
EXIT_SQL = """
SELECT c.underlying, c.dt AS ev_dt, c.option_type,
       date(o.time AT TIME ZONE 'Asia/Kolkata') AS dt,
       (array_agg(o.close ORDER BY o.time DESC))[1] AS prem
FROM con c
JOIN option_premium_candles o
  ON o.underlying = c.underlying AND o.interval = '30minute'
 AND o.expiry = c.expiry AND o.strike = c.strike
 AND o.option_type = c.option_type
 AND date(o.time AT TIME ZONE 'Asia/Kolkata') BETWEEN c.dt AND c.dt + 12
 AND (o.time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:15' AND '15:15'
GROUP BY 1, 2, 3, 4
"""


def resolve(connection, ev: pd.DataFrame, min_expiry_days: int = MIN_EXPIRY_DAYS) -> tuple[pd.DataFrame, pd.DataFrame]:
    cur = connection.cursor()
    cur.execute("CREATE TEMP TABLE ev (underlying text, dt date, ts timestamp,"
                " side int, entry_spot numeric) ON COMMIT DROP")
    execute_values(cur, "INSERT INTO ev VALUES %s", [
        (r.underlying, r.dt.date(), r.break_ts.to_pydatetime(), int(r.side),
         float(r.entry)) for r in ev.itertuples()])
    cur.execute(f"CREATE TEMP TABLE con ON COMMIT DROP AS {ENTRY_SQL}",
                {"min_exp": timedelta(days=min_expiry_days),
                 "min_prem": MIN_PREMIUM, "max_dist": MAX_ATM_DIST})
    con = pd.read_sql("SELECT * FROM con", connection)
    exits = pd.read_sql(EXIT_SQL, connection)
    cur.close()
    return con, exits


def build_trades(con: pd.DataFrame, exits: pd.DataFrame) -> pd.DataFrame:
    for f in ("prem", "strike", "entry_spot", "iv", "delta", "tte"):
        con[f] = pd.to_numeric(con[f], errors="coerce")
    exits["prem"] = pd.to_numeric(exits["prem"], errors="coerce")
    con["dt"] = pd.to_datetime(con["dt"])
    exits["ev_dt"], exits["dt"] = pd.to_datetime(exits["ev_dt"]), pd.to_datetime(exits["dt"])

    # rank the sessions that actually traded, per contract, from the event day
    exits = exits.sort_values(["underlying", "ev_dt", "option_type", "dt"])
    exits["h"] = exits.groupby(["underlying", "ev_dt", "option_type"]).cumcount()
    wide = exits.pivot_table(index=["underlying", "ev_dt", "option_type"],
                             columns="h", values="prem").reset_index()
    wide = wide.rename(columns={h: f"exit_{h}" for h in HORIZONS})
    return con.merge(wide, left_on=["underlying", "dt", "option_type"],
                     right_on=["underlying", "ev_dt", "option_type"], how="left")


def describe(label: str, d: pd.DataFrame, col: str) -> None:
    r = d[col].dropna()
    if len(r) < 40:
        print(f"   {label:<30}{len(r):>7}   (too few)")
        return
    print(f"   {label:<30}{len(r):>7}{r.mean() * 100:>+9.1f}{r.median() * 100:>+9.1f}"
          f"{(r > 0).mean() * 100:>7.0f}%{(r >= 0.5).mean() * 100:>8.1f}%"
          f"{(r >= 1.0).mean() * 100:>8.1f}%{r.quantile(0.9) * 100:>+9.0f}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-days", type=int, default=560)
    parser.add_argument("--min-expiry-days", type=int, default=MIN_EXPIRY_DAYS)
    parser.add_argument("--cost", type=float, default=0.02,
                        help="round-trip cost as a fraction of premium")
    parser.add_argument("--dsn", default=dsn())
    args = parser.parse_args()

    start = date.today() - timedelta(days=args.lookback_days)
    connection = psycopg2.connect(args.dsn)
    try:
        s = load(connection, list(UNIVERSE), start)
        ev = s[(s["side"] != 0) & s["break_ts"].notna()].copy()
        print(f"break events from spot structure: {len(ev):,}")
        con, exits = resolve(connection, ev, args.min_expiry_days)
    finally:
        connection.rollback()
        connection.close()

    t = build_trades(con, exits)
    # `side` already rides along from the entry query, so only the spot-side
    # outcome columns are joined -- merging it twice would suffix both copies
    t = t.merge(ev[["underlying", "dt", "mfe_total", "ret_3d", "atr20",
                    "ib_width"]], on=["underlying", "dt"], how="left")
    # the leg the break actually calls for, and its mirror
    t["traded"] = np.where(t["side"] == 1, t["option_type"] == "CE",
                           t["option_type"] == "PE")

    print(f"resolved ATM contracts: {len(t):,} rows covering "
          f"{t.groupby(['underlying', 'dt']).ngroups:,} events "
          f"({t.groupby(['underlying', 'dt']).ngroups / max(len(ev), 1) * 100:.0f}% of breaks)")
    print(f"median days to expiry at entry: {(t['tte'] * 365).median():.0f}   "
          f"median premium {t['prem'].median():.1f}   "
          f"median premium/spot {(t['prem'] / t['entry_spot']).median() * 100:.2f}%")
    miss = t["exit_0"].isna().mean()
    print(f"missing same-session exit: {miss * 100:.1f}%  (dropped per horizon, "
          f"not filtered at entry)")

    for h in HORIZONS:
        t[f"r{h}"] = t[f"exit_{h}"] / t["prem"] - 1.0 - args.cost

    print(f"\nOPTION P&L, cost {args.cost * 100:.0f}% of premium round trip")
    print(f"   {'leg / horizon':<30}{'n':>7}{'mean%':>9}{'med%':>9}"
          f"{'win':>7}{'P(+50%)':>9}{'P(2x)':>8}{'p90%':>9}")
    traded, wrong = t[t["traded"]], t[~t["traded"]]
    for h in HORIZONS:
        describe(f"TRADED leg, +{h} session(s)", traded, f"r{h}")
    print()
    for h in HORIZONS:
        describe(f"WRONG leg (control), +{h}", wrong, f"r{h}")
    print()
    # straddle: both legs bought, so direction selection contributes nothing
    st = t.groupby(["underlying", "dt"]).agg(
        cost=("prem", "sum"), **{f"e{h}": (f"exit_{h}", "sum") for h in HORIZONS},
        legs=("prem", "size")).reset_index()
    st = st[st["legs"] == 2]
    for h in HORIZONS:
        st[f"r{h}"] = st[f"e{h}"] / st["cost"] - 1.0 - args.cost
        describe(f"STRADDLE (no direction), +{h}", st, f"r{h}")

    print(f"\nBY BREAK SIDE, +3 sessions")
    print(f"   {'leg / horizon':<30}{'n':>7}{'mean%':>9}{'med%':>9}"
          f"{'win':>7}{'P(+50%)':>9}{'P(2x)':>8}{'p90%':>9}")
    describe("UP break -> CE", traded[traded["side"] == 1], "r3")
    describe("DOWN break -> PE", traded[traded["side"] == -1], "r3")

    print(f"\nREALISED vs IMPLIED MOVE (what the premium already paid for)")
    d = traded.dropna(subset=["iv", "mfe_total", "ret_3d"]).copy()
    if len(d) > 100:
        # iv is annualised; a 3-session move is iv * sqrt(3/252)
        d["implied_3d"] = d["iv"] * np.sqrt(3 / 252)
        if d["implied_3d"].median() > 1.0:          # iv stored in percent
            d["implied_3d"] /= 100.0
        d["excess_mfe"] = d["mfe_total"] - d["implied_3d"]
        d["excess_ret"] = d["ret_3d"] - d["implied_3d"]
        print(f"   median implied 3-session move {d['implied_3d'].median() * 100:.2f}%"
              f"   median realised MFE {d['mfe_total'].median() * 100:.2f}%"
              f"   median realised ret {d['ret_3d'].median() * 100:.2f}%")
        per = d.groupby("dt")["excess_ret"].mean().dropna()
        tt = per.mean() / (per.std(ddof=1) / np.sqrt(len(per))) if len(per) > 2 else np.nan
        print(f"   realised MOVE minus implied:  MFE {d['excess_mfe'].median() * 100:+.2f}pp"
              f"   directional {d['excess_ret'].median() * 100:+.2f}pp   t={tt:+.2f}"
              f"   ({len(per)} sessions)")
        print("   MFE beating implied is expected — a maximum beats a standard\n"
              "   deviation. The directional line is the one that has to clear it.")

    # Indian single-stock options are MONTHLY, so a 0-3 session hold can be
    # forced into a 30-day contract whose premium has nothing to do with the
    # horizon. Only the indices carry weeklies. If short-dated is the fix, it
    # shows up here.
    print(f"\nBY DAYS TO EXPIRY AT ENTRY, traded leg, +3 sessions")
    print(f"   {'bucket':<30}{'n':>7}{'mean%':>9}{'med%':>9}"
          f"{'win':>7}{'P(+50%)':>9}{'P(2x)':>8}{'p90%':>9}")
    tr = traded.copy()
    tr["dte"] = tr["tte"] * 365
    for lo, hi in ((0, 7), (7, 14), (14, 21), (21, 32), (32, 999)):
        d = tr[(tr["dte"] >= lo) & (tr["dte"] < hi)]
        describe(f"{lo}-{hi} days to expiry", d, "r3")
    print(f"   median premium/spot by bucket: " + "  ".join(
        f"{lo}-{hi}d:{(tr[(tr['dte'] >= lo) & (tr['dte'] < hi)]['prem'] / tr[(tr['dte'] >= lo) & (tr['dte'] < hi)]['entry_spot']).median() * 100:.2f}%"
        for lo, hi in ((0, 7), (7, 14), (14, 21), (21, 32))))

    # The crisp number: how big a spot move does the ATM option need just to get
    # the premium back, and how often does the break actually deliver it?
    print(f"\nBREAKEVEN: the spot move an ATM option needs to return zero")
    d = traded.dropna(subset=["delta", "prem", "entry_spot", "mfe_total"]).copy()
    if len(d) > 100:
        dl = d["delta"].abs().replace(0, np.nan)
        # first-order: premium recovered when |dS| * delta = premium
        d["be_move"] = (d["prem"] / dl) / d["entry_spot"]
        d = d[d["be_move"].between(0.001, 0.30)]
        print(f"   median |delta| {dl.median():.2f}   median breakeven spot move "
              f"{d['be_move'].median() * 100:.2f}%   vs median realised MFE "
              f"{d['mfe_total'].median() * 100:.2f}%")
        print(f"   breaks whose BEST excursion cleared their own breakeven: "
              f"{(d['mfe_total'] >= d['be_move']).mean() * 100:.0f}%"
              f"   ... and at the +3 close: "
              f"{(d['ret_3d'] >= d['be_move']).mean() * 100:.0f}%")
        print("   The first figure uses the maximum, i.e. a perfect exit, so it is\n"
              "   the ceiling. The second is what holding to the horizon delivered.")

    print(f"\nSPLIT-HALF of the traded leg (the whole result rests on ~18 months)")
    tr = traded.sort_values("dt")
    half = len(tr) // 2
    for h in (0, 3):
        a, b = tr.iloc[:half][f"r{h}"].dropna(), tr.iloc[half:][f"r{h}"].dropna()
        print(f"   +{h} session(s):  first half {a.mean() * 100:+6.1f}% (n={len(a)})"
              f"   second half {b.mean() * 100:+6.1f}% (n={len(b)})")
    print(f"\n   drop the 2 best trades, +3 sessions: "
          f"{traded['r3'].dropna().nlargest(2).pipe(lambda x: traded['r3'].dropna().drop(x.index)).mean() * 100:+.1f}%"
          f"   (vs {traded['r3'].mean() * 100:+.1f}% with them)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
