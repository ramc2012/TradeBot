"""DECLARED SENSITIVITIES (descriptive only, never promoted without entering
the multiplicity count) — spot level, 30m, hold=1d, primary construct family.

Grid (all fixed a priori in regime_defs/timer_defs docstrings):
  deep_min   {0.0010, 0.0015*, 0.0025}   (deep_macd depth)
  rise_lb    {1, 3*, 5}                  (R1 SMA20-rising lookback)
  adx_thr    {18, 20*, 25}               (R2 threshold, deep_macd cell)
  anchor     {ema*, vwap}                (T2 pullback anchor)
  fill       {signal-close*, next-bar-open}  (primary cell fill lag)
(* = primary). Output: signed mean spot return, filtered vs unfiltered.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

from build_universe import load_spot_full, option_universe_names
from regime_defs import daily_regimes, resample_daily
from spot_analyse import collapse_episodes
from timer_defs import timer_signals

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")


def main() -> None:
    spot = load_spot_full()
    spot = spot[spot["underlying"].isin(option_universe_names())]
    univ = pd.read_parquet(os.path.join(DATA, "univ_30m.parquet"))
    univ["time_n"] = univ["time"].dt.tz_convert("UTC").dt.tz_localize(None)
    ret_map = univ.set_index(["underlying", "time_n"])[
        ["ret_1d", "exit_ts_1d", "session", "open_next", "close"]]

    frames = []
    for und, g in spot.groupby("underlying"):
        daily = resample_daily(g.reset_index(drop=True))
        if len(daily) < 60:
            continue
        regs = {}
        for lb in (1, 3, 5):
            regs[f"r1_lb{lb}"] = daily_regimes(daily, rise_lb=lb)[
                ["session", "r1_lag1"]].rename(columns={"r1_lag1": f"r1_lb{lb}"})
        for thr in (18.0, 20.0, 25.0):
            regs[f"r2_t{int(thr)}"] = daily_regimes(daily, adx_thr=thr)[
                ["session", "r2_lag1"]].rename(
                columns={"r2_lag1": f"r2_t{int(thr)}"})
        base = timer_signals(g.reset_index(drop=True))
        vw = timer_signals(g.reset_index(drop=True), anchor="vwap")
        b = base[["time", "session", "close"]].copy()
        c = base["close"].astype(float)
        macd = c.ewm(span=12, adjust=False, min_periods=12).mean() \
            - c.ewm(span=26, adjust=False, min_periods=26).mean()
        sigl = macd.ewm(span=9, adjust=False, min_periods=9).mean()
        x_up = (macd > sigl) & (macd.shift(1) <= sigl.shift(1))
        x_dn = (macd < sigl) & (macd.shift(1) >= sigl.shift(1))
        for dm in (0.0010, 0.0015, 0.0025):
            b[f"deep{int(dm * 1e4)}"] = (x_up & (macd < 0)
                                         & (-macd / c >= dm)).fillna(False)
            b[f"deep{int(dm * 1e4)}_dn"] = (x_dn & (macd > 0)
                                            & (macd / c >= dm)).fillna(False)
        b["pb_vwap"] = vw["t_pullback_anchor"]
        b["pb_vwap_dn"] = vw["t_pullback_anchor_dn"]
        b["underlying"] = und
        for f in regs.values():
            b = b.merge(f, on="session", how="left")
        frames.append(b)
    U = pd.concat(frames, ignore_index=True)
    for col in [c for c in U.columns if c.startswith(("r1_", "r2_"))]:
        U[col] = U[col].fillna(0).astype(int)
    U["time_n"] = U["time"].dt.tz_convert("UTC").dt.tz_localize(None)
    idx = pd.MultiIndex.from_arrays([U["underlying"], U["time_n"]])
    add = ret_map.reindex(idx).reset_index(drop=True)
    U = pd.concat([U.reset_index(drop=True),
                   add[["ret_1d", "exit_ts_1d", "open_next"]]], axis=1)
    U = U.rename(columns={"session_x": "session"}) if "session_x" in U else U

    def cellstat(sig_up, sig_dn, reg_col):
        e = U[(U[sig_up] & (U[reg_col] == 1))
              | (U[sig_dn] & (U[reg_col] == -1))].copy()
        e["dir"] = np.where(e[sig_up] & (e[reg_col] == 1), 1, -1)
        e = collapse_episodes(e, "1d")
        s = e["dir"].to_numpy(float) * e["ret_1d"].to_numpy(float)
        c1 = U[U[sig_up] | U[sig_dn]].copy()
        c1["dir"] = np.where(c1[sig_up], 1, -1)
        c1 = collapse_episodes(c1, "1d")
        s1 = c1["dir"].to_numpy(float) * c1["ret_1d"].to_numpy(float)
        return len(s), np.nanmean(s), len(s1), np.nanmean(s1)

    print("variant                          n_filt  mean_filt   n_unf  mean_unf")
    for dm in (10, 15, 25):
        n, m, n1, m1 = cellstat(f"deep{dm}", f"deep{dm}_dn", "r1_lb3")
        print(f"deep_min {dm / 1e4:.4f} x R1(lb3)      {n:7d} {m:10.5f} "
              f"{n1:7d} {m1:9.5f}")
    for lb in (1, 3, 5):
        n, m, n1, m1 = cellstat("deep15", "deep15_dn", f"r1_lb{lb}")
        print(f"R1 rise_lb {lb} x deep15          {n:7d} {m:10.5f} "
              f"{n1:7d} {m1:9.5f}")
    for thr in (18, 20, 25):
        n, m, n1, m1 = cellstat("deep15", "deep15_dn", f"r2_t{thr}")
        print(f"R2 adx_thr {thr} x deep15          {n:7d} {m:10.5f} "
              f"{n1:7d} {m1:9.5f}")
    n, m, n1, m1 = cellstat("pb_vwap", "pb_vwap_dn", "r1_lb3")
    print(f"T2 anchor=VWAP x R1            {n:7d} {m:10.5f} {n1:7d} {m1:9.5f}")

    # fill-lag: primary cell, entry at next bar OPEN instead of signal close
    e = U[(U["deep15"] & (U["r1_lb3"] == 1))
          | (U["deep15_dn"] & (U["r1_lb3"] == -1))].copy()
    e["dir"] = np.where(e["deep15"] & (e["r1_lb3"] == 1), 1, -1)
    e = collapse_episodes(e, "1d")
    exit_px = e["close"].astype(float) * (1.0 + e["ret_1d"].astype(float))
    lag = e["dir"].to_numpy(float) * (exit_px / e["open_next"].astype(float)
                                      - 1.0).to_numpy(float)
    base_s = e["dir"].to_numpy(float) * e["ret_1d"].to_numpy(float)
    print(f"fill: signal-close mean {np.nanmean(base_s):.5f}  "
          f"next-bar-open mean {np.nanmean(lag):.5f}  (n {len(e)})")


if __name__ == "__main__":
    main()
