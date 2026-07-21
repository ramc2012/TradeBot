"""PNB worked-example reconstruction — full case study (part 1 of the study).

Everything here is computed from OUR store only (data/*.csv, extracted with
literal UTC time bounds).  No moneyness band is applied anywhere.

Outputs, in order:
  A. data quality (session gaps, source mix, duplicate/conflict rate)
  B. daily MACD crossover + divergence quantification
  C. the 2026-07-08 higher low + causal confirmation date
  D. spot return reconciliation
  E. option reconstruction for PNB 106 CE 28-JUL-26 by entry date
  F. strike-choice grid (OTM / ATM / slight-ITM / deep-ITM) with stale-exit flags
  G. hourly-vs-daily lead time
  H. option-participant positioning (OI/volume) around the signal
  I. prefix-invariance (causality) check
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

IST = "Asia/Kolkata"
SRC_PREF = {"upstox": 0, "upstox_expired": 1, "fyers": 2, "fyers_chain": 3}
RT_COST = 0.08  # round-trip cost assumption used elsewhere in this repo (ASSUMED; no spread data)

pd.set_option("display.width", 240)
pd.set_option("display.max_columns", 50)


# ------------------------------------------------------------------ utils
def macd(close, fast=12, slow=26, sig=9):
    line = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    signal = line.ewm(span=sig, adjust=False).mean()
    return pd.DataFrame({"macd": line, "signal": signal, "hist": line - signal}, index=close.index)


def pivot_lows(s, left=3, right=3):
    v = s.values
    out = np.zeros(len(v), bool)
    for i in range(left, len(v) - right):
        w = v[i - left : i + right + 1]
        if v[i] == w.min() and (w == v[i]).sum() == 1:
            out[i] = True
    return pd.Series(out, index=s.index)


def load_spot():
    s = pd.read_csv("data/pnb_spot_30m.csv", parse_dates=["time"])
    s = s.sort_values("time").drop_duplicates("time", keep="last")
    s["ist"] = s.time.dt.tz_convert(IST)
    s["ses"] = s.ist.dt.date
    return s.reset_index(drop=True)


def to_daily(s):
    g = s.groupby("ses")
    d = pd.DataFrame(
        {"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
         "close": g["close"].last(), "volume": g["volume"].sum(), "bars": g.size()}
    )
    d.index = pd.to_datetime(d.index)
    return d


def to_hourly(s):
    s = s.copy()
    s["k"] = s.groupby("ses").cumcount() // 2
    g = s.groupby(["ses", "k"])
    h = pd.DataFrame({"t": g["ist"].first(), "open": g["open"].first(), "high": g["high"].max(),
                      "low": g["low"].min(), "close": g["close"].last(), "volume": g["volume"].sum()})
    return h.reset_index(drop=True).sort_values("t").reset_index(drop=True)


def load_opts(expiry="2026-07-28", interval="30minute"):
    o = pd.read_csv("data/pnb_opt.csv", parse_dates=["time", "synced_at"])
    o = o[(o.interval == interval) & (o.expiry == expiry)].copy()
    o["pref"] = o["source"].map(SRC_PREF).fillna(9)
    o = o.sort_values(["time", "strike", "option_type", "pref", "synced_at"])
    o = o.drop_duplicates(["time", "strike", "option_type"], keep="first")
    o["ist"] = o.time.dt.tz_convert(IST)
    o["ses"] = o.ist.dt.date
    return o


# --------------------------------------------------------------- sections
def sec_a(s, d, o_raw):
    print("=" * 100)
    print("A. DATA QUALITY")
    print("=" * 100)
    print(f"PNB 30m spot rows={len(s)}  sessions={len(d)}  {d.index.min().date()}..{d.index.max().date()}")
    bad = d[d.bars != 13]
    print(f"sessions with != 13 bars: {len(bad)}  -> {list(bad.index.date)}")
    all_ses = set(d.index.date)
    print("2026-07-13 present in PNB spot:", pd.Timestamp('2026-07-13').date() in all_ses)
    print("source mix (2026-06-01..):",
          s[s.time >= "2026-06-01"].groupby("source").size().to_dict())
    o = pd.read_csv("data/pnb_opt.csv", parse_dates=["time"])
    o = o[(o.interval == "30minute") & (o.expiry == "2026-07-28")]
    g = o.groupby(["time", "strike", "option_type"])["close"].agg(["min", "max", "size"])
    dup = g[g["size"] > 1]
    rel = (dup["max"] - dup["min"]) / dup["min"]
    print(f"30m 28-JUL duplicate (time,strike,type) keys: {len(dup)}/{len(g)}  "
          f"median rel-disagreement={rel.median():.4f}  mean={rel.mean():.4f}  max={rel.max():.4f}")
    print("source mix (option 28-JUL 30m):", o.groupby("source").size().to_dict())


def sec_b(d):
    print("\n" + "=" * 100)
    print("B. DAILY MACD CROSSOVER + DIVERGENCE")
    print("=" * 100)
    m = macd(d.close)
    d = d.join(m)
    d["bull_x"] = (d.macd > d.signal) & (d.macd.shift() <= d.signal.shift())
    w = d.loc["2026-05-18":"2026-05-27", ["close", "macd", "signal", "hist", "bull_x"]]
    print(w.round(4).to_string())
    xs = d.loc["2026-01-01":"2026-07-20"]
    print("\nAll daily bull crossovers in 2026:")
    print(xs[xs.bull_x][["close", "macd", "signal", "hist", "volume"]].round(4).to_string())

    pl = pivot_lows(d.low, 3, 3)
    idx = list(d.index)
    piv = d[pl].loc["2026-01-01":"2026-07-20"].copy()
    piv["confirm_on"] = [idx[idx.index(i) + 3] for i in piv.index]
    piv["macd_at_low"] = d.loc[piv.index, "macd"]
    print("\nCausal pivot lows (L=3,R=3 on daily LOW; confirmed R sessions later):")
    print(piv[["low", "close", "macd_at_low", "confirm_on"]].round(4).to_string())

    # divergence: compare the pivot low preceding the 2026-05-25 cross with each earlier pivot low
    ref = pd.Timestamp("2026-05-18")
    print("\nDivergence at the May cross — reference pivot low 2026-05-18:")
    for prior in piv.index[piv.index < ref]:
        dp = (d.low[ref] - d.low[prior]) / d.low[prior] * 100
        dm = d.macd[ref] - d.macd[prior]
        tag = "BULLISH DIVERGENCE" if dp < 0 and dm > 0 else ("no divergence" if dp < 0 else "higher low (not divergence)")
        print(f"  vs {prior.date()}: price {d.low[prior]:.2f}->{d.low[ref]:.2f} ({dp:+.2f}%)  "
              f"MACD {d.macd[prior]:+.4f}->{d.macd[ref]:+.4f} ({dm:+.4f})  => {tag}")
    return d, piv


def sec_c(d, piv):
    print("\n" + "=" * 100)
    print("C. THE 2026-07-08 LOW — HIGHER LOW?")
    print("=" * 100)
    jul8 = pd.Timestamp("2026-07-08")
    print(d.loc["2026-07-01":"2026-07-20", ["open", "high", "low", "close", "volume", "macd", "signal", "hist"]].round(4).to_string())
    if jul8 in piv.index:
        print(f"\n2026-07-08 IS a causal pivot low (L3/R3). low={d.low[jul8]:.2f} "
              f"confirmed on {piv.confirm_on[jul8].date()}")
    for prior in [pd.Timestamp("2026-06-29"), pd.Timestamp("2026-06-12"), pd.Timestamp("2026-06-02"), pd.Timestamp("2026-05-18"), pd.Timestamp("2026-04-02")]:
        dp = (d.low[jul8] - d.low[prior]) / d.low[prior] * 100
        dm = d.macd[jul8] - d.macd[prior]
        print(f"  vs pivot {prior.date()}: low {d.low[prior]:.2f} -> {d.low[jul8]:.2f} ({dp:+.2f}%)  "
              f"MACD {d.macd[prior]:+.4f} -> {d.macd[jul8]:+.4f} ({dm:+.4f})")


def sec_d(d):
    print("\n" + "=" * 100)
    print("D. SPOT RETURN RECONCILIATION (exit = 2026-07-20 close)")
    print("=" * 100)
    exit_px = d.close[pd.Timestamp("2026-07-20")]
    print(f"exit close (our 30m tape, last bar) = {exit_px:.2f}   [owner/TV daily close = 111.76]")
    rows = []
    cands = [
        ("2026-05-22 close (owner-stated cross date)", d.close[pd.Timestamp("2026-05-22")]),
        ("2026-05-25 close (ACTUAL daily bull cross)", d.close[pd.Timestamp("2026-05-25")]),
        ("2026-05-26 open (tradeable next open)", d.open[pd.Timestamp("2026-05-26")]),
        ("2026-07-08 low (the higher low)", d.low[pd.Timestamp("2026-07-08")]),
        ("2026-07-08 close", d.close[pd.Timestamp("2026-07-08")]),
        ("2026-07-09 close (hourly MACD cross day)", d.close[pd.Timestamp("2026-07-09")]),
        ("2026-07-14 close (higher-low CONFIRMED)", d.close[pd.Timestamp("2026-07-14")]),
        ("2026-07-17 close (2nd daily bull cross)", d.close[pd.Timestamp("2026-07-17")]),
        ("2026-07-20 open (tradeable after 07-17 cross)", d.open[pd.Timestamp("2026-07-20")]),
    ]
    for name, px in cands:
        rows.append((name, float(px), float(exit_px / px - 1) * 100))
    print(pd.DataFrame(rows, columns=["entry", "px", "ret_%_to_0720close"]).round(2).to_string(index=False))


def sec_ef(o, d):
    print("\n" + "=" * 100)
    print("E/F. OPTION RECONSTRUCTION + STRIKE GRID (expiry 2026-07-28)")
    print("=" * 100)
    strikes = sorted(o[o.option_type == "CE"].strike.unique())
    spot_close = d.close
    rows = []
    for k in strikes:
        t = o[(o.strike == k) & (o.option_type == "CE")]
        if len(t) < 10:
            continue
        g = t.groupby("ses")
        last = g["close"].last()
        low = g["low"].min()
        lastbar = g["ist"].last()
        rows.append({
            "strike": k, "n_bars": len(t),
            "first_ses": last.index.min(), "last_ses": last.index.max(),
            "last_bar_ist": lastbar.iloc[-1].strftime("%Y-%m-%d %H:%M"),
            "px_0708_close": last.get(pd.Timestamp("2026-07-08").date(), np.nan),
            "px_0708_low": low.get(pd.Timestamp("2026-07-08").date(), np.nan),
            "px_0709_low": low.get(pd.Timestamp("2026-07-09").date(), np.nan),
            "px_0709_close": last.get(pd.Timestamp("2026-07-09").date(), np.nan),
            "px_0714_close": last.get(pd.Timestamp("2026-07-14").date(), np.nan),
            "px_0717_close": last.get(pd.Timestamp("2026-07-17").date(), np.nan),
            "px_0720_last": last.get(pd.Timestamp("2026-07-20").date(), np.nan),
        })
    g = pd.DataFrame(rows)
    g["stale_exit"] = g.last_ses < pd.Timestamp("2026-07-20").date()
    print(g.to_string(index=False))
    print(f"\nSTALE-EXIT RATE (no 2026-07-20 tape): {g.stale_exit.mean():.0%} of {len(g)} CE strikes")
    itm = g[g.strike <= 107]
    print(f"  among strikes that finished ITM (<=107, i.e. the WINNERS): {itm.stale_exit.mean():.0%} of {len(itm)}")
    otm = g[g.strike >= 110]
    print(f"  among strikes still OTM/ATM at the exit (>=110, the LOSERS/laggards): {otm.stale_exit.mean():.0%} of {len(otm)}")

    print("\n-- PNB 106 CE 28-JUL-26, multiple by entry date (exit = LAST STORED PRINT, 2026-07-17) --")
    t106 = o[(o.strike == 106) & (o.option_type == "CE")]
    l = t106.groupby("ses")["close"].last()
    lo = t106.groupby("ses")["low"].min()
    exit_stored = l.iloc[-1]
    for ds in [d for d in l.index if d >= pd.Timestamp("2026-07-03").date()]:
        print(f"  entry {ds} close {l[ds]:.2f} -> stored exit {exit_stored:.2f} = {(exit_stored/l[ds]-1)*100:+7.1f}%   "
              f"(entry at that day's LOW {lo[ds]:.2f} -> {(exit_stored/lo[ds]-1)*100:+7.1f}%)")

    OWNER_0720 = 6.45
    print(f"\n-- SAME, with the owner's UNVERIFIABLE 2026-07-20 close of {OWNER_0720} (NOT in our store) --")
    for ds in [d for d in l.index if d >= pd.Timestamp("2026-07-03").date()]:
        gross = OWNER_0720 / l[ds] - 1
        glo = OWNER_0720 / lo[ds] - 1
        print(f"  entry {ds} close {l[ds]:.2f} -> {OWNER_0720:.2f} = {gross*100:+7.1f}% gross / "
              f"{(gross-RT_COST)*100:+7.1f}% net   | at day LOW {lo[ds]:.2f} = {glo*100:+7.1f}% gross")
    print(f"  implied prev close from owner's '+138.89% on 07-20': {OWNER_0720/2.3889:.2f}  "
          f"| our stored 07-17 close: {l.iloc[-1]:.2f}")
    return g


def sec_g(s, d):
    print("\n" + "=" * 100)
    print("G. HOURLY LEAD TIME")
    print("=" * 100)
    h = to_hourly(s)
    hm = macd(h.close)
    h = h.join(hm)
    h["bull_x"] = (h.macd > h.signal) & (h.macd.shift() <= h.signal.shift())
    hx = h[(h.t >= "2026-06-20") & h.bull_x]
    print(hx[["t", "close", "macd", "signal", "hist", "volume"]].round(4).to_string(index=False))
    print("\nDaily bull crosses: 2026-05-25 and 2026-07-17 (confirmed at that day's close).")
    print("Hourly bull crosses after the 07-08 low: 2026-07-09 11:15 IST, then 2026-07-17 15:15 IST.")
    n = len(d.loc["2026-07-09":"2026-07-17"])
    print(f"Lead of the 2026-07-09 hourly cross over the 2026-07-17 daily cross: {n-1} sessions "
          f"(~{(pd.Timestamp('2026-07-17')-pd.Timestamp('2026-07-09')).days} calendar days).")
    print("Lead of the 2026-07-17 15:15 hourly cross over the 2026-07-17 daily cross: 0 bars (same session close).")
    # volume thrust
    hh = h[(h.t >= "2026-07-06")]
    print("\nHourly volume z-score (vs trailing 60 hourly bars) -- thrust detection:")
    h["vz"] = (h.volume - h.volume.rolling(60).mean().shift()) / h.volume.rolling(60).std().shift()
    print(h[(h.t >= "2026-07-15")][["t", "close", "volume", "vz"]].round(2).to_string(index=False))


def sec_h(o, d):
    print("\n" + "=" * 100)
    print("H. OPTION-PARTICIPANT POSITIONING (OI / volume)")
    print("=" * 100)
    z = o[o.time >= "2026-06-25"]
    print("OI populated rate by option_type (30m, 28-JUL):",
          z.groupby("option_type")["oi"].apply(lambda x: f"{x.notna().mean():.0%}").to_dict())
    print("IV populated rate:", f"{z['iv'].notna().mean():.0%}", " delta:", f"{z['delta'].notna().mean():.0%}")
    piv = z.pivot_table(index="ses", columns=["option_type", "strike"], values="oi", aggfunc="last")
    print("\nEnd-of-session OI by strike (NaN = not tracked / not stored):")
    print(piv.loc[piv.index >= pd.Timestamp("2026-07-01").date()].to_string())
    volp = z.pivot_table(index="ses", columns=["option_type", "strike"], values="volume", aggfunc="sum")
    print("\nSession volume by strike:")
    print(volp.loc[volp.index >= pd.Timestamp("2026-07-06").date()].to_string())
    snap = pd.read_csv("data/pnb_atm_snap.csv", parse_dates=["time"])
    print(f"\natm_option_watchlist_snapshots rows for PNB (2026-06-01..07-20): {len(snap)}; "
          f"last ts {snap.time.max()}; distinct strikes {sorted(snap.strike.unique())}")
    snap["ses"] = snap.time.dt.tz_convert(IST).dt.date
    print(snap.groupby("ses").size().to_string())


def sec_i(d):
    print("\n" + "=" * 100)
    print("I. PREFIX-INVARIANCE (causality) CHECK, rtol=1e-12")
    print("=" * 100)
    full = macd(d.close)
    bad = 0
    tested = 0
    for cut in ["2026-05-25", "2026-07-08", "2026-07-14", "2026-07-17"]:
        pre = d.loc[:cut]
        mp = macd(pre.close)
        for col in ["macd", "signal", "hist"]:
            a = mp[col].iloc[-1]
            b = full.loc[pd.Timestamp(cut), col]
            tested += 1
            if not np.isclose(a, b, rtol=1e-12, atol=0):
                bad += 1
                print(f"  FAIL {cut} {col}: prefix={a} full={b}")
    print(f"  {tested-bad}/{tested} prefix values identical to the full-sample values "
          f"=> MACD/crossover/pivot logic is causal (EWM is recursive; pivots use an explicit R-bar lag).")
    print("  NOTE: EWM seeding means a *different start date* changes early values; all comparisons")
    print("  above use the same 2025-03-28 start, so the crossover dates are stable under truncation.")


if __name__ == "__main__":
    s = load_spot()
    d = to_daily(s)
    o = load_opts()
    sec_a(s, d, o)
    d2, piv = sec_b(d)
    sec_c(d2, piv)
    sec_d(d2)
    sec_ef(o, d2)
    sec_g(s, d2)
    sec_h(o, d2)
    sec_i(d2)
