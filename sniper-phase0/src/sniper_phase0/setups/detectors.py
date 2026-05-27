"""Six setup-family detectors.

Each takes the current MPState (intraday + prior-session), spot price, and a
small intraday-context dict, and returns an Optional[Candidate].

Detectors are intentionally conservative — they emit only when the MP rules
are clearly met. The model decides which candidates are worth taking.

Stop/target geometry uses MP levels (POC, VAH, VAL) where possible — never
arbitrary point values. That's the whole point of using MP context.
"""
from __future__ import annotations

import pandas as pd

from sniper_phase0.data.mp_state import MPState
from sniper_phase0.setups.base import Candidate, _valid_barriers


def detect_va_rejection(
    ts: pd.Timestamp,
    instrument: str,
    spot: float,
    mp: MPState,
    prev_mp: MPState | None,
    recent_high: float,
    recent_low: float,
) -> Candidate | None:
    """Setup 1: price tests VAH/VAL, fails to accept outside, rotates toward POC."""
    if prev_mp is None:
        return None
    edge_tolerance_pct = 0.05
    above_vah = spot >= prev_mp.vah * (1 - edge_tolerance_pct / 100) and recent_high >= prev_mp.vah
    below_val = spot <= prev_mp.val * (1 + edge_tolerance_pct / 100) and recent_low <= prev_mp.val

    if above_vah and spot < recent_high:
        # Rejected from VAH — short back to POC.
        c = Candidate(
            decision_ts=ts, instrument=instrument, side="short",
            entry_price=spot,
            stop_price=max(recent_high, prev_mp.vah) * 1.002,
            target_price=prev_mp.poc,
            setup_name="va_rejection",
        )
        return c if _valid_barriers(c.side, c.entry_price, c.stop_price, c.target_price) else None

    if below_val and spot > recent_low:
        c = Candidate(
            decision_ts=ts, instrument=instrument, side="long",
            entry_price=spot,
            stop_price=min(recent_low, prev_mp.val) * 0.998,
            target_price=prev_mp.poc,
            setup_name="va_rejection",
        )
        return c if _valid_barriers(c.side, c.entry_price, c.stop_price, c.target_price) else None
    return None


def detect_va_acceptance(
    ts: pd.Timestamp,
    instrument: str,
    spot: float,
    mp: MPState,
    prev_mp: MPState | None,
    minutes_into_session: int,
) -> Candidate | None:
    """Setup 2: opens outside prev value, retests VAH/VAL, accepts → trade in direction of discovery."""
    if prev_mp is None or minutes_into_session < 30:
        return None
    # Acceptance = price holds outside VA for >30 min after retest.
    if mp.profile_high > prev_mp.vah and spot > prev_mp.vah * 1.001:
        c = Candidate(
            decision_ts=ts, instrument=instrument, side="long",
            entry_price=spot,
            stop_price=prev_mp.vah * 0.998,
            target_price=spot + (spot - prev_mp.vah) * 2,
            setup_name="va_acceptance",
        )
        return c if _valid_barriers(c.side, c.entry_price, c.stop_price, c.target_price) else None

    if mp.profile_low < prev_mp.val and spot < prev_mp.val * 0.999:
        c = Candidate(
            decision_ts=ts, instrument=instrument, side="short",
            entry_price=spot,
            stop_price=prev_mp.val * 1.002,
            target_price=spot - (prev_mp.val - spot) * 2,
            setup_name="va_acceptance",
        )
        return c if _valid_barriers(c.side, c.entry_price, c.stop_price, c.target_price) else None
    return None


def detect_ib_breakout(
    ts: pd.Timestamp,
    instrument: str,
    spot: float,
    mp: MPState,
    minutes_into_session: int,
) -> Candidate | None:
    """Setup 3: after IB completes (60 min), price breaks IB high/low with conviction."""
    if minutes_into_session < 60 or not (mp.ib_high == mp.ib_high and mp.ib_low == mp.ib_low):
        return None
    ib_range = mp.ib_high - mp.ib_low
    if ib_range <= 0:
        return None
    if spot > mp.ib_high + 0.1 * ib_range:
        c = Candidate(
            decision_ts=ts, instrument=instrument, side="long",
            entry_price=spot,
            stop_price=mp.ib_high - 0.2 * ib_range,
            target_price=spot + ib_range,
            setup_name="ib_breakout",
        )
        return c if _valid_barriers(c.side, c.entry_price, c.stop_price, c.target_price) else None
    if spot < mp.ib_low - 0.1 * ib_range:
        c = Candidate(
            decision_ts=ts, instrument=instrument, side="short",
            entry_price=spot,
            stop_price=mp.ib_low + 0.2 * ib_range,
            target_price=spot - ib_range,
            setup_name="ib_breakout",
        )
        return c if _valid_barriers(c.side, c.entry_price, c.stop_price, c.target_price) else None
    return None


