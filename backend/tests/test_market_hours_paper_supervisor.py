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


def test_default_auction_runner_never_runs_post_close_catchup() -> None:
    supervisor = MarketHoursPaperSupervisor(enabled=False)
    runner = supervisor._runners["auction_intelligence"].config

    assert runner.post_close_catchup is False
    assert runner.market_hours_fn is not None
    assert runner.next_open_fn is not None
    assert runner.timeout_seconds == 600.0


def test_default_institutional_convergence_runner_is_shadow_session_only() -> None:
    supervisor = MarketHoursPaperSupervisor(enabled=False)
    runner = supervisor._runners["institutional_convergence"].config

    assert runner.post_close_catchup is False
    assert runner.market_hours_fn is not None
    assert runner.next_open_fn is not None
    assert runner.timeout_seconds == 600.0


def test_market_hours_supervisor_treats_reported_error_as_failure() -> None:
    now = datetime(2026, 4, 21, 9, 20, tzinfo=IST)

    async def _runner() -> dict[str, object]:
        return {
            "status": "error",
            "message": "MACD Refined storage unavailable",
            "result_count": 0,
            "failure_count": 1,
        }

    supervisor = MarketHoursPaperSupervisor(
        enabled=True,
        runners=[
            RunnerConfig(
                key="macd_refined",
                label="MACD Refined",
                interval_seconds=60,
                callback=_runner,
            )
        ],
        now_fn=lambda: now,
        market_hours_fn=lambda _current: True,
        next_open_fn=lambda current: current + timedelta(days=1),
    )

    status = asyncio.run(supervisor.run_due_once())
    runner = status["runners"]["macd_refined"]

    assert runner["last_success_at"] is None
    assert runner["last_error"] == "MACD Refined storage unavailable"
    assert runner["last_result_meta"]["status"] == "error"


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


def test_background_scheduler_does_not_let_slow_lane_starve_fast_lane() -> None:
    async def _scenario() -> None:
        now = datetime(2026, 4, 21, 9, 20, tzinfo=IST)
        release_slow = asyncio.Event()
        fast_finished = asyncio.Event()

        async def _slow() -> dict[str, object]:
            await release_slow.wait()
            return {"result_count": 1}

        async def _fast() -> dict[str, object]:
            fast_finished.set()
            return {"result_count": 1}

        supervisor = MarketHoursPaperSupervisor(
            enabled=True,
            runners=[
                RunnerConfig(key="slow", label="Slow", interval_seconds=60, callback=_slow),
                RunnerConfig(key="fast", label="Fast", interval_seconds=60, callback=_fast),
            ],
            now_fn=lambda: now,
            market_hours_fn=lambda _current: True,
            next_open_fn=lambda current: current + timedelta(days=1),
        )

        await supervisor._schedule_due_once()
        running_tasks = list(supervisor._runner_tasks.values())
        await asyncio.wait_for(fast_finished.wait(), timeout=0.2)
        assert supervisor.get_status()["runners"]["slow"]["running"] is True
        assert supervisor.get_status()["runners"]["fast"]["last_success_at"] is not None

        release_slow.set()
        await asyncio.gather(*running_tasks)

    asyncio.run(_scenario())
