"""Do the big winning nights correspond to anything real?

The four overnight books (SBIN, AUBANK, ICICIBANK, FEDERALBNK) are carried by a
handful of nights: SBIN's Feb-2026 alone returned +11.6%, AUBANK's Aug-2025 alone
+9.0%, and dropping two trades moves every book by 10-15bp/night. This script
asks three separate questions about those nights and refuses to blend them:

  IS IT REAL DATA?  A 5.9% overnight gap can be a corporate action, a bad open
    print, or a multi-day hole in the history masquerading as one night. Every
    outsized trade is checked for bar count, calendar spacing, split ratios, and
    whether the gap held through the exit session or instantly reverted.

  IS IT THE MARKET?  A +5% single-name gap with a flat index is a company event
    and is not repeatable on demand; a +2% gap with BANKNIFTY up 1.5% is beta and
    would have been earned by any long. Each big night is decomposed into the
    index component (via a full-sample overnight beta) and the residual, and the
    other three names' gaps on the same night are printed alongside.

  DOES THE BOOK SURVIVE ITS OWN CONCENTRATION?  The mean is the statistic those
    nights move, so the mean is not the statistic to judge them by. Reported here
    are the SYMMETRIC trimmed mean (k dropped from BOTH tails, which is the only
    honest version of "drop the 2 best"), the median, a sign test, and the number
    of trades needed to reach half the book's total return.

    docker exec nomadcurie_vanguard_cycle python /vanguard/research/mp_four_concentration.py
"""
from __future__ import annotations

import argparse
import os
import sys
import warnings
from datetime import date

import numpy as np
import pandas as pd
import psycopg2

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.mp_auction import dsn, load  # noqa: E402

FOUR = ["SBIN", "AUBANK", "FEDERALBNK", "ICICIBANK"]
INDEX = ["BANKNIFTY", "NIFTY"]
RANK_WINDOW, MIN_PERIODS = 120, 60
TERTILE = 2 / 3
BIG = 0.02          # a night is "big" at 2% absolute
SPLITS = {"1:2": 2.0, "2:3": 1.5, "1:3": 3.0, "1:5": 5.0, "1:10": 10.0,
          "3:1 bonus": 4.0, "1:1 bonus": 2.0}


def rule(frame: pd.DataFrame) -> pd.DataFrame:
    """The exact rule from mp_four_books: own-name trailing rank of close_pos."""
    frame = frame.sort_values(["underlying", "dt"]).reset_index(drop=True)
    frame["cp_rank"] = (frame.groupby("underlying")["close_pos"]
                        .transform(lambda x: x.rolling(RANK_WINDOW, min_periods=MIN_PERIODS)
                                   .apply(lambda w: (w[-1] > w[:-1]).mean(), raw=True)))
    return frame


def sign_test(r: np.ndarray) -> tuple[float, float]:
    from scipy import stats
    pos = int((r > 0).sum())
    n = int((r != 0).sum())
    p_sign = stats.binomtest(pos, n, 0.5).pvalue if n else np.nan
    p_wil = stats.wilcoxon(r).pvalue if len(r) > 10 else np.nan
    return p_sign, p_wil


