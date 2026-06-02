"""ATM option structure and flow features (families C/D in the feature contract).

All features are null-safe. If no ATMOptionSeries is available, the builder emits the full option
feature schema with `None` values so the model schema is stable across underlying-only and
option-enriched runs.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from nomad_sniper.data.option_bars import ATMOptionSeries
from nomad_sniper.features.base import Feature, FeatureSnapshot
from nomad_sniper.utils.normalize import rolling_tod_baseline, zscore
from nomad_sniper.utils.timeutil import ensure_ist, tod_bucket_key

OPTION_FEATURE_NAMES = [
    "o_straddle_decay_vs_theta",
    "o_iv_level",
    "o_iv_change",
    "o_ce_pe_premium_ratio",
    "o_ce_pe_premium_ratio_drift",
    "o_ce_ret_minus_pe_ret",
    "o_balanced_divergence",
    "o_ce_value_break_vs_u_hold",
    "o_pe_value_break_vs_u_hold",
    "o_straddle_value_width_ratio",
    "o_ce_oi_change_pct",
    "o_pe_oi_change_pct",
    "o_ce_volume_z",
    "o_pe_volume_z",
    "o_pcr_volume",
    "o_pcr_oi",
    "o_ce_pe_aggressor_imbalance",
]


def build_option_structure_features(
    decision_time: datetime,
    bars_underlying: pd.DataFrame,
    atm: ATMOptionSeries | None = None,
    *,
    snapshot: FeatureSnapshot | None = None,
    lookback_minutes: int = 30,
    balanced_underlying_ret_threshold: float = 0.0015,
) -> FeatureSnapshot:
    decision_time = ensure_ist(decision_time)
    if snapshot is None:
        snapshot = FeatureSnapshot(decision_time=decision_time)

    if atm is None:
        _add_nulls(snapshot, decision_time)
        return snapshot

    ce = atm.ce[atm.ce.index <= decision_time].tail(lookback_minutes)
    pe = atm.pe[atm.pe.index <= decision_time].tail(lookback_minutes)
    st = atm.straddle[atm.straddle.index <= decision_time].tail(lookback_minutes)
    u = bars_underlying[bars_underlying.index <= decision_time].tail(lookback_minutes)
    if ce.empty or pe.empty or st.empty or u.empty:
        _add_nulls(snapshot, decision_time)
        return snapshot

    avail = max(ce.index[-1], pe.index[-1], st.index[-1])
    ce_close = ce["close"].astype(float)
    pe_close = pe["close"].astype(float)
    st_close = st["close"].astype(float)

    ratio = _safe_div(ce_close.iloc[-1], pe_close.iloc[-1])
    ratio_start = _safe_div(ce_close.iloc[0], pe_close.iloc[0])
    snapshot.add(Feature("o_ce_pe_premium_ratio", ratio, avail, "option"))
    snapshot.add(Feature(
        "o_ce_pe_premium_ratio_drift",
        (ratio - ratio_start) if ratio is not None and ratio_start is not None else None,
        avail,
        "option",
    ))

    ce_ret = _pct_change(ce_close.iloc[0], ce_close.iloc[-1])
    pe_ret = _pct_change(pe_close.iloc[0], pe_close.iloc[-1])
    u_ret = _pct_change(float(u["close"].iloc[0]), float(u["close"].iloc[-1]))
    ret_diff = (ce_ret - pe_ret) if ce_ret is not None and pe_ret is not None else None
    snapshot.add(Feature("o_ce_ret_minus_pe_ret", ret_diff, avail, "option"))
    balanced = u_ret is not None and abs(u_ret) < balanced_underlying_ret_threshold
    snapshot.add(Feature(
        "o_balanced_divergence",
        min(1.0, abs(ret_diff)) if balanced and ret_diff is not None else 0.0 if balanced else None,
        avail,
        "option",
    ))

    iv = _last_valid(ce.get("iv"), pe.get("iv"))
    iv_start = _first_valid(ce.get("iv"), pe.get("iv"))
    snapshot.add(Feature("o_iv_level", iv, avail, "option"))
    snapshot.add(Feature(
        "o_iv_change",
        (iv - iv_start) if iv is not None and iv_start is not None else None,
        avail,
        "option",
    ))

    realized_decay = _pct_change(st_close.iloc[0], st_close.iloc[-1])
    theta_proxy = -abs(float(st_close.iloc[0])) * max(1, len(st_close)) / (375.0 * 5.0)
    snapshot.add(Feature(
        "o_straddle_decay_vs_theta",
        _safe_div(float(st_close.iloc[-1] - st_close.iloc[0]), theta_proxy),
        avail,
        "option",
    ))

    u_hold = u_ret is not None and abs(u_ret) < balanced_underlying_ret_threshold
    snapshot.add(Feature("o_ce_value_break_vs_u_hold", int(u_hold and _breaks_recent_value(ce)), avail, "option"))
    snapshot.add(Feature("o_pe_value_break_vs_u_hold", int(u_hold and _breaks_recent_value(pe)), avail, "option"))
    snapshot.add(Feature("o_straddle_value_width_ratio", _value_width_ratio(st, atm.straddle, decision_time), avail, "option"))

    snapshot.add(Feature("o_ce_oi_change_pct", _oi_change_pct(ce), avail, "option"))
    snapshot.add(Feature("o_pe_oi_change_pct", _oi_change_pct(pe), avail, "option"))
    snapshot.add(Feature("o_ce_volume_z", zscore(float(ce["volume"].sum()), _volume_baseline(atm.ce, decision_time, len(ce))), avail, "option"))
    snapshot.add(Feature("o_pe_volume_z", zscore(float(pe["volume"].sum()), _volume_baseline(atm.pe, decision_time, len(pe))), avail, "option"))
    snapshot.add(Feature("o_pcr_volume", _safe_div(float(pe["volume"].sum()), float(ce["volume"].sum())), avail, "option"))
    snapshot.add(Feature("o_pcr_oi", _safe_div(_last_number(pe.get("oi")), _last_number(ce.get("oi"))), avail, "option"))

    ce_aggr = _signed_volume(ce)
    pe_aggr = _signed_volume(pe)
    denom = abs(ce_aggr) + abs(pe_aggr)
    snapshot.add(Feature(
        "o_ce_pe_aggressor_imbalance",
        ((ce_aggr - pe_aggr) / denom) if denom > 0 else None,
        avail,
        "option",
    ))
    return snapshot


def _add_nulls(snapshot: FeatureSnapshot, decision_time: datetime) -> None:
    for name in OPTION_FEATURE_NAMES:
        snapshot.add(Feature(name, None, decision_time, "option"))


def _safe_div(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0 or not np.isfinite(den):
        return None
    out = num / den
    return float(out) if np.isfinite(out) else None


def _pct_change(start: float, end: float) -> float | None:
    return _safe_div(float(end - start), float(start))


def _last_number(series: pd.Series | None) -> float | None:
    if series is None:
        return None
    vals = pd.to_numeric(series, errors="coerce").dropna()
    return float(vals.iloc[-1]) if not vals.empty else None


def _last_valid(*series_list: pd.Series | None) -> float | None:
    vals = []
    for series in series_list:
        if series is not None:
            vals.extend(pd.to_numeric(series, errors="coerce").dropna().tolist())
    return float(vals[-1]) if vals else None


def _first_valid(*series_list: pd.Series | None) -> float | None:
    vals = []
    for series in series_list:
        if series is not None:
            vals.extend(pd.to_numeric(series, errors="coerce").dropna().tolist())
    return float(vals[0]) if vals else None


def _oi_change_pct(df: pd.DataFrame) -> float | None:
    if "oi" not in df:
        return None
    oi = pd.to_numeric(df["oi"], errors="coerce").dropna()
    if len(oi) < 2 or oi.iloc[0] == 0:
        return None
    return float(100.0 * (oi.iloc[-1] - oi.iloc[0]) / oi.iloc[0])


def _volume_baseline(series: pd.DataFrame, decision_time: datetime, n: int):
    baseline = rolling_tod_baseline(series, decision_time, "volume", tod_key=tod_bucket_key)
    if baseline is None:
        return None
    mu, sigma = baseline
    return mu * n, sigma * (n**0.5)


def _signed_volume(df: pd.DataFrame) -> float:
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    body = (df["close"] - df["open"]) / rng
    return float((body.fillna(0) * df["volume"]).sum())


def _breaks_recent_value(df: pd.DataFrame) -> bool:
    close = df["close"].astype(float)
    if len(close) < 10:
        return False
    prior = close.iloc[:-1]
    vah = prior.quantile(0.70)
    val = prior.quantile(0.30)
    last = close.iloc[-1]
    return bool(last > vah or last < val)


def _value_width_ratio(window: pd.DataFrame, full: pd.DataFrame, decision_time: datetime) -> float | None:
    if len(window) < 5:
        return None
    width = float(window["close"].quantile(0.70) - window["close"].quantile(0.30))
    prior = full[full.index.date < decision_time.date()]
    if prior.empty:
        return None
    daily = prior.groupby(prior.index.date)["close"].agg(lambda s: s.quantile(0.70) - s.quantile(0.30))
    med = float(daily.tail(20).median())
    return _safe_div(width, med)
