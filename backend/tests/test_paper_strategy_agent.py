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
    _s2_engine_labels,
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


def test_detect_macd_zero_cross_ce_and_pe_use_same_up_cross_rule() -> None:
    # A10/D1 regression: PE previously used an inverted DOWN-cross. Each leg is
    # scored on its OWN premium, so CE and PE must use the SAME up-cross rule —
    # the detector's output is now independent of option_type.
    rising = [120.0 - (index * 1.2) for index in range(38)] + [80.0, 180.0]
    falling = [80.0 + (index * 1.2) for index in range(38)] + [180.0, 60.0]

    ce_up = detect_macd_zero_cross(rising, "CE")
    pe_up = detect_macd_zero_cross(rising, "PE")
    assert ce_up == pe_up
    assert ce_up[0] is True  # premium MACD crossing up → enter, both legs

    ce_down = detect_macd_zero_cross(falling, "CE")
    pe_down = detect_macd_zero_cross(falling, "PE")
    assert ce_down == pe_down
    assert pe_down[0] is False  # a down-cross must NOT trigger PE (the old bug)


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


def test_paper_portfolio_total_equity_includes_reserved_capital() -> None:
    portfolio = PaperPortfolio(initial_capital=1_000_000.0, session_id="equity")
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

    assert portfolio.available_capital < portfolio.initial_capital
    assert portfolio.total_equity == pytest.approx(portfolio.initial_capital)


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


def test_paper_portfolio_option_close_does_not_double_count_pnl(monkeypatch) -> None:
    monkeypatch.setattr("paper_engine.costs.PAPER_APPLY_COSTS", False)  # WS-1.4: gross double-count check
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


@pytest.mark.skip(reason="S2 lane (_run_strategy2) removed from registered agents 2026-06-02; captured underlyings are set by the S2 runner which no longer executes. S1's consumption of monthly-filtered native index rows is covered by test_run_once_s1_scans_only_monthly_index_rows.")
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


def test_run_once_s1_scans_only_monthly_index_rows(monkeypatch) -> None:
    """A3 regression: the native index (ex-S2) watchlist matrix returns both
    weekly and monthly composite rows, but S1 — the monthly physical-delivery
    lane — must scan ONLY the monthly track. A weekly index row leaking into
    S1's scan is the bug this locks out.
    """
    agent = PaperStrategyAgent()
    scanned_rows: dict[str, list[dict]] = {}

    async def fake_snapshot(*, force_validate: bool = False):
        return {
            "connected_brokers": ["fyers"], "upstox_ready": False, "fyers_ready": True,
            "broker_ready": True, "upstox_token_health": {"status": "disconnected"},
            "fyers_token_health": {"status": "valid"},
        }

    async def fake_active_windows(*, as_of: date | None = None):
        return []

    async def fake_scan_windows(*, as_of: date | None = None):
        # A stock scan window drives the S1 watchlist pipeline; the index rows
        # under test arrive via the native (ex-S2) matrix merge.
        return [{
            "underlying": "AUROPHARMA", "expiry": date(2026, 4, 28),
            "prev_expiry": date(2026, 3, 26), "window_start": date(2026, 3, 19),
            "window_end": date(2026, 4, 21), "window_state": "active",
        }]

    async def fake_expiries(_expiry: str | None = None, *, live_refresh: bool = False):
        return {"default_expiry": "2026-04-28", "monthly_expiry": "2026-04-28",
                "stock_monthly_expiry": "2026-04-28", "index_monthlies": {"NIFTY": "2026-04-28"}}

    async def fake_watchlist(expiry: str | None = None, symbols=None, *, live_refresh: bool = False):
        # Primary monthly scan returns the plain NIFTY monthly row.
        return {"rows": [{"underlying": "NIFTY", "kind": "INDEX", "expiry": "2026-04-28",
                          "spot_price": 24200.0, "ce": {"option_type": "CE"}, "pe": {"option_type": "PE"}}],
                "detail": None}

    async def fake_native_rows(_expiry_scope, *, live_refresh: bool = False):
        # The native matrix hands back BOTH tracks for NIFTY.
        return {
            "NIFTY:weekly": {"underlying": "NIFTY", "kind": "INDEX", "expiry": "2026-04-09",
                             "expiry_track": "weekly", "spot_price": 24200.0,
                             "ce": {"option_type": "CE"}, "pe": {"option_type": "PE"}},
            "NIFTY:monthly": {"underlying": "NIFTY", "kind": "INDEX", "expiry": "2026-04-28",
                              "expiry_track": "monthly", "spot_price": 24200.0,
                              "ce": {"option_type": "CE"}, "pe": {"option_type": "PE"}},
        }

    async def fake_scan_entries(runtime, rows, window_map):
        scanned_rows["rows"] = list(rows)
        runtime.last_message = f"Scanned {len(rows)} instruments."

    async def fake_manage_exits(_runtime, _rows=None):
        return None

    async def fake_bootstrap(**_kwargs):
        return {"status": "ready", "counts_after": {"keyed_rows": 100}}

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
    monkeypatch.setattr(agent, "_load_strategy2_native_watchlist_rows", fake_native_rows)
    monkeypatch.setattr(agent, "_manage_exits", fake_manage_exits)
    monkeypatch.setattr(agent, "_scan_entries", fake_scan_entries)
    monkeypatch.setattr(agent, "_status_with_risk_snapshot", fake_status)
    monkeypatch.setattr(agent, "_maybe_send_telegram_report", fake_async_noop)
    monkeypatch.setattr(agent, "_maybe_sync_spot_candles", fake_async_noop)
    monkeypatch.setattr(strategy_agent_module.option_history_service, "reset_health", lambda: None)
    monkeypatch.setattr(strategy_agent_module.option_history_service, "get_health_snapshot", lambda: {})

    for runtime in (agent._strategy1, agent._strategy2):
        runtime.portfolio.snapshot_equity = lambda: None
        runtime.portfolio.persist_equity_to_redis = fake_async_noop

    asyncio.run(agent.run_once(force=False))

    nifty_rows = [r for r in scanned_rows.get("rows", []) if r.get("underlying") == "NIFTY"]
    assert nifty_rows, "NIFTY should still reach the S1 scan"
    # No weekly-track row, and every NIFTY row is the monthly expiry.
    assert all(r.get("expiry_track", "monthly") == "monthly" for r in nifty_rows)
    assert all(str(r.get("expiry")) == "2026-04-28" for r in nifty_rows)


