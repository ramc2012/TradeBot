from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

import paper_engine.commodity_mp_signal as mp_signal
import paper_engine.commodity_strategy_agent as strategy_module
from paper_engine.commodity_strategy_agent import CommodityStrategyAgent


@pytest.fixture(autouse=True)
def _isolated_strategy_state(monkeypatch):
    monkeypatch.setattr(
        strategy_module,
        "_load_saved_state",
        lambda: (strategy_module._default_saved_state(), None),
    )
    monkeypatch.setattr(strategy_module, "_save_state", lambda state: None)
    # Tests below cover the pre-existing textbook mechanics independently. The
    # high-conviction admission gate has focused tests that explicitly enable it.
    monkeypatch.setattr(
        strategy_module.settings,
        "COMMODITY_HIGH_CONVICTION_SETUP_ENABLED",
        False,
    )


@dataclass
class _Profile:
    poc: float
    vah: float
    val: float
    initial_balance_high: float
    initial_balance_low: float
    high_price: float
    low_price: float
    close_price: float
    period_count: int = 8
    poor_high: bool = False
    poor_low: bool = False


def _bar(close: float, *, high: float | None = None, low: float | None = None) -> dict:
    return {
        "time": "2026-07-02T12:00:00+05:30",
        "open": close,
        "high": high if high is not None else close + 0.1,
        "low": low if low is not None else close - 0.1,
        "close": close,
        "volume": 100,
    }


def test_causal_15m_atr_excludes_the_forming_bucket() -> None:
    start = datetime.fromisoformat("2026-07-02T09:00:00+05:30")
    candles = []
    for index in range(14):
        close = 100.0 + index
        candles.append(
            {
                "time": (start + timedelta(minutes=index * 15 + 14)).isoformat(),
                "open": close - 0.5,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 100,
            }
        )
    # An extreme move in the current 15-minute bucket must not leak into ATR.
    candles.append(
        {
            "time": (start + timedelta(minutes=14 * 15 + 5)).isoformat(),
            "open": 113.0,
            "high": 1_000.0,
            "low": 1.0,
            "close": 500.0,
            "volume": 100,
        }
    )

    completed = strategy_module._completed_period_rows(candles)
    atr = strategy_module._completed_15m_atr(candles)

    assert len(completed) == 14
    assert atr == pytest.approx(2.0)


def test_high_conviction_gate_checks_atr_geometry_without_widening_stop(monkeypatch) -> None:
    monkeypatch.setattr(
        strategy_module.settings,
        "COMMODITY_HIGH_CONVICTION_SETUP_ENABLED",
        True,
    )
    base = {
        "signal": "BUY",
        "entry_style": "ib_break",
        "mp_day_type": "trend_up",
        "price": 100.0,
        "mp_poc": 98.0,
        "atr": 0.1,
        "atr_15m": 1.0,
        "stop_hint": 96.0,
    }

    ready = strategy_module._high_conviction_entry_verdict(base)
    late = strategy_module._high_conviction_entry_verdict({**base, "mp_poc": 96.0})
    tight = strategy_module._high_conviction_entry_verdict({**base, "stop_hint": 99.8})

    assert ready["allowed"] is True
    assert ready["code"] == "high_conviction_ready"
    assert ready["poc_distance_atr_15m"] == pytest.approx(2.0)
    assert ready["planned_stop_distance_atr_15m"] == pytest.approx(4.0)
    assert late["code"] == "high_conviction_late_extension"
    assert tight["code"] == "high_conviction_insufficient_invalidation_room"
    assert tight["planned_stop_price"] == pytest.approx(99.5)
    assert tight["planned_stop_distance_atr_15m"] == pytest.approx(0.5)


