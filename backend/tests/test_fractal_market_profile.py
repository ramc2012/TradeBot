from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auction_intelligence import live as auction_live
import api.routers.fractal_market_profile as fmp_router_module
from fractal_market_profile.ai_model import FMPHybridTradingModel
from fractal_market_profile.config import SUPPORTED_SYMBOLS
from fractal_market_profile.paper import FMPPaperStore
from fractal_market_profile.policy import FMPPolicy
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


def test_fmp_supports_banknifty_and_symbol_code_alias() -> None:
    assert "BANKNIFTY" in SUPPORTED_SYMBOLS
    assert fmp_router_module._resolve_symbol(None, "banknifty") == "BANKNIFTY"
    assert fmp_router_module._resolve_symbol("NIFTY", "SENSEX") == "SENSEX"


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


def test_fmp_data_status_blocks_post_close_paper_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FractalMarketProfileService()
    fixed_now = datetime(2026, 4, 16, 16, 0, tzinfo=fmp_service_module.IST)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr(fmp_service_module, "datetime", _FixedDateTime)
    rows = [{"time": datetime(2026, 4, 16, 15, 29, tzinfo=fmp_service_module.IST).isoformat()}]

    status = service._build_live_data_status("NIFTY", rows, {"source": "market_ticks"})

    assert status["minute_history_ready"] is True
    assert status["final_session_snapshot"] is True
    assert status["execution_ready"] is False
    assert status["paper_record_ready"] is False
    assert status["degraded_reason"] == "market_closed"