def test_get_status_exposes_next_scan_and_runtime_timestamps() -> None:
    agent = PaperStrategyAgent()
    agent._auto_run_enabled = True
    agent.scan_interval_seconds = 60
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
    # S2 (index_mp_strategy) was removed from the registered lanes 2026-06-02, so
    # get_status() now exposes only the S1 lane.
    assert len(status["strategies"]) == 1


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

    async def fake_snapshot_state(_rows, *, bucket_minutes: int = 30):
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


def test_strategy1_open_position_prefers_contract_lot_over_underlying_row(monkeypatch) -> None:
    agent = PaperStrategyAgent()
    runtime = agent._build_runtime("strategy1", "Strategy 1")

    async def fake_resolve_lot_size(**kwargs):
        assert kwargs["instrument_key"] == "BSE_FO|SENSEX_CE"
        return 20

    monkeypatch.setattr(strategy_entries_module.option_history_service, "resolve_lot_size", fake_resolve_lot_size)

    # The entry path is fail-closed against fo_contract_catalog (2026-08-04).
    # Stand in for the catalog so this test stays about lot-size precedence.
    async def fake_resolve_contract(**kwargs):
        assert float(kwargs["strike"]) == 75000.0
        return {
            "ok": True,
            "strike": 75000.0,
            "requested": 75000.0,
            "outcome": "exact",
            "reason": None,
            "instrument_key": "BSE_FO|SENSEX_CE",
            "trading_symbol": "SENSEX75000CE",
            # Deliberately None: the catalog must not be able to override the
            # contract lot resolved by option_history_service.
            "lot_size": None,
            "ladder": [75000.0],
        }

    monkeypatch.setattr(strategy_entries_module, "resolve_catalog_contract", fake_resolve_contract)

    candidate = {
        "row": {
            "underlying": "SENSEX",
            "expiry": "2026-05-29",
            "lot_size": 10,
        },
        "side": {
            "instrument_key": "BSE_FO|SENSEX_CE",
            "trading_symbol": "SENSEX75000CE",
            "strike": 75000.0,
        },
        "latest_close": 100.0,
        "opt_type": "CE",
        "latest_bar_time": "2026-05-19T10:00:00+05:30",
        "signal_key": "SENSEX:2026-05-29:75000:CE",
        "reason": "macd_zero_cross",
        "tte_days": 10,
        "spot_setup": "breakout",
        "quadrant": type("Quadrant", (), {"regime": "bullish"})(),
        "strength": 1.0,
        "fraction_override": 0.0,
    }

    asyncio.run(agent._open_position(runtime, candidate))

    position = next(iter(runtime.positions.values()))
    assert position.lot_size == 20
    assert position.qty == 20


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


def test_strategy2_signal_context_enters_when_mp_confirms_macd_above_zero(monkeypatch) -> None:
    agent = PaperStrategyAgent()
    started_at = datetime(2026, 4, 16, 10, 30, tzinfo=strategy_agent_module.IST)
    requested_intervals: list[str] = []

    def _candles(option_type: str) -> list[dict]:
        base = datetime(2026, 4, 16, 9, 15, tzinfo=strategy_agent_module.IST)
        start = 100.0 if option_type == "CE" else 90.0
        return [
            {
                "time": (base + timedelta(minutes=15 * index)).isoformat(),
                "open": start + index,
                "high": start + index,
                "low": start + index,
                "close": start + index,
                "volume": 1000 + index,
            }
            for index in range(40)
        ]

    async def fake_load_candles(_row, side, *, interval="5minute", limit=96):
        requested_intervals.append(interval)
        return _candles(side.get("option_type"))

    async def fake_spot_rows(_underlying, _started_at):
        base = datetime(2026, 4, 16, 9, 15, tzinfo=strategy_agent_module.IST)
        return (
            [
                {
                    "time": (base + timedelta(minutes=index)).isoformat(),
                    "open": 22000.0 + index,
                    "high": 22000.0 + index,
                    "low": 22000.0 + index,
                    "close": 22000.0 + index,
                    "volume": 1000,
                }
                for index in range(90)
            ],
            "test",
        )

    class _Profile:
        poc = 22050.0
        vah = 22075.0
        val = 22025.0

    def fake_strategy_macd(closes, *, symbol=None, timeframe="5minute", last_bar_time=None):
        assert timeframe == "15minute"
        if str(symbol).endswith(":CE"):
            return [0.25, 0.40], [0.1, 0.2], [0.15, 0.2]
        return [0.15, 0.20], [0.1, 0.15], [0.05, 0.05]

    monkeypatch.setattr(strategy_agent_module.settings, "NSE_STRATEGY_BYPASS_MARKET_PROFILE_GATE", False)
    monkeypatch.setattr(agent, "_load_candles", fake_load_candles)
    monkeypatch.setattr(agent, "_load_strategy2_spot_rows", fake_spot_rows)
    monkeypatch.setattr(strategy_agent_module.market_profile_builder, "build_profile_from_rows", lambda *args, **kwargs: _Profile())
    monkeypatch.setattr(agent, "_classify_strategy2_market_profile", lambda **kwargs: ("CE", "trend_up", "mp_trend_up"))
    monkeypatch.setattr(strategy_agent_module, "detect_macd_zero_cross", lambda *args, **kwargs: (False, None, None))
    monkeypatch.setattr(strategy_agent_module, "_strategy_macd", fake_strategy_macd)

    row = {
        "underlying": "NIFTY",
        "expiry": "2026-04-16",
        "spot_price": 22090.0,
        "ce": {"option_type": "CE", "ltp": 140.0},
        "pe": {"option_type": "PE", "ltp": 85.0},
    }

    context = asyncio.run(agent._build_strategy2_signal_context(row, started_at))

    assert requested_intervals == ["15minute", "15minute"]
    assert context["direction"] == "CE"
    assert context["can_enter"] is True
    assert context["entry_reason"] == "macd_above_zero"
    assert context["signal"]["status"] == "entry-ready"
    assert context["signal"]["entry_reason"] == "macd_above_zero"


