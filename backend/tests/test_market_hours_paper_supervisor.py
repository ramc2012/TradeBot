from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta

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


def test_open_stagger_delays_first_start_after_open() -> None:
    clock = {"now": datetime(2026, 4, 21, 9, 15, tzinfo=IST)}
    calls: list[str] = []

    async def _runner() -> dict[str, object]:
        calls.append("auction")
        return {"result_count": 1}

    supervisor = MarketHoursPaperSupervisor(
        enabled=True,
        runners=[
            RunnerConfig(
                key="auction_intelligence",
                label="Auction",
                interval_seconds=60,
                callback=_runner,
                start_offset_seconds=90.0,
            )
        ],
        now_fn=lambda: clock["now"],
        market_hours_fn=lambda _current: True,
        next_open_fn=lambda current: current + timedelta(days=1),
    )

    # First pass AT the open stamps market_open_since and holds the lane.
    status = asyncio.run(supervisor.run_due_once())
    assert calls == []
    runner = status["runners"]["auction_intelligence"]
    assert runner["market_open_since"] == clock["now"].isoformat()
    assert runner["next_run_at"] == (clock["now"] + timedelta(seconds=90)).isoformat()
    assert runner["stale"] is False

    # Still inside the stagger window → still held.
    clock["now"] += timedelta(seconds=60)
    asyncio.run(supervisor.run_due_once())
    assert calls == []

    # Past the offset → the lane starts.
    clock["now"] += timedelta(seconds=31)
    asyncio.run(supervisor.run_due_once())
    assert calls == ["auction"]


def test_no_start_before_guard_holds_lane_until_wall_clock() -> None:
    clock = {"now": datetime(2026, 4, 21, 9, 20, tzinfo=IST)}
    calls: list[str] = []

    async def _runner() -> dict[str, object]:
        calls.append("macd")
        return {"result_count": 1}

    supervisor = MarketHoursPaperSupervisor(
        enabled=True,
        runners=[
            RunnerConfig(
                key="macd_refined",
                label="MACD Refined",
                interval_seconds=1800,
                callback=_runner,
                no_start_before=time(9, 45),
            )
        ],
        now_fn=lambda: clock["now"],
        market_hours_fn=lambda _current: True,
        next_open_fn=lambda current: current + timedelta(days=1),
    )

    status = asyncio.run(supervisor.run_due_once())
    assert calls == []
    runner = status["runners"]["macd_refined"]
    assert runner["next_run_at"] == clock["now"].replace(hour=9, minute=45, second=0, microsecond=0).isoformat()

    clock["now"] = clock["now"].replace(hour=9, minute=46)
    asyncio.run(supervisor.run_due_once())
    assert calls == ["macd"]


def test_force_run_bypasses_open_stagger() -> None:
    now = datetime(2026, 4, 21, 9, 15, tzinfo=IST)
    calls: list[str] = []

    async def _runner() -> dict[str, object]:
        calls.append("auction")
        return {"result_count": 1}

    supervisor = MarketHoursPaperSupervisor(
        enabled=True,
        runners=[
            RunnerConfig(
                key="auction_intelligence",
                label="Auction",
                interval_seconds=60,
                callback=_runner,
                start_offset_seconds=90.0,
                no_start_before=time(9, 45),
            )
        ],
        now_fn=lambda: now,
        market_hours_fn=lambda _current: True,
        next_open_fn=lambda current: current + timedelta(days=1),
    )

    asyncio.run(supervisor.run_due_once(force=True))
    assert calls == ["auction"]


def test_open_stagger_rearms_after_each_market_open() -> None:
    clock = {"now": datetime(2026, 4, 21, 9, 15, tzinfo=IST)}
    market = {"open": True}
    calls: list[str] = []

    async def _runner() -> dict[str, object]:
        calls.append("auction")
        return {"result_count": 1}

    supervisor = MarketHoursPaperSupervisor(
        enabled=True,
        runners=[
            RunnerConfig(
                key="auction_intelligence",
                label="Auction",
                interval_seconds=60,
                callback=_runner,
                start_offset_seconds=90.0,
                post_close_catchup=False,
            )
        ],
        now_fn=lambda: clock["now"],
        market_hours_fn=lambda _current: market["open"],
        next_open_fn=lambda current: current + timedelta(days=1),
    )

    asyncio.run(supervisor.run_due_once())          # stamps the open, held
    clock["now"] += timedelta(seconds=91)
    asyncio.run(supervisor.run_due_once())          # past the offset → runs
    assert calls == ["auction"]

    # Market closes → the open stamp resets.
    market["open"] = False
    clock["now"] += timedelta(hours=7)
    status = asyncio.run(supervisor.run_due_once())
    assert status["runners"]["auction_intelligence"]["market_open_since"] is None

    # Next session: the stagger applies AGAIN from the new open.
    market["open"] = True
    clock["now"] += timedelta(hours=17)
    asyncio.run(supervisor.run_due_once())          # re-stamps, held
    assert calls == ["auction"]
    clock["now"] += timedelta(seconds=91)
    asyncio.run(supervisor.run_due_once())
    assert calls == ["auction", "auction"]


