from datetime import datetime, timezone

import pytest

from auction_intelligence.paper.service import PaperTradingService
from directional_options.paper import DirectionalOptionsPaperStore
from gann_tp_delta.agent import GannTPDeltaPaperAgent
from gann_tp_delta.config import clone_default_config
from market_data.completed_candles import completed_candles
from market_data.option_history import OptionHistoryService
from paper_engine.costs import round_trip_charges


def test_gann_option_uses_option_charges(monkeypatch):
    monkeypatch.setattr("paper_engine.costs.PAPER_APPLY_COSTS", True)
    position = {
        "instrument_type": "OPTION",
        "option_type": "CE",
        "direction": "long_call",
        "entry_price": 100.0,
        "current_price": 120.0,
        "qty_units": 750,
        "trading_symbol": "NIFTY26DEC25000CE",
    }
    agent = GannTPDeltaPaperAgent(service=object(), config=clone_default_config())
    closed = agent._close_position(position, "target")
    expected = round_trip_charges(
        symbol=position["trading_symbol"],
        instrument_type="CE",
        entry_price=100,
        exit_price=120,
        qty=750,
        entry_action="BUY",
    )
    assert closed["transaction_cost"] == expected
    assert expected == 207.85


@pytest.mark.parametrize("entry_slip,exit_slip", [(0.0, 0.0), (0.01, 0.02)])
def test_directional_close_honors_effective_config(tmp_path, entry_slip, exit_slip):
    store = DirectionalOptionsPaperStore(
        tmp_path,
        execution_config={
            "entry_slippage_pct": entry_slip,
            "exit_slippage_pct": exit_slip,
        },
    )
    position = {
        "position_id": "p",
        "underlying": "NIFTY",
        "option_type": "CE",
        "trading_symbol": "NIFTY26DEC25000CE",
        "quantity_units": 75,
        "entry_premium": 100.0,
    }
    store._close_position(
        position,
        mark={"premium": 120.0, "spot": 25000},
        close_time="2026-10-21T05:00:00+00:00",
        close_reason="target",
    )
    assert position["entry_fill_premium"] == pytest.approx(100 * (1 + entry_slip))
    assert position["exit_fill_premium"] == pytest.approx(120 * (1 - exit_slip))


@pytest.mark.parametrize("bps", [0.0, 7.5])
def test_auction_service_passes_slippage_to_book(tmp_path, bps):
    assert (
        PaperTradingService(str(tmp_path), slippage_bps=bps).book._slippage_bps() == bps
    )


def test_completed_filter_excludes_live_bucket_in_both_timezones():
    now = datetime(2026, 10, 21, 4, 35, tzinfo=timezone.utc)  # 10:05 IST
    rows = [
        {"time": "2026-10-21T09:15:00+05:30", "close": 100},
        {"time": "2026-10-21T09:45:00+05:30", "close": 150},
        {"time": "2026-10-21T04:15:00", "close": 151},
        {"time": "bad", "close": 200},
    ]
    assert completed_candles(rows, "30minute", now=now) == rows[:1]
    assert (
        len(
            completed_candles(
                rows, "30minute", now=datetime(2026, 10, 21, 4, 45, tzinfo=timezone.utc)
            )
        )
        == 3
    )


def test_snapshot_30m_buckets_match_nse_session_origin():
    rows = [
        {"time": "2026-10-21T09:16:00+05:30", "close": 100},
        {"time": "2026-10-21T09:44:00+05:30", "close": 110},
        {"time": "2026-10-21T09:46:00+05:30", "close": 120},
    ]
    buckets = OptionHistoryService._aggregate_rows(rows, 30, origin_minutes=15)
    assert [r["time"] for r in buckets] == [
        "2026-10-21T09:15:00+05:30",
        "2026-10-21T09:45:00+05:30",
    ]
    assert [r["close"] for r in buckets] == [110, 120]