@pytest.mark.skip(reason="S2 (index_mp_strategy) lane removed from registered agents 2026-06-02; get_status no longer exposes strategies[1].")
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

    async def fake_market_intelligence_health():
        return {"ready": False, "execution_ready": False}

    async def fake_broker_snapshot(*, force_validate: bool = False):
        return {"broker_ready": False, "fyers_ready": False, "upstox_ready": False}

    monkeypatch.setattr(strategy_agent_module, "_in_market_hours", lambda _: False)
    monkeypatch.setattr(strategy_agent_module.market_intelligence_runtime, "get_strategy_health", fake_market_intelligence_health)
    monkeypatch.setattr(strategy_agent_module, "get_broker_connection_snapshot", fake_broker_snapshot)

    status = asyncio.run(agent.run_once(force=False))

    assert status["last_message"].startswith("Market closed.")
    assert status["strategies"][0]["last_scan_at"]
    assert status["strategies"][1]["last_scan_at"]
    assert status["strategies"][1]["signals"][0]["underlying"] == "NIFTY"
    assert status["strategies"][1]["meta"]["mode"] in {"market_closed", "prepared_market_closed"}
    assert status["strategies"][1]["meta"]["market_state"] == "closed"


def test_closed_market_empty_watchlist_preserves_saved_strategy_state(monkeypatch) -> None:
    agent = PaperStrategyAgent()
    agent._last_run_at = "2026-05-08T15:29:54+05:30"
    agent._strategy1.meta = {
        "mode": "prepared_market_closed",
        "watchlist_rows": 171,
        "prepared_watchlist": [
            {
                "underlying": "NIFTY",
                "direction": "PE",
                "status": "watching",
                "freshness": "prepared",
            }
        ],
    }
    agent._strategy2.signal_lane = [
        {
            "underlying": "NIFTY",
            "status": "trend-aligned",
            "freshness": "session-close",
        }
    ]
    agent._strategy2.meta = {
        "mode": "historical_recovery",
        "pipeline": [{"name": "Strategy 2 NIFTY", "status": "ok", "rows": 45}],
        "watchlist_rows": 1,
    }

    async def fake_market_intelligence_health():
        return {
            "ready": True,
            "execution_ready": False,
            "watchlist_rows_today": 0,
            "watchlist_rows_latest": 171,
            "latest_watchlist_time": "2026-05-08T09:59:54+00:00",
        }

    async def fake_broker_snapshot(*, force_validate: bool = False):
        return {"broker_ready": False, "fyers_ready": False, "upstox_ready": False}

    async def fake_expiries(_expiry: str | None = None, *, live_refresh: bool = False):
        return {"default_expiry": "2026-05-26", "expiries": ["2026-05-26"]}

    async def fake_watchlist(*_args, **_kwargs):
        return {"rows": [], "detail": "holiday/offline"}

    async def fake_noop(*_args, **_kwargs):
        return None

    async def fake_empty_list(*_args, **_kwargs):
        return []

    monkeypatch.setattr(strategy_agent_module, "_in_market_hours", lambda _: False)
    monkeypatch.setattr(strategy_agent_module.market_intelligence_runtime, "get_strategy_health", fake_market_intelligence_health)
    monkeypatch.setattr(strategy_agent_module, "get_broker_connection_snapshot", fake_broker_snapshot)
    monkeypatch.setattr(strategy_agent_module.atm_watchlist_service, "get_expiries", fake_expiries)
    monkeypatch.setattr(strategy_agent_module.atm_watchlist_service, "get_watchlist", fake_watchlist)
    monkeypatch.setattr(strategy_agent_module, "get_all_active_windows", fake_empty_list)
    monkeypatch.setattr(strategy_agent_module, "get_all_strategy1_scan_windows", fake_empty_list)
    monkeypatch.setattr(agent, "ensure_recovered_state", fake_noop)
    monkeypatch.setattr(strategy_agent_module.option_history_service, "reset_health", lambda: None)
    monkeypatch.setattr(strategy_agent_module.option_history_service, "get_health_snapshot", lambda: {})

    status = asyncio.run(agent.run_once(force=False))

    assert "closed-market watchlist returned 0 rows" in status["last_message"]
    assert status["strategies"][0]["meta"]["prepared_watchlist"][0]["underlying"] == "NIFTY"
    assert status["strategies"][0]["meta"]["watchlist_rows"] == 171
    # S2 lane removed 2026-06-02 — status exposes only S1 now.
    assert len(status["strategies"]) == 1


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


