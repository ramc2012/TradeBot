"""Timeframe policy (2026-07-15) — SLOW lanes on 30m bars, FAST lanes on 3m.

Locks in:
  * runner cadences: FAST lanes (directional, convergence NSE+commodity,
    auction) scan every 180s aligned to 3-minute bar closes; SLOW lanes
    (S1 market-intelligence feed aside, macd_refined) stay at 1800s.
  * directional_options defaults to 3-minute bars end-to-end
    (config → features → regime), with 5m/15m still selectable.
  * commodity strategy signal bars are 3-minute (MP TPO periods and the
    unified 1-minute store writes unchanged).
  * commodity order flow is tick-first: real market_ticks CVD when the tape
    streams, bar-inference fallback only with the visible `of_source` /
    `of_degraded` flags.

Asserts on `Settings.model_fields[...]` defaults so a stray .env override in
the dev environment cannot flip the policy assertions.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

import paper_engine.commodity_strategy_agent as commodity_module
from core.config import Settings
from directional_options.config import DEFAULT_CONFIG as DIRECTIONAL_DEFAULT_CONFIG
from directional_options.features import (
    TIMEFRAME_TO_PANDAS,
    resample_frame,
    timeframe_minutes,
)
from directional_options.regime import RegimeClassifier
from paper_engine.commodity_mp_signal import (
    evaluate_commodity_mp_signal,
    tick_signed_volume_overrides,
)
from paper_engine.commodity_strategy_agent import (
    FUTURES_TIMEFRAME,
    FUTURES_TIMEFRAME_MINUTES,
    CommodityStrategyAgent,
    _interval_minutes,
)


IST = timezone(timedelta(hours=5, minutes=30))


def _settings_default(name: str) -> Any:
    return Settings.model_fields[name].default


# ─── Runner cadence defaults ─────────────────────────────────────────────────


def test_fast_lane_runner_cadence_is_180s() -> None:
    assert _settings_default("DIRECTIONAL_OPTIONS_AUTO_INTERVAL_SECONDS") == 180
    assert _settings_default("INSTITUTIONAL_CONVERGENCE_AUTO_INTERVAL_SECONDS") == 180
    assert _settings_default("INSTITUTIONAL_CONVERGENCE_COMMODITY_INTERVAL_SECONDS") == 180
    assert _settings_default("AUCTION_INTELLIGENCE_AUTO_INTERVAL_SECONDS") == 180


def test_slow_lane_runner_cadence_unchanged() -> None:
    # macd_refined scans on 30-minute bars — one pass per bar close.
    assert _settings_default("MACD_REFINED_AUTO_INTERVAL_SECONDS") == 1800


# ─── Directional options: 3-minute default, 5m selectable ───────────────────


def test_directional_default_timeframe_is_3minute() -> None:
    assert DIRECTIONAL_DEFAULT_CONFIG["default_timeframe"] == "3minute"
    assert "3minute" in DIRECTIONAL_DEFAULT_CONFIG["timeframes"]
    # 5m/15m stay selectable via the API `timeframe` query param.
    assert "5minute" in DIRECTIONAL_DEFAULT_CONFIG["timeframes"]
    assert "15minute" in DIRECTIONAL_DEFAULT_CONFIG["timeframes"]


def test_directional_features_understand_3minute() -> None:
    assert timeframe_minutes("3minute") == 3
    assert TIMEFRAME_TO_PANDAS["3minute"] == "3min"


def test_directional_resample_produces_3minute_bars() -> None:
    start = datetime(2026, 7, 15, 9, 15, tzinfo=IST)
    frame = pd.DataFrame(
        {
            "time": [start + timedelta(minutes=index) for index in range(30)],
            "open": [100.0 + index for index in range(30)],
            "high": [101.0 + index for index in range(30)],
            "low": [99.0 + index for index in range(30)],
            "close": [100.5 + index for index in range(30)],
            "volume": [10.0] * 30,
            "oi": [0.0] * 30,
        }
    )
    resampled = resample_frame(frame, "3minute")
    # Right-closed/right-labelled buckets: the 09:15 bar lands alone in the
    # first bucket, then ten full 3-minute buckets follow.
    assert len(resampled) == 11
    deltas = resampled["time"].diff().dropna().unique()
    assert all(delta == pd.Timedelta(minutes=3) for delta in deltas)
    # Volume is conserved and interior buckets sum 3 one-minute bars.
    assert resampled["volume"].sum() == pytest.approx(300.0)
    assert resampled["volume"].iloc[1] == pytest.approx(30.0)


def test_directional_regime_treats_3minute_as_fast_tape() -> None:
    row = {
        "adx": 13.0,
        "breakout_up": 0.0,
        "breakout_down": 0.0,
        "rv_percentile": 0.2,
        "range_expansion": 1.0,
        "ema_spread_pct": 0.0005,
        "plus_di": 10.0,
        "minus_di": 10.0,
    }
    classifier = RegimeClassifier()
    fast = classifier.classify(row, timeframe="3minute")
    assert fast.label == "micro_trend"
    assert fast.trade_allowed is True
    # The same tape on a slow timeframe is NOT a fast micro-trend.
    slow = classifier.classify(row, timeframe="15minute")
    assert slow.label == "chop"


# ─── Commodity strategy: 3-minute signal bars ────────────────────────────────


def test_commodity_signal_timeframe_is_3minute() -> None:
    assert FUTURES_TIMEFRAME == "3minute"
    assert FUTURES_TIMEFRAME_MINUTES == 3
    assert _interval_minutes("3minute") == 3


def test_commodity_load_history_3minute_aggregates_broker_minute_rows(
    monkeypatch, tmp_path: Path
) -> None:
    """3minute is aggregate-only: fetch broker 1-minute rows and bucket them —
    never pass "3minute" straight to the broker (it would return mislabelled
    1-minute rows)."""
    config_path = tmp_path / "commodity_strategy.json"
    monkeypatch.setattr(commodity_module, "_COMMODITY_CONFIG_FILE", config_path)

    requests: list[tuple[str, str]] = []

    async def fake_resolve(_symbol: str):
        return {"instrument_key": "MCX_FO|123", "symbol": "MCX:GOLD26AUGFUT"}

    async def fake_fetch(**kwargs):
        requests.append((kwargs["instrument_key"], kwargs["interval"]))
        base = datetime(2026, 7, 15, 9, 0, tzinfo=IST)
        return [
            {
                "time": (base + timedelta(minutes=index)).isoformat(),
                "open": 100.0 + index,
                "high": 101.0 + index,
                "low": 99.0 + index,
                "close": 100.5 + index,
                "volume": 10,
            }
            for index in range(6)
        ]

    monkeypatch.setattr(commodity_module, "resolve_upstox_mcx_future", fake_resolve)
    monkeypatch.setattr(
        commodity_module.option_history_service, "_fetch_broker_candles", fake_fetch
    )

    agent = CommodityStrategyAgent()
    rows = asyncio.run(
        agent._load_history("MCX:GOLD26AUGFUT", interval="3minute", lookback_days=1)
    )

    assert requests == [("MCX_FO|123", "1minute")]
    assert len(rows) == 2  # 6 × 1-minute rows → 2 × 3-minute buckets
    assert rows[0]["volume"] == 30
    assert rows[0]["high"] == pytest.approx(103.0)
    assert rows[0]["close"] == pytest.approx(102.5)


# ─── Commodity order flow: tick-first CVD with visible fallback ─────────────


def _bars_3m(count: int, *, start_hour: int = 9, volume: float = 0.0) -> list[dict[str, Any]]:
    base = datetime(2026, 7, 15, start_hour, 0, tzinfo=IST)
    bars: list[dict[str, Any]] = []
    price = 100.0
    for index in range(count):
        bars.append(
            {
                "time": (base + timedelta(minutes=3 * index)).isoformat(),
                "open": price,
                "high": price + 0.6,
                "low": price - 0.1,
                "close": price + 0.5,
                "volume": volume,
            }
        )
        price += 0.5
    return bars


def _ticks_over(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Buy-side ticks (price at the ask) spanning every bar plus the forming
    bucket after the last closed bar, cumulative session volume."""
    first = datetime.fromisoformat(str(bars[0]["time"]))
    last = datetime.fromisoformat(str(bars[-1]["time"])) + timedelta(minutes=3)
    ticks: list[dict[str, Any]] = []
    cumulative = 0.0
    moment = first
    price = 100.0
    while moment <= last + timedelta(minutes=2):
        cumulative += 10.0
        ticks.append(
            {
                "time": moment,
                "ltp": price,
                "bid": price - 0.1,
                "ask": price,  # ltp >= ask → aggressor-signed BUY
                "bid_qty": 5,
                "ask_qty": 5,
                "volume": cumulative,
            }
        )
        price += 0.05
        moment += timedelta(seconds=45)
    return ticks


