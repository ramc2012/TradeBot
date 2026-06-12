"""Tests for the commodity 30-min regime gate (MP+OF redesign, 2026-06-09)."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from paper_engine.commodity_mp_signal import (
    _DIRECTIONAL_TRIGGERS,
    _MEAN_REVERT_TRIGGERS,
    _resample_30m,
    classify_htf_regime,
)


def _bars(fn, n=300):
    base = datetime(2026, 6, 9, 9, 0)
    return [
        {"time": (base + timedelta(minutes=i)).isoformat(),
         "open": fn(i), "high": fn(i) + 0.5, "low": fn(i) - 0.5, "close": fn(i)}
        for i in range(n)
    ]


def test_resample_30m_buckets():
    bars = _bars(lambda i: 100.0, n=300)
    assert len(_resample_30m(bars)) == 10  # 300 1-min → 10 30-min buckets


def test_regime_trend_up():
    regime, detail = classify_htf_regime(_bars(lambda i: 100 + i * 0.1), cvd_session=5000)
    assert regime == "TREND_UP"
    assert detail["efficiency"] >= 0.9


def test_regime_trend_down():
    assert classify_htf_regime(_bars(lambda i: 200 - i * 0.1), cvd_session=-5000)[0] == "TREND_DOWN"


def test_regime_balance_when_oscillating():
    # Price wanders (low efficiency) → BALANCE even if the endpoints drift.
    assert classify_htf_regime(_bars(lambda i: 100 + math.sin(i / 20) * 3), cvd_session=50)[0] == "BALANCE"


def test_regime_unknown_when_too_short():
    # < 3 30-min bars (early session) → UNKNOWN (gate stays permissive).
    assert classify_htf_regime(_bars(lambda i: 100 + i * 0.1, n=40))[0] == "UNKNOWN"


def test_trend_requires_cvd_agreement():
    # Price trends up but order flow disagrees (CVD < 0) → not a trend.
    assert classify_htf_regime(_bars(lambda i: 100 + i * 0.1), cvd_session=-5000)[0] == "BALANCE"


def test_trigger_categories_partition_the_five_triggers():
    assert _DIRECTIONAL_TRIGGERS.isdisjoint(_MEAN_REVERT_TRIGGERS)
    assert (_DIRECTIONAL_TRIGGERS | _MEAN_REVERT_TRIGGERS) == {
        "open_drive", "ib_break", "va_migration", "failed_auction", "lvn_fade",
    }


def test_position_trade_mode_survives_runtime_state_round_trip():
    """Regression (2026-06-11): asdict() persisted trade_mode/regime_htf but
    _restore_runtime_state's explicit reconstruction dropped them to the
    dataclass None defaults — every backend restart downgraded carried
    rides/scalps to legacy generic exits (all 4 carried positions showed
    trade_mode=None at the 06-11 pre-open)."""
    from dataclasses import asdict

    import paper_engine.commodity_strategy_agent as cm

    position = cm.CommodityPositionState(
        position_key="MCX:ZINCMINI26JUNFUT:commodity_futures",
        symbol="MCX:ZINCMINI26JUNFUT",
        live_symbol="MCX:ZINCMINI26JUNFUT",
        underlying="ZINCMINI",
        strategy_key="commodity_futures",
        strategy_title="Zinc Mini futures",
        instrument_type="FUTURES",
        action="SELL",
        qty=10,
        lots=1,
        lot_size=10,
        entry_price=255.0,
        current_price=252.0,
        stop_price=258.0,
        target_price=246.0,
        regime="bear",
        signal_reason="ib_break_down",
        atr=2.1,
        macd_value=None,
        mp_poc=254.0,
        mp_vah=256.0,
        mp_val=251.0,
        entered_at="2026-06-10T11:30:00+00:00",
        entry_bar_time="2026-06-10T11:30:00+00:00",
        contract_unit_label="kg",
        quote_unit_label="₹/kg",
        display_name="Zinc Mini",
        initial_qty=10,
        peak_price=255.0,
        trade_mode="ride",
        regime_htf="TREND_DOWN",
    )

    from paper_engine.order_book import PaperOrderBook
    from paper_engine.portfolio import PaperPortfolio

    class _StubAgent:
        _runtime = None
        _commentary: list = []

    stub = _StubAgent()
    stub._runtime = cm.CommodityRuntime(
        portfolio=PaperPortfolio(initial_capital=5_000_000.0),
        order_book=PaperOrderBook(),
    )
    stub._commentary = []

    payload = {"positions": [asdict(position)]}
    cm.CommodityStrategyAgent._restore_runtime_state(stub, payload)

    restored = stub._runtime.positions[position.position_key]
    assert restored.trade_mode == "ride"
    assert restored.regime_htf == "TREND_DOWN"