def test_strategy_agent_reset_keeps_positions_dict_and_status_readable(monkeypatch, tmp_path) -> None:
    state_file = tmp_path / "nse_strategy_state.json"
    module_file = tmp_path / "paper_engine" / "strategy_agent.py"
    module_file.parent.mkdir(parents=True, exist_ok=True)
    module_file.write_text("")
    persisted: dict[str, object] = {}

    def fake_save_state(payload: dict[str, object]):
        persisted["payload"] = payload
        state_file.write_text("{}")
        return None

    async def fake_record_audit_event(**_kwargs):
        return None

    monkeypatch.setattr(strategy_agent_module, "__file__", str(module_file))
    monkeypatch.setattr(strategy_agent_module, "_NSE_STRATEGY_STATE_FILE", state_file)
    monkeypatch.setattr(strategy_agent_module, "_save_strategy_state", fake_save_state)
    monkeypatch.setattr(strategy_agent_module, "_load_saved_strategy_state", lambda: ({}, None))
    monkeypatch.setattr(strategy_agent_module, "_load_saved_strategy_state_from_database", lambda: (None, None))
    monkeypatch.setattr("agentic_rag.audit_agent.record_audit_event", fake_record_audit_event)

    agent = PaperStrategyAgent()
    runtime = agent._strategy1
    runtime.positions["OPT:NIFTY:2026-05-26:24000:CE"] = StrategyPosition(
        symbol="OPT:NIFTY:2026-05-26:24000:CE",
        underlying="NIFTY",
        expiry="2026-05-26",
        strike=24000.0,
        option_type="CE",
        instrument_key="NSE_FO|1",
        trading_symbol="NIFTY 24000 CE",
        qty=75,
        initial_qty=75,
        entry_price=100.0,
        current_price=95.0,
        peak_price=110.0,
        entry_bar_time="2026-05-11T10:00:00+05:30",
        entered_at="2026-05-11T10:00:00+05:30",
        signal_reason="test",
    )

    result = asyncio.run(agent.archive_and_reset_paper_account(actor="test"))
    status = agent.get_status(refresh=False)

    assert result["archived"] is True
    assert isinstance(agent._strategy1.positions, dict)
    assert isinstance(agent._strategy2.positions, dict)
    assert status["strategies"][0]["summary"]["open_positions"] == 0
    assert status["strategies"][0]["summary"]["total_equity"] == 1_000_000.0
    assert persisted["payload"]["strategies"]["macd_strategy"]["positions"] == []


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
    monkeypatch.setattr(strategy_agent_module, "_load_saved_strategy_state", lambda: ({}, None))
    monkeypatch.setattr(strategy_agent_module, "_load_saved_strategy_state_from_database", lambda: (None, None))

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

    restore_args: dict[str, object] = {}

    async def fake_restore_from_historical_state(
        *, latest_snapshot_day=None, latest_position_day=None, rebuild_strategy1=True
    ):
        restore_args["latest_snapshot_day"] = latest_snapshot_day
        restore_args["latest_position_day"] = latest_position_day
        restore_args["rebuild_strategy1"] = rebuild_strategy1

    monkeypatch.setattr(strategy_agent_module, "AsyncSessionLocal", lambda: FakeSession())
    monkeypatch.setattr(agent, "_restore_from_historical_state", fake_restore_from_historical_state)

    asyncio.run(agent.ensure_recovered_state())

    assert restore_args == {
        "latest_snapshot_day": date(2026, 4, 10),
        "latest_position_day": None,
        # A STALE S2 SIGNAL LANE MUST NOT REBUILD S1'S BOOK (2026-08-04). S2 is
        # what needs recovery here; S1's open book is untouched. Rebuilding it
        # from the `positions` journal is lossy and re-strands any leg whose
        # strike has rotated out of the ATM watchlist at its entry price.
        "rebuild_strategy1": False,
    }


def test_ensure_recovered_state_ignores_rows_before_paper_reset(monkeypatch) -> None:
    agent = PaperStrategyAgent()
    agent._last_run_at = None
    agent._last_paper_reset_at = "2026-05-11T20:00:00+05:30"

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
                return datetime(2026, 5, 11, 15, 20)
            return datetime(2026, 5, 11, 14, 40)

    called = False

    async def fake_restore_from_historical_state(*, latest_snapshot_day=None, latest_position_day=None):
        nonlocal called
        called = True

    monkeypatch.setattr(strategy_agent_module, "AsyncSessionLocal", lambda: FakeSession())
    monkeypatch.setattr(agent, "_restore_from_historical_state", fake_restore_from_historical_state)

    asyncio.run(agent.ensure_recovered_state())

    assert called is False


def test_persist_position_serializes_expiry_for_positions_table(monkeypatch) -> None:
    agent = PaperStrategyAgent()
    runtime = agent.get_runtime("macd_strategy")
    assert runtime is not None

    captured: dict[str, object] = {}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def execute(self, _query, params):
            captured.update(params)

        async def commit(self) -> None:
            captured["committed"] = True

    async def fake_ensure_session(_session, _runtime):
        return "paper-session-id"

    position = StrategyPosition(
        symbol="OPT:NIFTY:2026-05-26:24000:CE",
        underlying="NIFTY",
        expiry=date(2026, 5, 26),
        strike=24000.0,
        option_type="CE",
        instrument_key=None,
        trading_symbol=None,
        qty=65,
        initial_qty=65,
        entry_price=120.5,
        current_price=120.5,
        peak_price=120.5,
        entry_bar_time="2026-05-19T10:00:00+05:30",
        entered_at="2026-05-19T10:00:00+05:30",
        signal_reason="test",
    )

    import db.database as database_module

    monkeypatch.setattr(database_module, "AsyncSessionLocal", lambda: FakeSession())
    monkeypatch.setattr(agent, "_ensure_paper_session_record", fake_ensure_session)

    asyncio.run(agent._persist_position(runtime, position))

    assert captured["expiry"] == "2026-05-26"
    assert isinstance(captured["expiry"], str)
    assert captured["committed"] is True


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


