"""Build the 2-3 day horizon research panel from extracted CSVs.

Produces:
  panel_opt.parquet  : one row per (contract, session) EOD snapshot with
                       fwd 1/2/3-session premium returns, moneyness, DTE,
                       weekly/monthly flag.
  daily_spot.parquet : per (underlying, session) daily OHLC + ATR14.

Causality: every forward column is strictly forward; every conditioning column
(moneyness, DTE, ATR) uses only information available at the snapshot bar.
"""
from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
IST = pd.Timedelta(hours=5, minutes=30)

# NSE session bars: 03:45 UTC (09:15 IST) .. 09:45 UTC (15:15 IST)
SESSION_MIN_LO, SESSION_MIN_HI = 225, 585


def _load(pattern: str) -> pd.DataFrame:
    frames = [pd.read_csv(p) for p in sorted(glob.glob(os.path.join(DATA, pattern)))]
    df = pd.concat(frames, ignore_index=True)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    ist = df["time"] + IST
    df["session"] = ist.dt.date
    df["mins"] = df["time"].dt.hour * 60 + df["time"].dt.minute
    return df


def build_spot() -> pd.DataFrame:
    s = _load("spot_*.csv")
    s = s[(s["mins"] >= SESSION_MIN_LO) & (s["mins"] <= SESSION_MIN_HI)]
    g = s.sort_values("time").groupby(["underlying", "session"])
    d = g.agg(
        s_open=("open", "first"),
        s_high=("high", "max"),
        s_low=("low", "min"),
        s_close=("close", "last"),
        bars=("close", "size"),
    ).reset_index()
    d = d[d["bars"] >= 6].copy()  # require a real session
    d = d.sort_values(["underlying", "session"]).reset_index(drop=True)
    grp = d.groupby("underlying", group_keys=False)
    d["prev_close"] = grp["s_close"].shift(1)
    tr = pd.concat(
        [
            d["s_high"] - d["s_low"],
            (d["s_high"] - d["prev_close"]).abs(),
            (d["s_low"] - d["prev_close"]).abs(),
        ],
        axis=1,
    ).max(axis=1)
    d["tr"] = tr
    # ATR14 as of PRIOR close (strictly backward looking at snapshot time we use
    # the ATR computed through the current session close, which is available at
    # 15:15 IST — that is the decision bar, so it is causal).
    d["atr14"] = d.groupby("underlying")["tr"].transform(
        lambda x: x.rolling(14, min_periods=10).mean()
    )
    d["atr_pct"] = d["atr14"] / d["s_close"]
    # corrupt spot rows (cross-symbol tick contamination / bad prints) blow ATR up
    d.loc[(d["atr_pct"] > 0.15) | (d["atr_pct"] <= 0.0005), "atr_pct"] = np.nan
    d.loc[d["atr_pct"].isna(), "atr14"] = np.nan
    # forward session index for horizon joins
    d["sidx"] = d.groupby("underlying").cumcount()
    for h in (1, 2, 3):
        d[f"s_close_f{h}"] = d.groupby("underlying")["s_close"].shift(-h)
        d[f"s_high_f{h}"] = d.groupby("underlying")["s_high"].shift(-h)
        d[f"s_low_f{h}"] = d.groupby("underlying")["s_low"].shift(-h)
    # forward-looking extremes over the NEXT 1..3 sessions, built from explicit
    # negative shifts so no reverse-frame groupby misalignment is possible.
    hs = [d.groupby("underlying")["s_high"].shift(-k) for k in (1, 2, 3)]
    ls = [d.groupby("underlying")["s_low"].shift(-k) for k in (1, 2, 3)]
    d["s_maxhigh_2"] = pd.concat(hs[:2], axis=1).max(axis=1)
    d["s_minlow_2"] = pd.concat(ls[:2], axis=1).min(axis=1)
    d["s_maxhigh_3"] = pd.concat(hs, axis=1).max(axis=1)
    d["s_minlow_3"] = pd.concat(ls, axis=1).min(axis=1)
    d["n_fwd_sessions"] = pd.concat(hs, axis=1).notna().sum(axis=1)
    return d


