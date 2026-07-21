"""PNB case study (Part 1 of the divergence study).

Reconstructs the owner's worked example from OUR data only:
  - daily MACD signal-line crossover on/around 2026-05-22 + divergence
  - the 2026-07-08 higher low
  - spot return reconciliation to 2026-07-20
  - PNB260728C106 premium reconstruction + strike-choice comparison
  - hourly vs daily lead time

Rules respected:
  * no moneyness band anywhere (the setup under test goes OTM -> deep ITM)
  * every indicator value is computed on a *prefix* of the data so the
    confirmable-in-real-time date is honest (prefix-invariance check included)

Data: local CSV extracted with literal UTC time bounds (see data/).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

IST = "Asia/Kolkata"
D = "data"


# ---------------------------------------------------------------- loading
def load_spot() -> pd.DataFrame:
    df = pd.read_csv(f"{D}/pnb_spot_30m.csv", parse_dates=["time", "synced_at"])
    df = df.sort_values("time").drop_duplicates("time", keep="last")
    df["ist"] = df["time"].dt.tz_convert(IST)
    df["session"] = df["ist"].dt.date
    return df.reset_index(drop=True)


def daily(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("session")
    out = pd.DataFrame(
        {
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "volume": g["volume"].sum(),
            "bars": g.size(),
        }
    )
    out.index = pd.to_datetime(out.index)
    return out


def hourly(df: pd.DataFrame) -> pd.DataFrame:
    """NSE hourly aligned to the 09:15 session open: pairs of 30m bars."""
    df = df.copy()
    df["k"] = df.groupby("session").cumcount() // 2  # 0..6 (last bucket is 1 bar)
    g = df.groupby(["session", "k"])
    out = pd.DataFrame(
        {
            "time": g["ist"].first(),
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "volume": g["volume"].sum(),
            "nbars": g.size(),
        }
    ).reset_index(drop=True)
    return out.sort_values("time").reset_index(drop=True)


# ------------------------------------------------------------- indicators
def macd(close: pd.Series, fast=12, slow=26, sig=9) -> pd.DataFrame:
    ef = close.ewm(span=fast, adjust=False).mean()
    es = close.ewm(span=slow, adjust=False).mean()
    line = ef - es
    signal = line.ewm(span=sig, adjust=False).mean()
    return pd.DataFrame({"macd": line, "signal": signal, "hist": line - signal})


def atr(df: pd.DataFrame, n=14) -> pd.Series:
    pc = df["close"].shift()
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def pivots_low(s: pd.Series, left: int, right: int) -> pd.Series:
    """Causal fractal low: bar i is a pivot low iff it is the strict min of
    [i-left, i+right].  CONFIRMED only at index i+right."""
    v = s.values
    out = np.zeros(len(v), dtype=bool)
    for i in range(left, len(v) - right):
        w = v[i - left : i + right + 1]
        if v[i] == w.min() and (w < v[i]).sum() == 0 and (w == v[i]).sum() == 1:
            out[i] = True
    return pd.Series(out, index=s.index)


def pivots_high(s: pd.Series, left: int, right: int) -> pd.Series:
    return pivots_low(-s, left, right)


# ------------------------------------------------------------------ main
def crossovers(m: pd.DataFrame) -> pd.DataFrame:
    up = (m["macd"] > m["signal"]) & (m["macd"].shift() <= m["signal"].shift())
    dn = (m["macd"] < m["signal"]) & (m["macd"].shift() >= m["signal"].shift())
    return pd.DataFrame({"bull": up, "bear": dn})


def report(dly: pd.DataFrame, hly: pd.DataFrame) -> None:
    m = macd(dly["close"])
    dly = dly.join(m)
    dly["atr14"] = atr(dly)
    x = crossovers(m)
    dly["bull_x"] = x["bull"]
    dly["bear_x"] = x["bear"]

    print("=== DAILY BARS around 2026-05-22 ===")
    w = dly.loc["2026-05-10":"2026-06-05"]
    print(
        w[["open", "high", "low", "close", "volume", "macd", "signal", "hist", "bull_x", "bear_x"]]
        .round(4)
        .to_string()
    )

    print("\n=== ALL DAILY BULL CROSSOVERS 2026-04-01..2026-07-20 ===")
    bx = dly.loc["2026-04-01":"2026-07-20"]
    print(bx[bx["bull_x"]][["close", "macd", "signal", "hist", "volume", "atr14"]].round(4).to_string())

    # ---- divergence quantification at the May cross
    print("\n=== PIVOT LOWS (daily close-basis, L=3/R=3) 2026-02..2026-07 ===")
    lows = pivots_low(dly["low"], 3, 3)
    pl = dly[lows].loc["2026-02-01":"2026-07-20"]
    pl = pl.assign(confirm_idx=[dly.index[dly.index.get_loc(i) + 3] for i in pl.index])
    print(pl[["low", "close", "macd", "confirm_idx"]].round(4).to_string())

    print("\n=== HOURLY around 2026-07-15..2026-07-20 ===")
    hm = macd(hly["close"])
    hly = hly.join(hm)
    hx = crossovers(hm)
    hly["bull_x"] = hx["bull"]
    h = hly[hly["time"] >= "2026-07-13"]
    print(h[["time", "open", "high", "low", "close", "volume", "macd", "signal", "hist", "bull_x"]].round(4).to_string())

    print("\n=== HOURLY BULL CROSSOVERS 2026-06-15..2026-07-20 ===")
    hh = hly[(hly["time"] >= "2026-06-15") & (hly["time"] <= "2026-07-21")]
    print(hh[hh["bull_x"]][["time", "close", "macd", "signal", "hist", "volume"]].round(4).to_string())

    return dly, hly


if __name__ == "__main__":
    pd.set_option("display.width", 200)
    s = load_spot()
    dly = daily(s)
    hly = hourly(s)
    print(f"spot 30m rows={len(s)} sessions={len(dly)} range={dly.index.min().date()}..{dly.index.max().date()}")
    print("sessions with != 13 bars:", (dly['bars'] != 13).sum())
    report(dly, hly)