@pytest.mark.skip(reason="ma20_pullback_exit was deleted from the exit cascade 2026-06-02 (proven destructive); this asserts the removed behavior.")
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

    async def fake_latest_quotes(_positions):
        return {}

    closed: list[str] = []

    async def fake_close_position(_runtime, _position, _exit_price, reason, **_kwargs):
        closed.append(reason)

    monkeypatch.setattr(strategy_agent_module.option_history_service, "load_candles", fake_load_candles)
    monkeypatch.setattr(agent, "_latest_position_quote_map", fake_latest_quotes)
    monkeypatch.setattr(agent, "_close_position", fake_close_position)

    asyncio.run(agent._manage_exits(runtime))

    assert closed == []
    assert position.first_pullback_ignored_at is not None


@pytest.mark.skip(reason="ma20_pullback_exit was deleted from the exit cascade 2026-06-02 (proven destructive); this asserts the removed behavior.")
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

    async def fake_latest_quotes(_positions):
        return {}

    closed: list[str] = []

    async def fake_close_position(_runtime, _position, _exit_price, reason, **_kwargs):
        closed.append(reason)

    monkeypatch.setattr(strategy_agent_module.option_history_service, "load_candles", fake_load_candles)
    monkeypatch.setattr(agent, "_latest_position_quote_map", fake_latest_quotes)
    monkeypatch.setattr(agent, "_close_position", fake_close_position)

    asyncio.run(agent._manage_exits(runtime))

    assert closed == ["ma20_pullback_exit"]


def test_manage_exits_uses_latest_contract_quote_for_mark(monkeypatch) -> None:
    # Pin the clock inside the test's own synthetic session (2026-05-20,
    # NSE open, quote 35s old). Without this the test is time-of-day flaky:
    # run during REAL market hours months later, the mark-staleness gate
    # (age > _MARK_STALE_SECONDS while the exchange is open) suppresses the
    # price-based hard_stop and the window_end backstop closes instead.
    from freezegun import freeze_time

    with freeze_time("2026-05-20T11:20:00+05:30"):
        _run_manage_exits_uses_latest_contract_quote_for_mark(monkeypatch)


def _run_manage_exits_uses_latest_contract_quote_for_mark(monkeypatch) -> None:
    agent = PaperStrategyAgent()
    runtime = agent._strategy1
    runtime.positions.clear()
    position = StrategyPosition(
        symbol="OPT:BANKNIFTY:2026-05-26:53000:PE",
        underlying="BANKNIFTY",
        expiry="2026-05-26",
        strike=53000.0,
        option_type="PE",
        instrument_key="NSE_FO|123",
        trading_symbol="BANKNIFTY 53000 PE",
        qty=150,
        initial_qty=150,
        entry_price=624.0,
        current_price=675.0,
        peak_price=675.0,
        entry_bar_time="2026-05-20T09:15:00+05:30",
        entered_at="2026-05-20T09:36:00+05:30",
        signal_reason="macd_zero_cross",
        phase="phase1",
    )
    runtime.positions[position.symbol] = position

    start = datetime(2026, 5, 20, 9, 15, tzinfo=UTC)
    candles = [
        {"time": (start + timedelta(minutes=30 * index)).isoformat(), "close": 640.0}
        for index in range(25)
    ]

    async def fake_load_candles(**_kwargs):
        return candles

    async def fake_latest_quotes(_positions):
        return {position.symbol: (460.0, "2026-05-20T11:19:25+05:30")}

    closed: list[tuple[str, float]] = []

    async def fake_close_position(_runtime, _position, exit_price, reason, **_kwargs):
        closed.append((reason, exit_price))

    monkeypatch.setattr(strategy_agent_module.option_history_service, "load_candles", fake_load_candles)
    monkeypatch.setattr(agent, "_latest_position_quote_map", fake_latest_quotes)
    monkeypatch.setattr(agent, "_close_position", fake_close_position)

    asyncio.run(agent._manage_exits(runtime))

    assert position.current_price == 460.0
    assert position.price_updated_at == "2026-05-20T11:19:25+05:30"
    assert closed == [("hard_stop", 460.0)]


def test_refresh_prices_from_watchlist_does_not_overwrite_newer_quote() -> None:
    agent = PaperStrategyAgent()
    runtime = agent._strategy1
    runtime.positions.clear()
    position = StrategyPosition(
        symbol="OPT:NIFTY:2026-05-26:23500:PE",
        underlying="NIFTY",
        expiry="2026-05-26",
        strike=23500.0,
        option_type="PE",
        instrument_key="NSE_FO|123",
        trading_symbol="NIFTY 23500 PE",
        qty=65,
        initial_qty=65,
        entry_price=190.0,
        current_price=172.0,
        peak_price=190.0,
        entry_bar_time="2026-05-20T13:30:00+05:30",
        entered_at="2026-05-20T13:31:00+05:30",
        signal_reason="macd_zero_cross",
        phase="phase1",
        price_updated_at="2026-05-20T13:40:58+05:30",
    )
    runtime.positions[position.symbol] = position

    agent._refresh_prices_from_watchlist(
        runtime,
        [
            {
                "underlying": "NIFTY",
                "time": "2026-05-20T13:31:00+05:30",
                "pe": {"ltp": 190.45, "time": "2026-05-20T13:31:00+05:30"},
            }
        ],
    )

    assert position.current_price == 172.0
    assert position.price_updated_at == "2026-05-20T13:40:58+05:30"


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