def test_high_conviction_gate_is_enforced_again_at_order_creation(monkeypatch) -> None:
    monkeypatch.setattr(
        strategy_module.settings,
        "COMMODITY_HIGH_CONVICTION_SETUP_ENABLED",
        True,
    )
    monkeypatch.setattr(strategy_module.settings, "COMMODITY_HTF_GATE_ENABLED", False)
    monkeypatch.setattr(strategy_module.settings, "COMMODITY_NAKED_POC_TARGET_ENABLED", False)

    async def _noop_audit(**kwargs):
        return None

    monkeypatch.setattr(strategy_module, "record_audit_event", _noop_audit)
    agent = CommodityStrategyAgent()
    eligible = {
        "symbol": "MCX:GOLD26AUGFUT",
        "underlying": "GOLD",
        "signal": "BUY",
        "signal_validation": "ready",
        "price": 100.0,
        "atr": 0.1,
        "atr_15m": 1.0,
        "mp_poc": 98.0,
        "bar_time": "2026-07-02T12:00:00+05:30",
        "reason": "ib_break_up",
        "entry_style": "ib_break",
        "mp_day_type": "trend_up",
        "stop_hint": 96.0,
    }
    ineligible = {
        **eligible,
        "symbol": "MCX:SILVERM26AUGFUT",
        "underlying": "SILVERM",
        "entry_style": "open_drive",
        "reason": "open_drive_up",
    }

    asyncio.run(agent._open_new_futures_positions([eligible, ineligible]))

    position = agent._runtime.positions["commodity_futures:MCX:GOLD26AUGFUT"]
    assert position.stop_price == pytest.approx(96.0)
    assert eligible["high_conviction_validation"] == "high_conviction_ready"
    assert ineligible["signal_validation"] == "high_conviction_ib_break_only"
    assert "commodity_futures:MCX:SILVERM26AUGFUT" not in agent._runtime.positions


def test_failed_auction_requires_reentry_through_value_edge(monkeypatch) -> None:
    profile = _Profile(
        poc=100,
        vah=101,
        val=99,
        initial_balance_high=103,
        initial_balance_low=97,
        high_price=102,
        low_price=98,
        close_price=100.5,
        poor_high=True,
    )
    monkeypatch.setattr(
        mp_signal,
        "cvd_divergence",
        lambda *args, **kwargs: SimpleNamespace(kind="bearish", strength=0.8),
    )

    # A poor high by itself is unfinished business, not a rejected auction.
    assert mp_signal._trigger_failed_auction(
        today_profile=profile,
        closed_1m=[_bar(100.7), _bar(100.5)],
        cvd_total=[10, 5],
        atr_1m=0.2,
    ) is None

    result = mp_signal._trigger_failed_auction(
        today_profile=profile,
        closed_1m=[_bar(101.4, high=102.0), _bar(100.5, high=101.5)],
        cvd_total=[10, 5],
        atr_1m=0.2,
    )

    assert result is not None
    assert result.signal == "SELL"
    assert result.reason == "failed_auction_high"
    assert result.target_hint == pytest.approx(100.0)
    assert result.stop_hint > 102.0


def test_va_migration_requires_acceptance_and_fresh_flow() -> None:
    prior = _Profile(98, 100, 96, 99, 97, 100, 96, 98.5, period_count=24)
    today = _Profile(105, 107, 103, 108, 102, 108, 102, 106, period_count=8)

    # Developing value shifted up, but price has not accepted above prior VAH.
    assert mp_signal._trigger_va_migration(
        today_profile=today,
        prior_profile=prior,
        closed_1m=[_bar(99.8), _bar(106.0)],
        cvd_anchored=[0, 50],
        vwap_last=104.0,
    ) is None

    result = mp_signal._trigger_va_migration(
        today_profile=today,
        prior_profile=prior,
        closed_1m=[_bar(105.5), _bar(106.0)],
        cvd_anchored=[0, 25, 80],
        vwap_last=104.0,
    )

    assert result is not None
    assert result.signal == "BUY"
    assert result.reason == "va_migration_up"
    assert result.evidence["cvd_delta_15m"] == pytest.approx(80.0)


def test_orderflow_quality_blocks_structure_only_entries(monkeypatch) -> None:
    prior = _Profile(99, 100, 98, 100, 98, 100, 98, 99, period_count=24)
    today = _Profile(105, 106, 104, 107, 104.5, 107, 104, 106.5, period_count=4)
    bars = [_bar(106.0 + index * 0.02) for index in range(60)]
    for bar in bars:
        bar["volume"] = 0

    result = mp_signal.evaluate_commodity_mp_signal(
        bars,
        symbol="MCX:GOLD26AUGFUT",
        today_profile=today,
        prior_profile=prior,
        cvd_anchor_index=0,
        atr_1m=0.2,
    )

    assert result["signal"] is None
    assert result["reason"] == "of_quality_block"
    assert result["of_volume_coverage"] == 0.0