def boot_mean(r: np.ndarray, n: int = 20000, seed: int = 7) -> tuple[float, float, float]:
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(r), size=(n, len(r)))
    m = r[idx].mean(axis=1)
    return float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5)), float((m > 0).mean())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=dsn())
    args = ap.parse_args()

    conn = psycopg2.connect(args.dsn)
    try:
        raw = load(conn, FOUR + INDEX, date(2025, 3, 1))
        bars = pd.read_sql(
            """SELECT underlying, date(time AT TIME ZONE 'Asia/Kolkata') dt,
                      count(*) nbars, count(DISTINCT time) ndistinct,
                      sum(volume) vol
               FROM underlying_spot_candles
               WHERE interval='30minute' AND underlying = ANY(%(n)s)
                 AND time >= %(s)s
                 AND (time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:15' AND '15:15'
               GROUP BY 1,2""",
            conn, params={"n": FOUR + INDEX, "s": date(2025, 3, 1)})
        allbars = pd.read_sql(
            """SELECT underlying, (time AT TIME ZONE 'Asia/Kolkata') ts,
                      date(time AT TIME ZONE 'Asia/Kolkata') dt,
                      open, high, low, close, volume
               FROM underlying_spot_candles
               WHERE interval='30minute' AND underlying = ANY(%(n)s)
                 AND time >= %(s)s
                 AND (time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:15' AND '15:15'
               ORDER BY underlying, ts""",
            conn, params={"n": FOUR + INDEX, "s": date(2025, 3, 1)})
    finally:
        conn.close()

    bars["dt"] = pd.to_datetime(bars["dt"])
    allbars["dt"] = pd.to_datetime(allbars["dt"])
    allbars["ts"] = pd.to_datetime(allbars["ts"])
    for c in ("open", "high", "low", "close"):
        allbars[c] = pd.to_numeric(allbars[c], errors="coerce")
    s = rule(raw)
    s = s.merge(bars, on=["underlying", "dt"], how="left")

    # the 09:15 bar itself, and the extremes of the REMAINING 12 bars
    ab = allbars.sort_values(["underlying", "dt", "ts"])
    b1 = ab.groupby(["underlying", "dt"], as_index=False).first()[
        ["underlying", "dt", "open", "high", "low", "close", "volume"]]
    b1.columns = ["underlying", "dt", "b1_open", "b1_high", "b1_low", "b1_close", "b1_vol"]
    rst = (ab.groupby(["underlying", "dt"])
           .agg(rest_high=("high", lambda x: x.iloc[1:].max()),
                rest_low=("low", lambda x: x.iloc[1:].min())).reset_index())
    s = s.merge(b1, on=["underlying", "dt"], how="left").merge(
        rst, on=["underlying", "dt"], how="left")

    # exit-session context: the open we sell into, and what happened after it
    parts = []
    for name, g in s.groupby("underlying", sort=False):
        g = g.sort_values("dt").reset_index(drop=True)
        for a, b in (("x_open", "open"), ("x_high", "high"), ("x_low", "low"),
                     ("x_close", "close"), ("x_nbars", "nbars"),
                     ("x_b1_close", "b1_close"), ("x_b1_low", "b1_low"),
                     ("x_b1_high", "b1_high"), ("x_rest_high", "rest_high"),
                     ("x_rest_low", "rest_low")):
            g[a] = g[b].shift(-1)
        g["x_dt"] = g["dt"].shift(-1)
        g["hole_days"] = (g["x_dt"] - g["dt"]).dt.days
        parts.append(g)
    s = pd.concat(parts, ignore_index=True)

    stocks = s[s["underlying"].isin(FOUR)].copy()
    idx = s[s["underlying"].isin(INDEX)].copy()

    # overnight gap of each index / name, keyed by the ENTRY session date
    gap = s.pivot_table(index="dt", columns="underlying", values="next_open_ret")

    print("=" * 100)
    print("REPRODUCTION — the four books as reported (zero cost, close 15:15 -> next open 09:15)")
    print("=" * 100)
    u = stocks.dropna(subset=["cp_rank", "next_open_ret"])
    tr = u[u["cp_rank"] >= TERTILE].copy()
    print(f"   {'name':<12}{'trades':>8}{'bp/nt':>9}{'win%':>7}{'total%':>9}"
          f"{'best%':>8}{'worst%':>8}{'first':>12}{'last':>12}")
    books = {}
    for n in FOUR:
        r = tr[tr["underlying"] == n]["next_open_ret"].dropna()
        g = tr[tr["underlying"] == n]
        books[n] = g.dropna(subset=["next_open_ret"]).reset_index(drop=True)
        print(f"   {n:<12}{len(r):>8}{r.mean()*1e4:>+9.1f}{(r>0).mean()*100:>6.0f}%"
              f"{((1+r).prod()-1)*100:>+9.1f}{r.max()*100:>+8.2f}{r.min()*100:>+8.2f}"
              f"{books[n]['dt'].min().date()!s:>12}{books[n]['dt'].max().date()!s:>12}")

    # ------------------------------------------------------------------ 0
    print("\n" + "=" * 100)
    print("0. DATA INTEGRITY SWEEP — all sessions of the four names, not just the traded ones")
    print("=" * 100)
    allst = stocks.dropna(subset=["next_open_ret"])
    print(f"   {'name':<12}{'sessions':>10}{'!=13 bars':>11}{'dup ts':>8}{'zero vol':>10}"
          f"{'hole>5d':>9}{'|gap|>10%':>11}{'open outside HL':>17}")
    for n in FOUR:
        g = allst[allst["underlying"] == n]
        outside = ((g["x_open"] > g["x_high"] + 1e-9) | (g["x_open"] < g["x_low"] - 1e-9)).sum()
        print(f"   {n:<12}{len(g):>10}{(g['nbars']!=13).sum():>11}"
              f"{(g['nbars']!=g['ndistinct']).sum():>8}{(g['vol']<=0).sum():>10}"
              f"{(g['hole_days']>5).sum():>9}{(g['next_open_ret'].abs()>0.10).sum():>11}"
              f"{outside:>17}")
    hol = allst[allst["hole_days"] > 5][["underlying", "dt", "x_dt", "hole_days",
                                         "next_open_ret"]]
    if len(hol):
        print("\n   sessions whose 'overnight' spans more than 5 calendar days:")
        for _, x in hol.iterrows():
            print(f"      {x['underlying']:<12}{x['dt'].date()} -> {x['x_dt'].date()}"
                  f"  {int(x['hole_days'])}d  ret {x['next_open_ret']*100:+.2f}%")
    else:
        print("\n   no 'overnight' hold spans more than 5 calendar days.")

    print("\n   DROPPED SESSIONS — a session present in the raw bars but rejected by the")
    print("   12-bar/09:15 filter turns an 'overnight' hold into a multi-day one silently:")
    print(f"   {'name':<12}{'raw sessions':>14}{'kept':>7}{'dropped':>9}"
          f"{'holds spanning a dropped session':>34}")
    for n in FOUR:
        rawd = pd.DatetimeIndex(sorted(bars.loc[bars["underlying"] == n, "dt"].unique()))
        g = allst[allst["underlying"] == n].dropna(subset=["x_dt"])
        skipped = sum(int(rawd.searchsorted(x, "right") - rawd.searchsorted(d, "right")) > 1
                      for d, x in zip(g["dt"], g["x_dt"]))
        kept = set(stocks.loc[stocks["underlying"] == n, "dt"])
        dropped = [str(d.date()) for d in rawd if d not in kept]
        print(f"   {n:<12}{len(rawd):>14}{len(kept):>7}"
              f"{len(rawd)-len(kept):>9}{skipped:>34}   dropped {dropped}")
    print("   the affected holds (a session existed inside the 'overnight' window):")
    for n in FOUR:
        rawd = pd.DatetimeIndex(sorted(bars.loc[bars["underlying"] == n, "dt"].unique()))
        g = allst[allst["underlying"] == n].dropna(subset=["x_dt"])
        for _, x in g.iterrows():
            if int(rawd.searchsorted(x["x_dt"], "right")
                   - rawd.searchsorted(x["dt"], "right")) > 1:
                traded = (np.isfinite(x["cp_rank"]) and x["cp_rank"] >= TERTILE)
                print(f"      {n:<12}{x['dt'].date()} -> {x['x_dt'].date()}  "
                      f"ret {x['next_open_ret']*100:+.2f}%   "
                      f"{'TRADED by the book' if traded else 'not traded'}")
    cal = {n: set(stocks.loc[stocks["underlying"] == n, "dt"]) for n in FOUR}
    union = set().union(*cal.values())
    print("   sessions one name has and another lacks:")
    for n in FOUR:
        miss = sorted(union - cal[n])
        print(f"      {n:<12}missing {len(miss)}: {[str(d.date()) for d in miss]}")
        g = allst[allst["underlying"] == n].dropna(subset=["x_dt"])
        for d in miss:
            hit = g[(g["dt"] < d) & (g["x_dt"] > d)]
            for _, x in hit.iterrows():
                traded = (np.isfinite(x["cp_rank"]) and x["cp_rank"] >= TERTILE)
                print(f"         the hold {x['dt'].date()} -> {x['x_dt'].date()} swallows it,"
                      f" ret {x['next_open_ret']*100:+.2f}%  "
                      f"{'TRADED by the book' if traded else 'not traded'}")

    # ------------------------------------------------------------------ 1
    print("\n" + "=" * 100)
    print("1. THE FIVE LARGEST WINNERS AND FIVE LARGEST LOSERS PER NAME")
    print("   ret = the trade itself (open(t+1)/close(t)-1). cp = close_pos that fired it,")
    print("   rank = its percentile against the name's own trailing 120 sessions.")
    print("   held = exit session's close vs its open: did the gap stick or revert?")
    print("=" * 100)
    extremes = {}
    for n in FOUR:
        b = books[n]
        win = b.nlargest(5, "next_open_ret")
        los = b.nsmallest(5, "next_open_ret")
        extremes[n] = pd.concat([win, los])
        print(f"\n   {n}   ({len(b)} trades)")
        print(f"      {'date':<12}{'ret%':>8}{'cp':>7}{'rank':>7}{'atr20%':>8}"
              f"{'ret/atr':>9}{'held%':>8}{'day_type':>20}{'hole':>6}{'bars':>6}")
        for tag, blk in (("WIN", win), ("LOSS", los)):
            print(f"      -- {tag}")
            for _, x in blk.iterrows():
                held = (x["x_close"] / x["x_open"] - 1) * 100
                a = x["atr20"] if np.isfinite(x["atr20"]) else np.nan
                print(f"      {x['dt'].date()!s:<12}{x['next_open_ret']*100:>+8.2f}"
                      f"{x['close_pos']:>7.2f}{x['cp_rank']:>7.2f}{a*100:>8.2f}"
                      f"{x['next_open_ret']/a:>+9.2f}{held:>+8.2f}"
                      f"{str(x['day_type']):>20}{int(x['hole_days']):>6}"
                      f"{int(x['x_nbars']):>6}")

    # split / corporate-action screen on every large move
    print("\n   CORPORATE-ACTION SCREEN on every |ret| > 2% (ratio close(t)/open(t+1)"
          " against common split and bonus ratios):")
    flagged = 0
    for n in FOUR:
        b = books[n]
        for _, x in b[b["next_open_ret"].abs() > BIG].iterrows():
            ratio = x["close"] / x["x_open"]
            hit = [k for k, v in SPLITS.items() if abs(ratio - v) < 0.03 * v]
            if hit:
                flagged += 1
                print(f"      {n:<12}{x['dt'].date()}  ratio {ratio:.3f}  matches {hit}")
    print(f"      {flagged} of the large moves match a split/bonus ratio."
          if flagged else "      none of the large moves match a split or bonus ratio.")

    # ---------------------------------------------------------------- 1b
    print("\n" + "=" * 100)
    print("1b. BAR-LEVEL FORENSICS on every traded night with |ret| > 2%.")
    print("    The trade sells the 09:15 OPEN of the exit session. If that open is a lone")
    print("    print — never revisited, with its own bar closing far below it — the fill is")
    print("    fiction. bar1 = the 09:15 bar itself; 'vs rest' = how far the open sits above")
    print("    the highest price of the REMAINING 12 bars (>0 means the open was never seen again).")
    print("=" * 100)
    print(f"   {'name':<11}{'exit date':<12}{'gap%':>7}{'bar1 o':>10}{'bar1 h':>9}"
          f"{'bar1 l':>9}{'bar1 c':>9}{'bar1 ret%':>10}{'vs rest%':>10}{'day ret%':>10}"
          f"{'bar1 vol':>12}")
    spikes = 0
    for n in FOUR:
        b = books[n]
        for _, x in b[b["next_open_ret"].abs() > BIG].sort_values("dt").iterrows():
            xd = x["x_dt"]
            g = allbars[(allbars["underlying"] == n) & (allbars["dt"] == xd)].sort_values("ts")
            if len(g) < 2:
                continue
            b1 = g.iloc[0]
            rest_hi, rest_lo = g["high"].iloc[1:].max(), g["low"].iloc[1:].min()
            vs_rest = ((b1["open"] - rest_hi) / b1["open"] * 100 if x["next_open_ret"] > 0
                       else (rest_lo - b1["open"]) / b1["open"] * 100)
            if vs_rest > 0.5 and abs(b1["close"] / b1["open"] - 1) > 0.02:
                spikes += 1
            print(f"   {n:<11}{xd.date()!s:<12}{x['next_open_ret']*100:>+7.2f}"
                  f"{b1['open']:>10.2f}{b1['high']:>9.2f}{b1['low']:>9.2f}{b1['close']:>9.2f}"
                  f"{(b1['close']/b1['open']-1)*100:>+10.2f}{vs_rest:>+10.2f}"
                  f"{(g['close'].iloc[-1]/b1['open']-1)*100:>+10.2f}"
                  f"{b1['volume']:>12.0f}")
    print(f"\n   {spikes} of these opens look like an unrevisited spike print"
          f" (open never traded again that day AND its own 30m bar reversed >2%).")

    # ---------------------------------------------------------------- 1c
    print("\n" + "=" * 100)
    print("1c. EXIT-FILL SENSITIVITY — the decisive test on the big nights.")
    print("    The book sells the 09:15 print. On the largest nights that print was never")
    print("    traded again. So re-run each book selling 30 minutes later instead (the")
    print("    09:15 bar's close), and at the midpoint. If the edge is an opening print,")
    print("    it dies here; if it is real overnight drift, it survives.")
    print("=" * 100)
    print(f"   {'name':<12}{'exit at open':>26}{'exit at mid(o,b1c)':>26}{'exit at 09:45':>26}")
    print(f"   {'':<12}" + "".join(f"{'bp/nt':>9}{'total%':>9}{'median':>8}" for _ in range(3)))
    fills = {}
    for n in FOUR:
        b = books[n].copy()
        b["r_open"] = b["next_open_ret"]
        b["r_b1c"] = b["x_b1_close"] / b["close"] - 1
        b["r_mid"] = (0.5 * (b["x_open"] + b["x_b1_close"])) / b["close"] - 1
        fills[n] = b
        row = f"   {n:<12}"
        for c in ("r_open", "r_mid", "r_b1c"):
            r = b[c].dropna()
            row += (f"{r.mean()*1e4:>+9.1f}{((1+r).prod()-1)*100:>+9.1f}"
                    f"{r.median()*1e4:>+8.1f}")
        print(row)
    print("\n   the same, restricted to the |ret|>2% nights that carry the books:")
    print(f"   {'name':<11}{'exit date':<12}{'at open%':>10}{'at mid%':>10}{'at 09:45%':>11}"
          f"{'give-up bp':>12}")
    for n in FOUR:
        b = fills[n]
        for _, x in b[b["r_open"].abs() > BIG].sort_values("dt").iterrows():
            print(f"   {n:<11}{x['x_dt'].date()!s:<12}{x['r_open']*100:>+10.2f}"
                  f"{x['r_mid']*100:>+10.2f}{x['r_b1c']*100:>+11.2f}"
                  f"{(x['r_b1c']-x['r_open'])*1e4:>+12.0f}")

    print("\n   HOW COMMON IS AN UNREVISITED OPEN?  Across every session of the four names:")
    print(f"   {'name':<12}{'sessions':>10}{'open==bar1 high':>17}{'open==day high':>16}"
          f"{'open>rest-of-day high':>23}{'and reversed >2%':>18}")
    for n in FOUR:
        g = allst[allst["underlying"] == n].dropna(subset=["b1_high", "rest_high"])
        oh = (g["b1_open"] >= g["b1_high"] - 1e-9).sum()
        dh = (g["b1_open"] >= g["high"] - 1e-9).sum()
        unre = (g["b1_open"] > g["rest_high"]).sum()
        rev = ((g["b1_open"] > g["rest_high"]) &
               ((g["b1_close"] / g["b1_open"] - 1) < -0.02)).sum()
        print(f"   {n:<12}{len(g):>10}{oh:>17}{dh:>16}{unre:>23}{rev:>18}")

    print("\n   FULL SESSION PRINT for the two opens the book most depends on:")
    for n, d in (("AUBANK", "2025-08-08"), ("SBIN", "2026-02-03")):
        g = allbars[(allbars["underlying"] == n) &
                    (allbars["dt"] == pd.Timestamp(d))].sort_values("ts")
        pv = allst[(allst["underlying"] == n) & (allst["x_dt"] == pd.Timestamp(d))]
        print(f"\n      {n}  exit session {d}   (entry close was "
              f"{pv['close'].iloc[0]:.2f} on {pv['dt'].iloc[0].date()})")
        print(f"      {'bar':<8}{'open':>10}{'high':>10}{'low':>10}{'close':>10}{'volume':>13}")
        for _, r in g.iterrows():
            print(f"      {r['ts'].strftime('%H:%M'):<8}{r['open']:>10.2f}{r['high']:>10.2f}"
                  f"{r['low']:>10.2f}{r['close']:>10.2f}{r['volume']:>13.0f}")
        ent = allbars[(allbars["underlying"] == n) &
                      (allbars["dt"] == pv["dt"].iloc[0])].sort_values("ts")
        print(f"      last 3 bars of the ENTRY session {pv['dt'].iloc[0].date()}:")
        for _, r in ent.tail(3).iterrows():
            print(f"      {r['ts'].strftime('%H:%M'):<8}{r['open']:>10.2f}{r['high']:>10.2f}"
                  f"{r['low']:>10.2f}{r['close']:>10.2f}{r['volume']:>13.0f}")

    # ------------------------------------------------------------------ 2
    print("\n" + "=" * 100)
    print("2. WAS THE MOVE MARKET-WIDE?  Overnight gaps on the same night.")
    print("=" * 100)
    beta = {}
    print("   full-sample overnight beta of each name on BANKNIFTY (all sessions, not just trades):")
    print(f"      {'name':<12}{'beta':>8}{'R2':>8}{'n':>6}")
    for n in FOUR:
        d = gap[[n, "BANKNIFTY"]].dropna()
        b1, b0 = np.polyfit(d["BANKNIFTY"], d[n], 1)
        r2 = np.corrcoef(d["BANKNIFTY"], d[n])[0, 1] ** 2
        beta[n] = b1
        print(f"      {n:<12}{b1:>8.2f}{r2:>8.2f}{len(d):>6}")

    print("\n   every traded night with |ret| > 2.0%, decomposed:")
    print(f"   {'name':<11}{'date':<12}{'ret%':>7}{'BNF%':>7}{'NIFTY%':>8}"
          f"{'beta*BNF':>10}{'resid%':>8}   {'SBIN':>7}{'AUBANK':>8}{'ICICI':>8}{'FEDBK':>8}  verdict")
    verdicts = {}
    for n in FOUR:
        b = books[n]
        for _, x in b[b["next_open_ret"].abs() > BIG].sort_values("dt").iterrows():
            d = x["dt"]
            bnf = gap.at[d, "BANKNIFTY"] if d in gap.index else np.nan
            nif = gap.at[d, "NIFTY"] if d in gap.index else np.nan
            exp = beta[n] * bnf
            res = x["next_open_ret"] - exp
            others = {o: (gap.at[d, o] if d in gap.index else np.nan) for o in FOUR}
            peers = [v for k, v in others.items() if k != n and np.isfinite(v)]
            same_dir = sum(1 for v in peers if np.sign(v) == np.sign(x["next_open_ret"])
                           and abs(v) > 0.01)
            if abs(res) > 0.7 * abs(x["next_open_ret"]) and abs(bnf) < 0.006:
                v = "IDIOSYNCRATIC"
            elif same_dir >= 2 and abs(bnf) < 0.006:
                v = "sector"
            elif abs(exp) > 0.5 * abs(x["next_open_ret"]):
                v = "beta"
            else:
                v = "mostly idio"
            verdicts.setdefault(n, []).append((d, x["next_open_ret"], res, v))
            print(f"   {n:<11}{d.date()!s:<12}{x['next_open_ret']*100:>+7.2f}"
                  f"{bnf*100:>+7.2f}{nif*100:>+8.2f}{exp*100:>+10.2f}{res*100:>+8.2f}   "
                  + "".join(f"{others[o]*100:>+8.2f}" if np.isfinite(others[o]) else f"{'na':>8}"
                            for o in ["SBIN", "AUBANK", "ICICIBANK", "FEDERALBNK"])
                  + f"  {v}")

    print("\n   how much of each book's TOTAL comes from nights the index barely moved"
          " (|BANKNIFTY gap| < 0.3%):")
    print(f"      {'name':<12}{'sum ret bp':>12}{'quiet-idx bp':>14}{'share':>8}")
    for n in FOUR:
        b = books[n].copy()
        b["bnf"] = b["dt"].map(gap["BANKNIFTY"])
        tot = b["next_open_ret"].sum()
        quiet = b.loc[b["bnf"].abs() < 0.003, "next_open_ret"].sum()
        print(f"      {n:<12}{tot*1e4:>+12.0f}{quiet*1e4:>+14.0f}"
              f"{quiet/tot*100 if tot else np.nan:>7.0f}%")

    # ---------------------------------------------------------------- 2b
    print("\n" + "=" * 100)
    print("2b. THE MARKET-WIDE NIGHTS IN CONTEXT — BANKNIFTY's own sessions around them.")
    print("    A +4.8% overnight index gap is either a real event or a broken series. If the")
    print("    prior session crashed and closed on its low, the gap is a rebound and is real.")
    print("=" * 100)
    big_nights = sorted({x["dt"] for n in FOUR for _, x in
                         books[n][books[n]["next_open_ret"].abs() > 0.03].iterrows()})
    bnf = s[s["underlying"] == "BANKNIFTY"].sort_values("dt").reset_index(drop=True)
    for d in big_nights:
        i = bnf.index[bnf["dt"] == d]
        if not len(i):
            print(f"   {d.date()}  BANKNIFTY has no session on this date")
            continue
        i = int(i[0])
        print(f"\n   entry {d.date()} -> exit {bnf.at[i, 'x_dt'].date()}   "
              f"BANKNIFTY overnight {bnf.at[i, 'next_open_ret']*100:+.2f}%")
        print(f"      {'session':<12}{'open':>11}{'high':>11}{'low':>11}{'close':>11}"
              f"{'day ret%':>10}{'close_pos':>11}")
        for j in range(max(i - 2, 0), min(i + 3, len(bnf))):
            row = bnf.iloc[j]
            prev = bnf.iloc[j - 1]["close"] if j else np.nan
            print(f"      {row['dt'].date()!s:<12}{row['open']:>11.1f}{row['high']:>11.1f}"
                  f"{row['low']:>11.1f}{row['close']:>11.1f}"
                  f"{(row['close']/prev-1)*100:>+10.2f}{row['close_pos']:>11.2f}")

    # ------------------------------------------------------------------ 3
    print("\n" + "=" * 100)
    print("3. CONCENTRATION, AND THE STATISTICS THAT SURVIVE IT")
    print("=" * 100)
    print("   share of the book's ARITHMETIC total return contributed by its top-k trades")
    print("   (and what the compounded total becomes once those k are removed):")
    print(f"   {'name':<12}{'sum bp':>9}{'top1':>8}{'top2':>8}{'top5':>8}{'top10':>8}"
          f"   {'-1 tot%':>9}{'-2 tot%':>9}{'-5 tot%':>9}{'-10 tot%':>10}")
    for n in FOUR:
        r = books[n]["next_open_ret"].values
        tot = r.sum()
        srt = np.sort(r)[::-1]
        cells = ""
        for k in (1, 2, 5, 10):
            cells += f"{srt[:k].sum()/tot*100:>8.0f}%" if tot else f"{'na':>8}"
        rest = ""
        for k in (1, 2, 5, 10):
            keep = np.sort(r)[:len(r) - k]
            rest += f"{((1+keep).prod()-1)*100:>+9.1f}" if k < 10 else f"{((1+keep).prod()-1)*100:>+10.1f}"
        print(f"   {n:<12}{tot*1e4:>+9.0f}{cells}   {rest}")
    print("   (a share above 100% means the rest of the book is net negative)")

    print("\n   SYMMETRIC trimming — drop k from BOTH tails. This is the fair version of")
    print("   'drop the 2 best': a book of real edge loses little, a book of two nights collapses.")
    print(f"   {'name':<12}{'n':>5}{'mean':>9}{'sym-1':>9}{'sym-2':>9}{'sym-3':>9}"
          f"{'sym-5':>9}{'trim10%':>10}{'trim20%':>10}{'median':>9}")
    from scipy import stats as sps
    for n in FOUR:
        r = np.sort(books[n]["next_open_ret"].values)
        row = f"   {n:<12}{len(r):>5}{r.mean()*1e4:>+9.1f}"
        for k in (1, 2, 3, 5):
            row += f"{r[k:len(r)-k].mean()*1e4:>+9.1f}"
        row += f"{sps.trim_mean(r, 0.10)*1e4:>+10.1f}{sps.trim_mean(r, 0.20)*1e4:>+10.1f}"
        row += f"{np.median(r)*1e4:>+9.1f}"
        print(row)

    print("\n   is the CENTRE of the distribution positive at all?  (bp; sign test on the")
    print("   median, Wilcoxon on the signed ranks, bootstrap CI on the mean)")
    print(f"   {'name':<12}{'median bp':>11}{'win%':>7}{'p(sign)':>9}{'p(wilcox)':>11}"
          f"{'mean bp':>9}{'boot 95% CI bp':>22}{'P(mean>0)':>11}")
    for n in FOUR:
        r = books[n]["next_open_ret"].values
        ps, pw = sign_test(r)
        lo, hi, pgt = boot_mean(r)
        print(f"   {n:<12}{np.median(r)*1e4:>+11.1f}{(r>0).mean()*100:>6.0f}%"
              f"{ps:>9.3f}{pw:>11.3f}{r.mean()*1e4:>+9.1f}"
              f"{f'[{lo*1e4:+.1f}, {hi*1e4:+.1f}]':>22}{pgt:>11.2f}")

    # ------------------------------------------------------------------ 4
    print("\n" + "=" * 100)
    print("4. HOW MANY TRADES CARRY HALF THE BOOK?")
    print("=" * 100)
    print(f"   {'name':<12}{'n':>5}{'sum bp':>9}{'trades to 1/2':>15}{'% of book':>11}"
          f"{'gross+ bp':>11}{'gross- bp':>11}{'top5 / gross+':>15}")
    for n in FOUR:
        r = np.sort(books[n]["next_open_ret"].values)[::-1]
        tot = r.sum()
        gp, gn = r[r > 0].sum(), r[r < 0].sum()
        if tot > 0:
            k = int(np.argmax(np.cumsum(r) >= 0.5 * tot) + 1)
            kk, pc = f"{k}", f"{k/len(r)*100:.1f}%"
        else:
            kk, pc = "n/a (neg)", "n/a"
        print(f"   {n:<12}{len(r):>5}{tot*1e4:>+9.0f}{kk:>15}{pc:>11}"
              f"{gp*1e4:>+11.0f}{gn*1e4:>+11.0f}{r[:5].sum()/gp*100:>14.0f}%")

    # ------------------------------------------------------------------ 5
    print("\n" + "=" * 100)
    print("5. THE TWO MONTHS THAT MADE THE HEADLINE")
    print("=" * 100)
    for n, per in (("SBIN", "2026-02"), ("AUBANK", "2025-08")):
        b = books[n]
        m = b[b["dt"].dt.to_period("M") == pd.Period(per)]
        rest = b[b["dt"].dt.to_period("M") != pd.Period(per)]
        print(f"\n   {n}  {per}: {len(m)} trades, "
              f"{((1+m['next_open_ret']).prod()-1)*100:+.1f}% compounded, "
              f"{m['next_open_ret'].mean()*1e4:+.0f} bp/night")
        print(f"      every trade that month:")
        for _, x in m.sort_values("dt").iterrows():
            bnf = gap.at[x["dt"], "BANKNIFTY"] if x["dt"] in gap.index else np.nan
            print(f"        {x['dt'].date()}  ret {x['next_open_ret']*100:>+6.2f}%"
                  f"   BNF {bnf*100:>+6.2f}%   cp {x['close_pos']:.2f}  rank {x['cp_rank']:.2f}")
        print(f"      the OTHER {len(rest)} trades: {rest['next_open_ret'].mean()*1e4:+.1f} bp/night, "
              f"{((1+rest['next_open_ret']).prod()-1)*100:+.1f}% compounded, "
              f"median {rest['next_open_ret'].median()*1e4:+.1f} bp, "
              f"win {(rest['next_open_ret']>0).mean()*100:.0f}%")
        lo, hi, pgt = boot_mean(rest["next_open_ret"].values)
        print(f"      without that month the mean's bootstrap 95% CI is "
              f"[{lo*1e4:+.1f}, {hi*1e4:+.1f}] bp, P(mean>0) = {pgt:.2f}")

    # ------------------------------------------------------------------ 5b
    print("\n" + "=" * 100)
    print("5b. DID THE SIGNAL SELECT THE BIG NIGHTS, OR JUST SHOW UP FOR THEM?")
    print("    Same window, same exit. TOP = the book. ALL = long every night unconditionally.")
    print("    BOTTOM = the bottom tertile. If the big nights land in every bucket, the")
    print("    ranking is not choosing them — it is present for a gap it did not predict.")
    print("=" * 100)
    print(f"   {'name':<12}{'bucket':<9}{'n':>5}{'bp/nt':>9}{'median':>9}{'win%':>7}"
          f"{'total%':>9}{'best%':>8}   biggest-night membership")
    for n in FOUR:
        gu = u[u["underlying"] == n]
        buckets = {"TOP": gu[gu["cp_rank"] >= TERTILE],
                   "MID": gu[(gu["cp_rank"] >= 1 / 3) & (gu["cp_rank"] < TERTILE)],
                   "BOTTOM": gu[gu["cp_rank"] < 1 / 3],
                   "ALL": gu}
        for lab, g in buckets.items():
            r = g["next_open_ret"].dropna()
            hit = [str(d.date()) for d in big_nights if d in set(g["dt"])]
            print(f"   {n if lab == 'TOP' else '':<12}{lab:<9}{len(r):>5}"
                  f"{r.mean()*1e4:>+9.1f}{r.median()*1e4:>+9.1f}{(r>0).mean()*100:>6.0f}%"
                  f"{((1+r).prod()-1)*100:>+9.1f}{r.max()*100:>+8.2f}   "
                  + (",".join(hit) if lab != "ALL" else ""))

    print("\n   the same, pooled across the four names (equal weight per name-night):")
    print(f"   {'bucket':<9}{'n':>6}{'bp/nt':>9}{'median':>9}{'win%':>7}{'t-stat':>9}")
    for lab, g in (("TOP", u[u["cp_rank"] >= TERTILE]),
                   ("MID", u[(u["cp_rank"] >= 1 / 3) & (u["cp_rank"] < TERTILE)]),
                   ("BOTTOM", u[u["cp_rank"] < 1 / 3]), ("ALL", u)):
        r = g["next_open_ret"].dropna()
        print(f"   {lab:<9}{len(r):>6}{r.mean()*1e4:>+9.1f}{r.median()*1e4:>+9.1f}"
              f"{(r>0).mean()*100:>6.0f}%{r.mean()/(r.std(ddof=1)/np.sqrt(len(r))):>+9.2f}")

    # ------------------------------------------------------------------ 6
    print("\n" + "=" * 100)
    print("6. VERDICT PER NAME")
    print("=" * 100)
    print(f"   {'name':<12}{'mean':>8}{'median':>8}{'sym-2':>8}{'trim20':>8}{'09:45 exit':>12}"
          f"{'#to half':>10}{'top2 share':>12}{'p(sign)':>9}   verdict")
    for n in FOUR:
        r = np.sort(books[n]["next_open_ret"].values)
        tot = r.sum()
        med = np.median(r)
        s2 = r[2:len(r) - 2].mean()
        t20 = sps.trim_mean(r, 0.20)
        top2 = np.sort(r)[::-1][:2].sum() / tot if tot else np.nan
        d = np.sort(r)[::-1]
        k = int(np.argmax(np.cumsum(d) >= 0.5 * tot) + 1) if tot > 0 else -1
        ps, _ = sign_test(r)
        late = fills[n]["r_b1c"].dropna().mean()
        if med > 0 and ps < 0.10 and s2 > 0 and t20 > 0:
            v = "BROAD BASED — survives its own concentration"
        elif s2 > 0 and t20 > 0 and med >= 0:
            v = "TAIL-CARRIED — the centre is flat, the tail is the book"
        elif tot > 0:
            v = "CARRIED BY A FEW NIGHTS — no edge underneath"
        else:
            v = "NO BOOK"
        print(f"   {n:<12}{r.mean()*1e4:>+8.1f}{med*1e4:>+8.1f}{s2*1e4:>+8.1f}{t20*1e4:>+8.1f}"
              f"{late*1e4:>+12.1f}{k if k > 0 else 'n/a':>10}{top2*100:>11.0f}%{ps:>9.3f}   {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