# ── Closed-market prep discipline (2026-07-16) ───────────────────────────────
# The own-loop used to run `_prepare_closed_market_state` on EVERY closed
# scan cycle: overnight that meant repeated live watchlist/expiry work, and
# after midnight the stale-row check kicked full-universe broker rebuilds
# hourly (observed 2026-07-16 00:00–05:30 IST, 216 underlyings/hour). The
# prep now runs once per closed stretch, with bounded retries while it
# yields no rows.


def _closed_market_agent(monkeypatch, prep_results: list[bool], prep_calls: list[int]):
    agent = PaperStrategyAgent()

    async def fake_market_intelligence_health():
        return {"ready": True, "execution_ready": False}

    async def fake_broker_snapshot(*, force_validate: bool = False):
        return {"broker_ready": False, "fyers_ready": False, "upstox_ready": False}

    async def fake_prepare(started_at, **_kwargs) -> bool:
        prep_calls.append(1)
        return prep_results[min(len(prep_calls), len(prep_results)) - 1]

    monkeypatch.setattr(strategy_agent_module, "_in_market_hours", lambda _: False)
    monkeypatch.setattr(
        strategy_agent_module.market_intelligence_runtime,
        "get_strategy_health",
        fake_market_intelligence_health,
    )
    monkeypatch.setattr(strategy_agent_module, "get_broker_connection_snapshot", fake_broker_snapshot)
    monkeypatch.setattr(agent, "_prepare_closed_market_state", fake_prepare)
    monkeypatch.setattr(strategy_agent_module.option_history_service, "reset_health", lambda: None)
    monkeypatch.setattr(strategy_agent_module.option_history_service, "get_health_snapshot", lambda: {})
    return agent


def test_closed_market_prep_runs_once_per_closed_stretch(monkeypatch) -> None:
    prep_calls: list[int] = []
    agent = _closed_market_agent(monkeypatch, prep_results=[True], prep_calls=prep_calls)

    for _ in range(4):
        asyncio.run(agent.run_once(force=False))

    assert len(prep_calls) == 1
    assert agent._closed_prep_done is True


def test_closed_market_prep_force_runs_again(monkeypatch) -> None:
    prep_calls: list[int] = []
    agent = _closed_market_agent(monkeypatch, prep_results=[True, True], prep_calls=prep_calls)

    asyncio.run(agent.run_once(force=False))
    asyncio.run(agent.run_once(force=False))
    assert len(prep_calls) == 1

    # Manual "run now" always re-preps.
    asyncio.run(agent.run_once(force=True))
    assert len(prep_calls) == 2


def test_closed_market_prep_empty_watchlist_retries_are_bounded(monkeypatch) -> None:
    from paper_engine.strategy_agent import CLOSED_PREP_MAX_ATTEMPTS

    prep_calls: list[int] = []
    agent = _closed_market_agent(
        monkeypatch,
        prep_results=[False],  # every prep yields 0 rows
        prep_calls=prep_calls,
    )

    for _ in range(CLOSED_PREP_MAX_ATTEMPTS + 4):
        asyncio.run(agent.run_once(force=False))

    assert len(prep_calls) == CLOSED_PREP_MAX_ATTEMPTS
    assert agent._closed_prep_done is False


# ── S2 engine attribution honesty (P1: identity changes silently) ────────────

def test_s2_engine_labels_mp_of_active() -> None:
    labels = _s2_engine_labels(True, {"signal": "BUY", "entry_style": "open_drive"})
    assert labels["strategy_id"] == "s2_mp_of"
    assert labels["strategy_version"] == "2.0"
    assert labels["engine"] == "mp_of"
    assert labels["fallback_reason"] is None


def test_s2_engine_labels_named_fallback_when_mp_silent() -> None:
    # MP+OF silent (insufficient history) → labelled MACD fallback, NOT MP_OF.
    labels = _s2_engine_labels(False, {"signal": None, "reason": "insufficient_1m_spot"})
    assert labels["strategy_id"] == "s2_macd_fallback"
    assert labels["strategy_version"] == "1.0"
    assert labels["engine"] == "macd_fallback"
    assert labels["fallback_reason"] == "insufficient_1m_spot"


def test_s2_engine_labels_blocked() -> None:
    labels = _s2_engine_labels(False, {"signal": None, "reason": "no_session_rows"}, blocked=True)
    assert labels["strategy_id"] == "s2_blocked"
    assert labels["engine"] == "blocked"
    assert labels["fallback_reason"] == "no_session_rows"


def test_s2_engine_labels_blocked_defaults_reason_when_absent() -> None:
    labels = _s2_engine_labels(False, {}, blocked=True)
    assert labels["strategy_id"] == "s2_blocked"
    assert labels["fallback_reason"] == "mp_of_insufficient_history"


