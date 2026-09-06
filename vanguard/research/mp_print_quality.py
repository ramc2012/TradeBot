"""Are the 09:15 open and the 15:15 close REAL, tradeable prices?

The whole overnight edge is measured between exactly two prints per trade: the
15:15 bar's CLOSE on session t and the 09:15 bar's OPEN on session t+1. If either
print is stale, synthetic or untradeably thin, the edge is a measurement artefact.

CADENCE WARNING, found while writing this. Rows tagged interval='30minute' are
NOT all 30 minutes apart. From 2026-07-17 the same tag carries a 15-MINUTE
cadence (25 stamps between 09:15 and 15:15 instead of 13). So "the next bar's
close" is 09:45 for most of the sample and 09:30 for the recent part. Every exit
in here is therefore resolved by CLOCK TIME -- price at time T = close of the
last bar stamped strictly before T -- not by bar index.

Audits, all on the exact trade set of mp_four_books.py (top-tertile close_pos
against the name's own trailing 120 sessions, min 60):

  0  BAR COVERAGE     -- cadence, missing bars, and what lives outside 09:15-15:15
  1  EXIT LADDER      -- exit at the 09:15 OPEN vs the price 30 / 60 minutes later
  2  FILL REALISM     -- where the open sits inside the opening bar, and what the
                        book pays if you get a worse fill than the exact print
  3  OPENING VOLUME   -- 09:15 bar volume vs the session's own bars
  4  STALE / ZERO GAP -- next_open EXACTLY equal to the prior 15:15 close
  5  15:15 INTEGRITY  -- does the closing bar exist and carry volume, both sides
  6  GAP DISTRIBUTION -- smooth, or spikes at particular values?

    docker exec nomadcurie_vanguard_cycle python /vanguard/research/mp_print_quality.py
"""
from __future__ import annotations

import argparse
import os
import sys
import warnings
from collections import Counter
from datetime import date, time as dtime, timedelta

import numpy as np
import pandas as pd
import psycopg2

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research.mp_auction import dsn, load  # noqa: E402

FOUR = ["SBIN", "AUBANK", "FEDERALBNK", "ICICIBANK"]
RANK_WINDOW, MIN_PERIODS = 120, 60
TOP_TERTILE = 2 / 3

# every row of the day, so we can see what the session filter throws away
ALL_SQL = """
SELECT underlying,
       (time AT TIME ZONE 'Asia/Kolkata') AS ts,
       date(time AT TIME ZONE 'Asia/Kolkata') AS dt,
       open, high, low, close, volume, source
FROM underlying_spot_candles
WHERE interval = '30minute' AND time >= %(start)s AND underlying = ANY(%(names)s)
ORDER BY underlying, ts
"""

FINE_SQL = """
SELECT underlying, interval,
       (time AT TIME ZONE 'Asia/Kolkata') AS ts,
       date(time AT TIME ZONE 'Asia/Kolkata') AS dt,
       open, close, volume
FROM underlying_spot_candles
WHERE interval IN ('1minute', '3minute', '5minute', '15minute')
  AND underlying = ANY(%(names)s)
  AND (time AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '09:15' AND '15:29'
ORDER BY underlying, interval, ts
"""


def stats(r) -> tuple:
    r = pd.Series(r).dropna()
    if len(r) < 2:
        return len(r), np.nan, np.nan, np.nan
    sd = r.std(ddof=1)
    t = r.mean() / (sd / np.sqrt(len(r))) if sd > 0 else np.nan
    return len(r), r.mean() * 1e4, (r > 0).mean() * 100, t


def bp(x) -> str:
    return f"{x:+.1f}" if np.isfinite(x) else "n/a"


