"""Family A4 — VWAP & imbalance-lean features (the institutional directional reference).

VWAP is where size transacts; price ACCEPTING above VWAP = buyers in control (bullish lean),
below = sellers. Distance/slope/acceptance vs session and multi-timeframe ANCHORED VWAP give a
directional read that POC/value-area distance alone miss. We also add a multi-TF value-migration
"imbalance lean": when value is migrating the same way across week/month/quarter the market is in
imbalance (follow); when flat it is balancing (fade). All ATR-normalized / bounded (§2).

Needs volume: on a pure index (volume 0) VWAP is null. Futures / ETFs / stocks have real volume.
Session VWAP from minute bars; weekly/monthly anchored VWAP from daily aggregation (cheap on
multi-year minute data, ~identical level).
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from nomad_sniper.features.base import Feature, FeatureSnapshot
from nomad_sniper.utils.timeutil import ensure_ist

_VWAP_NAMES = (
    "u_dist_session_vwap_atr", "u_session_vwap_slope_atr", "u_above_vwap_fraction",
    "u_vwap_zscore", "u_dist_vwap_week_atr", "u_dist_vwap_month_atr",
    "u_value_migration_lean", "u_above_vwap_all_tf",
)


def _anchored_vwap(daily: pd.DataFrame, keys, cur_key) -> float | None:
    sub = daily[[k == cur_key for k in keys]]
    v = sub["volume"].to_numpy(float)
    if v.sum() <= 0:
        return None
    typ = ((sub["high"] + sub["low"] + sub["close"]) / 3.0).to_numpy(float)
    return float((typ * v).sum() / v.sum())


def build_vwap_features(
    decision_time: datetime,
    bars: pd.DataFrame,
    atr_ref: float | None,
    *,
    snapshot: FeatureSnapshot | None = None,
    htf_migration: dict | None = None,
) -> FeatureSnapshot:
    decision_time = ensure_ist(decision_time)
    if snapshot is None:
        snapshot = FeatureSnapshot(decision_time=decision_time)

    out = {n: None for n in _VWAP_NAMES}
    if atr_ref is None or atr_ref <= 0:
        for n in _VWAP_NAMES:
            snapshot.add(Feature(n, None, decision_time, "mp"))
        return snapshot

    hist = bars[bars.index <= decision_time]
    if hist.empty:
        for n in _VWAP_NAMES:
            snapshot.add(Feature(n, None, decision_time, "mp"))
        return snapshot
    avail = ensure_ist(hist.index[-1].to_pydatetime())
    price = float(hist.iloc[-1]["close"])
    today = decision_time.date()
    dev = hist[hist.index.date == today]

    # ── session VWAP (minute) ──
    if not dev.empty and "volume" in dev.columns and float(dev["volume"].sum()) > 0:
        typ = ((dev["high"] + dev["low"] + dev["close"]) / 3.0).to_numpy(float)
        vol = dev["volume"].to_numpy(float)
        cum_pv = np.cumsum(typ * vol); cum_v = np.cumsum(vol)
        running = cum_pv / np.where(cum_v > 0, cum_v, np.nan)
        vwap = float(running[-1])
        out["u_dist_session_vwap_atr"] = float((price - vwap) / atr_ref)
        cl = dev["close"].to_numpy(float)
        out["u_above_vwap_fraction"] = float(np.nanmean(cl > running))
        # dispersion of price around VWAP (volume-weighted), as a z-score
        var = float((vol * (typ - vwap) ** 2).sum() / vol.sum())
        sd = var ** 0.5
        out["u_vwap_zscore"] = float(min(5.0, max(-5.0, (price - vwap) / sd))) if sd > 0 else 0.0
        if len(running) >= 6 and np.isfinite(running[-6]):
            out["u_session_vwap_slope_atr"] = float((vwap - running[-6]) / atr_ref)

    # ── anchored weekly / monthly VWAP (daily aggregation) ──
    daily = hist.resample("1D").agg(
        {"high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["close"])
    if not daily.empty and float(daily["volume"].sum()) > 0:
        ts = pd.DatetimeIndex(daily.index)
        iso = ts.isocalendar()
        wk = [f"{y}-W{w:02d}" for y, w in zip(iso.year, iso.week)]
        mo = [f"{t.year}-{t.month:02d}" for t in ts]
        vw = _anchored_vwap(daily, wk, wk[-1])
        vm = _anchored_vwap(daily, mo, mo[-1])
        if vw is not None:
            out["u_dist_vwap_week_atr"] = float((price - vw) / atr_ref)
        if vm is not None:
            out["u_dist_vwap_month_atr"] = float((price - vm) / atr_ref)
        # above ALL VWAPs (session+week+month) = strong bullish lean (signed -1/0/+1)
        above = [out.get(k) for k in ("u_dist_session_vwap_atr", "u_dist_vwap_week_atr",
                                      "u_dist_vwap_month_atr")]
        above = [x for x in above if x is not None]
        if above:
            if all(x > 0 for x in above):
                out["u_above_vwap_all_tf"] = 1.0
            elif all(x < 0 for x in above):
                out["u_above_vwap_all_tf"] = -1.0
            else:
                out["u_above_vwap_all_tf"] = 0.0

    # ── imbalance lean: net same-direction value migration across HTF (week/month/quarter) ──
    if htf_migration:
        vals = [htf_migration.get(k) for k in ("week", "month", "quarter")]
        vals = [v for v in vals if v is not None and np.isfinite(v)]
        if vals:
            s = sum(np.sign(v) for v in vals)
            out["u_value_migration_lean"] = float(s / len(vals))  # ∈[-1,1], magnitude=agreement

    for n in _VWAP_NAMES:
        snapshot.add(Feature(n, out[n], avail, "mp"))
    return snapshot
