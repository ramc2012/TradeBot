from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

from paper_engine.portfolio import PaperPortfolio
import paper_engine.strategy_agent as strategy_agent_module
from paper_engine.strategy_agent import (
    PaperStrategyAgent,
    StrategyPosition,
    _ensure_ist_datetime,
    _latest_session_rows,
    detect_greeks_signal,
    detect_macd_zero_cross,
)


UTC = timezone.utc


def test_detect_macd_zero_cross_on_rising_series() -> None:
    closes = [120.0 - (index * 1.2) for index in range(38)] + [80.0, 180.0]
    should_enter, strength, reason = detect_macd_zero_cross(closes)

    assert should_enter is True
    assert strength is not None
    assert reason == "macd_zero_cross"


def test_detect_greeks_signal_on_supportive_series() -> None:
    start = datetime(2026, 3, 1, 9, 15, tzinfo=UTC)
    candles = []
    for index in range(30):
        last = index == 29
        candles.append(
            {
                "time": (start + timedelta(minutes=30 * index)).isoformat(),
                "open": 100.0 + index,
                "high": 101.0 + (index * 2) + (20.0 if last else 0.0),
                "low": 99.5 + index,
                "close": 100.5 + (index * 1.1) + (30.0 if last else 0.0),
                "iv": 0.18 + (index * 0.002) + (0.05 if last else 0.0),
                "delta": 0.2 + (index * 0.01) + (0.4 if last else 0.0),
                "gamma": 0.02 if last else 0.003,
                "theta": -0.5,
                "vega": 30.0 if last else 4.0,
                "underlying_price": 22000.0 + (index * 10.0) + (200.0 if last else 0.0),
            }
        )

    should_enter, strength, reason = detect_greeks_signal(candles, "CE")

    assert should_enter is True
    assert strength is not None
    assert strength >= 70.0
    assert reason == "greeks_sync_signal"


def test_paper_portfolio_summary_sanitizes_infinite_profit_factor() -> None:
    portfolio = PaperPortfolio(initial_capital=1_000_000.0, session_id="test")
    summary = portfolio.get_summary()

    assert summary["profit_factor"] is None


def test_candidate_expiries_include_next_when_front_is_near() -> None:
    agent = PaperStrategyAgent()

    expiries = agent._select_candidate_expiries(
        datetime(2026, 3, 24).date(),
        ["2026-03-26", "2026-04-02", "2026-04-30"],
    )

    assert expiries == ["2026-03-26", "2026-04-02"]


def test_candidate_expiries_only_front_when_not_near() -> None:
    agent = PaperStrategyAgent()

    expiries = agent._select_candidate_expiries(
        datetime(2026, 3, 10).date(),
        ["2026-03-26", "2026-04-02", "2026-04-30"],
    )

    assert expiries == ["2026-03-26"]


def test_get_status_exposes_next_scan_and_runtime_timestamps() -> None:
    agent = PaperStrategyAgent()
    agent._last_run_at = "2026-04-09T09:15:00+05:30"
    agent._strategy.last_scan_at = "2026-04-09T09:15:00+05:30"
    agent._strategy.last_message = "Scanned 218 instruments."
    agent._strategy2.last_scan_at = "2026-04-09T09:15:00+05:30"
    agent._strategy2.last_message = "Scanned 4 indices."

    status = agent.get_status()

    assert status["next_scan_at"] == "2026-04-09T09:16:00+05:30"
    assert status["strategy_agents"][0]["key"] == "macd_strategy"
    assert status["strategy_agents"][0]["timeframe"] == "30minute"
    assert status["strategies"][0]["last_scan_at"] == "2026-04-09T09:15:00+05:30"
    assert status["strategies"][0]["agent"]["key"] == "macd_strategy"
    assert status["strategies"][0]["last_message"] == "Scanned 218 instruments."
    assert status["strategies"][1]["key"] == "index_mp_strategy"
    assert status["strategies"][1]["agent"]["timeframe"] == "5minute"
    assert status["strategies"][1]["last_message"] == "Scanned 4 indices."