def test_open_stagger_does_not_delay_post_close_catchup() -> None:
    # ADVERSARIAL: a lane with the heaviest stagger (30min offset + wall-clock
    # guard) that never got to run in-session must STILL fire its post-close
    # catch-up pass — the stagger gates only the in-session due path.
    # 2026-04-21 is a Tuesday (NSE session), post-close window opens 15:35.
    clock = {"now": datetime(2026, 4, 21, 15, 40, tzinfo=IST)}
    calls: list[str] = []

    async def _runner() -> dict[str, object]:
        calls.append("macd")
        return {"result_count": 1}

    supervisor = MarketHoursPaperSupervisor(
        enabled=True,
        runners=[
            RunnerConfig(
                key="macd_refined",
                label="MACD Refined",
                interval_seconds=1800,
                callback=_runner,
                start_offset_seconds=1800.0,
                no_start_before=time(9, 45),
                post_close_catchup=True,
            )
        ],
        now_fn=lambda: clock["now"],
        market_hours_fn=lambda _current: False,  # market CLOSED
        next_open_fn=lambda current: current + timedelta(days=1),
    )

    status = asyncio.run(supervisor.run_due_once())
    # market_open_since is None (closed) and the offset window never elapsed —
    # the catch-up path must not consult either.
    assert calls == ["macd"]
    runner = status["runners"]["macd_refined"]
    assert runner["market_open_since"] is None


def test_macd_refined_marks_runner_is_seconds_cadence_and_unkilled() -> None:
    """GAP 1: the protective-exit heartbeat is a separate seconds-cadence
    runner from the 30m decision cycle, market-hours only, and NOT tagged with
    a slow/fast profile — so the SLOW_LANES_ENABLED cadence-group kill switch
    can never dark held-position stop/target protection (it reads the Fyers-WS
    real-time plane, not an Upstox decision fetch)."""
    supervisor = MarketHoursPaperSupervisor(enabled=False)

    marks = supervisor._runners["macd_refined_marks"].config
    decision = supervisor._runners["macd_refined"].config

    # Seconds cadence, far below the 30m decision cycle.
    assert marks.interval_seconds <= 60
    assert marks.interval_seconds < decision.interval_seconds
    # Runs from the open (no stagger) so an early position is protected at once.
    assert marks.start_offset_seconds == 0.0
    assert marks.no_start_before is None
    # Market-hours only — nothing to re-mark on frozen post-close bars.
    assert marks.post_close_catchup is False
    # DEFAULT profile: neither cadence-group kill switch darks the safety pass.
    assert marks.broker_profile == "default"
    # The decision cycle itself is unchanged (still the slow 30m lane).
    assert decision.broker_profile == "slow"
    assert decision.interval_seconds == 1800


def test_default_runner_stagger_configuration() -> None:
    supervisor = MarketHoursPaperSupervisor(enabled=False)

    assert supervisor._runners["market_intelligence"].config.start_offset_seconds == 0.0
    for key in (
        "auction_intelligence",
        "directional_options",
        "institutional_convergence",
        "institutional_convergence_commodity",
    ):
        assert supervisor._runners[key].config.start_offset_seconds == 90.0, key
    macd = supervisor._runners["macd_refined"].config
    assert macd.start_offset_seconds == 1800.0
    assert macd.no_start_before == time(9, 45)


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

# ── Post-close catch-up discipline (2026-07-16) ──────────────────────────────
# NSE lanes must go idle after the close with AT MOST one catch-up pass per
# session. On 2026-07-15 the catch-up (a) double-fired force_daily runners
# while a slower batch peer was still running (lane_audit + directional_
# positioning both ran twice within a minute of 15:35 IST) and (b) re-fired
# in full after every backend restart (16:16, 16:20 and 22:26 IST) because
# the "done for today" marker only lived in memory.


def _post_close_supervisor(runners, *, now, state_path=None):
    return MarketHoursPaperSupervisor(
        enabled=True,
        runners=runners,
        now_fn=lambda: now,
        market_hours_fn=lambda _current: False,
        next_open_fn=lambda current: current + timedelta(days=1),
        catchup_state_path=state_path,
    )


def test_token_readiness_default_never_runs_post_close_catchup() -> None:
    supervisor = MarketHoursPaperSupervisor(enabled=False)
    runner = supervisor._runners["token_readiness"].config

    assert runner.post_close_catchup is False
    assert runner.market_hours_fn is not None
    assert runner.next_open_fn is not None