def test_tick_signed_volume_overrides_align_to_bars() -> None:
    bars = _bars_3m(12)
    ticks = _ticks_over(bars)
    overrides = tick_signed_volume_overrides(ticks, bars, bar_minutes=3)

    assert len(overrides) == len(bars)
    # First bucket is partial-by-construction and always falls back.
    assert overrides[0] is None
    # Interior bars are tick-covered with buy-signed (positive) deltas.
    covered = [value for value in overrides if value is not None]
    assert len(covered) >= 4
    assert all(value > 0 for value in covered)


def test_commodity_of_prefers_market_ticks_and_flags_fallback() -> None:
    # Zero-volume bars → bar-inference volume coverage is 0% → the R0 gate
    # would normally degrade order flow entirely.
    bars = _bars_3m(40, volume=0.0)
    ticks = _ticks_over(bars)
    overrides = tick_signed_volume_overrides(ticks, bars, bar_minutes=3)

    tick_sourced = evaluate_commodity_mp_signal(
        bars,
        symbol="MCX:GOLD26AUGFUT",
        today_profile=None,
        prior_profile=None,
        cvd_anchor_index=0,
        atr_1m=0.4,
        bar_minutes=3,
        tick_signed_volumes=overrides,
    )
    # today_profile=None returns the base row — the OF-source bookkeeping is
    # still computed and surfaced before any trigger evaluation.
    assert tick_sourced["indicator_timeframe"] == "3minute"

    inferred = evaluate_commodity_mp_signal(
        bars,
        symbol="MCX:GOLD26AUGFUT",
        today_profile=_fake_profile(),
        prior_profile=None,
        cvd_anchor_index=0,
        atr_1m=0.4,
        bar_minutes=3,
    )
    assert inferred["of_source"] == "bar_inference"
    assert inferred["of_degraded"] is True  # sparse bar volume + no real tape

    real = evaluate_commodity_mp_signal(
        bars,
        symbol="MCX:GOLD26AUGFUT",
        today_profile=_fake_profile(),
        prior_profile=None,
        cvd_anchor_index=0,
        atr_1m=0.4,
        bar_minutes=3,
        tick_signed_volumes=overrides,
    )
    assert real["of_source"] == "market_ticks"
    assert real["of_tick_covered_bars"] >= 4
    # Real tick CVD is measured flow, not inference — the bar-volume R0 gate
    # no longer blocks OF-dependent entries.
    assert real["of_degraded"] is False


class _FakeProfile:
    poc = 100.5
    vah = 101.0
    val = 99.5
    initial_balance_high = 101.2
    initial_balance_low = 99.4
    high_price = 101.5
    low_price = 99.3
    close_price = 100.8
    period_count = 6
    poor_high = False
    poor_low = False
    single_prints: list[float] = []
    tpo_counts: dict[float, int] = {}
    tpo_letters: dict[float, str] = {}
    buying_tail: list[float] = []
    selling_tail: list[float] = []
    session_date = "2026-07-15"
    tick_size = 0.1


def _fake_profile() -> _FakeProfile:
    return _FakeProfile()
