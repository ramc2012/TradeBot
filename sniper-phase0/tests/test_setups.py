from __future__ import annotations

import numpy as np
import pandas as pd

from sniper_phase0.data.mp_state import MPState
from sniper_phase0.setups.detectors import (
    detect_failed_auction,
    detect_ib_breakout,
    detect_poc_magnet,
    detect_va_rejection,
)


def _prev_mp(poc=25000.0, vah=25100.0, val=24900.0, phigh=25150.0, plow=24850.0) -> MPState:
    return MPState(
        poc=poc, vah=vah, val=val,
        ib_high=25060.0, ib_low=24930.0,
        profile_high=phigh, profile_low=plow,
        tpo_count=200, data_available_at=pd.Timestamp("2024-04-15 15:30").tz_localize("Asia/Kolkata"),
        hvn_prices=[25000.0, 25050.0],
        lvn_prices=[24950.0],
    )


def _today_mp(ib_high=25100.0, ib_low=25000.0, phigh=25200.0, plow=24950.0) -> MPState:
    return MPState(
        poc=25080.0, vah=25160.0, val=25020.0,
        ib_high=ib_high, ib_low=ib_low,
        profile_high=phigh, profile_low=plow,
        tpo_count=100, data_available_at=pd.Timestamp("2024-04-16 10:30").tz_localize("Asia/Kolkata"),
    )


def test_va_rejection_short_from_vah() -> None:
    ts = pd.Timestamp("2024-04-16 10:30").tz_localize("Asia/Kolkata")
    prev = _prev_mp()
    mp = _today_mp()
    # spot tagged VAH then rejected
    cand = detect_va_rejection(ts, "NIFTY", spot=25095.0, mp=mp, prev_mp=prev,
                               recent_high=25120.0, recent_low=25070.0)
    assert cand is not None
    assert cand.side == "short"
    assert cand.stop_price > cand.entry_price > cand.target_price


def test_ib_breakout_long() -> None:
    ts = pd.Timestamp("2024-04-16 10:30").tz_localize("Asia/Kolkata")
    mp = _today_mp(ib_high=25100.0, ib_low=25000.0)
    cand = detect_ib_breakout(ts, "NIFTY", spot=25120.0, mp=mp, minutes_into_session=75)
    assert cand is not None
    assert cand.side == "long"
    assert cand.stop_price < cand.entry_price < cand.target_price


def test_ib_breakout_too_early_returns_none() -> None:
    ts = pd.Timestamp("2024-04-16 09:30").tz_localize("Asia/Kolkata")
    mp = _today_mp()
    assert detect_ib_breakout(ts, "NIFTY", spot=25200.0, mp=mp, minutes_into_session=15) is None


def test_poc_magnet_long_below_poc() -> None:
    ts = pd.Timestamp("2024-04-16 10:00").tz_localize("Asia/Kolkata")
    prev = _prev_mp(poc=25000.0, vah=25100.0, val=24900.0)
    mp = _today_mp()
    cand = detect_poc_magnet(ts, "NIFTY", spot=24950.0, mp=mp, prev_mp=prev, minutes_into_session=45)
    assert cand is not None
    assert cand.side == "long"


def test_failed_auction_short_after_high_taken() -> None:
    ts = pd.Timestamp("2024-04-16 11:00").tz_localize("Asia/Kolkata")
    prev = _prev_mp(phigh=25150.0)
    mp = _today_mp()
    cand = detect_failed_auction(
        ts, "NIFTY", spot=25120.0, mp=mp, prev_mp=prev,
        intraday_high=25170.0,  # took prior high
        intraday_low=25000.0,
    )
    assert cand is not None
    assert cand.side == "short"