def test_adaptive_pressure_filters_unconfirmed_initiative_trade(monkeypatch) -> None:
    prior = _Profile(99, 100, 98, 100, 98, 100, 98, 99, period_count=24)
    today = _Profile(105, 106, 104, 107, 104.5, 107, 104, 106.5, period_count=4)
    bars = []
    for index in range(60):
        close = 104.8 + index * 0.04
        bar = _bar(close, high=close + 0.1, low=close - 0.3)
        bar["open"] = close - 0.2
        bars.append(bar)
    baseline = SimpleNamespace(ready=True, median=100.0, p90=200.0, p95=250.0)
    monkeypatch.setattr(mp_signal, "_vb_load_baseline", lambda root: baseline)
    monkeypatch.setattr(mp_signal, "_vb_pressure_ratio", lambda signed, base: 0.1)

    result = mp_signal.evaluate_commodity_mp_signal(
        bars,
        symbol="MCX:GOLD26AUGFUT",
        today_profile=today,
        prior_profile=prior,
        cvd_anchor_index=0,
        atr_1m=0.2,
    )

    assert result["signal"] is None
    assert result["reason"] == "context_filter"
    assert "adaptive OF pressure" in result["signal_validation_detail"]


def test_stopped_setup_is_locked_for_session_and_resets_next_day(monkeypatch) -> None:
    now = datetime.fromisoformat("2026-07-02T14:00:00+05:30")
    monkeypatch.setattr(strategy_module, "_now_ist", lambda: now)
    agent = CommodityStrategyAgent()
    key = agent._setup_stop_key("CRUDEOIL", "va_migration_down", "SELL", now.date())
    agent._runtime.stopped_setups[key] = now

    reason = agent._setup_stop_lock_reason("CRUDEOIL", "va_migration_down", "SELL")
    assert reason is not None
    assert "already stopped this session" in reason

    tomorrow = datetime.fromisoformat("2026-07-03T09:01:00+05:30")
    assert agent._setup_stop_lock_reason(
        "CRUDEOIL", "va_migration_down", "SELL", tomorrow
    ) is None


def test_runtime_normalization_preserves_cooldowns_and_setup_locks() -> None:
    stamp = "2026-07-02T14:00:00+05:30"
    setup_key = "2026-07-02|CRUDEOIL|SELL|va_migration_down"
    state = strategy_module._normalize_saved_state(
        {
            "runtime": {
                "last_exit_at": {"CRUDEOIL": stamp},
                "last_stop_at": {"CRUDEOIL": stamp},
                "stopped_setups": {setup_key: stamp},
            }
        }
    )
    agent = CommodityStrategyAgent()
    agent._restore_runtime_state(state["runtime"])

    assert agent._runtime.last_exit_at["CRUDEOIL"].isoformat() == stamp
    assert agent._runtime.last_stop_at["CRUDEOIL"].isoformat() == stamp
    assert agent._runtime.stopped_setups[setup_key].isoformat() == stamp


def test_entry_uses_structural_stop_target_and_risk_sizing(monkeypatch) -> None:
    monkeypatch.setattr(strategy_module.settings, "COMMODITY_HTF_GATE_ENABLED", False)
    monkeypatch.setattr(strategy_module.settings, "COMMODITY_NAKED_POC_TARGET_ENABLED", False)
    monkeypatch.setattr(strategy_module.settings, "COMMODITY_SCALP_MAX_TRADE_SHARE", 1.0)

    async def _noop_audit(**kwargs):
        return None

    monkeypatch.setattr(strategy_module, "record_audit_event", _noop_audit)
    agent = CommodityStrategyAgent()
    row = {
        "symbol": "MCX:GOLD26AUGFUT",
        "underlying": "GOLD",
        "signal": "BUY",
        "signal_validation": "ready",
        "price": 100.0,
        "atr": 0.1,
        "bar_time": "2026-07-02T12:00:00+05:30",
        "reason": "failed_auction_low",
        "entry_style": "failed_auction",
        "mp_day_type": "balance",
        "stop_hint": 90.0,
        "target_hint": 120.0,
    }

    asyncio.run(agent._open_new_futures_positions([row]))

    position = agent._runtime.positions["commodity_futures:MCX:GOLD26AUGFUT"]
    assert position.stop_price == pytest.approx(90.0)
    assert position.target_price == pytest.approx(120.0)
    assert row["planned_risk_rupees"] <= 12_500.0
    assert position.lots == row["risk_sized_lots"]
    assert position.trade_horizon == "scalp"
    assert agent._runtime.orders[0]["trade_horizon"] == "scalp"


