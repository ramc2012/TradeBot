from __future__ import annotations

import pytest

from analysis.commodity_atr_exit_sweep import Variant, simulate


def _entry() -> dict:
    return {
        "underlying": "ALUMINI",
        "session_date": "2026-07-01",
        "date": None,
        "entry_time": "2026-07-01T10:00:00+05:30",
        "exit_time": "2026-07-01T12:00:00+05:30",
        "action": "BUY",
        "entry": 100.0,
        "atr": 2.0,
        "original_exit": 110.0,
    }


def test_atr_runner_arms_then_exits_without_a_minimum_hold() -> None:
    result = simulate(
        _entry(),
        [
            {"time": "a", "high": 105.0, "low": 99.0, "close": 104.0},
            {"time": "b", "high": 104.0, "low": 101.0, "close": 101.5},
        ],
        Variant(stop_atr=2.0, arm_atr=2.0, trail_atr=1.5),
    )
    assert result["exit_reason"] == "atr_trail"
    # Trail = peak 105 - 1.5*ATR(2) = 102.
    assert result["net_atr"] == pytest.approx((2.0 - 0.101) / 2.0)


def test_existing_stop_wins_ambiguous_same_bar_before_new_trail() -> None:
    result = simulate(
        _entry(),
        [{"time": "a", "high": 106.0, "low": 95.0, "close": 104.0}],
        Variant(stop_atr=2.0, arm_atr=2.0, trail_atr=1.5),
    )
    assert result["exit_reason"] == "atr_stop"
    assert result["net_atr"] < -2.0  # 5 bps/side is charged after the -2 ATR stop.