def test_s2_capability_gate_skips_unsupported_symbol(monkeypatch) -> None:
    """The S2 request matrix must fail closed on an unrouted symbol (report it
    as unresolved) instead of silently defaulting it to a monthly expiry."""
    import paper_engine.strategy_agent as sa
    import paper_engine.strategy2_mp_of as s2

    agent = PaperStrategyAgent()
    requested: list[list[str]] = []

    # Universe carries one supported (NIFTY) and one unrouted (BANKEX) symbol.
    monkeypatch.setattr(sa, "STRATEGY2_UNDERLYINGS", ("NIFTY", "BANKEX"))

    async def _fake_inputs(_underlying):
        return {}  # forces the legacy resolver path for supported symbols

    async def _fake_watchlist(*, expiry=None, symbols=None, live_refresh=False):
        requested.append(list(symbols or []))
        return {"rows": [], "detail": None}

    monkeypatch.setattr(s2, "load_s2_expiry_inputs", _fake_inputs)
    monkeypatch.setattr(sa.atm_watchlist_service, "get_watchlist", _fake_watchlist)

    scope = {"index_monthlies": {"NIFTY": "2026-04-28"}}
    asyncio.run(agent._load_strategy2_native_watchlist_rows(scope, live_refresh=False))

    # BANKEX is reported unresolved and never requested; NIFTY still resolves.
    assert agent._strategy2_unresolved == [{"underlying": "BANKEX", "reason": "unsupported_s2_symbol"}]
    assert all("BANKEX" not in syms for syms in requested)
    assert any("NIFTY" in syms for syms in requested)


# ══════════════════════════════════════════════════════════════════════════
# forced_expiry_roll_2td — compulsory closure of a HELD position (2026-07-21)
#
# Owner rule: a position may be held past the 5TD watchlist roll, but only
# until <= 2 TRADING days remain to expiry. Wired as the LAST rung of the
# EXISTING cascade, behind window_end, so no existing exit changes attribution.
# ══════════════════════════════════════════════════════════════════════════
def _forced_close_fixture(monkeypatch, *, today, window_end):
    """A flat, un-stale S1 position on the 2026-07-28 expiry."""
    import paper_engine.strategy_agent_exits as exits_module

    agent = PaperStrategyAgent()
    runtime = agent._strategy1
    runtime.positions.clear()
    position = StrategyPosition(
        symbol="OPT:RELIANCE:2026-07-28:1400:CE",
        underlying="RELIANCE",
        expiry="2026-07-28",
        strike=1400.0,
        option_type="CE",
        instrument_key="NSE_FO|999",
        trading_symbol="RELIANCE 1400 CE",
        qty=500,
        initial_qty=500,
        entry_price=100.0,
        current_price=100.0,
        peak_price=100.0,
        entry_bar_time="2026-07-06T09:15:00+05:30",
        entered_at="2026-07-06T09:15:00+05:30",
        signal_reason="macd_zero_cross",
        window_end=window_end,
    )
    runtime.positions[position.symbol] = position

    start = datetime(2026, 7, 6, 9, 15, tzinfo=UTC)
    candles = [
        {"time": (start + timedelta(minutes=30 * index)).isoformat(), "close": 100.0}
        for index in range(20)
    ]

    async def fake_load_candles(**_kwargs):
        return candles

    async def fake_latest_quotes(_positions):
        return {}

    closed: list[str] = []

    async def fake_close_position(_runtime, _position, _exit_price, reason, **_kwargs):
        closed.append(reason)

    monkeypatch.setattr(strategy_agent_module.option_history_service, "load_candles", fake_load_candles)
    monkeypatch.setattr(agent, "_latest_position_quote_map", fake_latest_quotes)
    monkeypatch.setattr(agent, "_close_position", fake_close_position)
    monkeypatch.setattr(
        exits_module, "_now_ist", lambda: datetime.combine(today, datetime.min.time()).replace(
            hour=10, tzinfo=timezone(timedelta(hours=5, minutes=30))
        )
    )
    return agent, runtime, closed


def _set_forced_close_flags(monkeypatch, enabled: bool) -> None:
    from core.config import settings

    monkeypatch.setattr(settings, "EXPIRY_POLICY_ENABLED", enabled, raising=False)
    monkeypatch.setattr(settings, "EXPIRY_POLICY_FORCED_CLOSE_ENABLED", enabled, raising=False)


def test_forced_expiry_closure_fires_at_the_2td_boundary(monkeypatch) -> None:
    """2026-07-24 is 2 trading days from the 2026-07-28 expiry. window_end is
    deliberately set LATER so the new rung is the binding one and can be seen."""
    _set_forced_close_flags(monkeypatch, True)
    agent, runtime, closed = _forced_close_fixture(
        monkeypatch, today=date(2026, 7, 24), window_end="2026-07-27"
    )
    asyncio.run(agent._manage_exits(runtime))
    assert closed == ["forced_expiry_roll_2td"]


def test_forced_expiry_closure_does_not_fire_at_3td(monkeypatch) -> None:
    _set_forced_close_flags(monkeypatch, True)
    agent, runtime, closed = _forced_close_fixture(
        monkeypatch, today=date(2026, 7, 23), window_end="2026-07-27"
    )
    asyncio.run(agent._manage_exits(runtime))
    assert closed == []


def test_forced_expiry_closure_is_inert_with_the_flags_off(monkeypatch) -> None:
    """Byte-identical with the flags down: the same position on the same date
    produces NO exit at all."""
    _set_forced_close_flags(monkeypatch, False)
    agent, runtime, closed = _forced_close_fixture(
        monkeypatch, today=date(2026, 7, 24), window_end="2026-07-27"
    )
    asyncio.run(agent._manage_exits(runtime))
    assert closed == []


def test_window_end_keeps_its_attribution_when_both_would_fire(monkeypatch) -> None:
    """window_end (expiry − 7 calendar days) is STRICTLY EARLIER than the 2TD
    boundary at today's settings, so it fires first and keeps attribution. The
    new rung is a BACKSTOP; it must never re-label an existing exit."""
    _set_forced_close_flags(monkeypatch, True)
    agent, runtime, closed = _forced_close_fixture(
        monkeypatch, today=date(2026, 7, 24), window_end="2026-07-21"
    )
    asyncio.run(agent._manage_exits(runtime))
    assert closed == ["window_end"]