def test_stale_value_migration_row_cannot_open_entry(monkeypatch) -> None:
    monkeypatch.setattr(strategy_module.settings, "COMMODITY_HTF_GATE_ENABLED", False)
    agent = CommodityStrategyAgent()
    row = {
        "symbol": "MCX:COPPER26JULFUT",
        "underlying": "COPPER",
        "signal": "BUY",
        "signal_validation": "ready",
        "price": 1000.0,
        "atr": 2.0,
        "bar_time": "2026-07-02T12:00:00+05:30",
        "reason": "va_migration_up",
        "entry_style": "va_migration",
        "mp_day_type": "trend_up",
        "stop_hint": 900.0,
    }

    asyncio.run(agent._open_new_futures_positions([row]))

    assert not agent._runtime.positions
    assert row["signal_validation"] == "context_only"
    assert "cannot open" in row["signal_validation_detail"]


def test_responsive_scalps_are_capped_at_twenty_percent(monkeypatch) -> None:
    monkeypatch.setattr(strategy_module.settings, "COMMODITY_HTF_GATE_ENABLED", False)
    monkeypatch.setattr(strategy_module.settings, "COMMODITY_NAKED_POC_TARGET_ENABLED", False)
    monkeypatch.setattr(strategy_module.settings, "COMMODITY_SCALP_MAX_TRADE_SHARE", 0.20)
    monkeypatch.setattr(strategy_module.settings, "COMMODITY_SCALP_MIX_LOOKBACK", 20)
    agent = CommodityStrategyAgent()
    agent._runtime.orders = [
        {
            "time": f"2026-07-02T09:0{index}:00+05:30",
            "flow": "entry",
            "reason": "ib_break_up",
            "trade_horizon": "positional",
        }
        for index in range(3)
    ]
    row = {
        "symbol": "MCX:GOLD26AUGFUT",
        "underlying": "GOLD",
        "signal": "BUY",
        "signal_validation": "ready",
        "price": 100.0,
        "atr": 0.1,
        "bar_time": "2026-07-02T12:00:00+05:30",
        "reason": "failed_auction_low",
        "entry_style": "failed_auction",
        "mp_day_type": "balance",
        "stop_hint": 90.0,
        "target_hint": 120.0,
    }

    asyncio.run(agent._open_new_futures_positions([row]))

    assert not agent._runtime.positions
    assert row["signal_validation"] == "scalp_mix_cap"
    assert "25%" in row["signal_validation_detail"]

    agent._runtime.orders.append(
        {
            "time": "2026-07-02T09:04:00+05:30",
            "flow": "entry",
            "reason": "open_drive_up",
            "trade_horizon": "positional",
        }
    )
    row["signal_validation"] = "ready"
    asyncio.run(agent._open_new_futures_positions([row]))

    position = agent._runtime.positions["commodity_futures:MCX:GOLD26AUGFUT"]
    assert position.trade_horizon == "scalp"
    assert agent._runtime.orders[0]["trade_horizon"] == "scalp"
    mix = agent._scalp_mix_snapshot()
    assert mix["current_share"] == pytest.approx(0.20)


def _history_bar(day: str, hour: int = 15) -> dict:
    return {
        "time": f"{day}T{hour:02d}:00:00+05:30",
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.0,
        "volume": 100,
    }


