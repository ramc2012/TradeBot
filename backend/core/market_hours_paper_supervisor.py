from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Awaitable, Callable

from loguru import logger

from core.config import settings
from paper_engine.base_strategy_agent import _now_ist


RunnerCallback = Callable[[], Awaitable[dict[str, Any]]]
NowFn = Callable[[], datetime]
MarketHoursFn = Callable[[datetime], bool]
NextOpenFn = Callable[[datetime], datetime]


def _in_nse_market_hours(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    return time(9, 15) <= now.time() <= time(15, 30)


def _next_nse_market_open(now: datetime) -> datetime:
    next_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    if now >= next_open:
        next_open += timedelta(days=1)
    while next_open.weekday() >= 5:
        next_open += timedelta(days=1)
    return next_open


def _should_run_post_close_catchup(now: datetime) -> bool:
    return now.weekday() < 5 and now.time() > time(15, 30)


@dataclass
class RunnerConfig:
    key: str
    label: str
    interval_seconds: int
    callback: RunnerCallback
    enabled: bool = True


@dataclass
class RunnerRuntime:
    config: RunnerConfig
    running: bool = False
    last_started_at: datetime | None = None
    last_success_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_error: str | None = None
    last_message: str | None = None
    last_result_meta: dict[str, Any] = field(default_factory=dict)

    def is_due(self, now: datetime) -> bool:
        if not self.config.enabled or self.running:
            return False
        if self.last_started_at is None:
            return True
        return (now - self.last_started_at).total_seconds() >= max(int(self.config.interval_seconds), 1)

    def next_run_at(
        self,
        now: datetime,
        *,
        market_hours_fn: MarketHoursFn,
        next_open_fn: NextOpenFn,
    ) -> datetime | None:
        if not self.config.enabled:
            return None
        if not market_hours_fn(now):
            return next_open_fn(now)
        if self.last_started_at is None:
            return now
        return self.last_started_at + timedelta(seconds=max(int(self.config.interval_seconds), 1))

    def serialize(
        self,
        now: datetime,
        *,
        loop_active: bool,
        market_hours_fn: MarketHoursFn,
        next_open_fn: NextOpenFn,
    ) -> dict[str, Any]:
        next_run_at = self.next_run_at(now, market_hours_fn=market_hours_fn, next_open_fn=next_open_fn)
        return {
            "key": self.config.key,
            "label": self.config.label,
            "enabled": self.config.enabled,
            "interval_seconds": self.config.interval_seconds,
            "loop_active": loop_active,
            "running": self.running,
            "last_started_at": self.last_started_at.isoformat() if self.last_started_at else None,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_finished_at": self.last_finished_at.isoformat() if self.last_finished_at else None,
            "next_run_at": next_run_at.isoformat() if next_run_at else None,
            "last_error": self.last_error,
            "last_message": self.last_message,
            "last_result_meta": self.last_result_meta,
        }


class MarketHoursPaperSupervisor:
    def __init__(
        self,
        *,
        enabled: bool | None = None,
        runners: list[RunnerConfig] | None = None,
        now_fn: NowFn | None = None,
        market_hours_fn: MarketHoursFn | None = None,
        next_open_fn: NextOpenFn | None = None,
    ) -> None:
        self._enabled = settings.MARKET_HOURS_PAPER_SUPERVISOR_ENABLED if enabled is None else bool(enabled)
        self._now_fn = now_fn or _now_ist
        self._market_hours_fn = market_hours_fn or _in_nse_market_hours
        self._next_open_fn = next_open_fn or _next_nse_market_open
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._runners: dict[str, RunnerRuntime] = {
            runner.key: RunnerRuntime(config=runner)
            for runner in (runners or self._default_runners())
        }

    def _default_runners(self) -> list[RunnerConfig]:
        from auction_intelligence.automation import run_market_hours_cycle as run_auction_market_cycle
        from directional_options.service import DirectionalOptionsService
        from fractal_market_profile.config import SUPPORTED_SYMBOLS
        from fractal_market_profile.service import fmp_service
        from market_data.market_intelligence_runtime import market_intelligence_runtime

        directional_service = DirectionalOptionsService()

        async def _market_intelligence_runner() -> dict[str, Any]:
            return await market_intelligence_runtime.refresh_nse_runtime()

        async def _auction_runner() -> dict[str, Any]:
            return await run_auction_market_cycle()

        async def _fmp_runner() -> dict[str, Any]:
            results: list[dict[str, Any]] = []
            failures: dict[str, str] = {}
            for symbol in SUPPORTED_SYMBOLS:
                try:
                    snapshot = await fmp_service.record_paper_snapshot(symbol)
                    results.append(
                        {
                            "symbol_code": snapshot.get("symbol_code"),
                            "session_date": snapshot.get("session", {}).get("session_date"),
                            "signal_action": snapshot.get("current_signal", {}).get("action"),
                            "actionable": snapshot.get("current_signal", {}).get("actionable"),
                            "paper_summary": snapshot.get("paper_summary"),
                        }
                    )
                except Exception as exc:
                    failures[symbol] = str(exc)
            if not results and failures:
                joined = "; ".join(f"{symbol}: {detail}" for symbol, detail in failures.items())
                raise RuntimeError(f"Fractal Market Profile paper cycle failed: {joined}")
            return {
                "symbols_requested": list(SUPPORTED_SYMBOLS),
                "symbols_completed": [item.get("symbol_code") for item in results],
                "result_count": len(results),
                "failure_count": len(failures),
                "failures": failures,
                "results": results,
            }

        async def _directional_runner() -> dict[str, Any]:
            results: list[dict[str, Any]] = []
            failures: dict[str, str] = {}
            default_timeframe = str(directional_service.config["default_timeframe"])
            lookback_sessions = int(directional_service.config["backtest"]["lookback_sessions"])
            for underlying in list(directional_service.config["universe"]):
                try:
                    snapshot = await directional_service.record_paper_snapshot(
                        underlying,
                        default_timeframe,
                        lookback_sessions,
                    )
                    current_snapshot = dict(snapshot.get("snapshot") or {})
                    results.append(
                        {
                            "underlying": underlying,
                            "as_of": current_snapshot.get("as_of"),
                            "direction": (current_snapshot.get("signal") or {}).get("direction"),
                            "approved": bool((current_snapshot.get("risk") or {}).get("approved")),
                            "execution_ready": bool((current_snapshot.get("data_status") or {}).get("execution_ready")),
                            "trading_symbol": (current_snapshot.get("selected_contract") or {}).get("trading_symbol"),
                        }
                    )
                except Exception as exc:
                    failures[underlying] = str(exc)
            if not results and failures:
                joined = "; ".join(f"{symbol}: {detail}" for symbol, detail in failures.items())
                raise RuntimeError(f"Directional options paper cycle failed: {joined}")
            return {
                "symbols_requested": list(directional_service.config["universe"]),
                "symbols_completed": [item.get("underlying") for item in results],
                "result_count": len(results),
                "failure_count": len(failures),
                "failures": failures,
                "results": results,
            }

        return [
            RunnerConfig(
                key="market_intelligence",
                label="Market Intelligence Refresh",
                interval_seconds=settings.MARKET_INTELLIGENCE_REFRESH_INTERVAL_SECONDS,
                callback=_market_intelligence_runner,
                enabled=settings.MARKET_INTELLIGENCE_AUTO_ENABLED,
            ),
            RunnerConfig(
                key="auction_intelligence",
                label="Auction Intelligence Paper Cycle",
                interval_seconds=settings.AUCTION_INTELLIGENCE_AUTO_INTERVAL_SECONDS,
                callback=_auction_runner,
                enabled=settings.AUCTION_INTELLIGENCE_AUTO_ENABLED,
            ),
            RunnerConfig(
                key="fractal_market_profile",
                label="Fractal Market Profile Paper Cycle",
                interval_seconds=settings.FRACTAL_MARKET_PROFILE_AUTO_INTERVAL_SECONDS,
                callback=_fmp_runner,
                enabled=settings.FRACTAL_MARKET_PROFILE_AUTO_ENABLED,
            ),
            RunnerConfig(
                key="directional_options",
                label="Directional Options Paper Cycle",
                interval_seconds=settings.DIRECTIONAL_OPTIONS_AUTO_INTERVAL_SECONDS,
                callback=_directional_runner,
                enabled=settings.DIRECTIONAL_OPTIONS_AUTO_ENABLED,
            ),
        ]

    async def start(self) -> None:
        if not self._enabled:
            logger.info("[MarketHoursSupervisor] disabled by config")
            return
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="market-hours-paper-supervisor")
        logger.info("[MarketHoursSupervisor] started")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.info("[MarketHoursSupervisor] stopped")

    async def _loop(self) -> None:
        try:
            while True:
                await self.run_due_once()
                now = self._now_fn()
                if self._market_hours_fn(now):
                    await asyncio.sleep(max(int(settings.MARKET_HOURS_SUPERVISOR_LOOP_SECONDS), 5))
                else:
                    next_open = self._next_open_fn(now)
                    seconds_until_open = max((next_open - now).total_seconds(), 60.0)
                    await asyncio.sleep(min(seconds_until_open, 300.0))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(f"[MarketHoursSupervisor] loop failed: {exc}")
            raise

    async def run_due_once(self, *, force: bool = False) -> dict[str, Any]:
        if not self._enabled:
            return self.get_status()

        async with self._lock:
            now = self._now_fn()
            if not self._market_hours_fn(now) and not force:
                if _should_run_post_close_catchup(now):
                    catchup_session_date = now.date()
                    catchup_ran = False
                    for runtime in self._runners.values():
                        if not runtime.config.enabled or runtime.running:
                            continue
                        if runtime.last_success_at and runtime.last_success_at.date() >= catchup_session_date:
                            continue
                        await self._run_runner(runtime, now=now)
                        if runtime.last_error is None:
                            runtime.last_result_meta.setdefault(
                                "catchup_session_date",
                                catchup_session_date.isoformat(),
                            )
                            runtime.last_message = (
                                f"{runtime.last_message} Catch-up captured for "
                                f"{catchup_session_date.isoformat()}."
                            )
                        catchup_ran = True
                    if catchup_ran:
                        return self.get_status()
                for runtime in self._runners.values():
                    if runtime.config.enabled and not runtime.running and runtime.last_message is None:
                        runtime.last_message = "Armed for the next market session."
                return self.get_status()

            for runtime in self._runners.values():
                if force or runtime.is_due(now):
                    await self._run_runner(runtime, now=now)

        return self.get_status()

    async def _run_runner(self, runtime: RunnerRuntime, *, now: datetime) -> None:
        runtime.running = True
        runtime.last_started_at = now
        runtime.last_error = None
        try:
            result = await runtime.config.callback()
        except Exception as exc:
            runtime.last_error = str(exc)
            runtime.last_finished_at = self._now_fn()
            runtime.last_message = str(exc)
            runtime.last_result_meta = {"error": str(exc)}
            logger.warning(f"[MarketHoursSupervisor] {runtime.config.key} failed: {exc}")
        else:
            runtime.last_success_at = self._now_fn()
            runtime.last_finished_at = runtime.last_success_at
            runtime.last_result_meta = result if isinstance(result, dict) else {"result": result}
            completed = result.get("result_count") if isinstance(result, dict) else None
            failure_count = result.get("failure_count") if isinstance(result, dict) else None
            if completed is not None:
                runtime.last_message = f"Completed {completed} cycle(s)" + (
                    f" with {failure_count} failure(s)." if failure_count else "."
                )
            else:
                runtime.last_message = "Completed automated paper cycle."
            logger.info(f"[MarketHoursSupervisor] {runtime.config.key} completed")
        finally:
            runtime.running = False

    def get_runner_status(self, key: str) -> dict[str, Any]:
        now = self._now_fn()
        runtime = self._runners.get(key)
        loop_active = bool(self._task and not self._task.done())
        if runtime is None:
            return {
                "key": key,
                "enabled": False,
                "loop_active": loop_active,
                "running": False,
                "last_message": "Runner not configured.",
            }
        return runtime.serialize(
            now,
            loop_active=loop_active,
            market_hours_fn=self._market_hours_fn,
            next_open_fn=self._next_open_fn,
        )

    def get_status(self) -> dict[str, Any]:
        now = self._now_fn()
        market_open = self._market_hours_fn(now)
        next_open = self._next_open_fn(now)
        loop_active = bool(self._task and not self._task.done())
        runners = {
            key: runtime.serialize(
                now,
                loop_active=loop_active,
                market_hours_fn=self._market_hours_fn,
                next_open_fn=self._next_open_fn,
            )
            for key, runtime in self._runners.items()
        }
        healthy_runner_count = sum(1 for item in runners.values() if item.get("last_error") is None)
        return {
            "enabled": self._enabled,
            "loop_active": loop_active,
            "market_open": market_open,
            "now_ist": now.isoformat(),
            "next_market_open_ist": next_open.isoformat(),
            "runner_count": len(runners),
            "healthy_runner_count": healthy_runner_count,
            "runners": runners,
        }


market_hours_paper_supervisor = MarketHoursPaperSupervisor()