def test_market_closed_keeps_strategy2_last_signal_snapshot(monkeypatch) -> None:
    agent = PaperStrategyAgent()
    agent._last_run_at = "2026-04-09T15:20:00+05:30"
    agent._strategy1.last_scan_at = "2026-04-09T15:20:00+05:30"
    agent._strategy2.last_scan_at = "2026-04-09T15:20:00+05:30"
    agent._strategy2.last_message = "Scanned 4 indices. 2 aligned lanes, 0 open positions."
    agent._strategy2.signal_lane = [
        {
            "underlying": "NIFTY",
            "status": "trend-aligned",
            "freshness": "live",
            "instruction": "NIFTY aligned",
        }
    ]
    agent._strategy2.meta = {
        "mode": "live_scan",
        "scan_interval": "5minute",
        "watchlist_rows": 4,
        "pipeline": [{"name": "Strategy 2 NIFTY", "status": "ok", "rows": 45, "last_date": "2026-04-09 15:20"}],
    }

    monkeypatch.setattr(strategy_agent_module, "_in_market_hours", lambda _: False)

    status = asyncio.run(agent.run_once(force=False))

    assert status["last_message"].startswith("Market closed.")
    assert status["strategies"][0]["last_scan_at"] == "2026-04-09T15:20:00+05:30"
    assert status["strategies"][1]["last_scan_at"] == "2026-04-09T15:20:00+05:30"
    assert status["strategies"][1]["signals"][0]["underlying"] == "NIFTY"
    assert status["strategies"][1]["meta"]["mode"] == "market_closed"
    assert status["strategies"][1]["meta"]["market_state"] == "closed"


def test_latest_session_rows_use_most_recent_trading_day() -> None:
    rows = [
        {"time": "2026-04-08T09:15:00Z", "close": 100},
        {"time": "2026-04-08T09:16:00Z", "close": 101},
        {"time": "2026-04-09T09:15:00Z", "close": 110},
        {"time": "2026-04-09T09:16:00Z", "close": 111},
    ]

    session_rows, session_date = _latest_session_rows(rows)

    assert session_date.isoformat() == "2026-04-09"
    assert len(session_rows) == 2
    assert session_rows[-1]["close"] == 111


def test_strategy_agent_persists_and_restores_runtime_state(monkeypatch, tmp_path) -> None:
    state_file = tmp_path / "nse_strategy_state.json"
    monkeypatch.setattr(strategy_agent_module, "_NSE_STRATEGY_STATE_FILE", state_file)

    agent = PaperStrategyAgent()
    agent._last_run_at = "2026-04-09T15:20:00+05:30"
    agent._last_message = "Scanned 220 rows, S1=1, S2=0."
    agent._strategy1.last_scan_at = "2026-04-09T15:20:00+05:30"
    agent._strategy1.last_message = "Scanned 220 rows."
    agent._strategy1.entries = 1
    agent._strategy1.processed_signals = {"AUROPHARMA:CE": "2026-04-09T14:15:00+05:30"}
    agent._strategy1.signal_lane = [{"underlying": "AUROPHARMA", "status": "active"}]
    agent._strategy1.positions["OPT:AUROPHARMA:2026-04-30:1340:CE"] = StrategyPosition(
        symbol="OPT:AUROPHARMA:2026-04-30:1340:CE",
        underlying="AUROPHARMA",
        expiry="2026-04-30",
        strike=1340.0,
        option_type="CE",
        instrument_key="NSE_FO|123",
        trading_symbol="AUROPHARMA 1340 CE",
        qty=2500,
        initial_qty=2500,
        entry_price=39.07,
        current_price=40.25,
        peak_price=40.25,
        entry_bar_time="2026-04-09T14:15:00+05:30",
        entered_at="2026-04-09T14:18:00+05:30",
        signal_reason="macd_zero_cross",
    )
    agent._persist_state()

    restored = PaperStrategyAgent()

    assert restored._last_run_at == "2026-04-09T15:20:00+05:30"
    assert restored._strategy1.last_scan_at == "2026-04-09T15:20:00+05:30"
    assert restored._strategy1.entries == 1
    assert restored._strategy1.processed_signals["AUROPHARMA:CE"] == "2026-04-09T14:15:00+05:30"
    assert "OPT:AUROPHARMA:2026-04-30:1340:CE" in restored._strategy1.positions
    assert restored.get_status()["strategies"][0]["summary"]["open_positions"] == 1