def test_post_close_catchup_does_not_double_fire_while_batch_peer_running() -> None:
    async def _scenario() -> None:
        now = datetime(2026, 4, 20, 16, 0, tzinfo=IST)
        calls: list[str] = []
        release_slow = asyncio.Event()
        fast_done = asyncio.Event()

        async def _fast() -> dict[str, object]:
            calls.append("fast")
            fast_done.set()
            return {"result_count": 1}

        async def _slow() -> dict[str, object]:
            calls.append("slow")
            await release_slow.wait()
            return {"result_count": 1}

        supervisor = _post_close_supervisor(
            [
                RunnerConfig(
                    key="fast_eod",
                    label="Fast EOD",
                    interval_seconds=60,
                    callback=_fast,
                    post_close_force_daily=True,
                ),
                RunnerConfig(
                    key="slow_eod",
                    label="Slow EOD",
                    interval_seconds=60,
                    callback=_slow,
                    post_close_force_daily=True,
                ),
            ],
            now=now,
        )

        await supervisor._schedule_due_once()
        await asyncio.wait_for(fast_done.wait(), timeout=0.5)
        # Give the fast runner's done-callback a beat to clear `running`,
        # mimicking the production window where the batch gather is still
        # waiting on the slow peer.
        await asyncio.sleep(0.05)

        # Second scheduler pass while the slow peer is still running: the fast
        # runner already succeeded and MUST NOT be re-dispatched.
        await supervisor._schedule_due_once()
        await asyncio.sleep(0.05)
        assert calls.count("fast") == 1

        fast_runtime = supervisor._runners["fast_eod"]
        assert fast_runtime.last_post_close_success_date == now.date()

        release_slow.set()
        for _ in range(100):
            if not supervisor._runner_tasks and not supervisor._maintenance_tasks:
                break
            await asyncio.sleep(0.01)
        assert calls == ["fast", "slow"]

    asyncio.run(_scenario())


def test_post_close_catchup_success_persists_across_restarts(tmp_path) -> None:
    now = datetime(2026, 4, 20, 16, 0, tzinfo=IST)
    state_path = tmp_path / "post_close_catchup.json"
    calls: list[str] = []

    def _make_runner() -> RunnerConfig:
        async def _runner() -> dict[str, object]:
            calls.append("cbe")
            return {"result_count": 1}

        return RunnerConfig(
            key="cbe_scanner",
            label="CBE",
            interval_seconds=60,
            callback=_runner,
            post_close_force_daily=True,
        )

    first = _post_close_supervisor([_make_runner()], now=now, state_path=state_path)
    asyncio.run(first.run_due_once())
    assert calls == ["cbe"]
    assert state_path.exists()

    # Simulated backend restart: a brand-new supervisor instance sharing the
    # state file must NOT re-fire the already-captured catch-up.
    second = _post_close_supervisor([_make_runner()], now=now, state_path=state_path)
    asyncio.run(second.run_due_once())
    assert calls == ["cbe"]

    # The next session is a fresh date — the pass fires again.
    next_day = datetime(2026, 4, 21, 16, 0, tzinfo=IST)
    third = _post_close_supervisor([_make_runner()], now=next_day, state_path=state_path)
    asyncio.run(third.run_due_once())
    assert calls == ["cbe", "cbe"]


def test_post_close_catchup_gates_recovery_runners_across_restarts(tmp_path) -> None:
    """post_close_catchup (non-force) runners are also restart-proof."""
    now = datetime(2026, 4, 20, 16, 0, tzinfo=IST)
    state_path = tmp_path / "post_close_catchup.json"
    calls: list[str] = []

    def _make_runner() -> RunnerConfig:
        async def _runner() -> dict[str, object]:
            calls.append("mi")
            return {"result_count": 1}

        return RunnerConfig(
            key="market_intelligence",
            label="MI",
            interval_seconds=60,
            callback=_runner,
        )

    first = _post_close_supervisor([_make_runner()], now=now, state_path=state_path)
    asyncio.run(first.run_due_once())
    second = _post_close_supervisor([_make_runner()], now=now, state_path=state_path)
    asyncio.run(second.run_due_once())

    assert calls == ["mi"]


def test_post_close_catchup_failed_attempts_are_bounded() -> None:
    from core.market_hours_paper_supervisor import POST_CLOSE_MAX_ATTEMPTS_PER_SESSION

    now = datetime(2026, 4, 20, 16, 0, tzinfo=IST)
    calls: list[str] = []

    async def _failing() -> dict[str, object]:
        calls.append("boom")
        raise RuntimeError("broker offline")

    supervisor = _post_close_supervisor(
        [
            RunnerConfig(
                key="lane_audit",
                label="Lane Audit",
                interval_seconds=60,
                callback=_failing,
                post_close_force_daily=True,
            )
        ],
        now=now,
    )

    for _ in range(POST_CLOSE_MAX_ATTEMPTS_PER_SESSION + 3):
        asyncio.run(supervisor.run_due_once())

    assert len(calls) == POST_CLOSE_MAX_ATTEMPTS_PER_SESSION


def test_should_run_post_close_catchup_window() -> None:
    from core.market_hours_paper_supervisor import _should_run_post_close_catchup

    # Session day (Mon 2026-04-20): only after the 15:35 grace cutoff.
    assert _should_run_post_close_catchup(datetime(2026, 4, 20, 15, 20, tzinfo=IST)) is False
    assert _should_run_post_close_catchup(datetime(2026, 4, 20, 15, 35, tzinfo=IST)) is True
    assert _should_run_post_close_catchup(datetime(2026, 4, 20, 22, 30, tzinfo=IST)) is True
    # Weekend: never.
    assert _should_run_post_close_catchup(datetime(2026, 4, 19, 16, 0, tzinfo=IST)) is False
