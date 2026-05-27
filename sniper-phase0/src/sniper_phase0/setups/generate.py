"""Walk through ticks, sample at bar boundaries, run all six detectors,
emit candidates as a parquet that mirrors the trade-log schema.

Sampling cadence: every 30s. That's frequent enough to catch most setups
without exploding the candidate count.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from sniper_phase0.data.mp_state import compute_mp_state, compute_session_mp
from sniper_phase0.data.ticks import load_ticks
from sniper_phase0.setups.base import Candidate
from sniper_phase0.setups.detectors import (
    detect_failed_auction,
    detect_ib_breakout,
    detect_lvn_rejection,
    detect_poc_magnet,
    detect_va_acceptance,
    detect_va_rejection,
)
from sniper_phase0.utils.settings import Settings
from sniper_phase0.utils.time import minutes_into_session, session_bounds


SAMPLE_SECONDS = 30


def _prev_business_day(d: pd.Timestamp) -> pd.Timestamp:
    p = d - pd.Timedelta(days=1)
    while p.weekday() >= 5:
        p -= pd.Timedelta(days=1)
    return p


def generate_for_day(
    instrument: str,
    day: pd.Timestamp,
    settings: Settings,
) -> list[Candidate]:
    day = pd.Timestamp(day).normalize()
    ticks = load_ticks(settings.paths.underlying_ticks, instrument, day, day + pd.Timedelta(days=1))
    if ticks.empty:
        return []

    prev_day = _prev_business_day(day)
    prev_ticks = load_ticks(
        settings.paths.underlying_ticks, instrument, prev_day, prev_day + pd.Timedelta(days=1)
    )
    prev_mp = compute_session_mp(prev_ticks, prev_day) if not prev_ticks.empty else None

    open_ts, close_ts = session_bounds(ticks["ts"].iloc[0])
    sample_times = pd.date_range(open_ts, close_ts, freq=f"{SAMPLE_SECONDS}s")

    candidates: list[Candidate] = []
    intraday_high = float("-inf")
    intraday_low = float("inf")
    last_spot: float | None = None

    for ts in sample_times:
        # Strictly-before window for spot + intraday extremes.
        prior = ticks[ticks["ts"] < ts]
        if prior.empty:
            continue
        spot = float(prior["ltp"].iloc[-1])
        intraday_high = max(intraday_high, float(prior["ltp"].max()))
        intraday_low = min(intraday_low, float(prior["ltp"].min()))

        mp = compute_mp_state(ticks, ts)
        if mp is None:
            last_spot = spot
            continue

        mins = minutes_into_session(ts)
        last_window = prior.tail(20)
        recent_high = float(last_window["ltp"].max())
        recent_low = float(last_window["ltp"].min())
        last_ret = 0.0
        if last_spot is not None and last_spot > 0:
            last_ret = (spot - last_spot) / last_spot

        for cand in (
            detect_va_rejection(ts, instrument, spot, mp, prev_mp, recent_high, recent_low),
            detect_va_acceptance(ts, instrument, spot, mp, prev_mp, mins),
            detect_ib_breakout(ts, instrument, spot, mp, mins),
            detect_lvn_rejection(ts, instrument, spot, mp, prev_mp, last_ret),
            detect_poc_magnet(ts, instrument, spot, mp, prev_mp, mins),
            detect_failed_auction(ts, instrument, spot, mp, prev_mp, intraday_high, intraday_low),
        ):
            if cand is not None:
                candidates.append(cand)

        last_spot = spot

    return candidates


def candidates_as_pseudo_trades(
    candidates: list[Candidate], lot_size: int = 25
) -> pd.DataFrame:
    """Convert candidates into a trade-log-compatible schema for downstream feature/label runs.

    `entry_ts == exit_ts` and `exit_price == entry_price` at generation time —
    the triple-barrier labeler walks forward from entry_ts using actual ticks
    to determine true outcome.
    """
    rows = []
    for tid, c in enumerate(candidates):
        rows.append(
            {
                "trade_id": tid,
                "symbol": c.instrument,
                "instrument_type": "FUT",
                "entry_ts": c.decision_ts,
                "exit_ts": c.decision_ts,  # placeholder
                "side": c.side,
                "qty": lot_size,
                "entry_price": c.entry_price,
                "exit_price": c.entry_price,  # placeholder
                "gross_pnl": 0.0,
                "net_pnl_actual": pd.NA,
                "stop_price": c.stop_price,
                "target_price": c.target_price,
                "setup_name": c.setup_name,
            }
        )
    return pd.DataFrame(rows)