def test_prior_profile_preopen_uses_latest_completed_session(monkeypatch) -> None:
    monkeypatch.setattr(
        strategy_module,
        "_now_ist",
        lambda: datetime.fromisoformat("2026-07-02T08:00:00+05:30"),
    )
    agent = CommodityStrategyAgent()

    async def _history(*args, **kwargs):
        return [_history_bar("2026-06-30"), _history_bar("2026-07-01")]

    monkeypatch.setattr(agent, "_load_history", _history)
    monkeypatch.setattr(
        agent,
        "_build_market_profile",
        lambda symbol, rows: SimpleNamespace(
            session_date=strategy_module._parse_iso_timestamp(rows[-1]["time"]).date()
        ),
    )

    profile = asyncio.run(agent._load_prior_session_profile("MCX:GOLD26AUGFUT"))

    assert profile.session_date == date(2026, 7, 1)


def test_prior_profile_intraday_excludes_current_session(monkeypatch) -> None:
    monkeypatch.setattr(
        strategy_module,
        "_now_ist",
        lambda: datetime.fromisoformat("2026-07-02T12:00:00+05:30"),
    )
    agent = CommodityStrategyAgent()

    async def _history(*args, **kwargs):
        return [_history_bar("2026-07-01"), _history_bar("2026-07-02", 10)]

    monkeypatch.setattr(agent, "_load_history", _history)
    monkeypatch.setattr(
        agent,
        "_build_market_profile",
        lambda symbol, rows: SimpleNamespace(
            session_date=strategy_module._parse_iso_timestamp(rows[-1]["time"]).date()
        ),
    )

    profile = asyncio.run(agent._load_prior_session_profile("MCX:GOLD26AUGFUT"))

    assert profile.session_date == date(2026, 7, 1)


def test_confirmed_value_migration_manages_existing_position(monkeypatch) -> None:
    agent = CommodityStrategyAgent()
    position = strategy_module.CommodityPositionState(
        position_key="commodity_futures:MCX:GOLD26AUGFUT",
        symbol="MCX:GOLD26AUGFUT",
        live_symbol="MCX:GOLD26AUGFUT",
        underlying="GOLD",
        strategy_key="commodity_futures",
        strategy_title="MP+OF Futures",
        instrument_type="FUT",
        action="BUY",
        qty=100,
        lots=1,
        lot_size=100,
        entry_price=100.0,
        current_price=100.0,
        stop_price=90.0,
        target_price=120.0,
        regime="trend_up",
        signal_reason="ib_break_up",
        atr=1.0,
        macd_value=None,
        mp_poc=100.0,
        mp_vah=102.0,
        mp_val=98.0,
        entered_at="2026-07-02T10:00:00+05:30",
        entry_bar_time="2026-07-02T10:00:00+05:30",
        contract_unit_label="100 units",
        quote_unit_label="Rs / unit",
        display_name="Gold",
        initial_qty=100,
        peak_price=100.0,
        entry_style="ib_break",
        last_reviewed_bar_time="2026-07-02T10:00:00+05:30",
    )
    agent._runtime.positions[position.position_key] = position
    closes: list[str] = []

    async def _close(key, pos, price, reason, **kwargs):
        closes.append(reason)
        agent._runtime.positions.pop(key, None)

    monkeypatch.setattr(agent, "_close_futures_position", _close)
    aligned = {
        "symbol": position.symbol,
        "price": 101.0,
        "bar_time": "2026-07-02T10:00:00+05:30",
        "value_migration_state": "confirmed",
        "value_migration_direction": "up",
        "value_migration_signal": "BUY",
        "value_migration_detail": "Value migration up confirmed.",
    }
    asyncio.run(agent._manage_positions(object(), [aligned], []))

    assert position.position_key in agent._runtime.positions
    assert position.value_migration_alignment == "aligned"

    opposed = {
        **aligned,
        "price": 99.0,
        "bar_time": "2026-07-02T10:01:00+05:30",
        "value_migration_direction": "down",
        "value_migration_signal": "SELL",
        "value_migration_detail": "Value migration down confirmed.",
    }
    asyncio.run(agent._manage_positions(object(), [opposed], []))

    assert position.position_key not in agent._runtime.positions
    assert closes == ["value_migration_reversal"]
    assert position.value_migration_alignment == "opposed"