def build_opt(spot: pd.DataFrame) -> pd.DataFrame:
    o = _load("opt_*.csv")
    eod = o[o["mins"] == 585].copy()
    eod["expiry"] = pd.to_datetime(eod["expiry"]).dt.date
    eod = eod.dropna(subset=["expiry", "strike", "option_type"])
    eod["contract"] = eod["instrument_key"].astype(str)
    eod = eod.sort_values(["contract", "session"]).drop_duplicates(
        ["contract", "session"], keep="last"
    )

    sp = spot[["underlying", "session", "s_close", "atr_pct", "atr14", "sidx",
               "s_maxhigh_2", "s_minlow_2", "s_maxhigh_3", "s_minlow_3",
               "s_close_f1", "s_close_f2", "s_close_f3"]]
    eod = eod.merge(sp, on=["underlying", "session"], how="inner")

    eod["dte"] = (pd.to_datetime(eod["expiry"]) - pd.to_datetime(eod["session"])).dt.days
    eod = eod[(eod["dte"] >= 0) & (eod["dte"] <= 60)]

    # signed moneyness: >0 = OTM, <0 = ITM (in % of spot)
    m = (eod["strike"] - eod["s_close"]) / eod["s_close"]
    eod["mny"] = np.where(eod["option_type"] == "CE", m, -m)

    # monthly = last expiry available for that underlying within its calendar month
    ex = eod[["underlying", "expiry"]].drop_duplicates()
    ex["ym"] = pd.to_datetime(ex["expiry"]).dt.to_period("M")
    last = ex.groupby(["underlying", "ym"])["expiry"].max().rename("last_exp").reset_index()
    ex = ex.merge(last, on=["underlying", "ym"])
    ex["is_monthly"] = ex["expiry"] == ex["last_exp"]
    eod = eod.merge(ex[["underlying", "expiry", "is_monthly"]], on=["underlying", "expiry"], how="left")

    # forward premium returns by SESSION INDEX (not row position) so gaps in
    # collection do not silently shorten the horizon.
    key = eod[["contract", "sidx", "close"]].copy()
    for h in (1, 2, 3):
        nxt = key.copy()
        nxt["sidx"] = nxt["sidx"] - h
        nxt = nxt.rename(columns={"close": f"p_f{h}"})
        eod = eod.merge(nxt, on=["contract", "sidx"], how="left")
        eod[f"ret{h}"] = eod[f"p_f{h}"] / eod["close"] - 1.0
    eod["s_ret3"] = eod["s_close_f3"] / eod["s_close"] - 1.0
    eod["s_ret2"] = eod["s_close_f2"] / eod["s_close"] - 1.0
    return eod


def bucket_mny(x: float) -> str:
    if x < -0.03:
        return "1_deep_ITM(<-3%)"
    if x < -0.0075:
        return "2_slight_ITM(-3..-0.75%)"
    if x <= 0.0075:
        return "3_ATM(+-0.75%)"
    if x <= 0.03:
        return "4_slight_OTM(0.75..3%)"
    return "5_far_OTM(>3%)"


def bucket_dte(d: int) -> str:
    if d <= 2:
        return "A_0-2"
    if d <= 7:
        return "B_3-7"
    if d <= 22:
        return "C_8-22"
    return "D_23+"


if __name__ == "__main__":
    spot = build_spot()
    spot.to_parquet(os.path.join(DATA, "daily_spot.parquet"))
    print("spot sessions", len(spot), spot["underlying"].nunique())
    opt = build_opt(spot)
    opt["mny_b"] = opt["mny"].map(bucket_mny)
    opt["dte_b"] = opt["dte"].map(bucket_dte)
    opt.to_parquet(os.path.join(DATA, "panel_opt.parquet"))
    print("opt rows", len(opt), "contracts", opt["contract"].nunique(),
          "underlyings", opt["underlying"].nunique())
    print(opt["ret3"].notna().mean(), "have fwd3")
