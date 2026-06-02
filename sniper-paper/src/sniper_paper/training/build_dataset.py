"""Build the training dataset from NIFTY candles.

Since we don't have tick data historically, the v0 model trains on
*candle-derived* features only. Procedure:

  1. Pull 30-min NIFTY candles from the DB.
  2. For each candle close timestamp, treat it as a `decision_ts`.
  3. Build MP state from a synthetic tick stream reconstructed from intraday candles
     of that day (each candle close is a "tick"; multiple ticks per candle if we
     down-resample 30m → 5m).
  4. Apply the setup detectors at that timestamp.
  5. For each candidate, compute triple-barrier outcome on the FORWARD candle path.
  6. Apply the cost model — produce net_R as the label.

Limitation flagged upfront: the v0 model has no order-flow features. That's
honest — they don't exist in the historical data. The OF features will fill
in once live tick capture has accumulated enough history (Phase 2 work).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

import numpy as np
import pandas as pd

from sniper_paper.common.settings import Instrument, Settings
from sniper_paper.common.time import IST, parse_hm
from sniper_paper.execution.cost_model import round_trip_costs
from sniper_paper.features.live import build_feature_vector
from sniper_paper.signals.candidate import Candidate
from sniper_paper.signals.detectors import (
    detect_failed_auction, detect_ib_breakout, detect_lvn_rejection,
    detect_poc_magnet, detect_va_acceptance, detect_va_rejection,
)


@dataclass
class TrainingRow:
    decision_ts: pd.Timestamp
    instrument: str
    setup_name: str
    side: str
    entry_price: float
    stop_price: float
    target_price: float
    features: dict
    # labels
    outcome: str
    gross_R: float
    net_R: float
    mae_R: float
    mfe_R: float


def _candles_to_pseudo_ticks(day_candles: pd.DataFrame) -> pd.DataFrame:
    """Each 30m candle → 4 'tick' samples (open, high, low, close) at evenly spaced ts.

    This is a coarse approximation but lets MP profiling and detector code from
    sniper-phase0 run unchanged.
    """
    if day_candles.empty:
        return day_candles.copy()
    rows = []
    for _, c in day_candles.iterrows():
        base_ts = pd.Timestamp(c["ts"])
        for off_min, px in zip([0, 8, 16, 24], [c["open"], c["high"], c["low"], c["close"]]):
            rows.append({"ts": base_ts + pd.Timedelta(minutes=off_min), "ltp": float(px)})
    return pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)


def _triple_barrier_outcome(
    forward_ticks: pd.DataFrame, cand: Candidate, max_minutes: int = 90,
) -> tuple[str, float, float]:
    """Walk forward ticks until stop or target hit, return (outcome, mae_R, mfe_R)."""
    if forward_ticks.empty:
        return "timeout", 0.0, 0.0
    risk = abs(cand.entry_price - cand.stop_price)
    cutoff = cand.decision_ts + pd.Timedelta(minutes=max_minutes)
    mae = 0.0
    mfe = 0.0
    for _, t in forward_ticks.iterrows():
        if t["ts"] > cutoff:
            break
        ltp = float(t["ltp"])
        if cand.side == "long":
            mae = min(mae, ltp - cand.entry_price)
            mfe = max(mfe, ltp - cand.entry_price)
            if ltp <= cand.stop_price:
                return "stop", mae / risk, mfe / risk
            if ltp >= cand.target_price:
                return "target", mae / risk, mfe / risk
        else:
            mae = min(mae, cand.entry_price - ltp)
            mfe = max(mfe, cand.entry_price - ltp)
            if ltp >= cand.stop_price:
                return "stop", mae / risk, mfe / risk
            if ltp <= cand.target_price:
                return "target", mae / risk, mfe / risk
    return "timeout", mae / risk if risk else 0, mfe / risk if risk else 0


def _run_detectors(
    ts: pd.Timestamp, spot: float, today_ticks: pd.DataFrame,
    instrument: Instrument, prev_mp, mp, mins_into_session: int,
) -> list[Candidate]:
    if mp is None:
        return []
    last_window = today_ticks.tail(20)
    recent_high = float(last_window["ltp"].max()) if not last_window.empty else spot
    recent_low = float(last_window["ltp"].min()) if not last_window.empty else spot
    intraday_high = float(today_ticks["ltp"].max()) if not today_ticks.empty else spot
    intraday_low = float(today_ticks["ltp"].min()) if not today_ticks.empty else spot
    last_ret = 0.0
    if len(last_window) >= 2:
        first = float(last_window["ltp"].iloc[0])
        if first > 0:
            last_ret = (spot - first) / first

    cands = [
        detect_va_rejection(ts, instrument.name, instrument.near_month_symbol, spot, mp, prev_mp, recent_high, recent_low),
        detect_va_acceptance(ts, instrument.name, instrument.near_month_symbol, spot, mp, prev_mp, mins_into_session),
        detect_ib_breakout(ts, instrument.name, instrument.near_month_symbol, spot, mp, mins_into_session),
        detect_lvn_rejection(ts, instrument.name, instrument.near_month_symbol, spot, mp, prev_mp, last_ret),
        detect_poc_magnet(ts, instrument.name, instrument.near_month_symbol, spot, mp, prev_mp, mins_into_session),
        detect_failed_auction(ts, instrument.name, instrument.near_month_symbol, spot, mp, prev_mp, intraday_high, intraday_low),
    ]
    return [c for c in cands if c is not None]


def build_training_rows(
    candles: pd.DataFrame, instrument: Instrument, settings: Settings,
) -> list[TrainingRow]:
    """Walk through candle history, generate candidates, label them."""
    from sniper_paper.features.mp_state import compute_mp_state, compute_session_mp
    from sniper_paper.common.time import minutes_into_session

    if candles.empty:
        return []
    candles = candles.sort_values("ts").reset_index(drop=True)
    candles["date"] = candles["ts"].dt.date
    by_day = candles.groupby("date")

    rows: list[TrainingRow] = []
    days = sorted(by_day.groups.keys())
    for i, day in enumerate(days):
        day_candles = by_day.get_group(day)
        today_ticks = _candles_to_pseudo_ticks(day_candles)
        session_open = pd.Timestamp(
            datetime.combine(day, parse_hm(instrument.trading_hours_ist.open), tzinfo=IST)
        )
        session_close = pd.Timestamp(
            datetime.combine(day, parse_hm(instrument.trading_hours_ist.close), tzinfo=IST)
        )

        prev_mp = None
        if i > 0:
            prev_day = days[i - 1]
            prev_day_candles = by_day.get_group(prev_day)
            prev_ticks = _candles_to_pseudo_ticks(prev_day_candles)
            prev_open = pd.Timestamp(
                datetime.combine(prev_day, parse_hm(instrument.trading_hours_ist.open), tzinfo=IST)
            )
            prev_close = pd.Timestamp(
                datetime.combine(prev_day, parse_hm(instrument.trading_hours_ist.close), tzinfo=IST)
            )
            prev_mp = compute_session_mp(prev_ticks, prev_open, prev_close, instrument.tick_size)

        # Walk through this day's candle closes as decision_ts moments.
        for _, c in day_candles.iterrows():
            decision_ts = pd.Timestamp(c["ts"]) + pd.Timedelta(minutes=30)  # close of candle
            spot = float(c["close"])
            session_ticks = today_ticks[today_ticks["ts"] < decision_ts]
            mp = compute_mp_state(session_ticks, decision_ts, session_open, instrument.tick_size)
            mins = minutes_into_session(decision_ts, instrument)
            cands = _run_detectors(decision_ts, spot, session_ticks, instrument, prev_mp, mp, mins)
            if not cands:
                continue

            fv = build_feature_vector(
                instrument=instrument,
                decision_ts=decision_ts,
                session_ticks=session_ticks,
                prev_session_ticks=(
                    _candles_to_pseudo_ticks(by_day.get_group(days[i - 1])) if i > 0 else pd.DataFrame(columns=["ts", "ltp"])
                ),
                spot=spot,
            )

            forward = today_ticks[today_ticks["ts"] > decision_ts]
            for cand in cands:
                outcome, mae_R, mfe_R = _triple_barrier_outcome(forward, cand)
                # Compute exit price for cost-model purposes.
                exit_price = cand.target_price if outcome == "target" else (
                    cand.stop_price if outcome == "stop" else spot
                )
                costs = round_trip_costs(
                    settings.costs, instrument.exchange, instrument.lot_size,
                    cand.entry_price, exit_price,
                )
                gross_pnl = (
                    (exit_price - cand.entry_price) * instrument.lot_size
                    if cand.side == "long" else
                    (cand.entry_price - exit_price) * instrument.lot_size
                )
                risk_inr = abs(cand.entry_price - cand.stop_price) * instrument.lot_size
                gross_R = gross_pnl / risk_inr if risk_inr else 0.0
                net_R = (gross_pnl - costs["total"]) / risk_inr if risk_inr else 0.0

                rows.append(TrainingRow(
                    decision_ts=decision_ts,
                    instrument=instrument.name,
                    setup_name=cand.setup_name,
                    side=cand.side,
                    entry_price=cand.entry_price,
                    stop_price=cand.stop_price,
                    target_price=cand.target_price,
                    features=fv.values,
                    outcome=outcome,
                    gross_R=gross_R,
                    net_R=net_R,
                    mae_R=mae_R,
                    mfe_R=mfe_R,
                ))
    return rows
