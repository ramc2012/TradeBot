"""PNB case study, part 2: option reconstruction.

Per-CONTRACT extraction, NO moneyness band (deliberately: the contract under
test travels OTM -> deep ITM, and the setups_2d3d +/-8% band would delete
exactly the winning tape).  Stale-exit rate is reported explicitly.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

IST = "Asia/Kolkata"
SRC_PREF = {"upstox": 0, "upstox_expired": 1, "fyers": 2, "fyers_chain": 3}


def load_opts(expiry="2026-07-28", interval="30minute") -> pd.DataFrame:
    o = pd.read_csv("data/pnb_opt.csv", parse_dates=["time", "synced_at"])
    o = o[(o.interval == interval) & (o.expiry == expiry)].copy()
    o["pref"] = o["source"].map(SRC_PREF).fillna(9)
    o = o.sort_values(["time", "strike", "option_type", "pref", "synced_at"])
    n0 = len(o)
    o = o.drop_duplicates(["time", "strike", "option_type"], keep="first")
    print(f"[opts {expiry} {interval}] rows {n0} -> {len(o)} after source-preference dedupe")
    o["ist"] = o["time"].dt.tz_convert(IST)
    o["ses"] = o["ist"].dt.date
    return o


def tape(o: pd.DataFrame, strike: float, otype="CE") -> pd.DataFrame:
    t = o[(o.strike == strike) & (o.option_type == otype)].sort_values("time")
    return t


def daily_last(t: pd.DataFrame) -> pd.DataFrame:
    g = t.groupby("ses")
    return pd.DataFrame(
        {
            "first_px": g["close"].first(),
            "last_px": g["close"].last(),
            "hi": g["high"].max(),
            "lo": g["low"].min(),
            "vol": g["volume"].sum(),
            "oi_last": g["oi"].last(),
            "bars": g.size(),
            "last_bar_ist": g["ist"].last(),
        }
    )


if __name__ == "__main__":
    pd.set_option("display.width", 220)
    o = load_opts()
    spot = pd.read_csv("data/pnb_spot_30m.csv", parse_dates=["time"])
    spot["ist"] = spot.time.dt.tz_convert(IST)
    spot["ses"] = spot.ist.dt.date
    sd = spot.groupby("ses")["close"].last()

    for k in [102, 105, 106, 107, 110, 112]:
        t = tape(o, k)
        if t.empty:
            continue
        dl = daily_last(t)
        dl = dl.join(sd.rename("spot_close"))
        print(f"\n===== PNB {k} CE 28-JUL-2026  (rows={len(t)}) =====")
        print(dl.loc[dl.index >= pd.Timestamp("2026-06-25").date()].round(3).to_string())
