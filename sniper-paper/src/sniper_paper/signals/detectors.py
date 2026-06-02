"""Six setup-family detectors. Same logic as sniper-phase0, adapted for live use.

Each detector returns Optional[Candidate]. They are conservative — emit only
when MP rules are clearly met. The model filters which candidates become trades.

Stops/targets are MP-derived. Never arbitrary point values.
"""
from __future__ import annotations

import pandas as pd

from sniper_paper.features.mp_state import MPState
from sniper_paper.signals.candidate import Candidate, valid_barriers


def _wrap(c: Candidate | None) -> Candidate | None:
    if c is None:
        return None
    return c if valid_barriers(c.side, c.entry_price, c.stop_price, c.target_price) else None


def detect_va_rejection(
    ts: pd.Timestamp, instrument: str, symbol: str, spot: float,
    mp: MPState, prev_mp: MPState | None, recent_high: float, recent_low: float,
) -> Candidate | None:
    if prev_mp is None:
        return None
    above = spot >= prev_mp.vah * 0.9995 and recent_high >= prev_mp.vah
    below = spot <= prev_mp.val * 1.0005 and recent_low <= prev_mp.val
    if above and spot < recent_high:
        return _wrap(Candidate(
            decision_ts=ts, instrument=instrument, symbol=symbol, side="short",
            entry_price=spot,
            stop_price=max(recent_high, prev_mp.vah) * 1.002,
            target_price=prev_mp.poc,
            setup_name="va_rejection",
        ))
    if below and spot > recent_low:
        return _wrap(Candidate(
            decision_ts=ts, instrument=instrument, symbol=symbol, side="long",
            entry_price=spot,
            stop_price=min(recent_low, prev_mp.val) * 0.998,
            target_price=prev_mp.poc,
            setup_name="va_rejection",
        ))
    return None


def detect_va_acceptance(
    ts: pd.Timestamp, instrument: str, symbol: str, spot: float,
    mp: MPState, prev_mp: MPState | None, minutes_into_session: int,
) -> Candidate | None:
    if prev_mp is None or minutes_into_session < 30:
        return None
    if mp.profile_high > prev_mp.vah and spot > prev_mp.vah * 1.001:
        return _wrap(Candidate(
            decision_ts=ts, instrument=instrument, symbol=symbol, side="long",
            entry_price=spot, stop_price=prev_mp.vah * 0.998,
            target_price=spot + (spot - prev_mp.vah) * 2,
            setup_name="va_acceptance",
        ))
    if mp.profile_low < prev_mp.val and spot < prev_mp.val * 0.999:
        return _wrap(Candidate(
            decision_ts=ts, instrument=instrument, symbol=symbol, side="short",
            entry_price=spot, stop_price=prev_mp.val * 1.002,
            target_price=spot - (prev_mp.val - spot) * 2,
            setup_name="va_acceptance",
        ))
    return None


def detect_ib_breakout(
    ts: pd.Timestamp, instrument: str, symbol: str, spot: float,
    mp: MPState, minutes_into_session: int,
) -> Candidate | None:
    if minutes_into_session < 60 or not (mp.ib_high == mp.ib_high and mp.ib_low == mp.ib_low):
        return None
    ib_range = mp.ib_high - mp.ib_low
    if ib_range <= 0:
        return None
    if spot > mp.ib_high + 0.1 * ib_range:
        return _wrap(Candidate(
            decision_ts=ts, instrument=instrument, symbol=symbol, side="long",
            entry_price=spot, stop_price=mp.ib_high - 0.2 * ib_range,
            target_price=spot + ib_range, setup_name="ib_breakout",
        ))
    if spot < mp.ib_low - 0.1 * ib_range:
        return _wrap(Candidate(
            decision_ts=ts, instrument=instrument, symbol=symbol, side="short",
            entry_price=spot, stop_price=mp.ib_low + 0.2 * ib_range,
            target_price=spot - ib_range, setup_name="ib_breakout",
        ))
    return None


def detect_lvn_rejection(
    ts: pd.Timestamp, instrument: str, symbol: str, spot: float,
    mp: MPState, prev_mp: MPState | None, last_n_tick_ret: float,
) -> Candidate | None:
    if prev_mp is None or not prev_mp.lvn_prices:
        return None
    nearest = min(prev_mp.lvn_prices, key=lambda p: abs(p - spot))
    if abs(nearest - spot) / spot * 100 > 0.15:
        return None
    if last_n_tick_ret < -0.0005 and spot > nearest:
        return _wrap(Candidate(
            decision_ts=ts, instrument=instrument, symbol=symbol, side="long",
            entry_price=spot, stop_price=nearest * 0.998,
            target_price=prev_mp.poc if prev_mp.poc > spot else spot * 1.005,
            setup_name="lvn_rejection",
        ))
    if last_n_tick_ret > 0.0005 and spot < nearest:
        return _wrap(Candidate(
            decision_ts=ts, instrument=instrument, symbol=symbol, side="short",
            entry_price=spot, stop_price=nearest * 1.002,
            target_price=prev_mp.poc if prev_mp.poc < spot else spot * 0.995,
            setup_name="lvn_rejection",
        ))
    return None


def detect_poc_magnet(
    ts: pd.Timestamp, instrument: str, symbol: str, spot: float,
    mp: MPState, prev_mp: MPState | None, minutes_into_session: int,
) -> Candidate | None:
    if prev_mp is None or minutes_into_session > 90:
        return None
    if not (prev_mp.val <= spot <= prev_mp.vah):
        return None
    diff_pct = (prev_mp.poc - spot) / spot * 100
    if abs(diff_pct) < 0.10:
        return None
    if diff_pct > 0:
        return _wrap(Candidate(
            decision_ts=ts, instrument=instrument, symbol=symbol, side="long",
            entry_price=spot, stop_price=prev_mp.val * 0.998,
            target_price=prev_mp.poc, setup_name="poc_magnet",
        ))
    return _wrap(Candidate(
        decision_ts=ts, instrument=instrument, symbol=symbol, side="short",
        entry_price=spot, stop_price=prev_mp.vah * 1.002,
        target_price=prev_mp.poc, setup_name="poc_magnet",
    ))


def detect_failed_auction(
    ts: pd.Timestamp, instrument: str, symbol: str, spot: float,
    mp: MPState, prev_mp: MPState | None, intraday_high: float, intraday_low: float,
) -> Candidate | None:
    if prev_mp is None:
        return None
    if intraday_high > prev_mp.profile_high and spot < prev_mp.profile_high:
        return _wrap(Candidate(
            decision_ts=ts, instrument=instrument, symbol=symbol, side="short",
            entry_price=spot, stop_price=intraday_high * 1.002,
            target_price=prev_mp.poc, setup_name="failed_auction",
        ))
    if intraday_low < prev_mp.profile_low and spot > prev_mp.profile_low:
        return _wrap(Candidate(
            decision_ts=ts, instrument=instrument, symbol=symbol, side="long",
            entry_price=spot, stop_price=intraday_low * 0.998,
            target_price=prev_mp.poc, setup_name="failed_auction",
        ))
    return None


DETECTORS = {
    "va_rejection": detect_va_rejection,
    "va_acceptance": detect_va_acceptance,
    "ib_breakout": detect_ib_breakout,
    "lvn_rejection": detect_lvn_rejection,
    "poc_magnet": detect_poc_magnet,
    "failed_auction": detect_failed_auction,
}
