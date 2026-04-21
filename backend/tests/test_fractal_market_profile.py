from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auction_intelligence import live as auction_live
from fractal_market_profile.paper import FMPPaperStore
import fractal_market_profile.service as fmp_service_module
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


@pytest.mark.asyncio
async def test_live_signal_blocks_actionable_entries_when_tick_order_flow_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FractalMarketProfileService()

    async def _fake_option_selection(*_args, **_kwargs):
        return {
            "option_type": "CE",
            "premium": 210.0,
            "strike": 22500.0,
            "expiry": "2026-04-28",
            "pcr_oi": 1.4,
            "oi_change": 12.0,
            "iv_rank": 28.0,
        }

    monkeypatch.setattr(service, "_live_option_selection", _fake_option_selection)
    monkeypatch.setattr(
        fmp_service_module.sector_tracker,
        "_get_india_vix",
        lambda: asyncio.sleep(0, result={"price": 15.0}),
    )

    analysis = {
        "daily_profile": {
            "shape": "Elongated",
            "direction_bias": "bullish",
            "day_type": "TREND_UP",
            "tick_size": 5.0,
            "initial_balance_range": 70.0,
            "daily_ib_ratio": 1.1,
            "vah": 22590.0,
            "val": 22480.0,
            "poc": 22520.0,
            "single_prints": [22570.0],
            "high_price": 22630.0,
            "low_price": 22420.0,
        },
        "prior_daily_profile": {
            "vah": 22490.0,
            "val": 22420.0,
            "high_price": 22510.0,
            "low_price": 22380.0,
            "single_prints": [22470.0],
        },
        "current_hour_profile": _profile(
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
        "hourly_profiles": [
            _profile(
                hour_number=1,
                shape="P-shape",
                direction_bias="bullish",
                close_price=22532.0,
                vah=22528.0,
                val=22488.0,
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
        ],
        "data_status": {
            "execution_ready": False,
            "order_flow_source": "bar_proxy",
            "degraded_reason": "tick_order_flow_unavailable",
        },
    }
    order_flow = {
        "delta": 420.0,
        "timing_confidence": 0.72,
        "execution_aggression": "PASSIVE",
        "source": "bar_proxy",
    }

    signal = await service._build_live_signal("NIFTY", analysis, order_flow)

    assert signal["action"] == "LONG"
    assert signal["actionable"] is False
    assert any("tick/order-flow data is not ready" in item for item in signal["filters"])
    assert any("bar_proxy" in item for item in signal["metadata"]["advisories"])


@pytest.mark.asyncio
async def test_load_live_rows_reuses_shared_broker_history_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FractalMarketProfileService()
    first_session = _minute_rows(datetime(2026, 4, 2, 3, 45, tzinfo=timezone.utc), count=220)
    second_session = _minute_rows(
        datetime(2026, 4, 3, 3, 45, tzinfo=timezone.utc),
        count=220,
        base=22650.0,
    )
    expected_rows = first_session + second_session

    async def _fake_recent_rows(
        symbol_code: str,
        *,
        lookback_days: int = 7,
        allow_live_broker_refresh: bool = True,
    ):
        assert symbol_code == "NIFTY"
        assert lookback_days == 10
        assert allow_live_broker_refresh is True
        return expected_rows, "fyers_spot_index", "NSE:NIFTY50-INDEX"

    monkeypatch.setattr(auction_live, "_fetch_recent_minute_rows", _fake_recent_rows)

    rows, source, history_symbol = await service._load_live_rows("NIFTY")

    assert rows == expected_rows
    assert source == "fyers_spot_index"
    assert history_symbol == "NSE:NIFTY50-INDEX"


@pytest.mark.asyncio
async def test_live_option_selection_uses_symbol_scoped_watchlist(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FractalMarketProfileService()
    captured: dict[str, list[str] | None] = {"symbols": None}

    async def _fake_watchlist(*, expiry=None, symbols=None):
        captured["symbols"] = symbols
        return {
            "rows": [
                {
                    "underlying": "NIFTY",
                    "expiry": "2026-04-28",
                    "ce": {
                        "instrument_key": "NIFTY-CE",
                        "trading_symbol": "NIFTY26APRCE",
                        "ltp": 210.0,
                    },
                    "pe": {
                        "instrument_key": "NIFTY-PE",
                        "trading_symbol": "NIFTY26APRPE",
                        "ltp": 198.0,
                    },
                }
            ]
        }

    monkeypatch.setattr(fmp_service_module.atm_watchlist_service, "get_watchlist", _fake_watchlist)
    monkeypatch.setattr(
        fmp_service_module.option_chain_service,
        "get_cached",
        lambda symbol, expiry: asyncio.sleep(0, result={"pcr_oi": 1.02}),
    )
    monkeypatch.setattr(
        fmp_service_module.sector_tracker,
        "get_iv_rank",
        lambda symbol: asyncio.sleep(0, result={"iv_rank": 32.0}),
    )

    selection = await service._live_option_selection(
        "NIFTY",
        direction="LONG",
        horizon="intraday",
        confidence=0.84,
    )

    assert captured["symbols"] == ["NIFTY"]
    assert selection is not None
    assert selection["option_type"] == "CE"


def test_fmp_group_rows_by_session_keeps_partial_live_session_when_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    fixed_now = datetime(2026, 4, 16, 11, 30, tzinfo=ist)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr(fmp_service_module, "datetime", _FixedDateTime)

    rows = _minute_rows(datetime(2026, 4, 16, 3, 45, tzinfo=timezone.utc), count=130)

    dropped = fmp_service_module._group_rows_by_session(rows)
    kept = fmp_service_module._group_rows_by_session(rows, allow_partial_live_session=True)

    assert datetime(2026, 4, 16).date() not in dropped
    assert datetime(2026, 4, 16).date() in kept
    assert len(kept[datetime(2026, 4, 16).date()]) == 130


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


def test_build_signal_detects_bearish_trend_pullback() -> None:
    service = FractalMarketProfileService()
    current_rows = _minute_rows(datetime(2026, 4, 16, 8, 0, tzinfo=timezone.utc), count=12, base=24195.0, drift=-1.8)
    daily_profile = {
        "shape": "Elongated",
        "direction_bias": "bearish",
        "day_type": "TREND_DN",
        "tick_size": 5.0,
        "initial_balance_range": 90.0,
        "daily_ib_ratio": 1.15,
        "vah": 24260.0,
        "val": 24190.0,
        "poc": 24220.0,
        "single_prints": [24170.0],
        "high_price": 24310.0,
        "low_price": 24120.0,
    }
    prior_daily = {
        "vah": 24140.0,
        "val": 24070.0,
        "high_price": 24180.0,
        "low_price": 24020.0,
        "single_prints": [24090.0],
    }
    hourly_profiles = [
        _profile(
            hour_number=4,
            shape="P-shape",
            direction_bias="bearish",
            close_price=24162.3,
            vah=24205.0,
            val=24145.0,
            poc=24179.76,
            ib_high=24215.0,
            ib_low=24138.0,
            score=-2,
            step=-1,
        )
    ]
    order_flow = {
        "delta": 2329.1667,
        "timing_confidence": 0.4088,
        "execution_aggression": "AGGRESSIVE",
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

    assert signal["setup_name"] == "trend_pullback_put"
    assert signal["action"] == "SHORT"
    assert signal["confidence"] >= 0.58
    assert signal["actionable"] is True


def test_build_signal_detects_balance_extreme_reversion_from_d_shape() -> None:
    service = FractalMarketProfileService()
    current_rows = _minute_rows(datetime(2026, 4, 16, 8, 0, tzinfo=timezone.utc), count=12, base=24205.0, drift=0.4)
    daily_profile = {
        "shape": "D-shape",
        "direction_bias": "bullish",
        "day_type": "NORMAL",
        "tick_size": 5.0,
        "initial_balance_range": 80.0,
        "daily_ib_ratio": 1.0,
        "vah": 24230.0,
        "val": 24170.0,
        "poc": 24200.0,
        "single_prints": [],
        "high_price": 24245.0,
        "low_price": 24150.0,
    }
    prior_daily = {
        "vah": 24220.0,
        "val": 24160.0,
        "high_price": 24255.0,
        "low_price": 24140.0,
        "single_prints": [],
    }
    hourly_profiles = [
        _profile(
            hour_number=3,
            shape="D-shape",
            direction_bias="bullish",
            close_price=24224.0,
            vah=24228.0,
            val=24192.0,
            poc=24208.0,
            ib_high=24216.0,
            ib_low=24188.0,
            score=0,
            step=0,
        ),
    ]
    order_flow = {
        "delta": -220.0,
        "timing_confidence": 0.69,
        "execution_aggression": "AGGRESSIVE",
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

    assert signal["setup_name"] == "daily_balance_extreme_reversion_put"
    assert signal["action"] == "SHORT"
    assert signal["actionable"] is True


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
