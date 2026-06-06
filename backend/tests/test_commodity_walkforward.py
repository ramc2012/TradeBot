"""Smoke tests for analysis/commodity_walkforward.py.

Guards against the regression where the module ImportError'd on load (it imported
``evaluate_commodity_signal``, which no longer exists — the real signal is
``evaluate_commodity_mp_signal`` with a different signature). These tests build the
MP profile / CVD-anchor / ATR arguments from synthetic candles and confirm both the
artifact runner and the harness-ready R-multiple backtest import and run.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

IST = ZoneInfo("Asia/Kolkata")
SYMBOL = "MCX:GOLD26JUNFUT"


def _synthetic_rows(days=(1, 2, 3), per_session=220, start=70000.0):
    """Trending+oscillating 1-minute MCX-style candles across several sessions."""
    rows = []
    price = start
    for day in days:
        base = datetime(2026, 6, day, 9, 0, tzinfo=IST)
        for i in range(per_session):
            price += math.sin(i / 8.0) * 30 + (15 if i < 70 else -10)
            t = base + timedelta(minutes=i)
            rows.append(
                {
                    "time": t.isoformat(),
                    "open": price - 5,
                    "high": price + 14,
                    "low": price - 14,
                    "close": price,
                    "volume": 100 + i,
                    "oi": 0,
                }
            )
    return rows


def test_module_imports():
    """The module must import without ImportError (the original bug)."""
    import analysis.commodity_walkforward as W  # noqa: F401

    assert hasattr(W, "evaluate_commodity_mp_signal")
    assert hasattr(W, "simulate_signal_backtest")
    assert hasattr(W, "CommodityFuturesWalkForwardRunner")


def test_evaluate_mp_runs_on_synthetic_candles():
    import analysis.commodity_walkforward as W
    from paper_engine.commodity_strategy_agent import CommodityStrategyAgent

    rows = W.CommodityFuturesWalkForwardRunner._normalize_rows(_synthetic_rows())
    agent = CommodityStrategyAgent()
    analysis = W._evaluate_mp(agent, symbol=SYMBOL, rows=rows, index=len(rows) - 1, prior_cache={})
    # The MP signal row shape the live agent consumes.
    for key in ("signal", "entry_style", "confidence", "atr", "mp_poc", "mp_vah", "mp_val"):
        assert key in analysis
    assert analysis.get("signal") in (None, "BUY", "SELL")


def test_simulate_signal_backtest_returns_harness_shape():
    import analysis.commodity_walkforward as W

    result = W.simulate_signal_backtest(_synthetic_rows(), symbol=SYMBOL)
    assert set(result) >= {"events", "summary"}
    assert isinstance(result["events"], list)
    assert result["summary"]["trades"] == len(result["events"])
    for event in result["events"]:
        assert "r_multiple" in event and "exit_time" in event
        assert math.isfinite(float(event["r_multiple"]))


def test_runner_simulate_symbol_runs():
    import analysis.commodity_walkforward as W

    runner = W.CommodityFuturesWalkForwardRunner(symbols=[SYMBOL], lookback_days=21)
    trades = runner._simulate_symbol(SYMBOL, runner._normalize_rows(_synthetic_rows()))
    assert isinstance(trades, list)
    for trade in trades:
        assert trade["action"] in ("BUY", "SELL")
        assert "exit_reason" in trade and "return_pct" in trade


def test_too_short_history_is_safe():
    import analysis.commodity_walkforward as W

    short = _synthetic_rows(days=(1,), per_session=20)
    assert W.simulate_signal_backtest(short, symbol=SYMBOL)["summary"]["trades"] == 0
    runner = W.CommodityFuturesWalkForwardRunner(symbols=[SYMBOL])
    assert runner._simulate_symbol(SYMBOL, runner._normalize_rows(short)) == []