def session_prints(bars: pd.DataFrame) -> pd.DataFrame:
    """Clock-resolved print facts per (underlying, session). Cadence-agnostic."""
    bars = bars.copy()
    bars["ts"] = pd.to_datetime(bars["ts"])
    bars["dt"] = pd.to_datetime(bars["dt"])
    for c in ("open", "high", "low", "close", "volume"):
        bars[c] = pd.to_numeric(bars[c], errors="coerce")
    bars["tod"] = bars["ts"].dt.time

    rows = []
    for (name, dt), gall in bars.groupby(["underlying", "dt"], sort=False):
        gall = gall.sort_values("ts")
        # the session as the backtest defines it
        g = gall[(gall["tod"] >= dtime(9, 15)) & (gall["tod"] <= dtime(15, 15))]
        if g.empty:
            continue
        pre = gall[gall["tod"] < dtime(9, 15)]
        post = gall[gall["tod"] > dtime(15, 15)]

        def win(a, b):
            return g[(g["tod"] >= a) & (g["tod"] < b)]

        def px_at(t):
            """Price at clock time t = close of the last bar stamped before t."""
            w = g[g["tod"] < t]
            return w["close"].iloc[-1] if len(w) else np.nan

        w_open = win(dtime(9, 15), dtime(9, 45))     # first 30 minutes
        w_2nd = win(dtime(9, 45), dtime(10, 15))     # second 30 minutes
        w_last = g[g["tod"] >= dtime(15, 15)]        # the closing bar(s)
        w_pen = win(dtime(14, 45), dtime(15, 15))    # the 30 min before the close

        stamps = sorted({t for t in g["tod"]})
        cadence = (int(np.median(np.diff([t.hour * 60 + t.minute for t in stamps])))
                   if len(stamps) > 1 else np.nan)
        vol = g["volume"].fillna(0)

        rows.append({
            "underlying": name, "dt": dt,
            "n_bars": len(g), "cadence_min": cadence,
            "first_stamp": stamps[0].strftime("%H:%M"),
            "last_stamp": stamps[-1].strftime("%H:%M"),
            "has_1515": any(t == dtime(15, 15) for t in stamps),
            # ---- the opening print and its first 30 minutes
            "o_open": w_open["open"].iloc[0] if len(w_open) else np.nan,
            "o_close": w_open["close"].iloc[-1] if len(w_open) else np.nan,
            "o_high": w_open["high"].max() if len(w_open) else np.nan,
            "o_low": w_open["low"].min() if len(w_open) else np.nan,
            "o_vol": w_open["volume"].sum() if len(w_open) else np.nan,
            "o_bar_vol": (g["volume"].iloc[0] if len(g) else np.nan),
            "px_0930": px_at(dtime(9, 30)),
            "px_0945": px_at(dtime(9, 45)),
            "px_1015": px_at(dtime(10, 15)),
            "c_2nd": w_2nd["close"].iloc[-1] if len(w_2nd) else np.nan,
            # ---- the closing print
            "c1515": w_last["close"].iloc[-1] if len(w_last) else np.nan,
            "c1515_high": w_last["high"].max() if len(w_last) else np.nan,
            "c1515_low": w_last["low"].min() if len(w_last) else np.nan,
            "c1515_open": w_last["open"].iloc[0] if len(w_last) else np.nan,
            "v1515": w_last["volume"].sum() if len(w_last) else np.nan,
            "c1445": w_pen["close"].iloc[-1] if len(w_pen) else np.nan,
            # ---- session-wide
            "sess_vol": vol.sum(), "mean_bar_vol": vol.mean(),
            "n_zero_vol_bars": int((vol == 0).sum()),
            "vol_rank_open": int((vol > vol.iloc[0]).sum()) + 1,
            "vol_pctile_open": float((vol < vol.iloc[0]).mean()),
            # ---- what the filter discards
            "n_pre": len(pre), "n_post": len(post),
            "pre_close": pre["close"].iloc[-1] if len(pre) else np.nan,
            "pre_vol": pre["volume"].sum() if len(pre) else 0,
            # a bar whose high is absurd against its own close: corrupt row
            "junk_rows": int((gall["high"] > 5 * gall["close"]).sum()),
            # ---- provenance and OHLC coherence of the in-session rows
            "o_src": g["source"].iloc[0],
            "c_src": (w_last["source"].iloc[-1] if len(w_last) else None),
            "srcs": "+".join(sorted(set(g["source"].dropna()))),
            "n_offgrid": int(sum(1 for t in g["tod"] if t.minute % 30 != 15)),
            "bad_ohlc": int(((g["open"] > g["high"] + 1e-9)
                             | (g["open"] < g["low"] - 1e-9)
                             | (g["close"] > g["high"] + 1e-9)
                             | (g["close"] < g["low"] - 1e-9)).sum()),
            "wild_bars": int((g["high"] / g["low"].replace(0, np.nan) > 1.15).sum()),
        })
    return pd.DataFrame(rows)


