"""Build the daily spot panel used by the regime-duration study.

Inputs (already on disk, nothing new is pulled here):
  ./data/spot_*.csv                       pre-2025 30m bars (extract.py)
  ../panel_2d3d/data/spot_*.csv           2025-01-01 .. 2026-07-20 30m bars

Output:
  ./data/regime/daily.parquet   one row per (underlying, IST session):
                         o/h/l/c, bar count, market class, session index.

Session construction is the same convention the 2-3 day study used: bars are
bucketed by IST calendar date; NSE keeps 09:15..15:15 IST bars, MCX keeps
09:00..23:30 IST bars.  A session must carry a minimum bar count to count as a
real session (holidays / half-feeds are dropped, not forward-filled).
"""
from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
os.makedirs(os.path.join(DATA, "regime"), exist_ok=True)
PANEL = os.path.abspath(os.path.join(HERE, "..", "panel_2d3d", "data"))
IST = pd.Timedelta(hours=5, minutes=30)

INDEX = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "NIFTYNXT50"}
# MCX futures roots. GOLD/NICKEL/CRUDEOIL were only added to the feed in
# late 2025/2026 and are kept here for correct session-window handling; the
# MIN_SESSIONS filter below removes them from the statistics.
MCX = {"COPPER", "NATURALGAS", "SILVERM", "ZINCMINI", "ALUMINI",
       "GOLD", "NICKEL", "CRUDEOIL"}

# a single-session jump this large in Indian cash/index/MCX data is a corporate
# action (demerger: SIEMENS, TMPV, VEDL) or a bad print, never a tradeable move.
# The series is CUT there rather than smoothed, so no phantom move is created.
BREAK_RET = 0.20
# a series shorter than this cannot support ADX warm-up + run/annual statistics
MIN_SESSIONS = 150

# UTC minute-of-day windows
NSE_LO, NSE_HI = 225, 585        # 09:15 .. 15:15 IST
MCX_LO, MCX_HI = 210, 1080       # 09:00 .. 23:30 IST
NSE_MIN_BARS = 8                 # of 13
MCX_MIN_BARS = 14                # of ~29


def mclass(u: str) -> str:
    if u in INDEX:
        return "index"
    if u in MCX:
        return "commodity"
    return "stock"


def _load() -> pd.DataFrame:
    paths = sorted(glob.glob(os.path.join(DATA, "spot_*.csv")))
    paths += sorted(glob.glob(os.path.join(PANEL, "spot_*.csv")))
    frames = []
    for p in paths:
        df = pd.read_csv(p, usecols=["time", "underlying", "open", "high", "low", "close", "volume"])
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.drop_duplicates(["underlying", "time"], keep="last")
    df["mclass"] = df["underlying"].map(mclass)
    df["mins"] = df["time"].dt.hour * 60 + df["time"].dt.minute
    lo = np.where(df["mclass"].eq("commodity"), MCX_LO, NSE_LO)
    hi = np.where(df["mclass"].eq("commodity"), MCX_HI, NSE_HI)
    df = df[(df["mins"] >= lo) & (df["mins"] <= hi)]
    df["session"] = (df["time"] + IST).dt.date
    return df


def build() -> pd.DataFrame:
    s = _load().sort_values(["underlying", "time"])
    g = s.groupby(["underlying", "mclass", "session"], observed=True)
    d = g.agg(
        o=("open", "first"), h=("high", "max"), l=("low", "min"),
        c=("close", "last"), bars=("close", "size"),
    ).reset_index()
    need = np.where(d["mclass"].eq("commodity"), MCX_MIN_BARS, NSE_MIN_BARS)
    d = d[d["bars"] >= need].copy()
    d = d[(d["c"] > 0) & (d["h"] >= d["l"])].copy()
    d = d.sort_values(["underlying", "session"]).reset_index(drop=True)
    d["raw_ret"] = d.groupby("underlying")["c"].pct_change()
    d["is_break"] = d["raw_ret"].abs() > BREAK_RET
    d["seg"] = d.groupby("underlying")["is_break"].cumsum()
    d["series_id"] = d["underlying"] + "#" + d["seg"].astype(str)
    n_before = d["series_id"].nunique()
    keep = d.groupby("series_id")["c"].transform("size") >= MIN_SESSIONS
    d = d[keep].copy()
    print(f"series after cutting at |ret|>{BREAK_RET}: {n_before}; "
          f"kept with >={MIN_SESSIONS} sessions: {d['series_id'].nunique()}")
    d = d.sort_values(["series_id", "session"]).reset_index(drop=True)
    d["sidx"] = d.groupby("series_id").cumcount()
    d["ret"] = d.groupby("series_id")["c"].pct_change()
    return d


if __name__ == "__main__":
    d = build()
    out = os.path.join(DATA, "regime", "daily.parquet")
    d.to_parquet(out, index=False)
    print("wrote", out, len(d), "rows")
    print(d.groupby("mclass")["underlying"].nunique())
    print(d.groupby("mclass")["session"].agg(["min", "max"]))
    bad = d[d["raw_ret"].abs() > BREAK_RET]
    print("break sessions cut:", len(bad))
    print(bad.groupby("mclass").size())