def detect_lvn_rejection(
    ts: pd.Timestamp,
    instrument: str,
    spot: float,
    mp: MPState,
    prev_mp: MPState | None,
    last_n_tick_ret: float,
) -> Candidate | None:
    """Setup 4: price enters a prior-session LVN, fails to accept, reverses."""
    if prev_mp is None or not prev_mp.lvn_prices:
        return None
    # Find nearest LVN within 0.15% of spot.
    nearest_lvn = min(prev_mp.lvn_prices, key=lambda p: abs(p - spot))
    if abs(nearest_lvn - spot) / spot * 100 > 0.15:
        return None
    if last_n_tick_ret < -0.0005 and spot > nearest_lvn:
        # entered from above, now reversing back up
        c = Candidate(
            decision_ts=ts, instrument=instrument, side="long",
            entry_price=spot,
            stop_price=nearest_lvn * 0.998,
            target_price=prev_mp.poc if prev_mp.poc > spot else spot * 1.005,
            setup_name="lvn_rejection",
        )
        return c if _valid_barriers(c.side, c.entry_price, c.stop_price, c.target_price) else None
    if last_n_tick_ret > 0.0005 and spot < nearest_lvn:
        c = Candidate(
            decision_ts=ts, instrument=instrument, side="short",
            entry_price=spot,
            stop_price=nearest_lvn * 1.002,
            target_price=prev_mp.poc if prev_mp.poc < spot else spot * 0.995,
            setup_name="lvn_rejection",
        )
        return c if _valid_barriers(c.side, c.entry_price, c.stop_price, c.target_price) else None
    return None


def detect_poc_magnet(
    ts: pd.Timestamp,
    instrument: str,
    spot: float,
    mp: MPState,
    prev_mp: MPState | None,
    minutes_into_session: int,
) -> Candidate | None:
    """Setup 5: opens inside prev value, weak conviction, expect rotation to prior POC."""
    if prev_mp is None or minutes_into_session > 90:
        return None
    if not (prev_mp.val <= spot <= prev_mp.vah):
        return None
    diff_pct = (prev_mp.poc - spot) / spot * 100
    if abs(diff_pct) < 0.10:
        return None  # already at POC
    if diff_pct > 0:
        c = Candidate(
            decision_ts=ts, instrument=instrument, side="long",
            entry_price=spot,
            stop_price=prev_mp.val * 0.998,
            target_price=prev_mp.poc,
            setup_name="poc_magnet",
        )
    else:
        c = Candidate(
            decision_ts=ts, instrument=instrument, side="short",
            entry_price=spot,
            stop_price=prev_mp.vah * 1.002,
            target_price=prev_mp.poc,
            setup_name="poc_magnet",
        )
    return c if _valid_barriers(c.side, c.entry_price, c.stop_price, c.target_price) else None


def detect_failed_auction(
    ts: pd.Timestamp,
    instrument: str,
    spot: float,
    mp: MPState,
    prev_mp: MPState | None,
    intraday_high: float,
    intraday_low: float,
) -> Candidate | None:
    """Setup 6: price takes prior high/low, no follow-through, reverses."""
    if prev_mp is None:
        return None
    # Took prior high but back inside.
    if intraday_high > prev_mp.profile_high and spot < prev_mp.profile_high:
        c = Candidate(
            decision_ts=ts, instrument=instrument, side="short",
            entry_price=spot,
            stop_price=intraday_high * 1.002,
            target_price=prev_mp.poc,
            setup_name="failed_auction",
        )
        return c if _valid_barriers(c.side, c.entry_price, c.stop_price, c.target_price) else None
    if intraday_low < prev_mp.profile_low and spot > prev_mp.profile_low:
        c = Candidate(
            decision_ts=ts, instrument=instrument, side="long",
            entry_price=spot,
            stop_price=intraday_low * 0.998,
            target_price=prev_mp.poc,
            setup_name="failed_auction",
        )
        return c if _valid_barriers(c.side, c.entry_price, c.stop_price, c.target_price) else None
    return None
