from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import paper_engine.strategy_agent as strategy_module
from paper_engine.portfolio import PaperPortfolio
from paper_engine.strategy_agent import PaperStrategyAgent, detect_greeks_signal, detect_macd_zero_cross


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


def test_run_once_surfaces_degraded_broker_data(monkeypatch) -> None:
    agent = PaperStrategyAgent()

    async def fake_snapshot(*, force_validate: bool) -> dict:
        return {"fyers_ready": True, "upstox_ready": True}

    async def fake_windows(*, as_of):
        return [{"underlying": "RELIANCE", "expiry": "2026-03-26", "window_end": "2026-03-25"}]

    async def fake_watchlist(expiry: str) -> dict:
        return {
            "rows": [{"underlying": "RELIANCE", "expiry": expiry, "ce": {}, "pe": {}}],
            "detail": "Live watchlist data failed for 2 symbols: SBIN, TCS.",
        }

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(agent, "_get_broker_snapshot", fake_snapshot)
    monkeypatch.setattr(strategy_module, "get_all_active_windows", fake_windows)
    monkeypatch.setattr(strategy_module.atm_watchlist_service, "get_watchlist", fake_watchlist)
    monkeypatch.setattr(strategy_module.option_history_service, "reset_health", lambda: None)
    monkeypatch.setattr(
        strategy_module.option_history_service,
        "get_health_snapshot",
        lambda: {
            "issue_count": 1,
            "issues": [
                {
                    "broker": "upstox",
                    "instrument_key": "NSE_FO|TEST",
                    "message": "Upstox returned no historical candles.",
                }
            ],
        },
    )
    monkeypatch.setattr(agent, "_manage_exits", noop)
    monkeypatch.setattr(agent, "_scan_entries", noop)
    monkeypatch.setattr(agent, "_maybe_send_telegram_report", noop)

    status = asyncio.run(agent.run_once(force=True))

    assert "Broker data degraded." in str(status["last_message"])
    assert any(
        "Live watchlist data failed for 2 symbols" in entry["message"]
        for entry in status["commentary"]
    )
