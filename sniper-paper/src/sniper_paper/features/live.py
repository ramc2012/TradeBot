"""Live feature builder. Reads from the tick buffer, computes the feature
vector the model expects at decision_ts. Mirrors Phase 0 features but adapted
for live windows.

Only the features listed in `Settings.model.predict_features` are required;
others are optional and emitted as NaN if unavailable.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

import numpy as np
import pandas as pd

from sniper_paper.common.settings import Instrument
from sniper_paper.common.time import IST, minutes_into_session, parse_hm, to_ist
from sniper_paper.features.mp_state import MPState, compute_mp_state, compute_session_mp


@dataclass
class FeatureVector:
    decision_ts: pd.Timestamp
    instrument: str
    spot: float
    values: dict[str, float]


def _pct(a: float, b: float) -> float:
    return (a - b) / b * 100.0 if b else float("nan")


def build_feature_vector(
    instrument: Instrument,
    decision_ts: pd.Timestamp,
    session_ticks: pd.DataFrame,
    prev_session_ticks: pd.DataFrame,
    spot: float,
    expiry_date: pd.Timestamp | None = None,
    prior_close: float | None = None,
    atr_14d: float | None = None,
) -> FeatureVector:
    decision_ts = to_ist(decision_ts)
    session_open = pd.Timestamp(
        datetime.combine(decision_ts.date(), parse_hm(instrument.trading_hours_ist.open), tzinfo=IST)
    )

    mp = compute_mp_state(session_ticks, decision_ts, session_open, instrument.tick_size)
    prev_mp = (
        compute_session_mp(
            prev_session_ticks,
            session_open - pd.Timedelta(days=1),
            session_open - pd.Timedelta(minutes=1),
            instrument.tick_size,
        )
        if not prev_session_ticks.empty
        else None
    )

    v: dict[str, float] = {}

    # Intraday MP
    if mp is not None:
        v["mp_dist_poc_pct"] = _pct(spot, mp.poc)
        v["mp_dist_vah_pct"] = _pct(spot, mp.vah)
        v["mp_dist_val_pct"] = _pct(spot, mp.val)
        v["mp_in_value_area"] = float(mp.val <= spot <= mp.vah)
        v["mp_above_vah"] = float(spot > mp.vah)
        v["mp_below_val"] = float(spot < mp.val)
        ib_range = mp.ib_high - mp.ib_low
        v["mp_ib_position"] = (spot - mp.ib_low) / ib_range if ib_range > 0 else float("nan")
        v["mp_va_width_pct"] = _pct(mp.vah, mp.val)
        v["mp_tpo_count"] = float(mp.tpo_count)
    else:
        for k in ["mp_dist_poc_pct", "mp_dist_vah_pct", "mp_dist_val_pct",
                  "mp_in_value_area", "mp_above_vah", "mp_below_val",
                  "mp_ib_position", "mp_va_width_pct", "mp_tpo_count"]:
            v[k] = float("nan")

    # Prior-session MP
    if prev_mp is not None:
        v["mp_prev_dist_poc_pct"] = _pct(spot, prev_mp.poc)
        v["mp_prev_dist_vah_pct"] = _pct(spot, prev_mp.vah)
        v["mp_prev_dist_val_pct"] = _pct(spot, prev_mp.val)
        v["mp_prev_in_value_area"] = float(prev_mp.val <= spot <= prev_mp.vah)
        v["mp_single_prints_prev"] = float(prev_mp.single_prints)
        v["mp_poor_high_prev"] = float(prev_mp.poor_high)
        v["mp_poor_low_prev"] = float(prev_mp.poor_low)
        if prev_mp.hvn_prices:
            v["mp_nearest_hvn_dist_pct"] = _pct(spot, min(prev_mp.hvn_prices, key=lambda p: abs(p - spot)))
        else:
            v["mp_nearest_hvn_dist_pct"] = float("nan")
        if prev_mp.lvn_prices:
            v["mp_nearest_lvn_dist_pct"] = _pct(spot, min(prev_mp.lvn_prices, key=lambda p: abs(p - spot)))
        else:
            v["mp_nearest_lvn_dist_pct"] = float("nan")
    else:
        for k in ["mp_prev_dist_poc_pct", "mp_prev_dist_vah_pct", "mp_prev_dist_val_pct",
                  "mp_prev_in_value_area", "mp_single_prints_prev",
                  "mp_poor_high_prev", "mp_poor_low_prev",
                  "mp_nearest_hvn_dist_pct", "mp_nearest_lvn_dist_pct"]:
            v[k] = float("nan")

    # Context
    v["ctx_minutes_into_session"] = float(minutes_into_session(decision_ts, instrument))
    v["ctx_dow"] = float(decision_ts.weekday())
    v["ctx_is_expiry_day"] = float(expiry_date is not None and decision_ts.date() == expiry_date.date())
    v["ctx_is_expiry_week"] = float(
        expiry_date is not None
        and 0 <= (expiry_date.date() - decision_ts.date()).days <= 4
    )
    v["ctx_dte"] = float((expiry_date.date() - decision_ts.date()).days) if expiry_date else float("nan")
    if prior_close is not None and spot:
        v["ctx_overnight_gap_pct"] = _pct(spot, prior_close)
    else:
        v["ctx_overnight_gap_pct"] = float("nan")
    v["ctx_atr_14d"] = float(atr_14d) if atr_14d is not None else float("nan")

    return FeatureVector(
        decision_ts=decision_ts,
        instrument=instrument.name,
        spot=spot,
        values=v,
    )


def feature_vector_to_array(fv: FeatureVector, feature_order: list[str]) -> np.ndarray:
    return np.array([fv.values.get(name, float("nan")) for name in feature_order], dtype=float)
