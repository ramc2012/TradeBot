from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from core.market_hours_paper_supervisor import MarketHoursPaperSupervisor, RunnerConfig
from paper_engine.base_strategy_agent import IST


def test_market_hours_supervisor_runs_due_runners_only_once_per_interval() -> None:
    now = datetime(2026, 4, 21, 9, 20, tzinfo=IST)
    calls: list[str] = []

    async def _runner() -> dict[str, object]:
        calls.append("auction")
        return {"result_count": 1, "symbols_completed": ["NIFTY"]}

    supervisor = MarketHoursPaperSupervisor(
        enabled=True,
        runners=[
            RunnerConfig(
                key="auction_intelligence",
                label="Auction",
                interval_seconds=60,
                callback=_runner,
            )
        ],
        now_fn=lambda: now,
        market_hours_fn=lambda current: True,
        next_open_fn=lambda current: current + timedelta(days=1),
    )

    first = asyncio.run(supervisor.run_due_once())
    second = asyncio.run(supervisor.run_due_once())

    assert calls == ["auction"]
    runner = first["runners"]["auction_intelligence"]
    assert runner["last_success_at"] is not None
    assert runner["last_error"] is None
    assert second["runners"]["auction_intelligence"]["last_success_at"] == runner["last_success_at"]


def test_market_hours_supervisor_stays_armed_when_market_is_closed() -> None:
    now = datetime(2026, 4, 20, 8, 0, tzinfo=IST)
    calls: list[str] = []

    async def _runner() -> dict[str, object]:
        calls.append("fmp")
        return {"result_count": 1}

    next_open = datetime(2026, 4, 21, 9, 15, tzinfo=IST)
    supervisor = MarketHoursPaperSupervisor(
        enabled=True,
        runners=[
            RunnerConfig(
                key="fractal_market_profile",
                label="FMP",
                interval_seconds=120,
                callback=_runner,
            )
        ],
        now_fn=lambda: now,
        market_hours_fn=lambda current: False,
        next_open_fn=lambda current: next_open,
    )

    status = asyncio.run(supervisor.run_due_once())

    assert calls == []
    assert status["market_open"] is False
    runner = status["runners"]["fractal_market_profile"]
    assert runner["next_run_at"] == next_open.isoformat()
    assert runner["last_message"] == "Armed for the next market session."


def test_market_hours_supervisor_runs_post_close_catchup_once_per_session() -> None:
    now = datetime(2026, 4, 20, 16, 0, tzinfo=IST)
    calls: list[str] = []

    async def _runner() -> dict[str, object]:
        calls.append("auction")
        return {"result_count": 1, "symbols_completed": ["NIFTY"]}

    next_open = datetime(2026, 4, 21, 9, 15, tzinfo=IST)
    supervisor = MarketHoursPaperSupervisor(
        enabled=True,
        runners=[
            RunnerConfig(
                key="auction_intelligence",
                label="Auction",
                interval_seconds=60,
                callback=_runner,
            )
        ],
        now_fn=lambda: now,
        market_hours_fn=lambda current: False,
        next_open_fn=lambda current: next_open,
    )

    first = asyncio.run(supervisor.run_due_once())
    second = asyncio.run(supervisor.run_due_once())

    assert calls == ["auction"]
    runner = first["runners"]["auction_intelligence"]
    assert runner["last_success_at"] is not None
    assert runner["last_result_meta"]["catchup_session_date"] == "2026-04-20"
    assert "Catch-up captured for 2026-04-20." in runner["last_message"]
    assert second["runners"]["auction_intelligence"]["last_success_at"] == runner["last_success_at"]


def test_runner_specific_market_hours_can_run_when_primary_market_closed() -> None:
    now = datetime(2026, 4, 20, 22, 0, tzinfo=IST)
    calls: list[str] = []

    async def _runner() -> dict[str, object]:
        calls.append("gann")
        return {"result_count": 1}

    next_primary_open = datetime(2026, 4, 21, 9, 15, tzinfo=IST)
    supervisor = MarketHoursPaperSupervisor(
        enabled=True,
        runners=[
            RunnerConfig(
                key="gann_tp_delta",
                label="Gann",
                interval_seconds=60,
                callback=_runner,
                market_hours_fn=lambda current: True,
                next_open_fn=lambda current: current,
            )
        ],
        now_fn=lambda: now,
        market_hours_fn=lambda current: False,
        next_open_fn=lambda current: next_primary_open,
    )

    status = asyncio.run(supervisor.run_due_once())

    assert calls == ["gann"]
    assert status["market_open"] is False
    assert status["any_runner_market_open"] is True
    runner = status["runners"]["gann_tp_delta"]
    assert runner["last_success_at"] is not None