def test_forced_expiry_closure_cannot_double_close(monkeypatch) -> None:
    """The real _close_position pops the symbol from runtime.positions and
    returns early if it is already gone — so a second cycle (or a racing normal
    exit) cannot book the same position twice."""
    _set_forced_close_flags(monkeypatch, True)
    agent, runtime, _closed = _forced_close_fixture(
        monkeypatch, today=date(2026, 7, 24), window_end="2026-07-27"
    )
    booked: list[str] = []

    async def counting_close(rt, position, price, reason, qty=None, partial=False):
        if position.symbol not in rt.positions:
            return
        rt.positions.pop(position.symbol, None)
        booked.append(reason)

    monkeypatch.setattr(agent, "_close_position", counting_close)
    asyncio.run(agent._manage_exits(runtime))
    asyncio.run(agent._manage_exits(runtime))
    assert booked == ["forced_expiry_roll_2td"]
    assert runtime.positions == {}


def test_index_position_is_not_force_closed_by_the_stock_rule(monkeypatch) -> None:
    """Cash-settled: no delivery risk, separate knob, default 0 = disabled."""
    _set_forced_close_flags(monkeypatch, True)
    agent, runtime, closed = _forced_close_fixture(
        monkeypatch, today=date(2026, 7, 24), window_end="2026-07-27"
    )
    position = next(iter(runtime.positions.values()))
    position.underlying = "NIFTY"
    asyncio.run(agent._manage_exits(runtime))
    assert closed == []


# ── Fail-closed contract guard (2026-08-04) ──────────────────────────────────
#
# ITC 287.5 PE entered the S1 book keyed OPT:ITC:2026-08-25:288:PE. 288 is not
# on ITC's 2.5-wide ladder, so the leg could never resolve to a tradeable
# symbol: NULL instrument_key, price frozen at entry, exactly 0.0 unrealized
# P&L from 2026-07-28, and no price-based exit reachable — ~Rs 71.6k of phantom
# notional. No leg may open unless fo_contract_catalog lists it.


def _guard_candidate(strike: float) -> dict:
    return {
        "row": {"underlying": "ITC", "expiry": "2026-08-25", "lot_size": 1725},
        "side": {"instrument_key": None, "trading_symbol": None, "strike": strike},
        "latest_close": 8.30,
        "opt_type": "PE",
        "latest_bar_time": "2026-07-28T12:05:00+05:30",
        "signal_key": "ITC:2026-08-25:PE",
        "reason": "macd_zero_cross",
        "tte_days": 28,
        "spot_setup": "reversal",
        "quadrant": type("Quadrant", (), {"regime": "bearish"})(),
        "strength": 1.0,
        "fraction_override": 0.0,
    }


def _stub_catalog(monkeypatch, verdict: dict) -> None:
    async def _fake(**_kwargs):
        return verdict

    monkeypatch.setattr(strategy_entries_module, "resolve_catalog_contract", _fake)


def test_open_position_refuses_strike_absent_from_catalog(monkeypatch) -> None:
    agent = PaperStrategyAgent()
    runtime = agent._build_runtime("strategy1", "Strategy 1")
    _stub_catalog(
        monkeypatch,
        {
            "ok": False,
            "strike": None,
            "requested": 288.0,
            "outcome": "off_ladder",
            "reason": "strike_not_in_catalog",
            "instrument_key": None,
            "trading_symbol": None,
            "lot_size": None,
            "ladder": [285.0, 287.5, 290.0],
        },
    )

    opened = asyncio.run(agent._open_position(runtime, _guard_candidate(288.0)))

    assert opened is False
    assert runtime.positions == {}
    reasons = (runtime.last_run_summary or {}).get("blocked_reasons", {})
    assert any("strike_not_in_catalog" in key for key in reasons)


def test_open_position_refuses_when_catalog_is_unreachable(monkeypatch) -> None:
    # A catalog outage must also fail closed — an unverifiable contract is
    # exactly the one that strands notional.
    agent = PaperStrategyAgent()
    runtime = agent._build_runtime("strategy1", "Strategy 1")
    _stub_catalog(
        monkeypatch,
        {
            "ok": False,
            "strike": None,
            "requested": 287.5,
            "outcome": "no_ladder",
            "reason": "catalog_unavailable",
            "instrument_key": None,
            "trading_symbol": None,
            "lot_size": None,
            "ladder": [],
        },
    )

    assert asyncio.run(agent._open_position(runtime, _guard_candidate(287.5))) is False
    assert runtime.positions == {}


def test_open_position_snaps_to_catalog_rung_and_takes_its_identity(monkeypatch) -> None:
    agent = PaperStrategyAgent()
    runtime = agent._build_runtime("strategy1", "Strategy 1")
    _stub_catalog(
        monkeypatch,
        {
            "ok": True,
            "strike": 287.5,
            "requested": 288.0,
            "outcome": "snapped",
            "reason": None,
            "instrument_key": "NSE_FO|117951",
            "trading_symbol": "ITC 287.5 PE 25 AUG 26",
            "lot_size": 1725,
            "ladder": [285.0, 287.5, 290.0],
        },
    )

    opened = asyncio.run(agent._open_position(runtime, _guard_candidate(288.0)))

    assert opened is True
    position = runtime.positions["OPT:ITC:2026-08-25:287.5:PE"]
    assert position.strike == 287.5
    # The identity that makes the leg priceable must come from the catalog.
    assert position.instrument_key == "NSE_FO|117951"
    assert position.trading_symbol == "ITC 287.5 PE 25 AUG 26"
