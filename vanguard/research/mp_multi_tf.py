"""Weekly and monthly TPO profiles, merged onto the daily session as context.

WHY MULTI-TIMEFRAME IS THE RIGHT NEXT LAYER. Everything so far judged a close
against its OWN session's value area -- a one-day frame of reference. MP is
explicitly hierarchical: a daily close above the day's value means little if the
day is still trading inside last month's value, and means a great deal if it has
cleared the prior WEEK'S and MONTH'S value highs at the same time. The composite
profile is where the longer-timeframe participant is visible, and that is the
participant who moves price over three or four days -- exactly the horizon being
targeted.

CONSTRUCTION. Weekly and monthly profiles are built from the SAME 30-minute bars
as the daily ones, so a week is one profile over ~65 bars and a month one over
~280, not an average of daily profiles. TPO rather than volume, because the
indices carry no volume.

STRICTLY PRIOR PERIODS ONLY. A daily session is given the value area of the last
COMPLETED week and the last COMPLETED month. Using the current week's profile
would leak the very move being predicted -- the day in question is inside it.
The developing current-week profile is computed separately and only from bars
BEFORE the session in question, so it is usable too.

    from research.mp_multi_tf import load_mtf
    load_mtf(connection, names, start) -> daily frame + prior-week / prior-month
    value-area context and the multi-timeframe alignment label.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

from research.mp_auction import BAR_SQL, Profile, TICK_BPS, add_context, sessions


def _period_profiles(bars: pd.DataFrame, freq: str) -> pd.DataFrame:
    """One TPO profile per (name, period) built from that period's 30m bars."""
    b = bars.copy()
    b["period"] = b["ts"].dt.to_period(freq)
    rows = []
    for (name, per), g in b.groupby(["underlying", "period"], sort=False):
        ref = float(g["close"].iloc[-1])
        if not np.isfinite(ref) or ref <= 0 or len(g) < 5:
            continue
        p = Profile(g["low"].values, g["high"].values, max(ref * TICK_BPS, 1e-6))
        val, vah = p.value_area()
        rows.append({"underlying": name, "period": per, "poc": p.poc,
                     "val": val, "vah": vah, "hi": g["high"].max(),
                     "lo": g["low"].min(), "close": ref, "bars": len(g)})
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out = out.sort_values(["underlying", "period"]).reset_index(drop=True)
    # shift to the PREVIOUS completed period -- the current one contains the
    # session being predicted and would leak it
    g = out.groupby("underlying")
    for c in ("poc", "val", "vah", "hi", "lo"):
        out[f"prev_{c}"] = g[c].shift(1)
    return out


def load_mtf(connection, names: list[str], start) -> pd.DataFrame:
    bars = pd.read_sql(BAR_SQL, connection, params={"start": start, "names": names})
    bars["ts"] = pd.to_datetime(bars["ts"])
    for c in ("open", "high", "low", "close", "volume"):
        bars[c] = pd.to_numeric(bars[c], errors="coerce")
    daily = add_context(sessions(bars))
    daily["wk"] = daily["dt"].dt.to_period("W")
    daily["mo"] = daily["dt"].dt.to_period("M")

    wk = _period_profiles(bars, "W").rename(columns={"period": "wk"})
    mo = _period_profiles(bars, "M").rename(columns={"period": "mo"})
    keep = ["underlying", "prev_poc", "prev_val", "prev_vah", "prev_hi", "prev_lo"]
    daily = daily.merge(
        wk[["wk"] + keep].rename(columns={c: f"w_{c[5:]}" for c in keep[1:]}),
        on=["underlying", "wk"], how="left")
    daily = daily.merge(
        mo[["mo"] + keep].rename(columns={c: f"m_{c[5:]}" for c in keep[1:]}),
        on=["underlying", "mo"], how="left")

    c = daily["close"]
    for p in ("w", "m"):
        width = (daily[f"{p}_vah"] - daily[f"{p}_val"]).replace(0, np.nan)
        daily[f"{p}_vs_vah"] = (c - daily[f"{p}_vah"]) / width
        daily[f"{p}_vs_poc"] = (c - daily[f"{p}_poc"]) / width
        daily[f"{p}_above"] = c > daily[f"{p}_vah"]
        daily[f"{p}_below"] = c < daily[f"{p}_val"]
        daily[f"{p}_loc"] = np.select(
            [daily[f"{p}_above"], daily[f"{p}_below"]],
            ["above", "below"], default="inside")
    # the hierarchical read: agreement across day, week and month
    daily["d_above"] = c > daily["vah"]
    daily["mtf_align"] = np.select(
        [daily["d_above"] & daily["w_above"] & daily["m_above"],
         daily["d_above"] & daily["w_above"],
         (c < daily["val"]) & daily["w_below"] & daily["m_below"],
         (c < daily["val"]) & daily["w_below"]],
        ["above all three", "above day+week", "below all three", "below day+week"],
        default="mixed")
    return daily


def targets(s: pd.DataFrame, horizons=(3, 4)) -> pd.DataFrame:
    """Forward touch/close excursions from each session's close."""
    s = s.sort_values(["underlying", "dt"]).copy()
    g = s.groupby("underlying")
    c0 = s["close"]
    for h in horizons:
        hi = pd.concat([g["high"].shift(-k) for k in range(1, h + 1)], axis=1).max(axis=1)
        lo = pd.concat([g["low"].shift(-k) for k in range(1, h + 1)], axis=1).min(axis=1)
        s[f"up{h}"] = (hi / c0 - 1) * 100
        s[f"dn{h}"] = (lo / c0 - 1) * 100
        s[f"cc{h}"] = (g["close"].shift(-h) / c0 - 1) * 100
    return s