def test_fmp_data_status_blocks_intraday_stale_history(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FractalMarketProfileService()
    fixed_now = datetime(2026, 4, 16, 10, 30, tzinfo=fmp_service_module.IST)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr(fmp_service_module, "datetime", _FixedDateTime)
    rows = [{"time": datetime(2026, 4, 16, 10, 20, tzinfo=fmp_service_module.IST).isoformat()}]

    status = service._build_live_data_status("NIFTY", rows, {"source": "market_ticks"})

    assert status["minute_history_ready"] is False
    assert status["execution_ready"] is False
    assert status["paper_record_ready"] is False
    assert status["degraded_reason"] == "minute_history_stale_or_missing"


@pytest.mark.asyncio
async def test_record_paper_snapshot_skips_post_close_position_management(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FractalMarketProfileService()
    recorded: dict[str, bool] = {"called": False}

    async def _snapshot(symbol_code: str):
        return {
            "symbol_code": symbol_code,
            "data_status": {
                "execution_ready": False,
                "paper_record_ready": False,
                "degraded_reason": "market_closed",
            },
            "current_signal": {"action": "FLAT", "actionable": False},
        }

    async def _record_signal(snapshot):
        recorded["called"] = True
        return {"open_positions": 0, "closed_positions": 1, "realized_pnl": 125.0, "unrealized_pnl": 0.0, "total_pnl": 125.0}

    monkeypatch.setattr(service, "live_snapshot", _snapshot)
    monkeypatch.setattr(service.paper, "record_signal", _record_signal)

    snapshot = await service.record_paper_snapshot("NIFTY")

    assert recorded["called"] is False
    assert snapshot["paper_record_skipped"] is True
    assert snapshot["paper_skip_reason"] == "market_closed"


def test_fmp_crude_keeps_mcx_evening_session_live(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 5, 19, 20, 0, tzinfo=fmp_service_module.IST)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr(fmp_service_module, "datetime", _FixedDateTime)
    rows = _minute_rows(datetime(2026, 5, 19, 9, 0, tzinfo=fmp_service_module.IST), count=660, base=9900.0, drift=0.1)

    sessions = fmp_service_module._group_rows_by_session(rows, allow_partial_live_session=True, symbol_code="CRUDEOIL")
    latest = fmp_service_module._to_ist(sessions[datetime(2026, 5, 19).date()][-1]["time"])
    status = FractalMarketProfileService()._build_live_data_status("CRUDEOIL", sessions[datetime(2026, 5, 19).date()], {"source": "bar_proxy"})

    assert latest.time() > fmp_service_module.SESSION_CLOSE
    assert status["market_open_for_latest_session"] is True
    assert status["futures_proxy_ready"] is True
    assert status["execution_ready"] is True
    assert status["paper_record_ready"] is True


@pytest.mark.asyncio
async def test_live_crude_selection_maps_to_futures_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FractalMarketProfileService()

    async def _fake_resolve(symbol):
        return {
            "instrument_key": "MCX_FO|12345",
            "trading_symbol": "CRUDEOIL26JUNFUT",
            "expiry": "2026-06-19",
            "lot_size": 100,
        }

    async def _fake_quotes(symbols):
        return {"MCX:CRUDEOIL26JUNFUT": {"price": 10025.0, "previous_close": 10000.0, "source": "upstox_full_quote"}}

    monkeypatch.setattr("market_data.upstox_commodity.resolve_upstox_mcx_future", _fake_resolve)
    monkeypatch.setattr("market_data.upstox_commodity.load_upstox_mcx_quote_snapshots", _fake_quotes)

    selection = await service._live_futures_selection(
        "CRUDEOIL",
        {"session": {"last_price": 10020.0}},
        horizon="swing",
    )

    assert selection is not None
    assert selection["instrument_type"] == "FUT"
    assert selection["option_type"] == "FUT"
    assert selection["instrument_key"] == "MCX_FO|12345"
    assert selection["premium"] == 10025.0
    assert selection["lot_size"] == 100


@pytest.mark.asyncio
async def test_paper_store_calculates_short_futures_pnl(tmp_path: Path) -> None:
    store = FMPPaperStore(tmp_path)
    actionable_snapshot = {
        "symbol_code": "CRUDEOIL",
        "session": {"session_date": "2026-05-19", "last_price": 10000.0},
        "current_signal": {
            "hourly_number": 8,
            "setup_name": "trend_pullback_put",
            "action": "SHORT",
            "confidence": 0.7,
            "horizon": "swing",
            "daily_shape": "Elongated",
            "hourly_shape": "Elongated",
            "entry_trigger": 9990.0,
            "stop_level": 10040.0,
            "target_level": 9900.0,
            "filters": [],
            "rationale": ["Crude futures short."],
            "order_flow_bias": {"source": "bar_proxy"},
            "actionable": True,
            "options": {
                "instrument_type": "FUT",
                "option_type": "FUT",
                "strike": 0.0,
                "expiry": "2026-06-19",
                "premium": 10000.0,
                "trading_symbol": "CRUDEOIL26JUNFUT",
                "instrument_key": "MCX_FO|12345",
                "lot_size": 100,
            },
        },
    }
    await store.record_signal(actionable_snapshot)

    # Backdate `opened_at` past the 5-minute minimum-hold guard so the
    # subsequent FLAT snapshot actually closes the position (rather than
    # being treated as same-cycle thrashing noise — see paper.py).
    _backdate_open(store, minutes=10)

    flat_snapshot = {
        "symbol_code": "CRUDEOIL",
        "session": {"session_date": "2026-05-19", "last_price": 9950.0},
        "current_signal": {
            "setup_name": "no_trade",
            "action": "FLAT",
            "confidence": 0.2,
            "horizon": "none",
            "filters": ["Flat"],
            "rationale": ["Exit."],
            "actionable": False,
            "options": None,
        },
    }
    summary = await store.record_signal(flat_snapshot)

    assert summary["realized_pnl"] == 5000.0


@pytest.mark.asyncio
async def test_paper_store_keeps_existing_futures_position_when_entry_cutoff_filter_trips(tmp_path: Path) -> None:
    store = FMPPaperStore(tmp_path)
    base_signal = {
        "hourly_number": 13,
        "setup_name": "trend_pullback_call",
        "action": "LONG",
        "confidence": 0.68,
        "horizon": "swing",
        "daily_shape": "Elongated",
        "hourly_shape": "D-shape",
        "entry_trigger": 10010.0,
        "stop_level": 9980.0,
        "target_level": 10120.0,
        "rationale": ["Crude futures long."],
        "order_flow_bias": {"source": "bar_proxy"},
        "options": {
            "instrument_type": "FUT",
            "option_type": "FUT",
            "strike": 0.0,
            "expiry": "2026-06-18",
            "premium": 10000.0,
            "trading_symbol": "CRUDEOIL26JUNFUT",
            "instrument_key": "MCX_FO|499095",
            "lot_size": 100,
        },
    }
    await store.record_signal(
        {
            "symbol_code": "CRUDEOIL",
            "session": {"session_date": "2026-05-19", "last_price": 10000.0},
            "current_signal": {**base_signal, "filters": [], "actionable": True},
        }
    )

    summary = await store.record_signal(
        {
            "symbol_code": "CRUDEOIL",
            "session": {"session_date": "2026-05-19", "last_price": 10040.0},
            "current_signal": {
                **base_signal,
                "filters": ["New FMP paper entries stop after 22:30 IST."],
                "actionable": False,
                "options": {**base_signal["options"], "premium": 10040.0},
            },
        }
    )

    positions = await store.list_positions(symbol="CRUDEOIL", status="all", limit=10)
    assert summary["open_positions"] == 1
    assert summary["closed_positions"] == 0
    assert positions["open_positions"][0]["latest_premium"] == 10040.0
    assert positions["open_positions"][0]["unrealized_pnl"] == 4000.0


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


def test_fmp_ai_model_scores_aligned_option_packet() -> None:
    model = FMPHybridTradingModel()
    signal = {
        "setup_name": "hourly_ib_breakout_call",
        "action": "LONG",
        "confidence": 0.78,
        "horizon": "swing",
        "entry_trigger": 22540.0,
        "stop_level": 22505.0,
        "target_level": 22620.0,
        "hourly_shape": "Elongated",
        "daily_shape": "Elongated",
        "hourly_number": 3,
        "value_migration_score": 2,
        "daily_context": "TREND_UP",
        "filters": [],
        "options": {
            "option_type": "CE",
            "premium": 186.5,
            "days_to_expiry": 6,
            "oi_change": 125000.0,
            "volume": 450000.0,
            "pcr_oi": 1.42,
            "iv_rank": 28.0,
        },
        "metadata": {
            "daily_direction": "bullish",
            "order_flow_direction": "bullish",
            "order_flow_alignment": 0.72,
            "india_vix": 15.0,
        },
    }
    analysis = {
        "data_status": {
            "execution_ready": True,
            "minute_history_ready": True,
            "order_flow_ready": True,
            "order_flow_source": "market_ticks",
        }
    }
    order_flow = {
        "delta": 900.0,
        "trade_imbalance": 0.24,
        "book_pressure": 0.18,
        "toxicity_score": 0.14,
    }

    evaluation = model.evaluate(signal=signal, analysis=analysis, order_flow=order_flow)

    assert evaluation.allowed is True
    assert evaluation.score >= 60.0
    assert evaluation.setup == "profile_breakout_with_flow"
    assert evaluation.components["profile_alignment"] > 0.6


def test_fmp_ai_model_blocks_broken_option_context() -> None:
    model = FMPHybridTradingModel()
    signal = {
        "setup_name": "hourly_ib_breakout_call",
        "action": "LONG",
        "confidence": 0.78,
        "horizon": "swing",
        "entry_trigger": 22540.0,
        "stop_level": 22505.0,
        "target_level": 22620.0,
        "hourly_shape": "Elongated",
        "daily_shape": "Elongated",
        "hourly_number": 3,
        "value_migration_score": 2,
        "daily_context": "TREND_UP",
        "filters": [],
        "options": {"option_type": "PE", "premium": 0.0, "days_to_expiry": 0},
        "metadata": {"daily_direction": "bullish", "order_flow_direction": "bullish"},
    }

    evaluation = model.evaluate(
        signal=signal,
        analysis={"data_status": {"execution_ready": True}},
        order_flow={"delta": 100.0},
    )

    assert evaluation.allowed is False
    assert "instrument_premium_invalid" in evaluation.blockers
    assert "option_direction_mismatch" in evaluation.blockers


@pytest.mark.asyncio
async def test_live_signal_attaches_fmp_ai_policy_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    service = FractalMarketProfileService()
    service.policy = FMPPolicy(tmp_path / "policy.json", seed=7, config={"warmup_trades": 99})

    async def _fake_option_selection(*_args, **_kwargs):
        return {
            "option_type": "CE",
            "premium": 210.0,
            "strike": 22500.0,
            "expiry": "2026-06-28",
            "pcr_oi": 1.4,
            "oi_change": 125000.0,
            "volume": 320000.0,
            "iv_rank": 28.0,
            "days_to_expiry": 8,
            "lot_size": 65,
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
                shape="D-shape",
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
            "execution_ready": True,
            "minute_history_ready": True,
            "order_flow_ready": True,
            "order_flow_source": "market_ticks",
        },
    }
    order_flow = {
        "delta": 900.0,
        "timing_confidence": 0.72,
        "execution_aggression": "PASSIVE",
        "source": "market_ticks",
        "trade_imbalance": 0.24,
        "book_pressure": 0.18,
        "toxicity_score": 0.14,
    }

    signal = await service._build_live_signal("NIFTY", analysis, order_flow)

    assert signal["actionable"] is True
    assert signal["ai_model"]["allowed"] is True
    assert signal["ai_model"]["score"] >= 60.0
    assert signal["policy"]["act"] is True
    assert signal["policy"]["warmup"] is True


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

    # Backdate so the FLAT follow-up passes the minimum-hold guard.
    _backdate_open(store, minutes=10)
    # Bump the latest_premium too, otherwise the "stalled refresh" guard
    # would still hold the position open at entry premium.
    _bump_latest_premium(store, delta=0.5)

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


@pytest.mark.asyncio
async def test_paper_store_trains_fmp_policy_on_closed_position(tmp_path: Path) -> None:
    policy = FMPPolicy(tmp_path / "policy.json", seed=11)
    store = FMPPaperStore(tmp_path / "paper", policy=policy)
    open_snapshot = _option_open_snapshot(premium=186.5)
    open_snapshot["current_signal"]["ai_model"] = {
        "allowed": True,
        "score": 76.0,
        "setup": "profile_breakout_with_flow",
        "blockers": [],
        "components": {
            "profile_alignment": 0.8,
            "auction_structure": 0.74,
            "order_flow_confirmation": 0.72,
            "instrument_quality": 0.7,
            "volatility_risk": 0.8,
            "execution_timing": 0.9,
            "data_quality": 1.0,
        },
        "features": {"execution_ready": True},
    }
    open_snapshot["current_signal"]["policy"] = {
        "act": True,
        "sampled_value": 0.2,
        "posterior_mean": 0.0,
        "warmup": True,
    }

    await store.record_signal(open_snapshot)
    _backdate_open(store, minutes=10)
    _bump_latest_premium(store, delta=0.5)
    await store.record_signal(_flat_followup_snapshot())

    positions = await store.list_positions(symbol="NIFTY", status="closed", limit=10)
    assert policy.snapshot()["n_seen"] == 1
    assert positions["closed_positions"][0]["policy_reward_r"] > 0.0
    assert positions["closed_positions"][0]["ai_rule_score"] == 76.0


# ── Auto-exit + sticky-level tests for the upgraded FMP paper engine ────────


def _backdate_open(store: FMPPaperStore, *, minutes: int) -> None:
    """Rewind every open position's `opened_at` by N minutes.

    The paper engine's FLAT-close path now refuses to close positions
    held less than 5 minutes (anti-thrash guard). Tests that want to
    exercise the close path without sleeping use this helper to make
    the position "old enough" for the close to fire.
    """
    state = store._load_positions()
    delta = timedelta(minutes=minutes)
    for row in state.get("open_positions", []):
        opened = row.get("opened_at")
        if not opened:
            continue
        try:
            ts = datetime.fromisoformat(str(opened).replace("Z", "+00:00")) - delta
            row["opened_at"] = ts.isoformat()
        except ValueError:
            continue
    store._save_positions(state)


def _bump_latest_premium(store: FMPPaperStore, *, delta: float) -> None:
    """Nudge `latest_premium` away from `entry_premium` on every open
    position so the "stalled-refresh" guard lets the close fire."""
    state = store._load_positions()
    for row in state.get("open_positions", []):
        latest = row.get("latest_premium")
        try:
            row["latest_premium"] = float(latest) + delta
        except (TypeError, ValueError):
            continue
    store._save_positions(state)


def _option_open_snapshot(
    *,
    underlying: str = "NIFTY",
    session_date: str = "2026-04-02",
    last_price: float | None = None,
    action: str = "LONG",
    stop_level: float = 80.0,
    target_level: float = 280.0,
    premium: float = 186.5,
    instrument_key: str = "nifty-22550-ce",
) -> dict:
    session: dict = {"session_date": session_date}
    if last_price is not None:
        session["last_price"] = last_price
    return {
        "symbol_code": underlying,
        "session": session,
        "current_signal": {
            "hourly_number": 3,
            "setup_name": "hourly_ib_breakout_call",
            "action": action,
            "confidence": 0.8,
            "horizon": "swing",
            "daily_shape": "Elongated",
            "hourly_shape": "Elongated",
            "entry_trigger": 22540.0,
            # Premium-based risk levels by default (small relative to premium).
            "stop_level": stop_level,
            "target_level": target_level,
            "filters": [],
            "rationale": ["Aligned breakout."],
            "order_flow_bias": {"delta": 100.0},
            "actionable": True,
            "options": {
                "option_type": "CE",
                "strike": 22550.0,
                "expiry": "2026-04-09",
                "premium": premium,
                "trading_symbol": "NIFTY 22550 CE",
                "instrument_key": instrument_key,
                "lot_size": 65,
            },
        },
    }


def _flat_followup_snapshot(
    *,
    session_date: str = "2026-04-02",
    last_price: float = 22550.0,
    underlying: str = "NIFTY",
) -> dict:
    return {
        "symbol_code": underlying,
        "session": {"session_date": session_date, "last_price": last_price},
        "current_signal": {
            "setup_name": "no_trade",
            "action": "FLAT",
            "confidence": 0.2,
            "filters": [],
            "rationale": [],
            "actionable": False,
            "options": None,
        },
    }


@pytest.mark.asyncio
async def test_auto_exit_stop_loss_on_premium_based_stop(tmp_path: Path) -> None:
    """Premium-based stop (≤ 5× entry premium) triggers a stop_loss exit
    on the next snapshot when the option premium drops to or below stop."""
    store = FMPPaperStore(tmp_path)
    # Open: entry 186.5, stop 80, target 280 — all premium-scaled.
    await store.record_signal(
        _option_open_snapshot(stop_level=80.0, target_level=280.0, premium=186.5)
    )

    # Next snapshot: option premium has collapsed to 50 (below the 80 stop).
    # The second open call overlays the new premium via record_signal's
    # refresh path; the auto-exit check then runs before any further logic.
    stop_hit = _option_open_snapshot(stop_level=80.0, target_level=280.0, premium=50.0)
    summary = await store.record_signal(stop_hit)
    positions = await store.list_positions(symbol="NIFTY", status="all", limit=10)

    assert summary["open_positions"] == 0
    assert summary["closed_positions"] == 1
    closed = positions["closed_positions"][0]
    assert closed["close_reason"] == "stop_loss"
    # PnL is (exit - entry) * qty for a LONG; exit_premium gets stamped
    # from latest_premium after refresh, which the second snapshot supplies.
    assert closed["exit_premium"] == 50.0
    assert closed["realized_pnl"] == pytest.approx((50.0 - 186.5) * 65, rel=1e-6)


@pytest.mark.asyncio
async def test_auto_exit_target_hit_on_premium_based_target(tmp_path: Path) -> None:
    store = FMPPaperStore(tmp_path)
    await store.record_signal(
        _option_open_snapshot(stop_level=80.0, target_level=280.0, premium=186.5)
    )
    target_hit = _option_open_snapshot(stop_level=80.0, target_level=280.0, premium=300.0)
    summary = await store.record_signal(target_hit)
    positions = await store.list_positions(symbol="NIFTY", status="all", limit=10)
    assert summary["open_positions"] == 0
    assert summary["closed_positions"] == 1
    assert positions["closed_positions"][0]["close_reason"] == "target_hit"


@pytest.mark.asyncio
async def test_spot_based_stops_do_not_falsely_trigger(tmp_path: Path) -> None:
    """The legacy FMP signals carry spot-level stops (e.g. 22505 for a
    ₹186 NIFTY option). Those must NOT trigger a stop-loss exit just
    because the spot stop value happens to be larger than the option
    premium."""
    store = FMPPaperStore(tmp_path)
    await store.record_signal(
        _option_open_snapshot(stop_level=22505.0, target_level=22620.0, premium=186.5)
    )
    # Past min-hold + premium moved → FLAT will close cleanly.
    _backdate_open(store, minutes=10)
    _bump_latest_premium(store, delta=0.5)
    # FLAT follow-up — should close as flat_snapshot, not stop_loss, even
    # though premium (186.5) is well below the spot stop (22505).
    summary = await store.record_signal(_flat_followup_snapshot())
    positions = await store.list_positions(symbol="NIFTY", status="all", limit=10)
    assert summary["closed_positions"] == 1
    assert positions["closed_positions"][0]["close_reason"] == "flat_snapshot"


@pytest.mark.asyncio
async def test_auto_exit_expired_contract(tmp_path: Path) -> None:
    """A position whose expiry is before the current session date should
    be force-closed with `expired_contract` even on a stale signal."""
    store = FMPPaperStore(tmp_path)
    snapshot = _option_open_snapshot(session_date="2026-04-08", premium=186.5)
    await store.record_signal(snapshot)

    # Next session is *after* expiry 2026-04-09. Send a benign refresh.
    expired_snapshot = _option_open_snapshot(session_date="2026-04-10", premium=186.5)
    summary = await store.record_signal(expired_snapshot)
    positions = await store.list_positions(symbol="NIFTY", status="all", limit=10)
    assert summary["closed_positions"] == 1
    assert positions["closed_positions"][0]["close_reason"] == "expired_contract"


@pytest.mark.asyncio
async def test_auto_exit_premium_zero_for_options(tmp_path: Path) -> None:
    """An option whose premium hits zero is force-closed (zero premium
    means the contract is effectively worthless)."""
    store = FMPPaperStore(tmp_path)
    await store.record_signal(_option_open_snapshot(premium=186.5))
    summary = await store.record_signal(_option_open_snapshot(premium=0.0))
    positions = await store.list_positions(symbol="NIFTY", status="all", limit=10)
    assert summary["closed_positions"] == 1
    assert positions["closed_positions"][0]["close_reason"] == "premium_zero"


@pytest.mark.asyncio
async def test_sticky_stop_target_levels_when_signal_drops_them(tmp_path: Path) -> None:
    """If a refresh snapshot doesn't carry stop/target (or carries zeros),
    the existing levels should be retained — wiping them silently loses
    the original risk plan and was the bug that prompted this rewrite."""
    store = FMPPaperStore(tmp_path)
    await store.record_signal(
        _option_open_snapshot(stop_level=80.0, target_level=280.0, premium=186.5)
    )
    # Same-direction refresh with no fresh risk levels.
    refresh = _option_open_snapshot(stop_level=0.0, target_level=0.0, premium=190.0)
    await store.record_signal(refresh)
    positions = await store.list_positions(symbol="NIFTY", status="open", limit=10)
    assert positions["open_positions"], "position should still be open"
    open_row = positions["open_positions"][0]
    assert open_row["stop_level"] == 80.0
    assert open_row["target_level"] == 280.0
    # New premium did get refreshed though.
    assert open_row["latest_premium"] == 190.0


@pytest.mark.asyncio
async def test_dedupe_removes_duplicate_matching_positions(tmp_path: Path) -> None:
    """If the on-disk state has duplicate same-contract / same-action
    positions, the next refresh should keep one and close the rest with
    `dedupe_repair`."""
    store = FMPPaperStore(tmp_path)
    # Open one normally, then plant a duplicate directly on disk to
    # simulate the corrupt state we're guarding against.
    await store.record_signal(_option_open_snapshot(premium=186.5))
    state = store._load_positions()
    duplicate = dict(state["open_positions"][0])
    duplicate["position_id"] = "dupe"
    state["open_positions"].append(duplicate)
    store._save_positions(state)

    # Same-direction refresh — should dedupe.
    await store.record_signal(_option_open_snapshot(premium=190.0))
    positions = await store.list_positions(symbol="NIFTY", status="all", limit=10)
    assert len(positions["open_positions"]) == 1
    closed = positions["closed_positions"]
    assert any(row.get("close_reason") == "dedupe_repair" for row in closed)


@pytest.mark.asyncio
async def test_signal_flip_closes_existing_and_opens_new(tmp_path: Path) -> None:
    store = FMPPaperStore(tmp_path)
    await store.record_signal(
        _option_open_snapshot(action="LONG", premium=186.5)
    )
    # Flip to SHORT — different action, different contract entirely.
    flipped = _option_open_snapshot(action="SHORT", premium=190.0)
    flipped["current_signal"]["options"]["option_type"] = "PE"
    flipped["current_signal"]["options"]["instrument_key"] = "nifty-22550-pe"
    flipped["current_signal"]["options"]["trading_symbol"] = "NIFTY 22550 PE"
    await store.record_signal(flipped)
    positions = await store.list_positions(symbol="NIFTY", status="all", limit=10)
    assert len(positions["open_positions"]) == 1
    assert positions["open_positions"][0]["action"] == "SHORT"
    assert any(row.get("close_reason") == "signal_flip" for row in positions["closed_positions"])


@pytest.mark.asyncio
async def test_flat_close_blocked_within_min_hold(tmp_path: Path) -> None:
    """A FLAT snapshot arriving within 5 minutes of the open should leave
    the position alone — the 4 zero-PnL trades we observed in production
    were all this pattern (open immediately followed by FLAT at unchanged
    premium)."""
    store = FMPPaperStore(tmp_path)
    await store.record_signal(_option_open_snapshot(premium=186.5))
    # No backdate — opened_at is essentially "now". FLAT should be ignored.
    summary = await store.record_signal(_flat_followup_snapshot())
    assert summary["open_positions"] == 1, "FLAT during min-hold should not close"
    assert summary["closed_positions"] == 0


@pytest.mark.asyncio
async def test_flat_close_blocked_when_premium_refresh_stalls(tmp_path: Path) -> None:
    """Even after the min-hold window expires, refuse to close at exactly
    the entry premium — that's the "premium refresh stalled" signature."""
    store = FMPPaperStore(tmp_path)
    await store.record_signal(_option_open_snapshot(premium=186.5))
    # Past min-hold but premium has not moved — refresh likely stalled.
    _backdate_open(store, minutes=10)
    summary = await store.record_signal(_flat_followup_snapshot())
    assert summary["open_positions"] == 1, "stalled refresh should not lock in fake PnL"
    assert summary["closed_positions"] == 0
