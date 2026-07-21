"""MEASUREMENT PASS, stage 1: build the bar-level UNIVERSE frames.

For every underlying and both timeframes (30m native, 1h paired) this
assembles one row per bar carrying:
  - the governing daily regime states (r1_lag1 / r2_lag1, prior-session close,
    per the causality contract in regime_defs.py) and their ages,
  - all four timer signals (up and mirrored down),
  - forward spot returns at the four holds (2h, eod, 1d, 3d) with the exact
    exit-bar timestamps (for option marks downstream),
  - next-bar-open (the declared fill-lag sensitivity).

No outcome is looked at here; this is plumbing. Everything is causal:
indicators at bar t use bars <= t (prefix-invariance verified in
test_causality.py); the regime governing session t is the state at t-1 close.

Spot input: panel_2d3d quarterly CSVs (legacy, no source column -> max-volume
dedup proxy) + the htf_regime extracts (source column -> declared priority
dedup). The two groups are deduped SEPARATELY by their proper rule, then the
sourced extract wins on overlap — this preserves determinism (D4-spot).

Universe restriction: underlyings that appear in the option extract (the
tradeable F&O set), NSE session bars only (03:45..09:45 UTC stamps). MCX
names in the panel spot are excluded by that same restriction.
"""
from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

from option_read_layer import load_spot_csvs
from regime_defs import daily_regimes, resample_daily
from timer_defs import TIMERS, timer_signals, to_hourly

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OPT_DIR = os.path.join(DATA, "opt")
PANEL_SPOT = os.path.join(HERE, "..", "panel_2d3d", "data")

HOLDS = ("2h", "eod", "1d", "3d")
NSE_FIRST_MIN, NSE_LAST_MIN = 9 * 60 + 15, 15 * 60 + 15   # IST bar stamps


def load_spot_full() -> pd.DataFrame:
    legacy = load_spot_csvs(sorted(glob.glob(os.path.join(PANEL_SPOT, "spot_*.csv"))))
    new = load_spot_csvs(sorted(glob.glob(os.path.join(OPT_DIR, "spot_*.csv"))))
    s = pd.concat([new, legacy], ignore_index=True)      # sourced extract first
    s = (s.drop_duplicates(["underlying", "time"], keep="first")
         .sort_values(["underlying", "time"], kind="mergesort")
         .reset_index(drop=True))
    ist = s["time"].dt.tz_convert("Asia/Kolkata")
    mins = ist.dt.hour * 60 + ist.dt.minute
    s = s[(mins >= NSE_FIRST_MIN) & (mins <= NSE_LAST_MIN)]
    return s.reset_index(drop=True)


def option_universe_names() -> set[str]:
    names: set[str] = set()
    for f in sorted(glob.glob(os.path.join(OPT_DIR, "opt_*.csv"))):
        names |= set(pd.read_csv(f, usecols=["underlying"])["underlying"].unique())
    return names


def _forward_cols(sig: pd.DataFrame, daily: pd.DataFrame,
                  bars_per_2h: int) -> pd.DataFrame:
    """Attach forward returns + exit timestamps to a signal frame (one
    underlying, one timeframe). `exit_ts_*` columns are 30m-bar STAMPS usable
    directly as option mark timestamps (for the hourly frame the paired bar's
    close instant is its second half's 30m stamp + nothing further needed —
    we store the stamp of the 30m bar whose close is the exit price)."""
    b = sig.reset_index(drop=True)
    n = len(b)
    close = b["close"].to_numpy(float)
    t_arr = b["time"].to_numpy()

    pos = b.groupby("session").cumcount().to_numpy()
    size = b.groupby("session")["time"].transform("size").to_numpy()
    start = np.arange(n) - pos
    exit_i = np.minimum(np.arange(n) + bars_per_2h, start + size - 1)
    b["ret_2h"] = close[exit_i] / close - 1.0
    b["exit_ts_2h"] = t_arr[exit_i]

    eod_close = b.groupby("session")["close"].transform("last").to_numpy(float)
    b["ret_eod"] = eod_close / close - 1.0
    b["exit_ts_eod"] = b.groupby("session")["time"].transform("last")

    # daily map: session ordinal -> close / last-bar time
    d = daily.reset_index(drop=True)
    d_close = d["close"].to_numpy(float)
    d_time = d["last_bar_time"].to_numpy()
    ord_map = {s: j for j, s in enumerate(d["session"])}
    j = b["session"].map(ord_map).to_numpy()
    for lbl, k in (("1d", 1), ("3d", 3)):
        jj = j + k
        ok = jj < len(d)
        r = np.full(n, np.nan)
        ts = np.full(n, np.datetime64("NaT"), dtype=t_arr.dtype)
        r[ok] = d_close[jj[ok]] / close[ok] - 1.0
        ts[ok] = d_time[jj[ok]]
        b[f"ret_{lbl}"] = r
        b[f"exit_ts_{lbl}"] = ts

    # fill-lag sensitivity: next bar's open (cross-session allowed)
    b["open_next"] = b["open"].shift(-1)
    return b