def test_ensure_ist_datetime_preserves_naive_ist_wall_clock() -> None:
    naive = datetime(2026, 4, 10, 15, 25)

    normalized = _ensure_ist_datetime(naive)

    assert normalized is not None
    assert normalized.isoformat() == "2026-04-10T15:25:00+05:30"


def test_restore_from_historical_state_uses_separate_days_for_strategy1_and_strategy2(monkeypatch) -> None:
    agent = PaperStrategyAgent()
    called: dict[str, date] = {}

    async def fake_restore_positions(trading_day: date):
        called["strategy1"] = trading_day
        return 1, 12, None

    async def fake_replay(trading_day: date):
        called["strategy2"] = trading_day
        return {"entry_ready_count": 0, "trend_aligned_count": 1, "last_seen_at": None}

    monkeypatch.setattr(agent, "_restore_strategy1_positions_from_db", fake_restore_positions)
    monkeypatch.setattr(agent, "_replay_strategy2_session", fake_replay)

    asyncio.run(
        agent._restore_from_historical_state(
            latest_snapshot_day=date(2026, 4, 10),
            latest_position_day=date(2026, 4, 9),
        )
    )

    assert called["strategy1"] == date(2026, 4, 9)
    assert called["strategy2"] == date(2026, 4, 10)


def test_ensure_recovered_state_refreshes_stale_strategy2_signal_lane(monkeypatch) -> None:
    agent = PaperStrategyAgent()
    agent._last_run_at = "2026-04-10T15:29:18.552610+05:30"
    agent._strategy2.signal_lane = [
        {"underlying": underlying, "spot_session_date": "2026-04-09", "signal_date": "2026-04-09"}
        for underlying in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")
    ]

    class FakeSession:
        def __init__(self) -> None:
            self.calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def scalar(self, _query):
            self.calls += 1
            if self.calls == 1:
                return date(2026, 4, 10)
            return None

    restore_args: dict[str, date | None] = {}

    async def fake_restore_from_historical_state(*, latest_snapshot_day=None, latest_position_day=None):
        restore_args["latest_snapshot_day"] = latest_snapshot_day
        restore_args["latest_position_day"] = latest_position_day

    monkeypatch.setattr(strategy_agent_module, "AsyncSessionLocal", lambda: FakeSession())
    monkeypatch.setattr(agent, "_restore_from_historical_state", fake_restore_from_historical_state)

    asyncio.run(agent.ensure_recovered_state())

    assert restore_args == {
        "latest_snapshot_day": date(2026, 4, 10),
        "latest_position_day": None,
    }


def test_run_once_stops_when_no_valid_broker_session_is_available(monkeypatch) -> None:
    agent = PaperStrategyAgent()

    async def fake_snapshot(*, force_validate: bool = False):
        return {
            "connected_brokers": [],
            "upstox_ready": False,
            "fyers_ready": False,
            "broker_ready": False,
            "upstox_token_health": {"status": "expired_reconnect_required"},
            "fyers_token_health": {"status": "expired_reconnect_required"},
        }

    monkeypatch.setattr(strategy_agent_module, "_in_market_hours", lambda _: True)
    monkeypatch.setattr(strategy_agent_module, "get_broker_connection_snapshot", fake_snapshot)

    status = asyncio.run(agent.run_once(force=False))

    assert status["last_error"] is not None
    assert "No valid NSE broker session is available" in status["last_message"]
    assert status["data_health"]["broker_snapshot"]["broker_ready"] is False
    assert status["strategies"][0]["last_message"] == status["last_message"]
