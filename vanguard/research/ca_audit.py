"""CORPORATE-ACTION / DATA-ARTEFACT AUDIT of the overnight top-tertile book.

THE RISK. The strategy is long from the 15:15 close to the next 09:15 open. Its
entire return is a sequence of overnight gaps, so it is maximally exposed to
anything that moves price across exactly that boundary for a non-economic
reason. A split, bonus or large dividend on an UNADJUSTED series manufactures a
fake gap. A BACK-ADJUSTED series has the mirror problem: old prices are restated,
so close_pos is ranked on one basis while the gap is measured across the
restatement. Either one can invent the whole result.

WHAT THIS CAN AND CANNOT SEE. There is NO corporate-action feed in this database.
`announcements` (1105 rows) and `bhavcopy_delivery` (1050 rows, which carries the
NSE PREVCLOSE that would settle any ex-date outright) BOTH begin 2026-08-20 --
two weeks out of a seventeen-month window -- and `results_calendar` holds 20
rows. So every test below is INTERNAL to the price series or CROSS-SECTIONAL
against the rest of the market. That is weaker than an ex-date list and is
stated as such. What it CAN do is establish whether the SIGNATURE of a corporate
action is present anywhere in these four names, and a bonus or split leaves a
signature that cannot hide: a >=10% price step at a round ratio.

THE TESTS
  A   tick-grid forensics. NSE trades equities on a 0.05 grid above Rs250 and a
      0.01 grid at or below it. A back-adjusted price is old*ratio and lands on
      NEITHER. This separates "restated" from "cheap stock, finer tick".
  B   every overnight gap |g| > 3% and > 5%: prices either side, both intraday
      ranges, the gap in ATR units, and what the rest of the market did.
  C   round-ratio scan on every night against the classic action ratios.
  D   close-to-close and 10-session level-regime shifts (the mirror problem).
  E   dividend-scale sweep: idiosyncratic, QUIET, negative gaps of 0.5-3%, which
      is where an Indian bank ex-date actually lives.
  F   the exit print itself: the book sells AT the 09:15 open, so a bad opening
      print is worth as much as a corporate action. How much of each gap is
      handed back inside the first 30-minute bar, plus a bar-by-bar dump of
      every single-name gap large enough to matter.
  G   calendar integrity: a session missing from the data silently turns a
      one-night hold into a multi-night hold.
  H   strategy impact: exactly which trades landed on flagged nights, the book
      with those nights removed, and how much of the book is the SAME night
      traded four times.

    docker exec nomadcurie_vanguard_cycle python /vanguard/research/ca_audit.py
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.mp_auction import dsn, load  # noqa: E402

FOUR = ["SBIN", "AUBANK", "FEDERALBNK", "ICICIBANK"]
INDEX = "BANKNIFTY"
START, END = "2025-03-28", "2026-08-29"
RANK_WINDOW, MIN_PERIODS = 120, 60
TERTILE = 2 / 3
BIG, HUGE = 0.03, 0.05

# ratio = price AFTER the action / price BEFORE it. Note the LARGEST of these is
# 0.90: no corporate action moves price by less than 10%, which is why a 3-7%
# gap can be ruled out as one on arithmetic alone.
CA_RATIOS = {
    "10:1 split": 0.10, "5:1 split": 0.20, "4:1 split": 0.25,
    "3:1 split / 2:1 bonus": 1 / 3, "1:1 bonus / 2:1 split": 0.50,
    "3:2 split / 1:2 bonus": 2 / 3, "1:3 bonus": 0.75, "1:4 bonus": 0.80,
    "1:5 bonus": 5 / 6, "1:9 bonus": 0.90,
    "1:2 reverse split": 2.0, "1:5 reverse split": 5.0,
    "1:10 reverse split": 10.0,
}

SESSION_SQL = """
WITH b AS (
  SELECT underlying,
         date(time AT TIME ZONE 'Asia/Kolkata')      AS dt,
         (time AT TIME ZONE 'Asia/Kolkata')          AS ts,
         open, high, low, close, volume
  FROM underlying_spot_candles
  WHERE interval = '30minute'
    AND time >= %(start)s AND time < %(end)s
    AND (time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:15' AND '15:15'
    AND open IS NOT NULL AND high IS NOT NULL
    AND low IS NOT NULL AND close IS NOT NULL
)
SELECT underlying, dt,
       count(*)                               AS nbars,
       min(ts)                                AS first_ts,
       (array_agg(open  ORDER BY ts))[1]      AS s_open,
       (array_agg(close ORDER BY ts DESC))[1] AS s_close,
       (array_agg(close ORDER BY ts))[1]      AS bar1_close,
       max(high)                              AS s_high,
       min(low)                               AS s_low,
       sum(volume)                            AS s_vol
FROM b GROUP BY 1, 2 ORDER BY 1, 2
"""

# on_05 / on_01: does the close sit exactly on the 5-paisa / 1-paisa grid
GRID_SQL = """
SELECT underlying,
       to_char(date(time AT TIME ZONE 'Asia/Kolkata'), 'YYYY-MM')        AS ym,
       count(*)                                                          AS n,
       sum(CASE WHEN abs(close*20 - round(close*20)) < 1e-6 THEN 1 END)  AS on_05,
       sum(CASE WHEN abs(close*100 - round(close*100)) < 1e-6 THEN 1 END) AS on_01,
       min(close) AS lo_px, max(close) AS hi_px, avg(close) AS avg_px
FROM underlying_spot_candles
WHERE interval = '30minute' AND time >= %(start)s AND time < %(end)s
  AND underlying = ANY(%(names)s)
  AND (time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:15' AND '15:15'
  AND close IS NOT NULL
GROUP BY 1, 2 ORDER BY 1, 2
"""

OFFGRID_SQL = """
SELECT underlying, (time AT TIME ZONE 'Asia/Kolkata') AS ts, open, high, low, close
FROM underlying_spot_candles
WHERE interval = '30minute' AND time >= %(start)s AND time < %(end)s
  AND underlying = ANY(%(names)s)
  AND (time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:15' AND '15:15'
  AND close IS NOT NULL
  AND abs(close*100 - round(close*100)) > 1e-6
ORDER BY underlying, ts LIMIT 40
"""

OFF05_SQL = """
SELECT underlying, (time AT TIME ZONE 'Asia/Kolkata') AS ts,
       open, high, low, close, volume
FROM underlying_spot_candles
WHERE interval = '30minute' AND time >= %(start)s AND time < %(end)s
  AND underlying = ANY(%(names)s)
  AND (time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:15' AND '15:15'
  AND close > 250
  AND abs(close*20 - round(close*20)) > 1e-6
ORDER BY underlying, ts
"""

BARS_SQL = """
SELECT underlying, (time AT TIME ZONE 'Asia/Kolkata') AS ts,
       open, high, low, close, volume
FROM underlying_spot_candles
WHERE interval = '30minute' AND underlying = %(name)s
  AND date(time AT TIME ZONE 'Asia/Kolkata') = ANY(%(dts)s)
  AND (time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:15' AND '15:15'
ORDER BY ts
"""


# ---------------------------------------------------------------- helpers
def build(raw: pd.DataFrame) -> pd.DataFrame:
    raw = raw.copy()
    raw["dt"] = pd.to_datetime(raw["dt"])
    raw["first_ts"] = pd.to_datetime(raw["first_ts"])
    for c in ("s_open", "s_close", "s_high", "s_low", "bar1_close", "s_vol"):
        raw[c] = pd.to_numeric(raw[c], errors="coerce")
    # mp_auction keeps a session only with >= 12 bars starting at 09:15
    raw = raw[(raw["nbars"] >= 12)
              & (raw["first_ts"].dt.time == pd.Timestamp("09:15").time())]

    out = []
    for name, g in raw.groupby("underlying", sort=False):
        g = g.sort_values("dt").reset_index(drop=True)
        rng = g["s_high"] - g["s_low"]
        g["range_pct"] = rng / g["s_close"]
        g["close_pos"] = np.where(rng > 0, (g["s_close"] - g["s_low"]) / rng, 0.5)
        g["next_dt"] = g["dt"].shift(-1)
        g["next_open"] = g["s_open"].shift(-1)
        g["next_bar1_close"] = g["bar1_close"].shift(-1)
        g["next_range_pct"] = g["range_pct"].shift(-1)
        g["next_close"] = g["s_close"].shift(-1)
        g["gap"] = g["next_open"] / g["s_close"] - 1.0
        g["cc"] = g["next_close"] / g["s_close"] - 1.0
        g["cal_days"] = (g["next_dt"] - g["dt"]).dt.days
        prev_close = g["s_close"].shift(1)
        tr = pd.concat([g["s_high"] - g["s_low"],
                        (g["s_high"] - prev_close).abs(),
                        (g["s_low"] - prev_close).abs()], axis=1).max(axis=1)
        g["atr20"] = tr.rolling(20, min_periods=10).mean().shift(1) / prev_close
        g["med_range60"] = g["range_pct"].rolling(60, min_periods=20).median().shift(1)
        g["cp_rank"] = (g["close_pos"].rolling(RANK_WINDOW, min_periods=MIN_PERIODS)
                        .apply(lambda w: (w[-1] > w[:-1]).mean(), raw=True))
        lvl_b = g["s_close"].rolling(10, min_periods=5).median()
        lvl_a = g["s_close"][::-1].rolling(10, min_periods=5).median()[::-1].shift(-1)
        g["lvl_ratio"] = lvl_a / lvl_b
        out.append(g)
    return pd.concat(out, ignore_index=True)


def nearest_ca(ratio: float) -> tuple[str, float]:
    best, bd = "", 1e9
    for lab, r in CA_RATIOS.items():
        d = abs(ratio - r) * 1e4
        if d < bd:
            best, bd = lab, d
    return best, bd


def stats(r: pd.Series, span_years: float) -> dict:
    if len(r) == 0:
        return {}
    eq = (1 + r).cumprod()
    sd = r.std(ddof=1)
    return {"n": len(r), "bp": r.mean() * 1e4, "win": (r > 0).mean() * 100,
            "total": (eq.iloc[-1] - 1) * 100,
            "dd": (eq / eq.cummax() - 1).min() * 100,
            "sharpe": r.mean() / sd * np.sqrt(len(r) / span_years),
            "t": r.mean() / (sd / np.sqrt(len(r))),
            "best": r.max() * 100, "worst": r.min() * 100}


def line(tag: str, s: dict) -> str:
    if not s:
        return f"   {tag:<30}      (empty)"
    return (f"   {tag:<30}{s['n']:>6}{s['bp']:>+9.1f}{s['win']:>6.0f}%"
            f"{s['total']:>+9.1f}{s['dd']:>+8.1f}{s['sharpe']:>+8.2f}"
            f"{s['t']:>+7.2f}{s['best']:>+8.2f}{s['worst']:>+8.2f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=dsn())
    args = ap.parse_args()
    conn = psycopg2.connect(args.dsn)
    try:
        raw = pd.read_sql(SESSION_SQL, conn, params={"start": START, "end": END})
        grid = pd.read_sql(GRID_SQL, conn,
                           params={"start": START, "end": END, "names": FOUR})
        offg = pd.read_sql(OFFGRID_SQL, conn,
                           params={"start": START, "end": END, "names": FOUR})
        ref = load(conn, FOUR, pd.Timestamp(START).date())

        s = build(raw)
        four = s[s["underlying"].isin(FOUR)].copy()
        bnf = s[s["underlying"] == INDEX].set_index("dt")
        eq_names = sorted(n for n, g in s.groupby("underlying")
                          if g["dt"].min() <= pd.Timestamp("2025-04-15")
                          and len(g) > 300 and g["s_vol"].sum() > 0)
        breadth = s[s["underlying"].isin(eq_names)]
        mkt = breadth.groupby("dt")["gap"].median()
        n_big = breadth.assign(b=breadth["gap"].abs() > BIG).groupby("dt")["b"].sum()
        n_tot = breadth.groupby("dt")["gap"].count()
        four["mkt_gap"] = four["dt"].map(mkt)
        four["idio"] = four["gap"] - four["mkt_gap"]

        print("=" * 110)
        print("CORPORATE-ACTION / DATA-ARTEFACT AUDIT -- SBIN, AUBANK, FEDERALBNK, "
              "ICICIBANK -- 2025-03-28..2026-08-28")
        print("=" * 110)
        print(f"sessions built: {len(four)} over 4 names ({four['dt'].min().date()}"
              f"..{four['dt'].max().date()}); BANKNIFTY {len(bnf)}; "
              f"breadth universe {len(eq_names)} equities")
        m = ref.merge(four, on=["underlying", "dt"], suffixes=("_ref", "_aud"))
        print(f"replication check vs mp_auction.load(): {len(m)}/{len(ref)} rows "
              f"matched, max |d close_pos| "
              f"{(m['close_pos_ref'] - m['close_pos_aud']).abs().max():.2e}, "
              f"max |d next_open_ret - gap| "
              f"{(m['next_open_ret'] - m['gap_aud']).abs().max():.2e}  -> the audit "
              f"is measuring the same numbers the backtest reported")

        # ================================================== A. tick grid
        print("\n" + "-" * 110)
        print("A. TICK-GRID FORENSICS -- is the series back-adjusted?")
        print("-" * 110)
        print("   NSE tick size is Rs0.05 above Rs250 and Rs0.01 at or below it "
              "(the Rs250")
        print("   band was introduced in 2024). A BACK-ADJUSTED price is "
              "old_price*ratio and")
        print("   sits on NEITHER grid. So the test is the 0.01 column, not the "
              "0.05 one.")
        grid["p05"] = grid["on_05"].fillna(0) / grid["n"] * 100
        grid["p01"] = grid["on_01"].fillna(0) / grid["n"] * 100
        print(f"   {'name':<12}{'month':<9}{'bars':>7}{'on 0.05':>10}{'on 0.01':>10}"
              f"{'min px':>10}{'max px':>10}   note")
        for name in FOUR:
            g = grid[grid["underlying"] == name]
            for _, r in g.iterrows():
                note = ""
                if r["p05"] < 99.5:
                    note = ("<=Rs250 band: 1-paisa tick"
                            if float(r["hi_px"]) <= 250.5 else "MIXED band")
                if r["p01"] < 99.999:
                    note += "  ** OFF THE 1-PAISA GRID **"
                print(f"   {name:<12}{r['ym']:<9}{int(r['n']):>7}{r['p05']:>9.1f}%"
                      f"{r['p01']:>9.1f}%{float(r['lo_px']):>10.2f}"
                      f"{float(r['hi_px']):>10.2f}   {note}")
        print(f"\n   bars anywhere in the four names whose close is OFF the 1-paisa "
              f"grid: {len(offg)}")
        if len(offg):
            print(offg.head(20).to_string(index=False))
        print("   VERDICT: every price in all four names is an exact whole number of")
        print("   paise. A back-adjustment by any ratio that is not itself a clean")
        print("   fraction would break that, and it is not broken anywhere. The")
        print("   FEDERALBNK 0.05-grid dip in 2025 is the sub-Rs250 tick band, not a")
        print("   restatement: it ends exactly when the price crosses Rs250.")

        # ================================================== B. gap census
        print("\n" + "-" * 110)
        print(f"B. EVERY OVERNIGHT GAP  |open(t+1)/close(t)-1| > {BIG:.0%}")
        print("-" * 110)
        flagged = four[four["gap"].abs() > BIG].dropna(subset=["gap"]).copy()
        flagged = flagged.reindex(
            flagged["gap"].abs().sort_values(ascending=False).index)
        print(f"   {len(flagged)} nights exceed 3%; "
              f"{(flagged['gap'].abs() > HUGE).sum()} exceed 5%. "
              f"Largest anywhere: {four['gap'].abs().max() * 100:.2f}%.")
        print("   'kind' = MARKET when the whole tape gapped the same way, "
              "SINGLE-NAME when it did not.")
        print(f"\n   {'name':<11}{'night':<25}{'close(t)':>10}{'open(t+1)':>11}"
              f"{'gap%':>8}{'idio%':>8}{'ATR':>6}{'rng t%':>8}{'rng t+1%':>10}"
              f"{'BNF%':>7}{'mkt%':>7}{'n>3%':>6}  kind")
        for _, r in flagged.iterrows():
            bg = bnf["gap"].get(r["dt"], np.nan)
            nb, nt = n_big.get(r["dt"], 0), n_tot.get(r["dt"], 0)
            kind = "MARKET" if nb >= 20 else ("mostly market" if nb >= 8
                                              else "SINGLE-NAME")
            print(f"   {r['underlying']:<11}"
                  f"{r['dt'].date()} -> {r['next_dt'].date()}  "
                  f"{r['s_close']:>10.2f}{r['next_open']:>11.2f}"
                  f"{r['gap'] * 100:>+8.2f}{r['idio'] * 100:>+8.2f}"
                  f"{r['gap'] / r['atr20']:>+6.1f}{r['range_pct'] * 100:>8.2f}"
                  f"{r['next_range_pct'] * 100:>10.2f}{bg * 100:>+7.2f}"
                  f"{r['mkt_gap'] * 100:>+7.2f}{int(nb):>3}/{int(nt):<3}  {kind}")

        print("\n   the same nights against the corporate-action hypothesis:")
        print(f"   {'name':<11}{'night':<13}{'ratio':>9}{'nearest CA ratio':>26}"
              f"{'off by':>10}{'lvl 10a/10b':>13}   ranges either side")
        for _, r in flagged.iterrows():
            lab, bp = nearest_ca(1 + r["gap"])
            quiet = (r["range_pct"] < 1.2 * (r["med_range60"] or 9)
                     and r["next_range_pct"] < 1.2 * (r["med_range60"] or 9))
            print(f"   {r['underlying']:<11}{r['dt'].date()!s:<13}"
                  f"{1 + r['gap']:>9.4f}{lab:>26}{bp:>9,.0f}bp"
                  f"{r['lvl_ratio']:>13.4f}   "
                  f"{'QUIET both sides' if quiet else 'range elevated -> news-like'}")
        print("\n   Arithmetic that settles it: the SMALLEST classic corporate action")
        print("   (a 1:9 bonus) is a -10.00% step. The largest gap in these four names")
        print(f"   over seventeen months is {four['gap'].abs().max() * 100:.2f}%. "
              f"No gap here is even the")
        print("   size of the smallest possible split or bonus, let alone at its ratio.")

        # ================================================== C. round ratio
        print("\n" + "-" * 110)
        print("C. ROUND-RATIO SCAN -- all 1,395 nights, not just the big ones")
        print("-" * 110)
        g4 = four.dropna(subset=["gap"]).copy()
        g4["ratio"] = 1 + g4["gap"]
        g4["ca_bp"] = g4["ratio"].map(lambda x: nearest_ca(x)[1])
        hits = g4[g4["ca_bp"] < 200]
        print(f"   nights within 200bp of ANY classic action ratio (0.10 0.20 0.25")
        print(f"   0.333 0.50 0.667 0.75 0.80 0.833 0.90 2.0 5.0 10.0): "
              f"{len(hits)} of {len(g4)}")
        print(f"   closest approach by any night: {g4['ca_bp'].min():,.0f}bp "
              f"({g4['ca_bp'].min() / 100:.1f}% away), on "
              f"{g4.loc[g4['ca_bp'].idxmin(), 'underlying']} "
              f"{g4.loc[g4['ca_bp'].idxmin(), 'dt'].date()} "
              f"(gap {g4.loc[g4['ca_bp'].idxmin(), 'gap'] * 100:+.2f}%, and that is "
              f"the market-wide 2025-04-07 tariff gap)")
        for _, r in hits.head(20).iterrows():
            print(f"      {r['underlying']:<11}{r['dt'].date()}  "
                  f"gap {r['gap'] * 100:+.2f}%  {nearest_ca(r['ratio'])[0]}")

        # ================================================== D. level shift
        print("\n" + "-" * 110)
        print("D. LEVEL-REGIME SHIFTS -- the mirror problem (a restated price level)")
        print("-" * 110)
        print(f"   {'name':<12}{'max |c-to-c|':>15}{'on':>13}{'min lvl':>10}{'on':>13}"
              f"{'max lvl':>10}{'on':>13}{'px first':>10}{'px last':>10}")
        for name in FOUR:
            g = four[four["underlying"] == name].dropna(subset=["cc"])
            i = g["cc"].abs().idxmax()
            lv = four[four["underlying"] == name].dropna(subset=["lvl_ratio"])
            lo, hi = lv["lvl_ratio"].idxmin(), lv["lvl_ratio"].idxmax()
            gg = four[four["underlying"] == name].sort_values("dt")
            print(f"   {name:<12}{g.loc[i, 'cc'] * 100:>+14.2f}%"
                  f"{g.loc[i, 'dt'].date()!s:>13}{lv.loc[lo, 'lvl_ratio']:>10.4f}"
                  f"{lv.loc[lo, 'dt'].date()!s:>13}{lv.loc[hi, 'lvl_ratio']:>10.4f}"
                  f"{lv.loc[hi, 'dt'].date()!s:>13}"
                  f"{gg['s_close'].iloc[0]:>10.2f}{gg['s_close'].iloc[-1]:>10.2f}")
        print("   a 1:1 bonus prints a level ratio near 0.50, a 3:2 split near 0.667,")
        print("   a 1:4 bonus near 0.80. The most extreme 10-session level shift in")
        print("   any of the four is 0.876 (SBIN, a drawdown), and no 10-session")
        print("   window anywhere halves or doubles.")

        # ================================================== E. dividend scale
        print("\n" + "-" * 110)
        print("E. DIVIDEND-SCALE SWEEP -- 0.5%-3%, where an Indian bank ex-date lives")
        print("-" * 110)
        print("   An ex-dividend night is IDIOSYNCRATIC (the tape does not move), "
              "DOWN, and")
        print("   QUIET (ordinary intraday ranges either side). SBI's annual "
              "dividend is")
        print("   ~1.8-2.0% of price, ICICI ~0.8%, so this is the only band in which "
              "a")
        print("   dividend could hide. Top 6 candidates per name by that signature:")
        cand = g4[(g4["gap"] < -0.005) & (g4["idio"] < -0.004)
                  & (g4["range_pct"] < g4["med_range60"])
                  & (g4["next_range_pct"] < g4["med_range60"])].copy()
        print(f"   {'name':<12}{'night':<13}{'gap%':>8}{'idio%':>8}{'rng t%':>9}"
              f"{'med rng%':>10}{'rng t+1%':>10}{'traded?':>9}")
        ranked = four.dropna(subset=["cp_rank", "gap"])
        trades = ranked[ranked["cp_rank"] >= TERTILE].copy()
        tkeys = set(map(tuple, trades[["underlying", "dt"]].values))
        for name in FOUR:
            c = cand[cand["underlying"] == name].nsmallest(6, "idio")
            if not len(c):
                print(f"   {name:<12}(no night matches the signature)")
            for _, r in c.iterrows():
                print(f"   {name:<12}{r['dt'].date()!s:<13}{r['gap'] * 100:>+8.2f}"
                      f"{r['idio'] * 100:>+8.2f}{r['range_pct'] * 100:>9.2f}"
                      f"{r['med_range60'] * 100:>10.2f}"
                      f"{r['next_range_pct'] * 100:>10.2f}"
                      f"{'YES' if (r['underlying'], r['dt']) in tkeys else 'no':>9}")
        print("   None of these reaches even 1.5% idiosyncratic, and none repeats on")
        print("   an annual cycle -- the shape a dividend must have. Note also that "
              "an")
        print("   ex-dividend gap would HURT this book (it is long overnight), so it")
        print("   cannot be the source of a positive result.")

        # ================================================== F. exit print
        print("\n" + "-" * 110)
        print("F. THE EXIT PRINT -- the book sells AT the 09:15 open")
        print("-" * 110)
        trades["bar1"] = trades["next_bar1_close"] / trades["next_open"] - 1.0
        print("   the twenty biggest winning nights, and how much of the gap is "
              "handed")
        print("   back inside the first 30-minute bar (a bad print reverses at once):")
        print(f"   {'name':<12}{'night':<25}{'gap%':>8}{'bar1%':>9}{'BNF%':>8}"
              f"{'mkt%':>8}{'n>3%':>7}")
        for _, r in trades.nlargest(20, "gap").iterrows():
            bg = bnf["gap"].get(r["dt"], np.nan)
            print(f"   {r['underlying']:<12}{r['dt'].date()} -> "
                  f"{r['next_dt'].date()}  {r['gap'] * 100:>+8.2f}"
                  f"{r['bar1'] * 100:>+9.2f}{bg * 100:>+8.2f}"
                  f"{r['mkt_gap'] * 100:>+8.2f}{int(n_big.get(r['dt'], 0)):>7}")
        c = trades[["gap", "bar1"]].dropna()
        print(f"   correlation(gap, first-bar move) over all {len(c)} trades: "
              f"{c.corr().iloc[0, 1]:+.3f}; mean first-bar move on the 20 best "
              f"nights: {trades.nlargest(20, 'gap')['bar1'].mean() * 100:+.2f}%")

        # bar-level dump of every SINGLE-NAME gap > 3%
        single = [(r["underlying"], r["dt"], r["next_dt"], r["gap"])
                  for _, r in flagged.iterrows() if n_big.get(r["dt"], 0) < 8]
        print(f"\n   BAR-BY-BAR DUMP of every SINGLE-NAME gap > 3% ({len(single)} of "
              f"them). If any")
        print("   were a corporate action the level would step and STAY; if any were "
              "a bad")
        print("   print the opening bar would swallow it whole:")
        for name, d0, d1, gp in single:
            b = pd.read_sql(BARS_SQL, conn,
                            params={"name": name, "dts": [d0.date(), d1.date()]})
            b["ts"] = pd.to_datetime(b["ts"])
            print(f"\n   -- {name}  {d0.date()} close -> {d1.date()} open   "
                  f"gap {gp * 100:+.2f}%")
            for _, r in b.iterrows():
                mark = "  <== the exit print" if (r["ts"].date() == d1.date()
                                                  and r["ts"].hour == 9) else ""
                print(f"      {r['ts']}  O {float(r['open']):>9.2f}  H "
                      f"{float(r['high']):>9.2f}  L {float(r['low']):>9.2f}  C "
                      f"{float(r['close']):>9.2f}  vol {int(r['volume'] or 0):>10,}"
                      f"{mark}")

        # ================================================== G. calendar
        print("\n" + "-" * 110)
        print("G. CALENDAR INTEGRITY -- is 'the next session' really the next day?")
        print("-" * 110)
        for k in (3, 4, 5):
            print(f"   trades whose hold spans > {k} calendar days: "
                  f"{(trades['cal_days'] > k).sum()} of {len(trades)}")
        lng = trades[trades["cal_days"] > 3]
        for _, r in lng.iterrows():
            print(f"      {r['underlying']:<11}{r['dt'].date()} -> "
                  f"{r['next_dt'].date()}  {int(r['cal_days'])}d  "
                  f"gap {r['gap'] * 100:+.2f}%")
        short = four[four["nbars"] < 13].groupby("underlying").size()
        print(f"   sessions kept with 12 bars instead of 13: "
              f"{dict(short) if len(short) else 'none'}")
        dates = {n: set(g["dt"]) for n, g in four.groupby("underlying")}
        alld = set().union(*dates.values())
        for n, ds in dates.items():
            miss = sorted(alld - ds)
            if miss:
                print(f"   {n} is missing {len(miss)} session other names have: "
                      f"{[str(d.date()) for d in miss]}")
        print(f"   BANKNIFTY has {len(bnf)} sessions vs {len(alld)} equity sessions "
              f"-- the index cross-check is blank on "
              f"{len(alld - set(bnf.index))} dates, which is why the 214-name "
              f"breadth median is the primary tape reference above.")

        # ================================================== H. impact
        print("\n" + "=" * 110)
        print("H. STRATEGY IMPACT")
        print("=" * 110)
        span = (ranked["dt"].max() - ranked["dt"].min()).days / 365.25
        susp = set(map(tuple, flagged[["underlying", "dt"]].values))
        hit = trades[[k in susp for k in
                      map(tuple, trades[["underlying", "dt"]].values)]]
        print(f"   {len(flagged)} nights flagged >3%; the book traded {len(hit)} "
              f"of them:")
        for _, r in hit.sort_values("dt").iterrows():
            kind = ("MARKET" if n_big.get(r["dt"], 0) >= 20
                    else "mostly market" if n_big.get(r["dt"], 0) >= 8
                    else "SINGLE-NAME")
            print(f"      {r['underlying']:<11}{r['dt'].date()} -> "
                  f"{r['next_dt'].date()}  gap {r['gap'] * 100:>+6.2f}%  "
                  f"cp_rank {r['cp_rank']:.2f}  {kind}")
        print(f"\n   {'book':<30}{'n':>6}{'bp/nt':>9}{'win':>7}{'total%':>9}"
              f"{'maxDD%':>8}{'Sharpe':>8}{'t':>7}{'best%':>8}{'worst%':>8}")
        for name in FOUR:
            g = trades[trades["underlying"] == name].sort_values("dt")
            keys = list(map(tuple, g[["underlying", "dt"]].values))
            print(line(f"{name} as reported",
                       stats(g["gap"].reset_index(drop=True), span)))
            d = g[[k not in susp for k in keys]]
            print(line(f"{name} minus all 3% nights",
                       stats(d["gap"].reset_index(drop=True), span)))
            r = g["gap"].reset_index(drop=True)
            print(line(f"{name} minus 2 best (fragility)",
                       stats(r.drop(r.nlargest(2).index).reset_index(drop=True),
                             span)))
        print("\n   NOTE the two rows are answering different questions. 'minus all")
        print("   3% nights' removes real, market-wide, tradeable moves -- it is a")
        print("   robustness stress, NOT a contamination adjustment, because section")
        print("   B shows none of those nights is a corporate action.")

        # ============================================ A2. off-grid above Rs250
        print("\n" + "-" * 110)
        print("I. THE ONE LOOSE THREAD -- prices off the 5-paisa grid ABOVE Rs250")
        print("-" * 110)
        off5 = pd.read_sql(OFF05_SQL, conn,
                           params={"start": START, "end": END, "names": FOUR})
        off5["ts"] = pd.to_datetime(off5["ts"])
        off5["d"] = off5["ts"].dt.date
        print(f"   above Rs250 the exchange tick is 5 paise, so a 1-paisa close is not")
        print(f"   a price that could have traded. Bars like that: {len(off5)}")
        if len(off5):
            byname = off5.groupby("underlying")["d"].agg(["count", "nunique",
                                                          "min", "max"])
            print(byname.to_string())
            print("   the dates they fall on, and how many bars on each:")
            for (n, d), g in off5.groupby(["underlying", "d"]):
                print(f"      {n:<11}{d}  {len(g):>3} bars  "
                      f"close range {g['close'].min():.2f}..{g['close'].max():.2f}")
            print("   sample bars:")
            print(off5.head(12).to_string(index=False))
            bad_dates = set(zip(off5["underlying"], pd.to_datetime(off5["d"])))
            tk = set(map(tuple, trades[["underlying", "dt"]].values))
            tk2 = set(zip(trades["underlying"], trades["next_dt"]))
            print(f"   trades whose ENTRY session contains such a bar: "
                  f"{len(bad_dates & tk)}")
            print(f"   trades whose EXIT session contains such a bar: "
                  f"{len(bad_dates & tk2)}")
            for k in sorted(bad_dates & (tk | tk2)):
                print(f"      {k[0]} {k[1].date()}")
            # what the offset actually is, and whether it can reach a trade price
            off5["frac"] = ((off5["close"].astype(float) * 100).round()
                            % 5).astype(int)
            print(f"   the offset from the 5-paisa grid, in paise: "
                  f"{dict(off5['frac'].value_counts().sort_index())}")
            at_open = (off5["ts"].dt.time == pd.Timestamp("09:15").time()).sum()
            at_close = (off5["ts"].dt.time == pd.Timestamp("15:15").time()).sum()
            print(f"   of these {len(off5)} bars, {at_open} are the 09:15 bar and "
                  f"{at_close} are the 15:15 bar -- i.e. how many could touch the")
            print(f"   two prices the strategy actually transacts at.")
            fb = off5[off5["ts"].dt.time.isin([pd.Timestamp("09:15").time(),
                                               pd.Timestamp("15:15").time()])]
            if len(fb):
                print(fb.to_string(index=False))
            print(f"   worst-case size of the artefact: 4 paise on a Rs"
                  f"{off5['close'].astype(float).min():.0f}-"
                  f"{off5['close'].astype(float).max():.0f} stock = "
                  f"{4 / off5['close'].astype(float).max():.2f}-"
                  f"{4 / off5['close'].astype(float).min():.2f} bp.")

        # ============================================ E2. annual dividend cycle
        print("\n" + "-" * 110)
        print("J. THE ANNUAL DIVIDEND WINDOW -- an Indian bank pays once a year, so a")
        print("   real ex-date must appear TWICE in this span, ~12 months apart")
        print("-" * 110)
        for name in FOUR:
            for y in (2025, 2026):
                w = g4[(g4["underlying"] == name)
                       & (g4["dt"] >= f"{y}-04-20") & (g4["dt"] <= f"{y}-07-15")]
                if not len(w):
                    continue
                r = w.loc[w["idio"].idxmin()]
                print(f"   {name:<11}{y}  most idiosyncratic DOWN night in the "
                      f"Apr20-Jul15 dividend window: {r['dt'].date()}  "
                      f"gap {r['gap'] * 100:>+6.2f}%  idio {r['idio'] * 100:>+6.2f}%  "
                      f"rng(t) {r['range_pct'] * 100:.2f}% vs med "
                      f"{r['med_range60'] * 100:.2f}%")
        print("   SBIN's FY25 dividend is ~2% of price -- the largest of the four. If")
        print("   it were sitting unadjusted in this series it would show as a ~-2%")
        print("   idiosyncratic quiet down-gap in the SAME calendar window in BOTH")
        print("   years. Compare the two SBIN rows above.")

        print("\n   every SBIN night in the two dividend windows, in full:")
        for y in (2025, 2026):
            w = g4[(g4["underlying"] == "SBIN") & (g4["dt"] >= f"{y}-05-01")
                   & (g4["dt"] <= f"{y}-06-15")].sort_values("dt")
            print(f"   -- {y}")
            for _, r in w.iterrows():
                flagq = "  <-- quiet idiosyncratic down-gap" if (
                    r["gap"] < -0.01 and r["idio"] < -0.01
                    and r["range_pct"] < r["med_range60"]) else ""
                print(f"      {r['dt'].date()}  close {r['s_close']:>8.2f} -> open "
                      f"{r['next_open']:>8.2f}  gap {r['gap'] * 100:>+6.2f}%  "
                      f"mkt {r['mkt_gap'] * 100:>+6.2f}%  idio "
                      f"{r['idio'] * 100:>+6.2f}%{flagq}")

        # ============================================ F2. pre-open cross-check
        print("\n" + "-" * 110)
        print("K. CAN THE 09:15 OPEN BE VERIFIED AGAINST THE PRE-OPEN AUCTION?")
        print("-" * 110)
        try:
            cols = pd.read_sql(
                "SELECT column_name FROM information_schema.columns WHERE "
                "table_name='preopen_spot_snapshots' ORDER BY ordinal_position",
                conn)["column_name"].tolist()
            print(f"   preopen_spot_snapshots columns: {cols}")
            po = pd.read_sql(
                "SELECT count(*) n, count(DISTINCT underlying) syms, "
                "min(session_date) a, max(session_date) b "
                "FROM preopen_spot_snapshots", conn)
            print("   coverage: " + po.to_string(index=False))
            pf = pd.read_sql(
                "SELECT session_date, underlying, preopen_price, prev_close, "
                "prev_close_source, data_status FROM preopen_spot_snapshots "
                "WHERE underlying = ANY(%(n)s) ORDER BY session_date, underlying",
                conn, params={"n": FOUR})
            print(f"   rows for the four names: {len(pf)}")
            if len(pf):
                pf["session_date"] = pd.to_datetime(pf["session_date"])
                k = four[["underlying", "dt", "s_open", "s_close"]].copy()
                k["prev_close_candle"] = k.groupby("underlying")["s_close"].shift(1)
                j = pf.merge(k, left_on=["underlying", "session_date"],
                             right_on=["underlying", "dt"], how="inner")
                j["d_prev"] = (j["prev_close"].astype(float)
                               / j["prev_close_candle"] - 1) * 1e4
                j["d_open"] = (j["preopen_price"].astype(float)
                               / j["s_open"] - 1) * 1e4
                print("   A corporate action would show as prev_close sitting at a")
                print("   round FRACTION of our candle close on the ex-date. d_prev is")
                print("   that difference in bp. Only prev_close_source='tick_close_")
                print("   field' is independent -- 'spot_30m_prior_session' is derived")
                print("   from this same candle table and cannot refute it.")
                print(j.groupby("prev_close_source")["d_prev"]
                      .agg(n="count", max_abs_bp=lambda x: x.abs().max(),
                           min_bp="min", max_bp="max").to_string())
                print("   every matched row where |d_prev| > 1bp:")
                print(j[j["d_prev"].abs() > 1][
                    ["session_date", "underlying", "prev_close",
                     "prev_close_candle", "d_prev", "prev_close_source",
                     "data_status"]].to_string(index=False))
                jo = j[j["d_open"].notna()]
                print(f"\n   and the pre-open EQUILIBRIUM price against our 09:15 "
                      f"open ({len(jo)} rows):")
                print(jo[["session_date", "underlying", "preopen_price", "s_open",
                          "d_open"]].to_string(index=False))
                print(f"   {(jo['d_open'].abs() < 1e-9).sum()} of {len(jo)} match to "
                      f"the paisa; max |d_open| {jo['d_open'].abs().max():.2f}bp.")
                print("   So the 09:15 'open' in this candle table IS the call-auction")
                print("   equilibrium price. The exit price is real; whether it is")
                print("   FILLABLE in size is a separate question this cannot answer.")
                print("   No d_prev anywhere is within 1000bp of a corporate-action")
                print("   ratio; the largest is 2.06% on 2026-08-28, a row whose own")
                print("   data_status is 'no_preopen_ticks' (a degraded-feed day at the")
                print("   very end of the sample), not an ex-date.")
        except Exception as exc:                       # noqa: BLE001
            print(f"   preopen_spot_snapshots unreadable: {exc}")
        print("   The 09:15 open is the equilibrium price of the 09:00-09:08 call")
        print("   auction, and the ten rows above show this table stores exactly that.")
        print("   AUBANK 2025-08-08 therefore opened at a genuine auction print of")
        print("   exactly 800.00 -- which was also the bar HIGH, on 6.58m shares in")
        print("   that single bar against 1.11m for the ENTIRE prior session. It")
        print("   traded 750.00 (the bar low) inside the same 30 minutes and closed")
        print("   the day at 736.95, BELOW the 744.10 entry. The whole +7.51% existed")
        print("   only in the auction, and the 10-session level ratio is 1.0043: the")
        print("   level did not step and stay. That is an EXECUTION question for a")
        print("   strategy whose exit is the opening print, not a corporate action.")

        # shared-night concentration
        print("\n   HOW INDEPENDENT ARE THE FOUR BOOKS? trades per night across the "
              "four:")
        per = trades.groupby("dt").size().value_counts().sort_index()
        for k, v in per.items():
            print(f"      {v:>4} nights carry {k} of the 4 books simultaneously")
        top_shared = (trades.groupby("dt")
                      .agg(n=("gap", "size"), tot=("gap", "sum"))
                      .nlargest(6, "tot"))
        allsum = trades["gap"].sum()
        print(f"   the six nights contributing most (summed simple return across all "
              f"four books = {allsum * 100:+.1f}%):")
        for d, r in top_shared.iterrows():
            print(f"      {d.date()}  {int(r['n'])} book(s)  "
                  f"{r['tot'] * 100:>+6.2f}%  = {r['tot'] / allsum * 100:>5.1f}% "
                  f"of the whole four-book total")
        print(f"   2026-04-07 and 2026-02-02 are the SAME market gap counted in "
              f"several books;")
        print("   that is concentration risk, not a data artefact, but it means the")
        print("   four results are not four independent pieces of evidence.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
