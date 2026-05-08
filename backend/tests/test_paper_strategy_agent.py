from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from agent import window_calculator as window_calculator_module
from paper_engine.order_book import PaperOrderBook
from paper_engine.portfolio import PaperPortfolio
import paper_engine.strategy_agent as strategy_agent_module
import paper_engine.strategy_agent_entries as strategy_entries_module
from paper_engine.strategy_agent import (
    PHASE_TRAILING,
    PaperStrategyAgent,
    StrategyPosition,
    _ensure_ist_datetime,
    _latest_populated_session_rows,
    _latest_session_rows,
    _strategy2_expected_session_date,
    _strategy2_is_regular_session,
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


def test_latest_populated_session_rows_ignores_partial_after_hours_bucket() -> None:
    full_start = datetime(2026, 5, 6, 9, 15, tzinfo=UTC)
    partial_start = datetime(2026, 5, 6, 18, 30, tzinfo=UTC)
    rows = [
        {"time": (full_start + timedelta(minutes=index)).isoformat(), "close": 100.0 + index}
        for index in range(60)
    ] + [
        {"time": (partial_start + timedelta(minutes=index)).isoformat(), "close": 200.0 + index}
        for index in range(3)
    ]

    session_rows, session_date = _latest_populated_session_rows(rows, min_rows=30)

    assert session_date == date(2026, 5, 6)
    assert len(session_rows) == 60


def test_strategy2_expected_session_date_uses_previous_date_before_open() -> None:
    before_open = datetime(2026, 5, 7, 0, 15, tzinfo=UTC)
    after_open = datetime(2026, 5, 7, 9, 20, tzinfo=UTC)

    assert _strategy2_expected_session_date(before_open) == date(2026, 5, 6)
    assert _strategy2_expected_session_date(after_open) == date(2026, 5, 7)
    assert _strategy2_is_regular_session(before_open) is False
    assert _strategy2_is_regular_session(after_open) is True


def test_paper_portfolio_summary_sanitizes_infinite_profit_factor() -> None:
    portfolio = PaperPortfolio(initial_capital=1_000_000.0, session_id="test")
    summary = portfolio.get_summary()

    assert summary["profit_factor"] is None


def test_trade_history_persists_signal_metadata() -> None:
    portfolio = PaperPortfolio(initial_capital=1_000_000.0, session_id="meta")
    order_book = PaperOrderBook(on_fill=portfolio.on_fill)

    order_book.place_order(
        symbol="OPT:AUROPHARMA:2026-04-30:1340:CE",
        action="BUY",
        order_type="MARKET",
        qty=2500,
        instrument_type="CE",
        expiry="2026-04-30",
        strike=1340.0,
        option_type="CE",
        ltp=80.0,
        signal_id="signal-123",
        setup_type="breakout",
        entry_iv_pct=24.5,
        regime="bullish",
    )
    order_book.place_order(
        symbol="OPT:AUROPHARMA:2026-04-30:1340:CE",
        action="SELL",
        order_type="MARKET",
        qty=2500,
        instrument_type="CE",
        expiry="2026-04-30",
        strike=1340.0,
        option_type="CE",
        ltp=100.0,
    )

    trade = portfolio._trade_history[0]

    assert trade.signal_id == "signal-123"
    assert trade.setup_type == "breakout"
    assert trade.entry_iv_pct == 24.5
    assert trade.regime == "bullish"


def test_paper_portfolio_option_close_does_not_double_count_pnl() -> None:
    portfolio = PaperPortfolio(initial_capital=1_000_000.0, session_id="cash")
    order_book = PaperOrderBook(on_fill=portfolio.on_fill)

    order_book.place_order(
        symbol="OPT:NIFTY:2026-04-30:24000:CE",
        action="BUY",
        order_type="MARKET",
        qty=100,
        instrument_type="CE",
        option_type="CE",
        ltp=100.0,
    )
    order_book.place_order(
        symbol="OPT:NIFTY:2026-04-30:24000:CE",
        action="SELL",
        order_type="MARKET",
        qty=100,
        instrument_type="CE",
        option_type="CE",
        ltp=80.0,
    )

    trade = portfolio._trade_history[0]

    assert trade.pnl == pytest.approx((79.96 - 100.05) * 100)
    assert portfolio.available_capital == pytest.approx(1_000_000.0 + trade.pnl)


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


def test_get_all_strategy1_scan_windows_prefers_active_then_rolls_to_next(monkeypatch) -> None:
    async def fake_fetch(_query: str, _params: dict[str, object]) -> list[dict]:
        return [
            {
                "symbol": "ACTIVE",
                "kind": "STOCK",
            },
            {
                "symbol": "BANKNIFTY",
                "kind": "INDEX",
            },
            {
                "symbol": "UPCOMING",
                "kind": "STOCK",
            },
        ]

    monkeypatch.setattr(window_calculator_module, "_fetch_underlying_rows", fake_fetch)

    windows = asyncio.run(window_calculator_module.get_all_strategy1_scan_windows(as_of=date(2026, 4, 15)))
    selected = {str(window["underlying"]): window for window in windows}

    assert selected["ACTIVE"]["expiry"] == date(2026, 4, 28)
    assert selected["ACTIVE"]["window_state"] == "active"
    assert selected["BANKNIFTY"]["expiry"] == date(2026, 4, 28)
    assert selected["BANKNIFTY"]["window_state"] == "active"
    assert selected["UPCOMING"]["expiry"] == date(2026, 4, 28)
    assert selected["UPCOMING"]["window_state"] == "active"


def test_run_once_uses_next_strategy1_window_instead_of_idling(monkeypatch) -> None:
    agent = PaperStrategyAgent()

    async def fake_snapshot(*, force_validate: bool = False):
        return {
            "connected_brokers": ["fyers"],
            "upstox_ready": False,
            "fyers_ready": True,
            "broker_ready": True,
            "upstox_token_health": {"status": "disconnected"},
            "fyers_token_health": {"status": "valid"},
        }

    async def fake_active_windows(*, as_of: date | None = None):
        return []

    async def fake_scan_windows(*, as_of: date | None = None):
        return [
            {
                "underlying": "AUROPHARMA",
                "expiry": date(2026, 5, 26),
                "prev_expiry": date(2026, 4, 28),
                "window_start": date(2026, 4, 21),
                "window_end": date(2026, 5, 19),
                "window_state": "future",
            }
        ]

    async def fake_watchlist(
        expiry: str | None = None,
        symbols: list[str] | None = None,
        *,
        live_refresh: bool = False,
    ):
        if expiry == "2026-05-26":
            return {
                "rows": [
                    {
                        "underlying": "AUROPHARMA",
                        "kind": "STOCK",
                        "expiry": "2026-05-26",
                        "spot_price": 1298.5,
                        "ce": None,
                        "pe": None,
                    }
                ],
                "detail": None,
            }
        return {"rows": [], "detail": None}

    async def fake_expiries(_expiry: str | None = None, *, live_refresh: bool = False):
        return {
            "default_expiry": "2026-05-26",
            "index_monthlies": {
                "NIFTY": "2026-05-26",
                "BANKNIFTY": "2026-05-26",
                "FINNIFTY": "2026-05-26",
                "MIDCPNIFTY": "2026-05-26",
                "SENSEX": "2026-05-30",
            },
        }

    async def fake_manage_exits(_runtime, _rows=None):
        return None

    async def fake_bootstrap(**_kwargs):
        return {
            "status": "ready",
            "counts_after": {"keyed_rows": 100, "keyed_stocks": 95},
        }

    async def fake_scan_entries(runtime, rows, window_map):
        runtime.last_message = f"Scanned {len(rows)} instruments across {len(window_map)} windows."

    async def fake_run_strategy2(runtime, rows, started_at):
        runtime.last_message = "Strategy 2 skipped for unit test."
        runtime.meta = {
            **(runtime.meta or {}),
            "mode": "test_stub",
            "updated_at": started_at.isoformat(),
            "watchlist_rows": len(rows),
        }

    async def fake_status():
        return agent.get_status()

    async def fake_async_noop():
        return None

    monkeypatch.setattr(strategy_agent_module, "_in_market_hours", lambda _: True)
    monkeypatch.setattr(strategy_agent_module, "get_broker_connection_snapshot", fake_snapshot)
    monkeypatch.setattr(strategy_agent_module, "get_all_active_windows", fake_active_windows)
    monkeypatch.setattr(strategy_agent_module, "get_all_strategy1_scan_windows", fake_scan_windows)
    monkeypatch.setattr(strategy_agent_module, "ensure_fo_underlying_catalog", fake_bootstrap)
    monkeypatch.setattr(strategy_agent_module.atm_watchlist_service, "get_expiries", fake_expiries)
    monkeypatch.setattr(strategy_agent_module.atm_watchlist_service, "get_watchlist", fake_watchlist)
    monkeypatch.setattr(agent, "_manage_exits", fake_manage_exits)
    monkeypatch.setattr(agent, "_scan_entries", fake_scan_entries)
    monkeypatch.setattr(agent, "_run_strategy2", fake_run_strategy2)
    monkeypatch.setattr(agent, "_status_with_risk_snapshot", fake_status)
    monkeypatch.setattr(agent, "_maybe_send_telegram_report", fake_async_noop)
    monkeypatch.setattr(agent, "_maybe_sync_spot_candles", fake_async_noop)
    monkeypatch.setattr(strategy_agent_module.option_history_service, "reset_health", lambda: None)
    monkeypatch.setattr(strategy_agent_module.option_history_service, "get_health_snapshot", lambda: {})

    for runtime in (agent._strategy1, agent._strategy2):
        runtime.portfolio.snapshot_equity = lambda: None
        runtime.portfolio.persist_equity_to_redis = fake_async_noop

    status = asyncio.run(agent.run_once(force=False))

    assert status["active_windows"] == 0
    assert status["strategy1_scan_windows"] == 1
    assert status["candidate_expiries"] == ["2026-05-26"]
    assert status["target_expiry"] == "2026-05-26"
    assert status["strategy_agents"][0]["mode"] == "live_scan"
    assert "No active Strategy 1 monthly trading windows." not in status["strategy_agents"][0]["last_message"]


def test_run_once_includes_native_strategy2_index_expiry_rows(monkeypatch) -> None:
    agent = PaperStrategyAgent()
    captured: dict[str, list[str]] = {}
    requested_watchlists: list[tuple[str | None, tuple[str, ...]]] = []

    async def fake_snapshot(*, force_validate: bool = False):
        return {
            "connected_brokers": ["fyers"],
            "upstox_ready": False,
            "fyers_ready": True,
            "broker_ready": True,
            "upstox_token_health": {"status": "disconnected"},
            "fyers_token_health": {"status": "valid"},
        }

    async def fake_active_windows(*, as_of: date | None = None):
        return []

    async def fake_scan_windows(*, as_of: date | None = None):
        return [
            {
                "underlying": "AUROPHARMA",
                "expiry": date(2026, 4, 28),
                "prev_expiry": date(2026, 3, 26),
                "window_start": date(2026, 3, 19),
                "window_end": date(2026, 4, 21),
                "window_state": "active",
            }
        ]

    async def fake_expiries(_expiry: str | None = None, *, live_refresh: bool = False):
        return {
            "default_expiry": "2026-04-28",
            "index_monthlies": {
                "NIFTY": "2026-04-28",
                "BANKNIFTY": "2026-04-28",
                "FINNIFTY": "2026-04-28",
                "MIDCPNIFTY": "2026-04-28",
                "SENSEX": "2026-04-24",
            },
        }

    async def fake_watchlist(
        expiry: str | None = None,
        symbols: list[str] | None = None,
        *,
        live_refresh: bool = False,
    ):
        requested_watchlists.append((expiry, tuple(symbols or [])))
        if expiry == "2026-04-24":
            return {
                "rows": [
                    {
                        "underlying": "SENSEX",
                        "kind": "INDEX",
                        "expiry": "2026-04-24",
                        "spot_price": 78000.0,
                        "ce": {"option_type": "CE"},
                        "pe": {"option_type": "PE"},
                    }
                ],
                "detail": None,
            }
        return {
            "rows": [
                {"underlying": "NIFTY", "kind": "INDEX", "expiry": "2026-04-28", "spot_price": 24200.0, "ce": {"option_type": "CE"}, "pe": {"option_type": "PE"}},
                {"underlying": "BANKNIFTY", "kind": "INDEX", "expiry": "2026-04-28", "spot_price": 56500.0, "ce": {"option_type": "CE"}, "pe": {"option_type": "PE"}},
                {"underlying": "FINNIFTY", "kind": "INDEX", "expiry": "2026-04-28", "spot_price": 26600.0, "ce": {"option_type": "CE"}, "pe": {"option_type": "PE"}},
                {"underlying": "MIDCPNIFTY", "kind": "INDEX", "expiry": "2026-04-28", "spot_price": 13500.0, "ce": {"option_type": "CE"}, "pe": {"option_type": "PE"}},
                {"underlying": "AUROPHARMA", "kind": "STOCK", "expiry": "2026-04-28", "spot_price": 1298.5, "ce": None, "pe": None},
            ],
            "detail": None,
        }

    async def fake_manage_exits(_runtime, _rows=None):
        return None

    async def fake_bootstrap(**_kwargs):
        return {"status": "ready", "counts_after": {"keyed_rows": 100}}

    async def fake_scan_entries(runtime, rows, window_map):
        runtime.last_message = f"Scanned {len(rows)} instruments across {len(window_map)} windows."

    async def fake_run_strategy2(runtime, rows, started_at):
        index_rows = [row for row in rows if row.get("underlying") in strategy_agent_module.STRATEGY2_UNDERLYINGS]
        captured["underlyings"] = [row["underlying"] for row in index_rows]
        runtime.last_message = f"Scanned {len(index_rows)} indices."
        runtime.signal_lane = [{"underlying": row["underlying"], "status": "waiting-cross"} for row in index_rows]
        runtime.meta = {
            "mode": "live_scan",
            "updated_at": started_at.isoformat(),
            "watchlist_rows": len(index_rows),
            "pipeline": [],
        }

    async def fake_status():
        return agent.get_status()

    async def fake_async_noop():
        return None

    monkeypatch.setattr(strategy_agent_module, "_in_market_hours", lambda _: True)
    monkeypatch.setattr(strategy_agent_module, "get_broker_connection_snapshot", fake_snapshot)
    monkeypatch.setattr(strategy_agent_module, "get_all_active_windows", fake_active_windows)
    monkeypatch.setattr(strategy_agent_module, "get_all_strategy1_scan_windows", fake_scan_windows)
    monkeypatch.setattr(strategy_agent_module, "ensure_fo_underlying_catalog", fake_bootstrap)
    monkeypatch.setattr(strategy_agent_module.atm_watchlist_service, "get_expiries", fake_expiries)
    monkeypatch.setattr(strategy_agent_module.atm_watchlist_service, "get_watchlist", fake_watchlist)
    monkeypatch.setattr(agent, "_manage_exits", fake_manage_exits)
    monkeypatch.setattr(agent, "_scan_entries", fake_scan_entries)
    monkeypatch.setattr(agent, "_run_strategy2", fake_run_strategy2)
    monkeypatch.setattr(agent, "_status_with_risk_snapshot", fake_status)
    monkeypatch.setattr(agent, "_maybe_send_telegram_report", fake_async_noop)
    monkeypatch.setattr(agent, "_maybe_sync_spot_candles", fake_async_noop)
    monkeypatch.setattr(strategy_agent_module.option_history_service, "reset_health", lambda: None)
    monkeypatch.setattr(strategy_agent_module.option_history_service, "get_health_snapshot", lambda: {})

    for runtime in (agent._strategy1, agent._strategy2):
        runtime.portfolio.snapshot_equity = lambda: None
        runtime.portfolio.persist_equity_to_redis = fake_async_noop

    status = asyncio.run(agent.run_once(force=False))

    assert "SENSEX" in captured["underlyings"]
    assert status["strategy_agents"][1]["mode"] == "live_scan"
    assert status["strategy_agents"][1]["signals"] == 5
    assert ("2026-04-28", ()) in requested_watchlists
    assert ("2026-04-24", strategy_agent_module.STRATEGY2_UNDERLYINGS) in requested_watchlists
    assert (None, strategy_agent_module.STRATEGY2_UNDERLYINGS) in requested_watchlists


def test_get_status_exposes_next_scan_and_runtime_timestamps() -> None:
    agent = PaperStrategyAgent()
    agent._last_run_at = "2026-04-09T09:15:00+05:30"
    agent._strategy.last_scan_at = "2026-04-09T09:15:00+05:30"
    agent._strategy.last_message = "Scanned 218 instruments."
    agent._strategy2.last_scan_at = "2026-04-09T09:15:00+05:30"
    agent._strategy2.last_message = "Scanned 5 indices."

    status = agent.get_status()

    assert status["next_scan_at"] == "2026-04-09T09:16:00+05:30"
    assert status["strategy_agents"][0]["key"] == "macd_strategy"
    assert status["strategy_agents"][0]["timeframe"] == "30minute"
    assert status["strategies"][0]["last_scan_at"] == "2026-04-09T09:15:00+05:30"
    assert status["strategies"][0]["agent"]["key"] == "macd_strategy"
    assert status["strategies"][0]["last_message"] == "Scanned 218 instruments."
    assert status["strategies"][1]["key"] == "index_mp_strategy"
    assert status["strategies"][1]["agent"]["timeframe"] == "5minute"
    assert status["strategies"][1]["last_message"] == "Scanned 5 indices."


def test_strategy2_spot_rows_prefer_fyers_history_before_upstox(monkeypatch) -> None:
    agent = PaperStrategyAgent()
    started_at = datetime(2026, 4, 20, 10, 0, tzinfo=strategy_agent_module.IST)
    upstox_calls = {"count": 0}

    class _FakeFyersAdapter:
        async def get_historical_candles(self, symbol: str, resolution: str, date_from: str, date_to: str):
            assert symbol == strategy_agent_module.STRATEGY2_FYERS_SYMBOLS["NIFTY"]
            assert resolution == "1"
            return [{"time": started_at.isoformat(), "close": 24310.0}]

    async def fake_fetch_broker_candles(**kwargs):
        upstox_calls["count"] += 1
        return [{"time": started_at.isoformat(), "close": 24295.0}]

    monkeypatch.setattr(strategy_agent_module, "get_active_adapter", lambda broker: _FakeFyersAdapter() if broker == "fyers" else None)
    monkeypatch.setattr(strategy_agent_module, "ensure_fyers_session", lambda **kwargs: asyncio.sleep(0, result=False))
    monkeypatch.setattr(strategy_agent_module.option_history_service, "_fetch_broker_candles", fake_fetch_broker_candles)

    rows, source = asyncio.run(agent._load_strategy2_spot_rows("NIFTY", started_at))

    assert source == "fyers"
    assert len(rows) == 1
    assert upstox_calls["count"] == 0


def test_strategy1_market_profile_gate_can_be_bypassed(monkeypatch) -> None:
    agent = PaperStrategyAgent()

    monkeypatch.setattr(strategy_agent_module.settings, "NSE_STRATEGY_BYPASS_MARKET_PROFILE_GATE", True)

    gate = asyncio.run(agent._build_strategy1_market_profile_gate("SENSEX", "CE"))

    assert gate["confirmed"] is True
    assert gate["direction"] == "CE"
    assert gate["day_type"] == "bypassed"
    assert gate["reason"] == "market_profile_gate_bypassed"
    assert gate["source"] == "bypass"


def test_strategy1_scan_entries_uses_snapshot_macd_cross(monkeypatch) -> None:
    agent = PaperStrategyAgent()
    runtime = agent._strategy1
    runtime.positions.clear()
    runtime.processed_signals.clear()
    opened: list[dict] = []

    monkeypatch.setattr(strategy_entries_module, "_now_ist", lambda: datetime(2026, 4, 16, 12, 0, tzinfo=strategy_agent_module.IST))

    async def fake_snapshot_state(_rows):
        return {
            "NSE_FO|CE1": {
                1: {
                    "time": datetime(2026, 4, 16, 11, 58, tzinfo=strategy_agent_module.IST),
                    "macd_bucket": datetime(2026, 4, 16, 11, 30),
                    "macd": 1.4,
                    "macd_signal": 0.8,
                    "macd_histogram": 0.6,
                    "rsi": 62.5,
                    "ltp": 118.0,
                },
                2: {
                    "time": datetime(2026, 4, 16, 11, 55, tzinfo=strategy_agent_module.IST),
                    "macd_bucket": datetime(2026, 4, 16, 11, 30),
                    "macd": 1.4,
                },
                3: {
                    "time": datetime(2026, 4, 16, 11, 28, tzinfo=strategy_agent_module.IST),
                    "macd_bucket": datetime(2026, 4, 16, 11, 0),
                    "macd": -0.3,
                },
            },
            "NSE_FO|PE1": {
                1: {
                    "time": datetime(2026, 4, 16, 11, 58, tzinfo=strategy_agent_module.IST),
                    "macd_bucket": datetime(2026, 4, 16, 11, 30),
                    "macd": 0.6,
                    "macd_signal": 0.4,
                    "macd_histogram": 0.3,
                    "rsi": 38.0,
                    "ltp": 91.0,
                },
                2: {
                    "time": datetime(2026, 4, 16, 11, 28, tzinfo=strategy_agent_module.IST),
                    "macd_bucket": datetime(2026, 4, 16, 11, 0),
                    "macd": 0.4,
                },
            },
        }

    async def fake_spot_context(_underlying, _window):
        return {"setup": "breakout"}

    async def fake_mp_gate(_underlying, _direction):
        return {"confirmed": True, "day_type": "bypassed", "reason": "market_profile_gate_bypassed", "direction": "CE", "source": "bypass", "session_date": "2026-04-16"}

    async def fake_open_position(_runtime, candidate):
        opened.append(candidate)

    monkeypatch.setattr(agent, "_load_strategy1_recent_snapshot_state", fake_snapshot_state)
    monkeypatch.setattr(agent, "_compute_spot_context", fake_spot_context)
    monkeypatch.setattr(agent, "_build_strategy1_market_profile_gate", fake_mp_gate)
    monkeypatch.setattr(agent, "_open_position", fake_open_position)

    rows = [
        {
            "underlying": "AUROPHARMA",
            "expiry": "2026-04-28",
            "spot_price": 1298.5,
            "lot_size": 550,
                "ce": {
                    "instrument_key": "NSE_FO|CE1",
                    "option_type": "CE",
                    "strike": 1300.0,
                    "ltp": 118.0,
                    "iv": 18.5,
                    "macd": 1.4,
                    "macd_signal": 0.8,
                    "macd_histogram": 0.6,
                    "rsi": 62.5,
                },
                "pe": {
                    "instrument_key": "NSE_FO|PE1",
                    "option_type": "PE",
                    "strike": 1300.0,
                    "ltp": 91.0,
                    "iv": 19.0,
                    "macd": -0.6,
                    "macd_signal": -0.9,
                    "macd_histogram": 0.3,
                    "rsi": 38.0,
                },
        }
    ]
    window_map = {
        "AUROPHARMA": {
            "underlying": "AUROPHARMA",
            "window_start": date(2026, 4, 1),
            "window_end": date(2026, 4, 21),
            "expiry": date(2026, 4, 28),
        }
    }

    asyncio.run(agent._scan_entries(runtime, rows, window_map))

    assert len(opened) == 1
    assert opened[0]["opt_type"] == "CE"
    assert opened[0]["reason"] == "macd_zero_cross"
    assert opened[0]["strength"] == 1.4
    assert opened[0]["quadrant"].regime == "bullish"


def test_strategy2_signal_context_can_bypass_market_profile_gate(monkeypatch) -> None:
    agent = PaperStrategyAgent()
    started_at = datetime(2026, 4, 16, 9, 45, tzinfo=strategy_agent_module.IST)

    def _candles_from_closes(closes: list[float]) -> list[dict]:
        base = datetime(2026, 4, 16, 9, 15, tzinfo=strategy_agent_module.IST)
        rows: list[dict] = []
        for index, close in enumerate(closes):
            rows.append(
                {
                    "time": (base + timedelta(minutes=5 * index)).isoformat(),
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 1000 + index,
                }
            )
        return rows

    ce_closes = [120.0 - (index * 1.2) for index in range(38)] + [80.0, 180.0]
    pe_closes = [110.0 + (index * 0.1) for index in range(40)]

    async def fake_load_candles(_row, side, *, interval="5minute", limit=96):
        option_type = side.get("option_type")
        return _candles_from_closes(ce_closes if option_type == "CE" else pe_closes)

    monkeypatch.setattr(strategy_agent_module.settings, "NSE_STRATEGY_BYPASS_MARKET_PROFILE_GATE", True)
    monkeypatch.setattr(agent, "_load_candles", fake_load_candles)

    row = {
        "underlying": "SENSEX",
        "expiry": "2026-04-16",
        "spot_price": 78211.7,
        "ce": {"option_type": "CE", "ltp": 180.0},
        "pe": {"option_type": "PE", "ltp": 95.0},
    }

    context = asyncio.run(agent._build_strategy2_signal_context(row, started_at))

    assert context["direction"] == "CE"
    assert context["day_type"] == "bypassed"
    assert context["gate_reason"] == "market_profile_gate_bypassed"
    assert context["can_enter"] is True
    assert context["signal"]["status"] == "entry-ready"
    assert context["signal"]["instruction"].startswith("SENSEX: CE zero-cross confirmed while Market Profile gate is bypassed")


def test_market_closed_keeps_strategy2_last_signal_snapshot(monkeypatch) -> None:
    agent = PaperStrategyAgent()
    agent._last_run_at = "2026-04-09T15:20:00+05:30"
    agent._strategy1.last_scan_at = "2026-04-09T15:20:00+05:30"
    agent._strategy2.last_scan_at = "2026-04-09T15:20:00+05:30"
    agent._strategy2.last_message = "Scanned 5 indices. 2 aligned lanes, 0 open positions."
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
    assert status["strategies"][0]["last_scan_at"]
    assert status["strategies"][1]["last_scan_at"]
    assert status["strategies"][1]["signals"][0]["underlying"] == "NIFTY"
    assert status["strategies"][1]["meta"]["mode"] in {"market_closed", "prepared_market_closed"}
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
    persisted: dict[str, object] = {}

    def fake_save_state(payload: dict[str, object]):
        persisted["payload"] = payload
        state_file.write_text("{}")
        return None

    def fake_load_state():
        return persisted.get("payload", {}), None

    monkeypatch.setattr(strategy_agent_module, "_save_strategy_state", fake_save_state)
    monkeypatch.setattr(strategy_agent_module, "_load_saved_strategy_state", fake_load_state)
    monkeypatch.setattr(strategy_agent_module, "_load_saved_strategy_state_from_database", lambda: (None, None))

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


def test_strategy_agent_refreshes_runtime_state_from_database(monkeypatch) -> None:
    agent = PaperStrategyAgent()
    updated_at = datetime(2026, 4, 16, 6, 0, tzinfo=UTC)
    payload = {
        "last_run_at": "2026-04-16T11:30:00+05:30",
        "last_error": None,
        "last_message": "Scanned 218 rows, S1=1, S2=0.",
        "last_expiry": "2026-04-28",
        "last_candidate_expiries": ["2026-04-28"],
        "commentary": [],
        "strategies": {
            "macd_strategy": {
                "entries": 0,
                "exits": 0,
                "last_scan_at": "2026-04-16T11:30:00+05:30",
                "last_message": "Strategy 1 live scan complete.",
                "processed_signals": {},
                "signal_lane": [],
                "meta": {"mode": "live_scan"},
                "recent_events": [],
                "positions": [],
                "portfolio": {},
            },
            "index_mp_strategy": {
                "entries": 0,
                "exits": 0,
                "last_scan_at": "2026-04-16T11:30:00+05:30",
                "last_message": "Strategy 2 live scan complete.",
                "processed_signals": {},
                "signal_lane": [{"underlying": "SENSEX", "status": "trend-aligned"}],
                "meta": {"mode": "live_scan"},
                "recent_events": [],
                "positions": [],
                "portfolio": {},
            },
        },
    }

    monkeypatch.setattr(
        strategy_agent_module,
        "_load_saved_strategy_state_from_database",
        lambda: (payload, updated_at),
    )

    agent._state_synced_at = datetime(2026, 4, 16, 5, 0, tzinfo=UTC)
    changed = agent._refresh_state_from_store()

    assert changed is True
    assert agent._last_run_at == "2026-04-16T11:30:00+05:30"
    assert agent._strategy2.last_scan_at == "2026-04-16T11:30:00+05:30"
    assert agent._strategy2.meta["mode"] == "live_scan"
    assert agent._strategy2.signal_lane[0]["underlying"] == "SENSEX"


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
    agent._strategy1.meta = {}
    agent._strategy2.meta = {}
    agent._strategy2.signal_lane = [
        {"underlying": underlying, "spot_session_date": "2026-04-09", "signal_date": "2026-04-09"}
        for underlying in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX")
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


def test_run_once_uses_market_intelligence_health_when_paper_only(monkeypatch) -> None:
    agent = PaperStrategyAgent()
    broker_calls = {"count": 0}

    async def fake_broker_snapshot(*, force_validate: bool = False):
        broker_calls["count"] += 1
        return {"broker_ready": True}

    async def fake_market_intelligence_health():
        return {
            "ready": False,
            "watchlist_rows_today": 0,
            "latest_watchlist_time": None,
        }

    monkeypatch.setattr(strategy_agent_module.settings, "PAPER_TRADING_ONLY", True)
    monkeypatch.setattr(strategy_agent_module, "_in_market_hours", lambda _: True)
    monkeypatch.setattr(strategy_agent_module, "get_broker_connection_snapshot", fake_broker_snapshot)
    monkeypatch.setattr(
        strategy_agent_module.market_intelligence_runtime,
        "get_strategy_health",
        fake_market_intelligence_health,
    )

    status = asyncio.run(agent.run_once(force=False))

    assert broker_calls["count"] == 0
    assert "Shared market-intelligence data is not ready" in status["last_message"]
    assert status["data_health"]["market_intelligence"]["ready"] is False


def test_run_once_blocks_stale_market_intelligence_when_paper_only(monkeypatch) -> None:
    agent = PaperStrategyAgent()
    broker_calls = {"count": 0}

    async def fake_broker_snapshot(*, force_validate: bool = False):
        broker_calls["count"] += 1
        return {"broker_ready": True}

    async def fake_market_intelligence_health():
        return {
            "ready": True,
            "execution_ready": False,
            "readiness_mode": "latest_session",
            "execution_mode": "stale_latest_session",
            "watchlist_rows_today": 0,
            "watchlist_rows_latest": 171,
            "latest_watchlist_time": "2026-04-24T09:29:55.977752+00:00",
            "watchlist_age_seconds": 14 * 24 * 60 * 60,
            "max_execution_age_seconds": 36 * 60 * 60,
        }

    monkeypatch.setattr(strategy_agent_module.settings, "PAPER_TRADING_ONLY", True)
    monkeypatch.setattr(strategy_agent_module, "_in_market_hours", lambda _: True)
    monkeypatch.setattr(strategy_agent_module, "get_broker_connection_snapshot", fake_broker_snapshot)
    monkeypatch.setattr(
        strategy_agent_module.market_intelligence_runtime,
        "get_strategy_health",
        fake_market_intelligence_health,
    )

    status = asyncio.run(agent.run_once(force=False))

    assert broker_calls["count"] == 0
    assert "Shared market-intelligence data is stale" in status["last_message"]
    assert "execution mode=stale_latest_session" in status["last_message"]
    assert status["data_health"]["market_intelligence"]["ready"] is True
    assert status["data_health"]["market_intelligence"]["execution_ready"] is False
    assert status["strategies"][0]["meta"]["mode"] == "local_data_stale"


def test_manage_exits_ignores_first_ma20_pullback(monkeypatch) -> None:
    agent = PaperStrategyAgent()
    runtime = agent._strategy1
    runtime.positions.clear()
    position = StrategyPosition(
        symbol="OPT:AUROPHARMA:2026-04-30:1340:CE",
        underlying="AUROPHARMA",
        expiry="2026-04-30",
        strike=1340.0,
        option_type="CE",
        instrument_key="NSE_FO|123",
        trading_symbol="AUROPHARMA 1340 CE",
        qty=2500,
        initial_qty=2500,
        entry_price=80.0,
        current_price=99.0,
        peak_price=120.0,
        entry_bar_time="2026-04-09T09:15:00+05:30",
        entered_at="2026-04-09T09:15:00+05:30",
        signal_reason="macd_zero_cross",
        phase=PHASE_TRAILING,
        trailing_stop=90.0,
    )
    runtime.positions[position.symbol] = position

    start = datetime(2026, 4, 9, 9, 15, tzinfo=UTC)
    candles = [
        {"time": (start + timedelta(minutes=30 * index)).isoformat(), "close": 100.0}
        for index in range(20)
    ]
    candles[-1]["close"] = 99.0

    async def fake_load_candles(**_kwargs):
        return candles

    closed: list[str] = []

    async def fake_close_position(_runtime, _position, _exit_price, reason, **_kwargs):
        closed.append(reason)

    monkeypatch.setattr(strategy_agent_module.option_history_service, "load_candles", fake_load_candles)
    monkeypatch.setattr(agent, "_close_position", fake_close_position)

    asyncio.run(agent._manage_exits(runtime))

    assert closed == []
    assert position.first_pullback_ignored_at is not None


def test_manage_exits_closes_after_pullback_ignore_window(monkeypatch) -> None:
    agent = PaperStrategyAgent()
    runtime = agent._strategy1
    runtime.positions.clear()
    position = StrategyPosition(
        symbol="OPT:AUROPHARMA:2026-04-30:1340:CE",
        underlying="AUROPHARMA",
        expiry="2026-04-30",
        strike=1340.0,
        option_type="CE",
        instrument_key="NSE_FO|123",
        trading_symbol="AUROPHARMA 1340 CE",
        qty=2500,
        initial_qty=2500,
        entry_price=80.0,
        current_price=99.0,
        peak_price=120.0,
        entry_bar_time="2026-04-09T09:15:00+05:30",
        entered_at="2026-04-09T09:15:00+05:30",
        signal_reason="macd_zero_cross",
        phase=PHASE_TRAILING,
        trailing_stop=90.0,
    )
    runtime.positions[position.symbol] = position

    start = datetime(2026, 4, 9, 9, 15, tzinfo=UTC)
    candles = [
        {"time": (start + timedelta(minutes=30 * index)).isoformat(), "close": 100.0}
        for index in range(25)
    ]
    candles[-1]["close"] = 99.0

    async def fake_load_candles(**_kwargs):
        return candles

    closed: list[str] = []

    async def fake_close_position(_runtime, _position, _exit_price, reason, **_kwargs):
        closed.append(reason)

    monkeypatch.setattr(strategy_agent_module.option_history_service, "load_candles", fake_load_candles)
    monkeypatch.setattr(agent, "_close_position", fake_close_position)

    asyncio.run(agent._manage_exits(runtime))

    assert closed == ["ma20_pullback_exit"]


def test_load_candles_appends_newer_atm_watchlist_snapshot(monkeypatch) -> None:
    agent = PaperStrategyAgent()
    start = datetime(2026, 5, 6, 9, 15, tzinfo=UTC)

    async def fake_load_candles(**_kwargs):
        return [
            {"time": (start + timedelta(minutes=5 * index)).isoformat(), "close": 100.0 + index}
            for index in range(3)
        ]

    monkeypatch.setattr(strategy_agent_module.option_history_service, "load_candles", fake_load_candles)

    rows = asyncio.run(
        agent._load_candles(
            {"underlying": "BANKNIFTY", "expiry": "2026-05-26"},
            {
                "strike": 56400,
                "option_type": "CE",
                "instrument_key": "NSE_FO|67564",
                "ltp": 943.75,
                "volume": 312750,
                "as_of": (start + timedelta(minutes=30)).isoformat(),
            },
            interval="5minute",
            limit=96,
        )
    )

    assert datetime.fromisoformat(rows[-1]["time"]).timestamp() == pytest.approx(
        (start + timedelta(minutes=30)).timestamp()
    )
    assert rows[-1]["close"] == 943.75
    assert rows[-1]["source"] == "atm_watchlist_snapshot"