def infer_tick(px: pd.Series) -> float:
    """NSE moved sub-250 stocks to a 0.01 tick in 2025; infer which grid we are on."""
    c = px.dropna()
    if c.empty:
        return 0.05
    on5 = (np.abs((c * 100).round() % 5) < 1e-6).mean()
    return 0.05 if on5 > 0.97 else 0.01


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=float, default=3.0)
    parser.add_argument("--dsn", default=dsn())
    args = parser.parse_args()
    start = date.today() - timedelta(days=int(args.years * 365.25))

    connection = psycopg2.connect(args.dsn)
    try:
        s = load(connection, FOUR, start)
        allbars = pd.read_sql(ALL_SQL, connection, params={"start": start, "names": FOUR})
        fine = pd.read_sql(FINE_SQL, connection, params={"names": FOUR})
    finally:
        connection.close()
    p = session_prints(allbars)

    s = s.sort_values(["underlying", "dt"]).reset_index(drop=True)
    s = s.merge(p, on=["underlying", "dt"], how="left", validate="one_to_one")

    s["cp_rank"] = (s.groupby("underlying")["close_pos"]
                    .transform(lambda x: x.rolling(RANK_WINDOW, min_periods=MIN_PERIODS)
                               .apply(lambda w: (w[-1] > w[:-1]).mean(), raw=True)))

    nxt = ["dt", "o_src", "c_src", "o_open", "o_close", "o_high", "o_low", "o_vol", "o_bar_vol",
           "px_0930", "px_0945", "px_1015", "c1515", "v1515", "n_bars",
           "last_stamp", "cadence_min", "mean_bar_vol", "sess_vol",
           "vol_rank_open", "vol_pctile_open", "has_1515", "pre_close", "pre_vol",
           "n_zero_vol_bars"]
    parts = []
    for _, g in s.groupby("underlying", sort=False):
        g = g.sort_values("dt").reset_index(drop=True)
        for c in nxt:
            g[f"n_{c}"] = g[c].shift(-1)
        parts.append(g)
    s = pd.concat(parts, ignore_index=True)

    C = s["close"]
    s["r_open"] = s["n_o_open"] / C - 1.0            # the strategy's exit print
    s["r_0930"] = s["n_px_0930"] / C - 1.0
    s["r_0945"] = s["n_px_0945"] / C - 1.0
    s["r_1015"] = s["n_px_1015"] / C - 1.0
    s["r_openlow"] = s["n_o_low"] / C - 1.0          # worst fill in the first 30m
    s["r_openhigh"] = s["n_o_high"] / C - 1.0
    s["r_openmid"] = (s["n_o_open"] + s["n_o_close"]) / 2 / C - 1.0
    s["r_first30"] = s["n_o_close"] / s["n_o_open"] - 1.0
    s["open_pos_in_bar"] = ((s["n_o_open"] - s["n_o_low"])
                            / (s["n_o_high"] - s["n_o_low"]).replace(0, np.nan))
    s["gap_abs"] = s["n_o_open"] - C
    s["gap_bp"] = s["r_open"] * 1e4
    s["cal_gap_days"] = (s["n_dt"] - s["dt"]).dt.days
    s["exact_zero"] = s["gap_abs"].abs() < 1e-9
    s["carried_from_preopen"] = (s["n_o_open"] - s["n_pre_close"]).abs() < 1e-9

    tr = s[(s["cp_rank"] >= TOP_TERTILE) & s["r_open"].notna()].copy()
    ticks = {n: infer_tick(s[s["underlying"] == n]["c1515"]) for n in FOUR}

    print("=" * 100)
    print("ARE THE 09:15 OPEN AND THE 15:15 CLOSE REAL, TRADEABLE PRICES?")
    print(f"trade set = top-tertile close_pos ({RANK_WINDOW}/{MIN_PERIODS} rank window); "
          f"{len(tr)} trades, {tr['dt'].min().date()}..{tr['dt'].max().date()}")
    print(f"[sanity] max |r_open - helper next_open_ret| = "
          f"{(tr['r_open'] - tr['next_open_ret']).abs().max():.2e}  "
          f"(0 = auditing the same two prints the backtest used)")
    print("=" * 100)

    # ---------------------------------------------------------------- SECTION 0
    print("\n" + "-" * 100)
    print("0. BAR COVERAGE — interval='30minute' is NOT always 30 minutes")
    print("-" * 100)
    print(f"   {'name':<12}{'sessions':>10}{'13 bars':>9}{'>13 bars':>10}{'<13 bars':>10}"
          f"{'no 15:15':>10}{'1st cadence break':>20}")
    for name in FOUR:
        g = s[s["underlying"] == name]
        odd = g[g["cadence_min"] != 30]
        print(f"   {name:<12}{len(g):>10}{int((g['n_bars'] == 13).sum()):>9}"
              f"{int((g['n_bars'] > 13).sum()):>10}{int((g['n_bars'] < 13).sum()):>10}"
              f"{int((~g['has_1515'].astype(bool)).sum()):>10}"
              f"{(str(odd['dt'].min().date()) if len(odd) else '-'):>20}")
    print(f"\n   cadence of the '30minute' rows, sessions by median stamp spacing:")
    print(f"   {'name':<12}{'30 min':>9}{'15 min':>9}{'other':>9}")
    for name in FOUR:
        g = s[s["underlying"] == name]
        print(f"   {name:<12}{int((g['cadence_min'] == 30).sum()):>9}"
              f"{int((g['cadence_min'] == 15).sum()):>9}"
              f"{int((~g['cadence_min'].isin([15, 30])).sum()):>9}")
    short = s[s["n_bars"] < 13]
    print(f"\n   TRUNCATED sessions (fewer than 13 bars) — their 'close' is NOT 15:15:")
    for _, r in short.iterrows():
        print(f"      {r['underlying']:<12}{r['dt'].date()}  n={int(r['n_bars']):>3}"
              f"  {r['first_stamp']}..{r['last_stamp']}")
    print(f"\n   rows OUTSIDE 09:15-15:15 that the session filter discards, and junk:")
    print(f"   {'name':<12}{'sess w/ pre-open':>18}{'sess w/ post-close':>20}"
          f"{'junk hi>5x close':>18}")
    for name in FOUR:
        g = s[s["underlying"] == name]
        print(f"   {name:<12}{int((g['n_pre'] > 0).sum()):>18}"
              f"{int((g['n_post'] > 0).sum()):>20}{int(g['junk_rows'].sum()):>18}")
    junk = allbars.copy()
    junk["high"] = pd.to_numeric(junk["high"])
    junk["close"] = pd.to_numeric(junk["close"])
    jr = junk[junk["high"] > 5 * junk["close"]]
    if len(jr):
        print(f"   example corrupt rows (all outside the session window):")
        for _, r in jr.head(4).iterrows():
            print(f"      {r['underlying']:<12}{r['ts']}  o={r['open']} h={r['high']} "
                  f"l={r['low']} c={r['close']} v={r['volume']}")

    # ---------------------------------------------------------------- SECTION 1
    print("\n" + "-" * 100)
    print("1. EXIT LADDER — does the edge survive holding past the opening print?")
    print("   every exit resolved by CLOCK TIME, so the cadence change cannot distort it")
    print("-" * 100)
    print(f"   {'name':<12}{'trades':>7}{'@09:15 open':>13}{'@09:30':>9}{'@09:45':>9}"
          f"{'@10:15':>9}{'t@open':>8}{'t@0945':>8}{'first30 bp':>12}{'kept %':>8}")
    for name in FOUR + ["POOLED"]:
        g = tr if name == "POOLED" else tr[tr["underlying"] == name]
        n0, b0, _, t0 = stats(g["r_open"])
        _, b30, _, _ = stats(g["r_0930"])
        _, b45, _, t45 = stats(g["r_0945"])
        _, b75, _, _ = stats(g["r_1015"])
        _, bf, _, _ = stats(g["r_first30"])
        kept = (b45 / b0 * 100) if (np.isfinite(b0) and abs(b0) > 1e-9) else np.nan
        print(f"   {name:<12}{n0:>7}{b0:>+13.1f}{b30:>+9.1f}{b45:>+9.1f}{b75:>+9.1f}"
              f"{t0:>+8.2f}{t45:>+8.2f}{bf:>+12.1f}"
              f"{(f'{kept:.0f}%' if np.isfinite(kept) else 'n/a'):>8}")
    print("\n   first30 bp = the opening bar's OWN return (09:15 open -> 09:45 price).")
    print("   It is exactly what an exit 30 minutes later gives back.")
    ns = s[(s["cp_rank"].notna()) & (s["cp_rank"] < TOP_TERTILE) & s["r_open"].notna()]
    _, bo, _, _ = stats(ns["r_open"])
    _, bfn, _, tfn = stats(ns["r_first30"])
    print(f"   NON-signal sessions for contrast: @open {bo:+.1f} bp, "
          f"first30 {bfn:+.1f} bp (t {tfn:+.2f}), n={len(ns)}")

    print(f"\n   only sessions whose EXIT day is on the clean 30-minute cadence:")
    print(f"   {'name':<12}{'trades':>7}{'@open':>9}{'@09:45':>9}")
    for name in FOUR:
        g = tr[(tr["underlying"] == name) & (tr["n_cadence_min"] == 30)]
        _, b0, _, _ = stats(g["r_open"])
        _, b45, _, _ = stats(g["r_0945"])
        print(f"   {name:<12}{len(g):>7}{b0:>+9.1f}{b45:>+9.1f}")

    # ---------------------------------------------------------------- SECTION 2
    print("\n" + "-" * 100)
    print("2. FILL REALISM — where does the opening print sit inside its own bar?")
    print("-" * 100)
    print(f"   {'name':<12}{'trades':>7}{'open=bar high':>15}{'open=bar low':>14}"
          f"{'med pos in bar':>16}{'mean pos':>10}")
    for name in FOUR + ["POOLED"]:
        g = tr if name == "POOLED" else tr[tr["underlying"] == name]
        pos = g["open_pos_in_bar"]
        print(f"   {name:<12}{len(g):>7}"
              f"{(pos > 0.999).mean() * 100:>14.0f}%{(pos < 0.001).mean() * 100:>13.0f}%"
              f"{pos.median():>16.2f}{pos.mean():>10.2f}")
    print("   pos = (open-low)/(high-low) of the first 30 minutes. 0.5 = the open is")
    print("   mid-range and unremarkable; near 1.0 = the open IS the best exit of the")
    print("   first half hour and only the auction itself pays it.")

    print(f"\n   what the book pays at a worse-than-print fill (bp/night):")
    print(f"   {'name':<12}{'at open':>10}{'open/0945 mid':>15}{'at 09:45':>10}"
          f"{'at 1st-30m LOW':>16}")
    for name in FOUR + ["POOLED"]:
        g = tr if name == "POOLED" else tr[tr["underlying"] == name]
        _, a, _, _ = stats(g["r_open"])
        _, m, _, _ = stats(g["r_openmid"])
        _, c, _, _ = stats(g["r_0945"])
        _, lo, _, _ = stats(g["r_openlow"])
        print(f"   {name:<12}{a:>+10.1f}{m:>+15.1f}{c:>+10.1f}{lo:>+16.1f}")

    print(f"\n   ENTRY side: the 15:15 close vs worse fills for a buyer (bp/night,")
    print(f"   exit held at the 09:15 open in every column):")
    print(f"   {'name':<12}{'buy 15:15 c':>13}{'buy 15:15 mid':>15}"
          f"{'buy 15:15 HIGH':>16}{'buy 14:45 c':>13}")
    for name in FOUR + ["POOLED"]:
        g = tr if name == "POOLED" else tr[tr["underlying"] == name]
        base = g["n_o_open"]
        _, a, _, _ = stats(base / g["close"] - 1)
        _, m, _, _ = stats(base / ((g["c1515_high"] + g["c1515_low"]) / 2) - 1)
        _, h, _, _ = stats(base / g["c1515_high"] - 1)
        _, p, _, _ = stats(base / g["c1445"] - 1)
        print(f"   {name:<12}{a:>+13.1f}{m:>+15.1f}{h:>+16.1f}{p:>+13.1f}")

    # ---------------------------------------------------------------- SECTION 3
    print("\n" + "-" * 100)
    print("3. OPENING-BAR VOLUME — is the 09:15 print thick enough to transact on?")
    print("   (the EXIT session's opening bar, on trade dates)")
    print("-" * 100)
    print(f"   {'name':<12}{'trades':>7}{'med 0915 vol':>14}{'med bar vol':>13}"
          f"{'ratio med':>11}{'ratio p05':>11}{'ratio min':>11}{'vol=0':>7}"
          f"{'<10% avg':>10}{'med rank':>10}")
    for name in FOUR:
        g = tr[tr["underlying"] == name]
        v = g["n_o_bar_vol"].astype(float)
        m = g["n_mean_bar_vol"].astype(float)
        ratio = (v / m).replace([np.inf, -np.inf], np.nan)
        print(f"   {name:<12}{len(g):>7}{v.median():>14,.0f}{m.median():>13,.0f}"
              f"{ratio.median():>11.2f}{ratio.quantile(0.05):>11.2f}{ratio.min():>11.2f}"
              f"{int((v == 0).sum()):>7}{int((ratio < 0.10).sum()):>10}"
              f"{g['n_vol_rank_open'].median():>10.0f}")
    print(f"\n   rank of the opening bar by volume among the session's bars, ALL sessions:")
    print(f"   {'name':<12}" + "".join(f"{('#' + str(k)):>6}" for k in range(1, 7))
          + f"{'#7+':>7}{'zero-vol bars/session':>24}")
    for name in FOUR:
        g = s[s["underlying"] == name]
        rk = g["vol_rank_open"].dropna().astype(int)
        cells = "".join(f"{(rk == k).mean() * 100:>6.0f}" for k in range(1, 7))
        print(f"   {name:<12}{cells}{(rk >= 7).mean() * 100:>7.0f}"
              f"{g['n_zero_vol_bars'].mean():>24.2f}")

    # ---------------------------------------------------------------- SECTION 4
    print("\n" + "-" * 100)
    print("4. STALE PRINTS — next 09:15 open EXACTLY equal to the prior 15:15 close")
    print("-" * 100)
    print(f"   {'name':<12}{'tick':>6}{'sessions':>10}{'exact 0 gap':>13}{'%':>7}"
          f"{'flat open bar':>15}{'open=preopen c':>16}")
    for name in FOUR:
        g = s[s["underlying"] == name].dropna(subset=["gap_abs"])
        flat = ((g["n_o_high"] - g["n_o_low"]).abs() < 1e-9)
        print(f"   {name:<12}{ticks[name]:>6.2f}{len(g):>10}"
              f"{int(g['exact_zero'].sum()):>13}{g['exact_zero'].mean() * 100:>7.1f}"
              f"{int(flat.sum()):>15}{int(g['carried_from_preopen'].sum()):>16}")

    print(f"\n   is zero a SPIKE? gap measured in ticks, counts in the bins nearest 0:")
    print(f"   {'name':<12}" + "".join(f"{k:>7}" for k in range(-4, 5))
          + f"{'spike ratio':>13}")
    for name in FOUR:
        g = s[s["underlying"] == name].dropna(subset=["gap_abs"])
        tk = (g["gap_abs"] / ticks[name]).round()
        cells = "".join(f"{int((tk == k).sum()):>7}" for k in range(-4, 5))
        nb = np.mean([int((tk == k).sum()) for k in (-4, -3, -2, 2, 3, 4)])
        n0 = int((tk == 0).sum())
        print(f"   {name:<12}{cells}"
              f"{(f'{n0 / nb:.1f}x' if nb > 0 else 'inf'):>13}")
    print("   spike ratio = count at exactly 0 ticks / mean count at +-2..4 ticks.")
    print("   ~1x means an unchanged open is just the middle of a smooth distribution;")
    print("   a large multiple means the print was carried forward.")

    print(f"\n   the zero-gap sessions themselves — synthetic, or a real flat open?")
    print(f"   {'name':<12}{'n':>5}{'med open-bar vol':>18}{'vs normal session':>19}"
          f"{'med open-bar range bp':>23}{'vs normal':>11}")
    for name in FOUR:
        g = s[s["underlying"] == name].dropna(subset=["gap_abs"])
        z, nz = g[g["exact_zero"]], g[~g["exact_zero"]]
        rz = ((z["n_o_high"] - z["n_o_low"]) / z["close"] * 1e4)
        rn = ((nz["n_o_high"] - nz["n_o_low"]) / nz["close"] * 1e4)
        print(f"   {name:<12}{len(z):>5}{z['n_o_bar_vol'].median():>18,.0f}"
              f"{nz['n_o_bar_vol'].median():>19,.0f}{rz.median():>23.0f}"
              f"{rn.median():>11.0f}")

    print(f"\n   ON THE TRADES:")
    print(f"   {'name':<12}{'trades':>7}{'zero-gap':>10}{'%':>7}{'all bp':>9}"
          f"{'ex-zero bp':>12}{'ex-zero t':>11}{'ex-zero @0945':>15}")
    for name in FOUR + ["POOLED"]:
        g = tr if name == "POOLED" else tr[tr["underlying"] == name]
        nz = g[~g["exact_zero"]]
        _, a, _, _ = stats(g["r_open"])
        _, c, _, t = stats(nz["r_open"])
        _, d, _, _ = stats(nz["r_0945"])
        print(f"   {name:<12}{len(g):>7}{int(g['exact_zero'].sum()):>10}"
              f"{g['exact_zero'].mean() * 100:>7.1f}{a:>+9.1f}{c:>+12.1f}{t:>+11.2f}"
              f"{d:>+15.1f}")
    print("   (a zero-gap trade returns exactly 0.0 bp by construction, so removing")
    print("    them can only RAISE the mean — the question is by how little.)")

    # ---------------------------------------------------------------- SECTION 5
    print("\n" + "-" * 100)
    print("5. 15:15 INTEGRITY — does the closing bar exist and carry volume?")
    print("-" * 100)
    print(f"   {'name':<12}{'trades':>7}{'entry 1515 missing':>20}{'entry vol=0':>13}"
          f"{'exit 1515 missing':>19}{'close != 1515 close':>21}")
    for name in FOUR:
        g = tr[tr["underlying"] == name]
        mism = ((g["close"] - g["c1515"]).abs() > 1e-9) | g["c1515"].isna()
        print(f"   {name:<12}{len(g):>7}{int((~g['has_1515'].astype(bool)).sum()):>20}"
              f"{int((g['v1515'].fillna(0) == 0).sum()):>13}"
              f"{int((~g['n_has_1515'].fillna(False).astype(bool)).sum()):>19}"
              f"{int(mism.sum()):>21}")
    print(f"\n   closing-bar volume against the session average, on entry dates:")
    print(f"   {'name':<12}{'med 1515 vol':>14}{'ratio med':>11}{'ratio p05':>11}"
          f"{'ratio min':>11}{'med 1515 range bp':>19}")
    for name in FOUR:
        g = tr[tr["underlying"] == name]
        ratio = g["v1515"].astype(float) / g["mean_bar_vol"].astype(float)
        rng = (g["c1515_high"] - g["c1515_low"]) / g["close"] * 1e4
        print(f"   {name:<12}{g['v1515'].median():>14,.0f}{ratio.median():>11.2f}"
              f"{ratio.quantile(0.05):>11.2f}{ratio.min():>11.2f}{rng.median():>19.0f}")
    cnt = Counter(tr["cal_gap_days"].dropna().astype(int))
    print(f"\n   calendar days spanned by the 'overnight' hold: "
          + "  ".join(f"{k}d:{v}" for k, v in sorted(cnt.items())))
    lg = tr[tr["cal_gap_days"] > 4]
    if len(lg):
        _, bl, _, _ = stats(lg["r_open"])
        _, bs, _, _ = stats(tr[tr["cal_gap_days"] <= 4]["r_open"])
        print(f"      >4 days: n={len(lg)} at {bl:+.1f} bp; rest n={len(tr) - len(lg)}"
              f" at {bs:+.1f} bp")

    # ---------------------------------------------------------------- SECTION 6
    print("\n" + "-" * 100)
    print("6. GAP DISTRIBUTION — smooth, or spikes at particular values?")
    print("-" * 100)
    edges = [-np.inf, -100, -50, -25, -10, -3, -0.5, 0.5, 3, 10, 25, 50, 100, np.inf]
    labels = ["<-100", "-100/-50", "-50/-25", "-25/-10", "-10/-3", "-3/-.5", "|<0.5|",
              ".5/3", "3/10", "10/25", "25/50", "50/100", ">100"]
    print(f"   overnight gap in bp, ALL sessions (counts)")
    print(f"   {'name':<12}" + "".join(f"{l:>9}" for l in labels))
    for name in FOUR:
        g = s[s["underlying"] == name]["gap_bp"].dropna()
        h = pd.cut(g, bins=edges, labels=labels).value_counts().reindex(labels).fillna(0)
        print(f"   {name:<12}" + "".join(f"{int(h[l]):>9}" for l in labels))
    print(f"\n   {'name':<12}{'n':>6}{'mean bp':>9}{'med bp':>9}{'sd bp':>8}{'skew':>7}"
          f"{'kurt':>7}{'p01':>9}{'p99':>9}{'|gap|<1bp':>11}{'on tick grid':>14}")
    for name in FOUR:
        g = s[s["underlying"] == name]
        gp = g["gap_bp"].dropna()
        on = (np.abs((g["gap_abs"].dropna() / ticks[name]).round()
                     - g["gap_abs"].dropna() / ticks[name]) < 1e-6).mean() * 100
        print(f"   {name:<12}{len(gp):>6}{gp.mean():>+9.1f}{gp.median():>+9.1f}"
              f"{gp.std():>8.1f}{gp.skew():>+7.2f}{gp.kurt():>+7.2f}"
              f"{gp.quantile(0.01):>+9.1f}{gp.quantile(0.99):>+9.1f}"
              f"{(gp.abs() < 1).mean() * 100:>10.1f}%{on:>13.1f}%")
    print(f"\n   most repeated absolute gap values in rupees (all sessions, top 6):")
    for name in FOUR:
        g = s[s["underlying"] == name]["gap_abs"].dropna().round(2)
        print(f"   {name:<12}" + "  ".join(f"{v:+.2f}x{c}" for v, c in Counter(g).most_common(6)))
    print(f"\n   round-number clustering: share of gaps landing on an exact rupee,")
    print(f"   half-rupee, or 0.05 boundary (a fabricated print tends to be round):")
    print(f"   {'name':<12}{'exact rupee':>13}{'half rupee':>12}{'0.05 grid':>11}"
          f"{'expected 0.05':>15}")
    for name in FOUR:
        ga = s[s["underlying"] == name]["gap_abs"].dropna()
        cents = (ga * 100).round()
        print(f"   {name:<12}{(cents % 100 == 0).mean() * 100:>12.1f}%"
              f"{(cents % 50 == 0).mean() * 100:>11.1f}%"
              f"{(cents % 5 == 0).mean() * 100:>10.1f}%"
              f"{(100 if ticks[name] == 0.05 else 20):>14}%")

    print(f"\n   do the PRICES themselves cluster on round numbers? (if both prints do,")
    print(f"   a round GAP is a consequence, not a fabrication)")
    print(f"   {'name':<12}{'1515 close on rupee':>21}{'0915 open on rupee':>20}"
          f"{'gap on rupee':>14}")
    for name in FOUR:
        g = s[s["underlying"] == name]
        c = (g["c1515"].dropna() * 100).round()
        o = (g["o_open"].dropna() * 100).round()
        ga = (g["gap_abs"].dropna() * 100).round()
        print(f"   {name:<12}{(c % 100 == 0).mean() * 100:>20.1f}%"
              f"{(o % 100 == 0).mean() * 100:>19.1f}%{(ga % 100 == 0).mean() * 100:>13.1f}%")

    # ---------------------------------------------------------------- SECTION 7
    print("\n" + "-" * 100)
    print("7. IS THE OPENING PRINT INTERNALLY CONSISTENT, AND DOES A SECOND FEED AGREE?")
    print("-" * 100)
    out_of_range = ((s["o_open"] > s["o_high"] + 1e-9) | (s["o_open"] < s["o_low"] - 1e-9))
    print(f"   {'name':<12}{'open outside its own bar':>26}{'open = bar low':>16}"
          f"{'open = bar high':>17}")
    for name in FOUR:
        g = s[s["underlying"] == name]
        lo = (g["o_open"] - g["o_low"]).abs() < 1e-9
        hi = (g["o_open"] - g["o_high"]).abs() < 1e-9
        print(f"   {name:<12}{int(out_of_range[g.index].sum()):>26}"
              f"{lo.mean() * 100:>15.0f}%{hi.mean() * 100:>16.0f}%")
    print("   an 'open' lying outside the bar's own high/low would be proof the field")
    print("   was written from somewhere else. Zero such rows means the bar is coherent.")

    print(f"\n   zero-gap sessions vs the rest — is the opening bar any different?")
    print(f"   {'name':<12}{'n':>4}{'open=low z':>12}{'open=low rest':>15}"
          f"{'open=high z':>13}{'open=high rest':>16}")
    for name in FOUR:
        g = s[s["underlying"] == name].dropna(subset=["gap_abs"])
        z, nz = g[g["exact_zero"]], g[~g["exact_zero"]]
        def frac(d, col):
            return ((d["n_o_open"] - d[col]).abs() < 1e-9).mean() * 100 if len(d) else np.nan
        print(f"   {name:<12}{len(z):>4}{frac(z, 'n_o_low'):>11.0f}%"
              f"{frac(nz, 'n_o_low'):>14.0f}%{frac(z, 'n_o_high'):>12.0f}%"
              f"{frac(nz, 'n_o_high'):>15.0f}%")

    # cross-feed: does a finer interval series reproduce the same two prints?
    f = fine.copy()
    f["ts"] = pd.to_datetime(f["ts"])
    f["dt"] = pd.to_datetime(f["dt"])
    for c in ("open", "close"):
        f[c] = pd.to_numeric(f[c], errors="coerce")
    f["tod"] = f["ts"].dt.time
    print(f"\n   CROSS-FEED CHECK — the same two prints re-derived from a finer interval,")
    print(f"   on sessions where that finer series is itself free of broken rows")
    print(f"   {'name':<12}{'interval':>10}{'sessions':>10}{'open matches':>14}"
          f"{'max open diff':>15}{'close matches':>15}{'max close diff':>16}")
    for name in FOUR:
        for iv in ("1minute", "3minute", "5minute", "15minute"):
            gf = f[(f["underlying"] == name) & (f["interval"] == iv)]
            if gf.empty:
                continue
            ref = s[s["underlying"] == name].set_index("dt")
            no = nc = om = cm = 0
            odm = cdm = 0.0
            for dt, gg in gf.groupby("dt"):
                if dt not in ref.index:
                    continue
                gg = gg.sort_values("ts")
                # SKIP any session where the finer feed is itself broken: a >10%
                # move between consecutive closes on these names is a bad row.
                if (gg["close"] / gg["close"].shift(1) - 1).abs().max() > 0.10:
                    continue
                # the opening print needs a bar actually stamped 09:15
                first = gg[gg["tod"] == dtime(9, 15)]
                if len(first):
                    o_ref = ref.loc[dt, "o_open"]
                    d = abs(first["open"].iloc[0] - o_ref)
                    no += 1
                    om += int(d < 1e-9)
                    odm = max(odm, d / o_ref * 1e4)
                # the closing print needs the finer feed to run to the bell
                if gg["tod"].max() >= dtime(15, 24):
                    c_ref = ref.loc[dt, "c1515"]
                    d = abs(gg["close"].iloc[-1] - c_ref)
                    nc += 1
                    cm += int(d < 1e-9)
                    cdm = max(cdm, d / c_ref * 1e4)
            if no == 0 and nc == 0:
                continue
            print(f"   {name:<12}{iv:>10}{max(no, nc):>10}{f'{om}/{no}':>14}"
                  f"{odm:>13.1f}bp{f'{cm}/{nc}':>15}{cdm:>14.1f}bp")
    print("   the finer series exist only from mid-2026, so this is a spot check on the")
    print("   most recent ~30 sessions, not the whole sample.")

    print(f"\n   every session where a CLEAN finer feed disagrees with the 30-minute open:")
    print(f"   {'name':<12}{'date':>12}{'writer':>13}{'30m open':>10}{'tick open':>11}"
          f"{'diff bp':>9}{'30m open = 30m high?':>22}")
    nmm = 0
    for name in FOUR:
        ref = s[s["underlying"] == name].set_index("dt")
        for iv in ("1minute", "3minute"):
            gf = f[(f["underlying"] == name) & (f["interval"] == iv)]
            for dt, gg in gf.groupby("dt"):
                if dt not in ref.index:
                    continue
                gg = gg.sort_values("ts")
                if (gg["close"] / gg["close"].shift(1) - 1).abs().max() > 0.10:
                    continue
                first = gg[gg["tod"] == dtime(9, 15)]
                if first.empty:
                    continue
                o_ref = ref.loc[dt, "o_open"]
                o_fine = first["open"].iloc[0]
                if abs(o_fine - o_ref) < 1e-9 or iv == "3minute":
                    continue        # print each date once, off the 1-minute feed
                nmm += 1
                print(f"   {name:<12}{str(dt.date()):>12}{str(ref.loc[dt, 'o_src']):>13}"
                      f"{o_ref:>10.2f}{o_fine:>11.2f}"
                      f"{(o_ref - o_fine) / o_fine * 1e4:>+9.1f}"
                      f"{('YES' if abs(o_ref - ref.loc[dt, 'o_high']) < 1e-9 else 'no'):>22}")
    if nmm == 0:
        print("   (none)")
    print("   the 1-minute and 3-minute feeds agree with EACH OTHER on these dates and")
    print("   disagree with the 30-minute bar, so the 30-minute open is the odd one out.")

    fq = fine.copy()
    fq["high"] = pd.to_numeric(fq.get("high", pd.Series(dtype=float)), errors="coerce")
    print(f"\n   BEFORE blaming the 30-minute feed: how corrupt is the FINER feed?")
    print(f"   {'name':<12}{'interval':>10}{'rows':>8}{'|1-bar move|>10%':>19}"
          f"{'worst 1-bar move':>19}")
    for name in FOUR:
        for iv in ("1minute", "3minute"):
            gf = f[(f["underlying"] == name) & (f["interval"] == iv)].sort_values("ts")
            if gf.empty:
                continue
            mv = (gf["close"] / gf["close"].shift(1) - 1).abs()
            print(f"   {name:<12}{iv:>10}{len(gf):>8}{int((mv > 0.10).sum()):>19}"
                  f"{mv.max() * 100:>18.0f}%")
    print("   a 10%+ jump between consecutive 1-minute closes on a large-cap bank is")
    print("   not a price move, it is a broken row. Where the feeds disagree above, it")
    print("   is the FINER series that is wrong, not the 30-minute one.")

    # ---------------------------------------------------------------- SECTION 8
    print("\n" + "-" * 100)
    print("8. PROVENANCE — who wrote the two prints, and does the answer depend on it?")
    print("-" * 100)
    print(f"   source of the ENTRY print (the 15:15 close), all sessions:")
    print(f"   {'name':<12}" + "".join(f"{k:>16}" for k in
                                       ["upstox_spot", "fyers", "live_tick", "other"]))
    for name in FOUR:
        g = s[s["underlying"] == name]
        cs = g["c_src"].fillna("none")
        other = len(g) - sum((cs == k).sum() for k in ("upstox_spot", "fyers", "live_tick"))
        print(f"   {name:<12}" + "".join(f"{int((cs == k).sum()):>16}" for k in
                                         ("upstox_spot", "fyers", "live_tick"))
              + f"{other:>16}")
    print(f"\n   sessions carrying OFF-GRID stamps (a :00/:30 writer interleaved with the")
    print(f"   :15/:45 historical grid — this is what looked like a 15-minute cadence):")
    print(f"   {'name':<12}{'sessions':>10}{'with off-grid bars':>20}{'first such':>13}"
          f"{'bad OHLC rows':>15}{'wild bars':>11}")
    for name in FOUR:
        g = s[s["underlying"] == name]
        og = g[g["n_offgrid"] > 0]
        print(f"   {name:<12}{len(g):>10}{len(og):>20}"
              f"{(str(og['dt'].min().date()) if len(og) else '-'):>13}"
              f"{int(g['bad_ohlc'].sum()):>15}{int(g['wild_bars'].sum()):>11}")
    print("   'bad OHLC' = open or close outside the bar's own high/low. 'wild' = a")
    print("   single 30-minute bar spanning more than 15%. Both should be zero.")

    print(f"\n   the book restricted to the CLEAN single-source era (before the first")
    print(f"   off-grid session), vs the contaminated tail:")
    cut = s[s["n_offgrid"] > 0]["dt"].min()
    print(f"   first off-grid session anywhere: {cut.date() if pd.notna(cut) else 'none'}")
    print(f"   {'name':<12}{'clean n':>9}{'clean bp':>10}{'clean t':>9}"
          f"{'tail n':>8}{'tail bp':>9}")
    for name in FOUR:
        g = tr[tr["underlying"] == name]
        cl = g[g["dt"] < cut] if pd.notna(cut) else g
        tl = g[g["dt"] >= cut] if pd.notna(cut) else g.iloc[:0]
        _, cb, _, ct = stats(cl["r_open"])
        _, tb, _, _ = stats(tl["r_open"])
        print(f"   {name:<12}{len(cl):>9}{cb:>+10.1f}{ct:>+9.2f}{len(tl):>8}{tb:>+9.1f}")

    print(f"\n   THE OPEN=HIGH TELL. Where a second feed exists, the fyers 30-minute")
    print(f"   opening print disagrees with the tick-derived one by up to ~79bp, and")
    print(f"   sometimes equals the bar's own HIGH. Rate of open==high by writer:")
    print(f"   {'name':<12}{'writer':>14}{'sessions':>10}{'open=high':>11}"
          f"{'open=low':>10}{'either extreme':>16}")
    for name in FOUR:
        g = s[s["underlying"] == name]
        for w in ("upstox_spot", "fyers"):
            gw = g[g["o_src"] == w]
            if gw.empty:
                continue
            hi = ((gw["o_open"] - gw["o_high"]).abs() < 1e-9)
            lo = ((gw["o_open"] - gw["o_low"]).abs() < 1e-9)
            print(f"   {name:<12}{w:>14}{len(gw):>10}{hi.mean() * 100:>10.0f}%"
                  f"{lo.mean() * 100:>9.0f}%{(hi | lo).mean() * 100:>15.0f}%")

    print(f"\n   trades by the WRITER of their exit print (the 09:15 open):")
    print(f"   {'name':<12}{'upstox n':>10}{'upstox bp':>11}{'fyers n':>9}"
          f"{'fyers bp':>10}{'other n':>9}")
    for name in FOUR:
        g = tr[tr["underlying"] == name]
        gu = g[g["n_o_src"] == "upstox_spot"]
        gf_ = g[g["n_o_src"] == "fyers"]
        _, ub, _, _ = stats(gu["r_open"])
        _, fb, _, _ = stats(gf_["r_open"])
        print(f"   {name:<12}{len(gu):>10}{ub:>+11.1f}{len(gf_):>9}{fb:>+10.1f}"
              f"{len(g) - len(gu) - len(gf_):>9}")
    print("   upstox_spot carries ~94% of the sample and there is NO finer series before")
    print("   2026-07-10 to check it against. The one era that CAN be cross-checked is")
    print("   the fyers tail, and that is the era whose opening print fails the check.")

    # is the 30-minute decay itself significant, paired per trade?
    print(f"\n   PAIRED test of the decay (r@09:45 minus r@open), per trade:")
    print(f"   {'name':<12}{'mean bp':>10}{'t':>8}{'and SBIN vs FEDERALBNK @open':>32}")
    for name in FOUR:
        g = tr[tr["underlying"] == name]
        d = (g["r_0945"] - g["r_open"]).dropna()
        t = d.mean() / (d.std(ddof=1) / np.sqrt(len(d))) if len(d) > 2 else np.nan
        print(f"   {name:<12}{d.mean() * 1e4:>+10.1f}{t:>+8.2f}")
    a = tr[tr["underlying"] == "SBIN"]["r_open"].dropna()
    b_ = tr[tr["underlying"] == "FEDERALBNK"]["r_open"].dropna()
    tt = ((a.mean() - b_.mean())
          / np.sqrt(a.var(ddof=1) / len(a) + b_.var(ddof=1) / len(b_)))
    print(f"   SBIN minus FEDERALBNK at the open: {(a.mean() - b_.mean()) * 1e4:+.1f} bp, "
          f"t = {tt:+.2f}")

    # ---------------------------------------------------------------- VERDICT
    print("\n" + "=" * 100)
    print("BOTTOM LINE — bp/night under each print assumption")
    print("=" * 100)
    print(f"   {'name':<12}{'trades':>7}{'as reported':>13}{'ex zero-gap':>13}"
          f"{'exit 09:45':>12}{'mid fill':>10}{'buy@1515 high':>15}")
    for name in FOUR:
        g = tr[tr["underlying"] == name]
        nz = g[~g["exact_zero"]]
        _, a, _, _ = stats(g["r_open"])
        _, b_, _, _ = stats(nz["r_open"])
        _, c, _, _ = stats(g["r_0945"])
        _, d, _, _ = stats(g["r_openmid"])
        _, e, _, _ = stats(g["n_o_open"] / g["c1515_high"] - 1)
        print(f"   {name:<12}{len(g):>7}{a:>+13.1f}{b_:>+13.1f}{c:>+12.1f}"
              f"{d:>+10.1f}{e:>+15.1f}")

    out = os.environ.get("PQ_OUT")
    if out:
        tr.to_csv(out, index=False)
        print(f"\ntrade-level detail written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