def build_for_underlying(b30: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    daily = resample_daily(b30)
    if len(daily) < 60:                      # need SMA50 + a margin to matter
        return None
    g30 = b30.sort_values("time", kind="mergesort").drop_duplicates("time")
    lbt = (g30.assign(sess=g30["time"].dt.tz_convert("Asia/Kolkata").dt.date)
           .groupby("sess")["time"].last())
    daily["last_bar_time"] = daily["session"].map(
        lambda s: lbt.get(s.date(), pd.NaT))
    reg = daily_regimes(daily)
    reg_cols = ["session", "r1_lag1", "r2_lag1", "r1_age_lag1", "r2_age_lag1"]

    out = []
    for tf, frame, k2h in (("30m", timer_signals(b30), 4),
                           ("1h", timer_signals(to_hourly(b30)), 2)):
        f = _forward_cols(frame, daily, k2h)
        f = f.merge(reg[reg_cols], on="session", how="left")
        for c in ("r1_lag1", "r2_lag1"):
            f[c] = f[c].fillna(0).astype(np.int8)
        f["tf"] = tf
        keep = (["time", "session", "ist_min", "close", "open", "open_next",
                 "underlying", "tf"]
                + [f"t_{t}" for t in TIMERS] + [f"t_{t}_dn" for t in TIMERS]
                + reg_cols[1:]
                + [f"ret_{h}" for h in HOLDS] + [f"exit_ts_{h}" for h in HOLDS])
        f["underlying"] = b30["underlying"].iloc[0]
        out.append(f[keep])
    return out[0], out[1]


def main() -> None:
    spot = load_spot_full()
    names = option_universe_names()
    spot = spot[spot["underlying"].isin(names)]
    print(f"spot rows {len(spot):,}  underlyings {spot['underlying'].nunique()}"
          f"  {spot['time'].min()} .. {spot['time'].max()}", flush=True)
    u30, u1h = [], []
    for und, g in spot.groupby("underlying"):
        r = build_for_underlying(g.reset_index(drop=True))
        if r is None:
            continue
        u30.append(r[0])
        u1h.append(r[1])
    univ30 = pd.concat(u30, ignore_index=True)
    univ1h = pd.concat(u1h, ignore_index=True)
    univ30.to_parquet(os.path.join(DATA, "univ_30m.parquet"))
    univ1h.to_parquet(os.path.join(DATA, "univ_1h.parquet"))
    for tf, u in (("30m", univ30), ("1h", univ1h)):
        r1on = (u["r1_lag1"] != 0).mean()
        r2on = (u["r2_lag1"] != 0).mean()
        print(f"{tf}: rows {len(u):,} names {u['underlying'].nunique()} "
              f"sessions {u['session'].nunique()} r1_on {r1on:.1%} r2_on {r2on:.1%}",
              flush=True)
        for t in TIMERS:
            print(f"   {t}: up {int(u[f't_{t}'].sum()):,} "
                  f"dn {int(u[f't_{t}_dn'].sum()):,}", flush=True)


if __name__ == "__main__":
    main()
