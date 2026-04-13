from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fractal_market_profile.paper import FMPPaperStore
from fractal_market_profile.service import FractalMarketProfileService


def _minute_rows(start: datetime, count: int = 24, *, base: float = 22500.0, drift: float = 2.5) -> list[dict[str, float | str]]:
    rows: list[dict[str, float | str]] = []
    for index in range(count):
        open_price = base + (index * drift)
        close_price = open_price + (1.2 if index % 2 == 0 else 0.6)
        rows.append(
            {
                "time": (start + timedelta(minutes=index)).isoformat(),
                "open": round(open_price, 2),
                "high": round(close_price + 0.8, 2),
                "low": round(open_price - 0.7, 2),
                "close": round(close_price, 2),
                "volume": 1000 + (index * 35),
            }
        )
    return rows


def _profile(
    *,
    hour_number: int,
    shape: str,
    direction_bias: str,
    close_price: float,
    vah: float,
    val: float,
    poc: float,
    ib_high: float,
    ib_low: float,
    score: int,
    step: int = 0,
) -> dict[str, float | int | str | list[float] | bool]:
    return {
        "scope": "hourly",
        "hour_number": hour_number,
        "completed": True,
        "session_date": "2026-04-02",
        "open_price": close_price - 20,
        "high_price": close_price + 28,
        "low_price": close_price - 34,
        "close_price": close_price,
        "poc": poc,
        "vah": vah,
        "val": val,
        "initial_balance_high": ib_high,
        "initial_balance_low": ib_low,
        "initial_balance_range": max(ib_high - ib_low, 1),
        "day_range": 110.0,
        "range_extension_up": 35.0,
        "range_extension_down": 18.0,
        "tick_size": 5.0,
        "single_prints": [],
        "poor_high": False,
        "poor_low": False,
        "shape": shape,
        "direction_bias": direction_bias,
        "tpo_rows": [],
        "sample_count": 20,
        "period_count": 20,
        "value_area_overlap": 0.52,
        "value_migration": 30.0,
        "poc_shift": 18.0,
        "prior_poc_untouched": True,
        "bracket_state": "expanding",
        "value_migration_score": score,
        "value_migration_step": step,
    }


@pytest.mark.asyncio
async def test_live_order_flow_falls_back_to_bar_proxy_when_ticks_are_missing() -> None:
    service = FractalMarketProfileService()
    rows = _minute_rows(datetime(2026, 4, 2, 9, 15, tzinfo=timezone.utc))

    async def _no_history(*args, **kwargs):
        return []

    service._recent_quote_history = _no_history  # type: ignore[method-assign]
    snapshot = await service._build_live_order_flow("NIFTY", rows)

    assert snapshot["source"] == "bar_proxy"
    assert len(snapshot["quote_history"]) == len(rows)
    assert len(snapshot["trade_prints"]) == len(rows)
    assert "timing_confidence" in snapshot


def test_build_signal_detects_bullish_hourly_breakout() -> None:
    service = FractalMarketProfileService()
    current_rows = _minute_rows(datetime(2026, 4, 2, 9, 15, tzinfo=timezone.utc), count=12)
    daily_profile = {
        "shape": "Elongated",
        "direction_bias": "bullish",
        "day_type": "TREND_UP",
        "tick_size": 5.0,
        "initial_balance_range": 70.0,
        "daily_ib_ratio": 1.1,
        "vah": 22590.0,
        "val": 22480.0,
        "poc": 22520.0,
        "single_prints": [22620.0],
        "high_price": 22630.0,
        "low_price": 22450.0,
    }
    prior_daily = {
        "vah": 22510.0,
        "val": 22440.0,
        "high_price": 22580.0,
        "low_price": 22390.0,
        "single_prints": [22560.0],
    }
    hourly_profiles = [
        _profile(
            hour_number=1,
            shape="D-shape",
            direction_bias="bullish",
            close_price=22510.0,
            vah=22525.0,
            val=22470.0,
            poc=22498.0,
            ib_high=22520.0,
            ib_low=22480.0,
            score=0,
            step=0,
        ),
        _profile(
            hour_number=2,
            shape="Elongated",
            direction_bias="bullish",
            close_price=22605.0,
            vah=22595.0,
            val=22530.0,
            poc=22570.0,
            ib_high=22540.0,
            ib_low=22500.0,
            score=2,
            step=1,
        ),
    ]
    order_flow = {
        "delta": 420.0,
        "timing_confidence": 0.72,
        "execution_aggression": "PASSIVE",
    }

    signal = service._build_signal(
        "NIFTY",
        current_rows=current_rows,
        daily_profile=daily_profile,
        prior_daily_profile=prior_daily,
        current_hour_profile=hourly_profiles[-1],
        hourly_profiles=hourly_profiles,
        order_flow=order_flow,
        historical_options=False,
    )

    assert signal["setup_name"] == "hourly_ib_breakout_call"
    assert signal["action"] == "LONG"
    assert signal["actionable"] is True
    assert signal["confidence"] >= 0.64


@pytest.mark.asyncio
async def test_paper_store_tracks_open_and_closed_positions(tmp_path: Path) -> None:
    store = FMPPaperStore(tmp_path)
    actionable_snapshot = {
        "symbol_code": "NIFTY",
        "session": {"session_date": "2026-04-02"},
        "current_signal": {
            "hourly_number": 3,
            "setup_name": "hourly_ib_breakout_call",
            "action": "LONG",
            "confidence": 0.81,
            "horizon": "swing",
            "daily_shape": "Elongated",
            "hourly_shape": "Elongated",
            "entry_trigger": 22540.0,
            "stop_level": 22505.0,
            "target_level": 22620.0,
            "filters": [],
            "rationale": ["Daily and hourly breakouts are aligned."],
            "order_flow_bias": {"delta": 320.0},
            "actionable": True,
            "options": {
                "option_type": "CE",
                "strike": 22550.0,
                "expiry": "2026-04-09",
                "premium": 186.5,
                "trading_symbol": "NIFTY 22550 CE",
                "instrument_key": "nifty-22550-ce",
                "lot_size": 65,
            },
        },
    }

    summary = await store.record_signal(actionable_snapshot)
    assert summary["open_positions"] == 1
    assert summary["closed_positions"] == 0

    flat_snapshot = {
        "symbol_code": "NIFTY",
        "session": {"session_date": "2026-04-02"},
        "current_signal": {
            "setup_name": "no_trade",
            "action": "FLAT",
            "confidence": 0.2,
            "horizon": "none",
            "daily_shape": "D-shape",
            "hourly_shape": "D-shape",
            "entry_trigger": 0.0,
            "stop_level": 0.0,
            "target_level": 0.0,
            "filters": ["Balanced day"],
            "rationale": ["Standing down."],
            "order_flow_bias": {"delta": -5.0},
            "actionable": False,
            "options": None,
        },
    }

    summary = await store.record_signal(flat_snapshot)
    positions = await store.list_positions(symbol="NIFTY", status="all", limit=10)

    assert summary["open_positions"] == 0
    assert summary["closed_positions"] == 1
    assert positions["summary"]["closed_positions"] == 1
    assert positions["closed_positions"][0]["close_reason"] == "flat_snapshot"
