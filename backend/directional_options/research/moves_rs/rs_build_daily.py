"""(B) Relative-strength study — build a clean daily spot panel.

NOTE ON FILE OWNERSHIP: a second workflow (Study A, monthly moves) is writing
into this same directory. Everything this study owns is prefixed `rs_` and its
artefacts live in `data_rs/`. Nothing here reads or writes Study A's files.

Source: the 30-minute `underlying_spot_candles` CSV extracts already on disk
(`../panel_2d3d/data/spot_*.csv`, read-only). No new PG query is issued, which
is deliberate: other workflows are querying the database right now and the
standing rule is "extract once, analyse in pandas". Those CSVs were produced by
an extraction that bounded `time` directly with literal UTC timestamps.

Session hygiene (this tape is known to be contaminated):
  * Only the 13 canonical NSE 30m slots are kept (03:45..09:45 UTC at :15/:45,
    i.e. 09:15..15:15 IST). The table also holds rows at every other half hour
    for a handful of names; those are other-source / cross-symbol artefacts.
  * A session must carry >= 10 of those 13 bars.
  * OHLC coherence asserted (high >= max(open,close), low <= min(open,close)).
  * A daily close-to-close move beyond +-25% is dropped as a contamination
    candidate (Fyers cross-symbol tick contamination, 2026-07-20).

Output: data_rs/rs_daily.parquet -> underlying, session, o, h, l, c, v, bars
"""
from __future__ import annotations

import glob
import os

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data_rs")
SRC = os.path.abspath(os.path.join(HERE, "..", "panel_2d3d", "data"))
os.makedirs(DATA, exist_ok=True)

# 09:15..15:15 IST == 03:45..09:45 UTC, thirteen 30m slots.
CANONICAL_SLOTS = tuple(
    f"{h:02d}:{m:02d}"
    for h, m in [
        (3, 45), (4, 15), (4, 45), (5, 15), (5, 45), (6, 15), (6, 45),
        (7, 15), (7, 45), (8, 15), (8, 45), (9, 15), (9, 45),
    ]
)
MIN_BARS = 10
MAX_ABS_DAILY_RET = 0.25


def build() -> pd.DataFrame:
    frames = []
    for f in sorted(glob.glob(os.path.join(SRC, "spot_*.csv"))):
        d = pd.read_csv(f)
        t = pd.to_datetime(d["time"], utc=True)
        keep = t.dt.strftime("%H:%M").isin(CANONICAL_SLOTS)
        d = d.loc[keep].copy()
        d["session"] = t.loc[keep].dt.date
        frames.append(d[["session", "underlying", "open", "high", "low", "close", "volume"]])
    raw = pd.concat(frames, ignore_index=True)
    raw = raw.drop_duplicates(subset=["underlying", "session", "open", "close"], keep="first")

    g = raw.groupby(["underlying", "session"], sort=True)
    daily = pd.DataFrame({
        "o": g["open"].first(),
        "h": g["high"].max(),
        "l": g["low"].min(),
        "c": g["close"].last(),
        "v": g["volume"].sum(),
        "bars": g["close"].size(),
    }).reset_index()

    n0 = len(daily)
    daily = daily[daily["bars"] >= MIN_BARS]
    daily = daily[(daily[["o", "h", "l", "c"]] > 0).all(axis=1)]
    ok = (daily["h"] >= daily[["o", "c"]].max(axis=1) - 1e-9) & \
         (daily["l"] <= daily[["o", "c"]].min(axis=1) + 1e-9)
    daily = daily[ok]
    print(f"session-hygiene: {n0} -> {len(daily)} daily bars")

    daily["session"] = pd.to_datetime(daily["session"])
    daily = daily.sort_values(["underlying", "session"]).reset_index(drop=True)
    ret = daily.groupby("underlying")["c"].pct_change()
    bad = (ret.abs() > MAX_ABS_DAILY_RET).fillna(False)
    print(f"dropped {int(bad.sum())} contamination-candidate bars (|ret| > 25%)")
    daily = daily[~bad].reset_index(drop=True)
    return daily


if __name__ == "__main__":
    d = build()
    out = os.path.join(DATA, "rs_daily.parquet")
    d.to_parquet(out, index=False)
    print("wrote", out, d.shape)
    print("underlyings:", d["underlying"].nunique())
    print("sessions:", d["session"].nunique(),
          d["session"].min().date(), d["session"].max().date())
    cov = d.groupby("underlying").size()
    print(cov.describe())
    for n in (200, 250, 300):
        print(f"  names with >= {n} sessions:", int((cov >= n).sum()))
    for ix in ("NIFTY", "BANKNIFTY"):
        s = d[d.underlying == ix]
        print(ix, len(s), s["session"].min().date() if len(s) else None,
              s["session"].max().date() if len(s) else None)
