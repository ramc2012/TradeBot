from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from core.config import settings
from core.trading_calendar import trading_calendar
from paper_engine.base_strategy_agent import _now_ist


RunnerCallback = Callable[[], Awaitable[dict[str, Any]]]
NowFn = Callable[[], datetime]
MarketHoursFn = Callable[[datetime], bool]
NextOpenFn = Callable[[datetime], datetime]


def _in_nse_market_hours(now: datetime) -> bool:
    return trading_calendar.is_exchange_open("NSE", now)


def _never_in_session(now: datetime) -> bool:
    """Always-closed window: the runner can ONLY reach the post-close path.

    The supervisor dispatches a runner when ``_runtime_market_open(...) and
    is_due(...)``; the post-close catch-up branch is the ``elif``, and
    ``_post_close_catchup_eligible`` further requires ``market_open`` False. So
    a post-close-only data job must report its market as never open — giving it
    ``_in_nse_market_hours`` instead makes it ALSO fire in-session on its
    ``interval_seconds`` cadence (and immediately at the open, since
    ``last_started_at`` is None), which is exactly what such a job must not do.
    """
    return False


def _in_institutional_convergence_window(now: datetime) -> bool:
    return trading_calendar.has_exchange_session("NSE", now.date()) and time(8, 45) <= now.time() <= time(15, 30)


def _next_institutional_convergence_open(now: datetime) -> datetime:
    if _in_institutional_convergence_window(now):
        return now
    candidate = now.replace(hour=8, minute=45, second=0, microsecond=0)
    while candidate <= now or not trading_calendar.has_exchange_session("NSE", candidate.date()):
        candidate = (candidate + timedelta(days=1)).replace(hour=8, minute=45, second=0, microsecond=0)
    return candidate


def _in_mcx_market_hours(now: datetime) -> bool:
    return trading_calendar.is_exchange_open("MCX", now)


def _in_commodity_convergence_window(now: datetime) -> bool:
    # Mirrors the NSE convergence window: a 15-minute pre-market prep slot
    # (08:45-09:00) plus the MCX session itself.
    return trading_calendar.has_exchange_session("MCX", now.date()) and (
        time(8, 45) <= now.time() < time(9, 0) or trading_calendar.is_exchange_open("MCX", now)
    )


def _next_commodity_convergence_open(now: datetime) -> datetime:
    if _in_commodity_convergence_window(now):
        return now
    candidate = now.replace(hour=8, minute=45, second=0, microsecond=0)
    while candidate <= now or not trading_calendar.has_exchange_session("MCX", candidate.date()):
        candidate = (candidate + timedelta(days=1)).replace(hour=8, minute=45, second=0, microsecond=0)
    return candidate


def _in_gann_market_hours(now: datetime) -> bool:
    return _in_nse_market_hours(now) or _in_mcx_market_hours(now)


def _next_nse_market_open(now: datetime) -> datetime:
    return trading_calendar.next_exchange_open("NSE", now)


def _next_mcx_market_open(now: datetime) -> datetime:
    return trading_calendar.next_exchange_open("MCX", now)


def _next_gann_market_open(now: datetime) -> datetime:
    if _in_gann_market_hours(now):
        return now
    return min(_next_nse_market_open(now), _next_mcx_market_open(now))


def _should_run_post_close_catchup(now: datetime) -> bool:
    # Allow final bars and database writers a short grace period before any
    # end-of-session strategy snapshots are captured.
    return trading_calendar.has_exchange_session("NSE", now.date()) and now.time() >= time(15, 35)


async def _dispatch_strategy_command(action: str, args: dict) -> dict:
    """Phase-2 ITEM 2: execute a proxied strategy command IN the strategy plane.

    Runs the same in-process agent method the core-plane router would have run
    single-process. Unknown actions raise (the consumer returns the error to the
    core caller, which surfaces a 502)."""
    args = args or {}
    if action == "nse_run_once":
        from paper_engine.strategy_agent import paper_strategy_agent

        return await paper_strategy_agent.run_once(force=bool(args.get("force", True)))
    if action == "nse_close_position":
        from paper_engine.strategy_agent import paper_strategy_agent

        return await paper_strategy_agent.operator_close_position(
            strategy_key=args.get("strategy_key"),
            symbol=args.get("symbol"),
            reason=args.get("reason"),
        )
    if action == "commodity_start":
        from paper_engine.commodity_strategy_agent import commodity_strategy_agent

        return await commodity_strategy_agent.start_loop()
    if action == "commodity_run_once":
        from paper_engine.commodity_strategy_agent import commodity_strategy_agent

        return await commodity_strategy_agent.run_once(force=bool(args.get("force", True)))
    raise ValueError(f"unknown strategy command action: {action!r}")


# A failed post-close catch-up may retry, but only this many attempts per
# session — a persistently failing runner must not spin against the broker
# every scheduler tick for the rest of the evening.
POST_CLOSE_MAX_ATTEMPTS_PER_SESSION = 3


def _in_token_readiness_window(now: datetime) -> bool:
    """Pre-open sweep window: 07:00–09:20 IST on NSE session days. Both brokers
    expire tokens daily (Upstox 03:30 IST); this window validates/refreshes
    BEFORE 09:15 so a dead token is an actionable pre-open alert, not a
    mid-session surprise."""
    return (
        trading_calendar.has_exchange_session("NSE", now.date())
        and time(7, 0) <= now.time() <= time(9, 20)
    )


def _next_token_readiness_open(now: datetime) -> datetime:
    if _in_token_readiness_window(now):
        return now
    candidate = now.replace(hour=7, minute=0, second=0, microsecond=0)
    while candidate <= now or not trading_calendar.has_exchange_session("NSE", candidate.date()):
        candidate = (candidate + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
    return candidate


@dataclass
class RunnerConfig:
    key: str
    label: str
    interval_seconds: int
    callback: RunnerCallback
    enabled: bool = True
    market_hours_fn: MarketHoursFn | None = None
    next_open_fn: NextOpenFn | None = None
    # When False, this runner is excluded from the post-close (15:30+) catch-up
    # pass — it only ever fires inside its market-hours window. Used by runners
    # that must "act in market hours only" (e.g. CBE) rather than capture a
    # once-a-day end-of-session snapshot.
    post_close_catchup: bool = True
    # When True, the post-close catch-up fires ONCE per session after the
    # 15:35 grace cutoff EVEN IF in-session passes already succeeded — it is a
    # guaranteed end-of-day pass, not just a recovery net. Needed by runners
    # whose in-session signal uses the PRIOR completed session (e.g. CBE's
    # _completed_session_cutoff excludes today until 15:35), so only a
    # post-15:35 pass scores on today's finalized close. Default False keeps
    # the recovery-only behavior for every other runner.
    post_close_force_daily: bool = False
    # WS-0.5a — per-runner hard timeout (seconds). None → use the global
    # settings.MARKET_HOURS_SUPERVISOR_RUNNER_TIMEOUT_SECONDS ceiling.
    timeout_seconds: float | None = None
    # Open stagger (2026-07-15): delay this runner's FIRST start by this many
    # seconds after each observed closed→open transition of ITS market window,
    # so every lane doesn't slam the shared broker budget at 09:15 in one
    # thundering herd. Applied per open (the marker resets when the window
    # closes); after a mid-session backend restart the offset re-arms from the
    # restart — deliberate, a restart IS the same herd moment. Does not gate
    # the post-close catch-up pass or a forced run_due_once(force=True).
    start_offset_seconds: float = 0.0
    # Wall-clock guard (IST): never start before this time of day, regardless
    # of when the open was observed. Session-anchored complement to the
    # transition-anchored offset (e.g. macd_refined must not start before
    # 09:45 even if the backend restarts at 09:20 and re-stamps the open).
    no_start_before: time | None = None
    # Per-lane broker profile (owner directive 2026-07-17, cadence axis). SLOW
    # (30m) lanes prefer Upstox for their REST; FAST (3m + tick) lanes prefer
    # Fyers. Set once here at dispatch — the contextvar propagates through the
    # whole callback subtree so every route_order() fetch the lane makes inherits
    # it. "default" = the global failover order, unchanged. Gated at the READ
    # seam by settings.LANE_BROKER_ROUTING_ENABLED, so tagging a runner with a
    # profile is a provable no-op while the flag is off. See
    # market_data.source_policy.route_order and brokers.rate_limiter.
    broker_profile: str = "default"
    # Phase-1 process split (2026-07-18): which boot plane owns this runner.
    # "core" = data-plane/auth/freshness runners (MI watchlist build, token
    # readiness, option-flow watchdog) that live with the WS/watchlist plane;
    # "strategies" (default) = every trading lane. Only consulted when
    # LANESET != "all" — the default single-process boot schedules ALL runners
    # exactly as today.
    plane: str = "strategies"


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
    cycle_history: list[dict[str, Any]] = field(default_factory=list)
    # Session date of the most recent SUCCESSFUL post-close catch-up run.
    # Gates EVERY post_close_catchup runner (loaded from the persisted state
    # file at startup so a backend restart doesn't re-fire the pass — on
    # 2026-07-15 three post-close restarts re-ran the full catch-up batch at
    # 16:16, 16:20 and 22:26 IST because this only lived in memory).
    last_post_close_success_date: date | None = None
    # Attempt tracking for the post-close catch-up: stamped at DISPATCH time so
    # the background scheduler can never launch the same runner's catch-up
    # twice while a batch peer is still finishing (2026-07-15: lane_audit and
    # directional_positioning both fired twice within a minute at 15:35 IST
    # because the success date was only stamped after the whole batch's
    # gather). Failures may retry up to POST_CLOSE_MAX_ATTEMPTS_PER_SESSION.
    last_post_close_attempt_date: date | None = None
    post_close_attempts: int = 0
    # When the supervisor first OBSERVED this runner's market window open
    # (reset to None while closed). Anchors start_offset_seconds.
    market_open_since: datetime | None = None

    def note_post_close_attempt(self, session_date: date) -> None:
        if self.last_post_close_attempt_date != session_date:
            self.last_post_close_attempt_date = session_date
            self.post_close_attempts = 0
        self.post_close_attempts += 1

    def stagger_clear(self, now: datetime) -> bool:
        """Whether the open stagger (start_offset_seconds + no_start_before)
        permits a start at `now`. Only consulted on the in-session due path —
        post-close catch-up and forced runs are exempt."""
        no_start_before = self.config.no_start_before
        if no_start_before is not None and now.time() < no_start_before:
            return False
        offset = float(self.config.start_offset_seconds or 0.0)
        if offset > 0.0:
            if self.market_open_since is None:
                # Open not observed yet (first pass stamps it before is_due
                # runs) — hold rather than start unstaggered.
                return False
            if (now - self.market_open_since).total_seconds() < offset:
                return False
        return True

    def is_due(self, now: datetime) -> bool:
        if not self.config.enabled or self.running:
            return False
        # Per-cadence-group kill switch ("work independently"): dark an entire
        # broker-dependent group in one move when that broker degrades, without
        # touching each lane's own *_AUTO_ENABLED flag. Orthogonal to
        # LANE_BROKER_ROUTING_ENABLED (this gates SCHEDULING, not broker choice);
        # both default True so this is a no-op out of the box. Keyed off the
        # runner's own broker_profile so the cadence group and the routing group
        # are the same set by construction.
        profile = str(self.config.broker_profile or "default").strip().lower()
        if profile == "slow" and not bool(getattr(settings, "SLOW_LANES_ENABLED", True)):
            return False
        if profile == "fast" and not bool(getattr(settings, "FAST_LANES_ENABLED", True)):
            return False
        if not self.stagger_clear(now):
            return False
        if self.last_started_at is None:
            return True
        return (now - self.last_started_at).total_seconds() >= max(int(self.config.interval_seconds), 1)

    def _stagger_gate_at(self, now: datetime) -> datetime | None:
        """Earliest moment the open stagger would allow a start (None when the
        stagger imposes no bound that is still in the future)."""
        candidates: list[datetime] = []
        no_start_before = self.config.no_start_before
        if no_start_before is not None and now.time() < no_start_before:
            candidates.append(
                now.replace(
                    hour=no_start_before.hour,
                    minute=no_start_before.minute,
                    second=no_start_before.second,
                    microsecond=0,
                )
            )
        offset = float(self.config.start_offset_seconds or 0.0)
        if offset > 0.0 and self.market_open_since is not None:
            candidates.append(self.market_open_since + timedelta(seconds=offset))
        if not candidates:
            return None
        return max(candidates)

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
            base = now
        else:
            base = self.last_started_at + timedelta(seconds=max(int(self.config.interval_seconds), 1))
        gate = self._stagger_gate_at(now)
        if gate is not None and gate > base:
            return gate
        return base

    def serialize(
        self,
        now: datetime,
        *,
        loop_active: bool,
        market_hours_fn: MarketHoursFn,
        next_open_fn: NextOpenFn,
    ) -> dict[str, Any]:
        next_run_at = self.next_run_at(now, market_hours_fn=market_hours_fn, next_open_fn=next_open_fn)
        durations = [
            float(item["duration_seconds"])
            for item in self.cycle_history
            if item.get("duration_seconds") is not None
        ]
        starts = [
            datetime.fromisoformat(str(item["started_at"]))
            for item in self.cycle_history
            if item.get("started_at")
        ]
        observed_intervals = [
            (current - previous).total_seconds()
            for previous, current in zip(starts, starts[1:])
            if current >= previous
        ]
        sorted_durations = sorted(durations)
        median_duration = (
            sorted_durations[len(sorted_durations) // 2] if sorted_durations else None
        )
        observed_interval = (
            sum(observed_intervals) / len(observed_intervals) if observed_intervals else None
        )
        # Staleness: a runner is stale when it SHOULD have run (enabled, its
        # market open, the loop live) but hasn't succeeded within a generous
        # multiple of its interval. This distinguishes a silently-dead lane from
        # a healthy idle one — the count alone showed 10/10 healthy after a
        # restart (all last_error None) even when nothing had run.
        stale = False
        market_open_now = market_hours_fn(now)
        if (
            self.config.enabled
            and market_open_now
            and loop_active
            and not self.running
            # A lane held by the open stagger is deliberately idle, not dead.
            and self.stagger_clear(now)
        ):
            overdue = max(self.config.interval_seconds * 3, 300)
            if self.last_success_at is not None:
                stale = (now - self.last_success_at).total_seconds() > overdue
            elif self.last_started_at is not None:
                # Ran but never SUCCEEDED (erroring in a loop) — stale once overdue.
                stale = (now - self.last_started_at).total_seconds() > overdue
            # last_started_at None → just armed this session; grace period, not stale.
        return {
            "key": self.config.key,
            "label": self.config.label,
            "enabled": self.config.enabled,
            "interval_seconds": self.config.interval_seconds,
            "start_offset_seconds": self.config.start_offset_seconds,
            "no_start_before": self.config.no_start_before.isoformat() if self.config.no_start_before else None,
            "market_open_since": self.market_open_since.isoformat() if self.market_open_since else None,
            "loop_active": loop_active,
            "running": self.running,
            "stale": stale,
            "last_started_at": self.last_started_at.isoformat() if self.last_started_at else None,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_finished_at": self.last_finished_at.isoformat() if self.last_finished_at else None,
            "next_run_at": next_run_at.isoformat() if next_run_at else None,
            "last_error": self.last_error,
            "last_message": self.last_message,
            "last_result_meta": self.last_result_meta,
            "cycle_stats": {
                "sample_count": len(self.cycle_history),
                "median_duration_seconds": median_duration,
                "observed_interval_seconds": observed_interval,
                "frequency_drift_pct": (
                    round(
                        ((observed_interval / max(self.config.interval_seconds, 1)) - 1.0) * 100.0,
                        2,
                    )
                    if observed_interval is not None
                    else None
                ),
            },
            "recent_cycles": self.cycle_history[-20:],
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
        catchup_state_path: Path | None = None,
    ) -> None:
        self._enabled = settings.MARKET_HOURS_PAPER_SUPERVISOR_ENABLED if enabled is None else bool(enabled)
        self._now_fn = now_fn or _now_ist
        self._market_hours_fn = market_hours_fn or _in_nse_market_hours
        self._next_open_fn = next_open_fn or _next_nse_market_open
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._runner_tasks: dict[str, asyncio.Task] = {}
        self._maintenance_tasks: set[asyncio.Task] = set()
        # Phase-2 ITEM 2: strategy-command proxy consumer (split strategy plane
        # only). None in single-process / core plane.
        self._proxy_consumer_task: asyncio.Task | None = None
        # Crash watchdog: if the scheduling loop ever raises it would otherwise
        # stop ALL lanes silently forever. Auto-restart, bounded so a hard crash
        # loop can't spin — after this many consecutive crashes we give up and
        # leave the loud audit/log trail instead of hammering.
        self._loop_crash_count = 0
        self._loop_max_restarts = 5
        # Durable "which session already got its post-close catch-up" marker
        # ({runner_key: "YYYY-MM-DD"}). Without it every backend restart after
        # 15:35 re-fired the whole catch-up batch (observed 3× on 2026-07-15).
        # None (the default, used by tests) keeps the state in-memory only.
        self._catchup_state_path = catchup_state_path
        # Phase-1 process split: which plane THIS supervisor instance schedules.
        # "all" (default) keeps every runner — byte-identical scheduling. In a
        # split boot each plane keeps only its own runners and mirrors the
        # other plane's runner status via Redis (see _publish_plane_status).
        from core.laneset import normalized_laneset

        self._laneset = normalized_laneset()
        self._foreign_plane_status: dict[str, Any] | None = None
        self._runners: dict[str, RunnerRuntime] = {
            runner.key: RunnerRuntime(config=runner)
            for runner in (runners or self._default_runners())
            if self._laneset == "all"
            or str(getattr(runner, "plane", "strategies") or "strategies") == self._laneset
        }
        self._load_catchup_state()

    def _load_catchup_state(self) -> None:
        if self._catchup_state_path is None:
            return
        try:
            raw = json.loads(self._catchup_state_path.read_text())
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001 — a corrupt marker file must never block startup
            logger.warning("[MarketHoursSupervisor] catch-up state unreadable: {}", exc)
            return
        for key, value in (raw or {}).items():
            runtime = self._runners.get(str(key))
            if runtime is None:
                continue
            try:
                runtime.last_post_close_success_date = date.fromisoformat(str(value))
            except (TypeError, ValueError):
                continue

    def _persist_catchup_state(self) -> None:
        if self._catchup_state_path is None:
            return
        payload = {
            key: runtime.last_post_close_success_date.isoformat()
            for key, runtime in self._runners.items()
            if runtime.last_post_close_success_date is not None
        }
        try:
            self._catchup_state_path.parent.mkdir(parents=True, exist_ok=True)
            self._catchup_state_path.write_text(json.dumps(payload, indent=0, sort_keys=True))
        except Exception as exc:  # noqa: BLE001 — persistence is best-effort
            logger.warning("[MarketHoursSupervisor] catch-up state persist failed: {}", exc)

    def _post_close_catchup_eligible(
        self,
        runtime: RunnerRuntime,
        *,
        now: datetime,
        session_date: date,
        market_open: bool,
    ) -> bool:
        """One post-close catch-up pass max per NSE session.

        Success is stamped per-runner the moment its catch-up run finishes (not
        after the batch gather) and persisted, so neither a slow batch peer nor
        a backend restart re-fires it. Failed attempts may retry, bounded by
        POST_CLOSE_MAX_ATTEMPTS_PER_SESSION.
        """
        if not runtime.config.post_close_catchup or market_open:
            return False
        if not _should_run_post_close_catchup(now):
            return False
        if (
            runtime.last_post_close_success_date is not None
            and runtime.last_post_close_success_date >= session_date
        ):
            return False
        if not runtime.config.post_close_force_daily:
            # Recovery-only runners skip the pass when an in-session run
            # already succeeded today.
            if runtime.last_success_at is not None and runtime.last_success_at.date() >= session_date:
                return False
        if (
            runtime.last_post_close_attempt_date == session_date
            and runtime.post_close_attempts >= POST_CLOSE_MAX_ATTEMPTS_PER_SESSION
        ):
            return False
        return True

    def _default_runners(self) -> list[RunnerConfig]:
        from auction_intelligence.automation import run_market_hours_cycle as run_auction_market_cycle
        from directional_options.service import directional_options_service
        from fractal_market_profile.config import SUPPORTED_SYMBOLS
        from fractal_market_profile.service import fmp_service
        from gann_tp_delta.service import gann_tp_delta_service
        from macd_refined.service import macd_refined_service
        from market_data.market_intelligence_runtime import market_intelligence_runtime
        from institutional_convergence.service import institutional_convergence_service

        directional_service = directional_options_service

        async def _market_intelligence_runner() -> dict[str, Any]:
            return await market_intelligence_runtime.refresh_nse_runtime()

        async def _token_readiness_runner() -> dict[str, Any]:
            from api.routers.auth import morning_token_readiness
            return await morning_token_readiness()

        async def _macd_preopen_watchlist_runner() -> dict[str, Any]:
            # Owner spec 2026-07-20: fix the expiries pre-market, then build the
            # MACD ATM ladder ONCE from settled/pre-open prices and FREEZE it.
            # Two phases inside one runner (see run_preopen_phase): expiry
            # validation + history warm-up in the 07:00-09:00 dead zone, then the
            # anchor sample + strike pick in the 09:04-09:14 window. Flag-gated
            # OFF (MACD_PREOPEN_WATCHLIST_ENABLED) — a no-op until flipped.
            from market_data.macd_watchlist import run_preopen_phase
            return await run_preopen_phase()

        async def _option_flow_watchdog_runner() -> dict[str, Any]:
            # Detection/telemetry only (default OFF): flags a frozen REST premium
            # feed once the FAST lanes lean on the Fyers tick path. See
            # market_data/option_flow_watchdog.py.
            from market_data.option_flow_watchdog import run_option_flow_watchdog
            return await run_option_flow_watchdog()

        async def _stock_spot_sweep_runner() -> dict[str, Any]:
            # Post-close data maintenance: fills the F&O stock intraday spot grid
            # that had no durable live writer. Bounded + idempotent + CLASS_BULK.
            from market_data.stock_spot_sweeper import sweep_stock_spot
            return await sweep_stock_spot()

        async def _auction_runner() -> dict[str, Any]:
            return await run_auction_market_cycle()

        async def _auction_commodity_runner() -> dict[str, Any]:
            from auction_intelligence.commodity import run_commodity_market_hours_cycle

            return await run_commodity_market_hours_cycle()

        async def _institutional_convergence_runner() -> dict[str, Any]:
            return await institutional_convergence_service.run_cycle()

        async def _institutional_convergence_commodity_runner() -> dict[str, Any]:
            from institutional_convergence.commodity import commodity_convergence_service

            return await commodity_convergence_service.run_cycle()

        async def _fmp_runner() -> dict[str, Any]:
            results: list[dict[str, Any]] = []
            failures: dict[str, str] = {}
            for symbol in SUPPORTED_SYMBOLS:
                try:
                    snapshot = await fmp_service.record_paper_snapshot(symbol)
                    sig = snapshot.get("current_signal") or {}
                    data_status = snapshot.get("data_status") or {}
                    rationale = list(sig.get("rationale") or [])
                    filters = list(sig.get("filters") or [])
                    not_actionable_because: list[str] = []
                    if sig.get("action") == "FLAT":
                        not_actionable_because.append("action is FLAT")
                    if filters:
                        not_actionable_because.extend(f"filter: {f}" for f in filters[:3])
                    if not bool(data_status.get("execution_ready", True)):
                        reason = data_status.get("degraded_reason") or "unknown"
                        if reason == "market_closed":
                            not_actionable_because.append("market closed (entries disabled)")
                        elif bool(data_status.get("paper_record_ready")):
                            not_actionable_because.append(f"execution not ready ({reason})")
                        else:
                            not_actionable_because.append(f"data not ready ({reason})")
                    confidence = sig.get("confidence")
                    if confidence is not None and float(confidence) < 0.55:
                        not_actionable_because.append(
                            f"confidence {float(confidence):.2f} < 0.55 threshold"
                        )
                    results.append(
                        {
                            "symbol_code": snapshot.get("symbol_code"),
                            "session_date": snapshot.get("session", {}).get("session_date"),
                            "signal_action": sig.get("action"),
                            "actionable": sig.get("actionable"),
                            "confidence": confidence,
                            "setup": sig.get("setup_name"),
                            "rationale": rationale[:3],
                            "rejection_reasons": not_actionable_because,
                            "data_status": {
                                "execution_ready": data_status.get("execution_ready"),
                                "paper_record_ready": data_status.get("paper_record_ready"),
                                "reason": data_status.get("degraded_reason"),
                            },
                            "paper_summary": snapshot.get("paper_summary"),
                        }
                    )
                except Exception as exc:
                    failures[symbol] = str(exc)
            if not results and failures:
                joined = "; ".join(f"{symbol}: {detail}" for symbol, detail in failures.items())
                raise RuntimeError(f"Fractal Market Profile paper cycle failed: {joined}")
            from collections import Counter as _Counter

            rejection_counts: _Counter = _Counter()
            actionable_count = 0
            for item in results:
                if item.get("actionable"):
                    actionable_count += 1
                else:
                    for reason in item.get("rejection_reasons") or []:
                        rejection_counts[str(reason)[:80]] += 1
            return {
                "symbols_requested": list(SUPPORTED_SYMBOLS),
                "symbols_completed": [item.get("symbol_code") for item in results],
                "result_count": len(results),
                "actionable_count": actionable_count,
                "rejection_counts": dict(rejection_counts.most_common(10)),
                "failure_count": len(failures),
                "failures": failures,
                "results": results,
            }

        async def _directional_runner() -> dict[str, Any]:
            """Directional paper cycle: 3 indices + a rotating NIFTY-50 batch.

            LOAD DESIGN (2026-07-17 stock expansion): 53 symbols at a 180s
            cadence must never seize the loop or the broker budget —
              * indices scan serially first (unchanged semantics), each under
                a generous per-symbol wait_for;
              * stocks are pre-filtered by data readiness (ONE grouped DB
                query — unready names are skipped-and-reported, not crashed),
                then a rotating batch runs under an asyncio.Semaphore with a
                short per-symbol wait_for. Feature builds / selector / policy
                already run in asyncio.to_thread inside the service.
            Worst case ≈ 3×75s (indices) + 90s (batch watchlist refresh
            budget) + 2×15s (readiness passes) + ceil(25/5)×20s (scans)
            ≈ 445s < the 600s runner timeout; a typical pass is well under
            one cadence interval.
            """
            results: list[dict[str, Any]] = []
            failures: dict[str, str] = {}
            default_timeframe = str(directional_service.config["default_timeframe"])
            lookback_sessions = int(directional_service.config["backtest"]["lookback_sessions"])

            def _result_row(underlying: str, snapshot: dict[str, Any], kind: str) -> dict[str, Any]:
                current_snapshot = dict(snapshot.get("snapshot") or {})
                signal = current_snapshot.get("signal") or {}
                risk = current_snapshot.get("risk") or {}
                data_status = current_snapshot.get("data_status") or {}
                regime = current_snapshot.get("regime") or {}
                contract = current_snapshot.get("selected_contract") or {}
                approved = bool(risk.get("approved"))
                execution_ready = bool(data_status.get("execution_ready"))
                # Surface the *why-not* so the summary view is self-explanatory
                # instead of "signal=None actionable=False" for every desk.
                rejection_reasons = list(risk.get("reasons") or [])
                if not execution_ready:
                    rejection_reasons.insert(
                        0,
                        f"data_status not ready: {data_status.get('degraded_reason') or 'unknown'}",
                    )
                return {
                    "underlying": underlying,
                    "kind": kind,
                    "as_of": current_snapshot.get("as_of"),
                    "spot_price": current_snapshot.get("spot_price"),
                    "direction": signal.get("direction"),
                    "confidence": signal.get("confidence"),
                    "expected_move": signal.get("expected_move"),
                    "expected_move_pct": signal.get("expected_move_pct"),
                    "sleeve": signal.get("sleeve"),
                    "thesis": signal.get("thesis"),
                    "regime_label": regime.get("label"),
                    "regime_confidence": regime.get("confidence"),
                    "approved": approved,
                    "actionable": approved and execution_ready and signal.get("direction") is not None,
                    "execution_ready": execution_ready,
                    "selection_reason": current_snapshot.get("selection_reason"),
                    "rejection_reasons": rejection_reasons,
                    "trading_symbol": contract.get("trading_symbol"),
                    "bucket": current_snapshot.get("bucket"),
                }

            async def _scan(underlying: str, *, kind: str, timeout_s: float) -> None:
                try:
                    snapshot = await asyncio.wait_for(
                        directional_service.record_paper_snapshot(
                            underlying,
                            default_timeframe,
                            lookback_sessions,
                        ),
                        timeout=timeout_s,
                    )
                    results.append(_result_row(underlying, snapshot, kind))
                except asyncio.TimeoutError:
                    failures[underlying] = f"{kind} scan timed out after {timeout_s:g}s"
                except Exception as exc:  # noqa: BLE001 — per-symbol isolation
                    failures[underlying] = str(exc) or type(exc).__name__

            universe = await directional_service.resolve_runner_universe()
            indices = list(universe.get("indices") or [])
            stocks = list(universe.get("stocks") or [])

            index_timeout = float(settings.DIRECTIONAL_INDEX_SYMBOL_TIMEOUT_SECONDS)
            for underlying in indices:
                await _scan(underlying, kind="index", timeout_s=index_timeout)

            stock_batch: list[str] = []
            skipped_unready: dict[str, str] = {}
            candidate_stocks: list[str] = []
            refresh_counts: dict[str, int] = {}
            if stocks:
                # Pass 1 — SPOT freshness decides batch candidacy only. The BG
                # watchlist build rotates all ~217 F&O names over hours, so
                # requiring a young snapshot row up front skipped ALL 50 stocks
                # on day one (option_quotes_stale_9000-12000s with live spot).
                # Option-quote honesty is enforced in pass 2, AFTER the batch's
                # rows are refreshed just-in-time.
                spot_limit = float(directional_service.config["paper_trading"]["stale_watchlist_seconds"])
                quote_limit = float(settings.DIRECTIONAL_STOCK_WATCHLIST_MAX_AGE_SECONDS)
                pre: dict[str, dict[str, Any]] | None
                try:
                    pre = await asyncio.wait_for(
                        directional_service.store.stock_readiness(
                            stocks,
                            spot_max_age_seconds=spot_limit,
                            watchlist_max_age_seconds=quote_limit,
                        ),
                        timeout=15.0,
                    )
                except Exception as exc:  # noqa: BLE001 — fail closed for stocks only
                    pre = None
                    skipped_unready = {s: f"readiness_check_failed: {exc}" for s in stocks}
                if pre is not None:
                    for symbol in stocks:
                        info = pre.get(str(symbol).upper()) or {}
                        if not info.get("latest_spot_time"):
                            skipped_unready[symbol] = "no_recent_spot_bars"
                        elif not info.get("spot_fresh"):
                            skipped_unready[symbol] = f"spot_stale_{int(info.get('spot_age_seconds') or 0)}s"
                        else:
                            candidate_stocks.append(symbol)
                    stock_batch = directional_service.next_stock_batch(candidate_stocks)
                if stock_batch and pre is not None:
                    refresh_age = float(settings.DIRECTIONAL_STOCK_WATCHLIST_REFRESH_AGE_SECONDS)
                    need_refresh = []
                    for symbol in stock_batch:
                        age = (pre.get(str(symbol).upper()) or {}).get("watchlist_age_seconds")
                        if age is None or float(age) > refresh_age:
                            need_refresh.append(symbol)
                    refresh_report: dict[str, str] = {}
                    if need_refresh:
                        try:
                            from market_data.atm_watchlist import atm_watchlist_service

                            refresh_report = await atm_watchlist_service.refresh_stock_snapshot_rows(
                                need_refresh,
                                budget_seconds=float(
                                    settings.DIRECTIONAL_STOCK_WATCHLIST_REFRESH_BUDGET_SECONDS
                                ),
                                concurrency=int(
                                    settings.DIRECTIONAL_STOCK_WATCHLIST_REFRESH_CONCURRENCY
                                ),
                            )
                        except Exception as exc:  # noqa: BLE001 — refresh is best-effort; pass 2 still gates
                            refresh_report = {s: f"error:{str(exc)[:80]}" for s in need_refresh}
                    from collections import Counter as _RefreshCounter

                    refresh_counts = dict(_RefreshCounter(refresh_report.values()))
                    # Pass 2 — the honesty gate proper, batch-only. Symbols whose
                    # rows are STILL stale (refresh timeout/budget/error) are
                    # skipped-and-reported, never evaluated against old quotes.
                    try:
                        post = await asyncio.wait_for(
                            directional_service.store.stock_readiness(
                                stock_batch,
                                spot_max_age_seconds=spot_limit,
                                watchlist_max_age_seconds=quote_limit,
                            ),
                            timeout=15.0,
                        )
                    except Exception as exc:  # noqa: BLE001 — fail closed
                        post = None
                        for symbol in stock_batch:
                            skipped_unready[symbol] = f"readiness_check_failed: {exc}"
                        stock_batch = []
                    if post is not None:
                        evaluable: list[str] = []
                        for symbol in stock_batch:
                            info = post.get(str(symbol).upper()) or {}
                            if info.get("spot_fresh") and info.get("watchlist_fresh"):
                                evaluable.append(symbol)
                                continue
                            if not info.get("latest_spot_time"):
                                base = "no_recent_spot_bars"
                            elif not info.get("spot_fresh"):
                                base = f"spot_stale_{int(info.get('spot_age_seconds') or 0)}s"
                            elif not int(info.get("watchlist_rows") or 0):
                                base = "no_atm_watchlist_rows"
                            else:
                                base = f"option_quotes_stale_{int(info.get('watchlist_age_seconds') or 0)}s"
                            note = refresh_report.get(symbol)
                            skipped_unready[symbol] = f"{base} (refresh={note})" if note else base
                        stock_batch = evaluable
                if stock_batch:
                    semaphore = asyncio.Semaphore(max(1, int(settings.DIRECTIONAL_STOCK_SCAN_CONCURRENCY)))
                    stock_timeout = float(settings.DIRECTIONAL_STOCK_SYMBOL_TIMEOUT_SECONDS)

                    async def _bounded_scan(symbol: str) -> None:
                        async with semaphore:
                            await _scan(symbol, kind="stock", timeout_s=stock_timeout)

                    await asyncio.gather(*(_bounded_scan(symbol) for symbol in stock_batch))

            if not results and failures:
                joined = "; ".join(f"{symbol}: {detail}" for symbol, detail in failures.items())
                raise RuntimeError(f"Directional options paper cycle failed: {joined}")
            from collections import Counter as _Counter

            rejection_counts: _Counter = _Counter()
            skip_reason_counts: _Counter = _Counter()
            actionable_count = 0
            for item in results:
                if item.get("actionable"):
                    actionable_count += 1
                else:
                    for reason in item.get("rejection_reasons") or []:
                        rejection_counts[str(reason)[:80]] += 1
            for reason in skipped_unready.values():
                skip_reason_counts[str(reason)[:60]] += 1
            return {
                "symbols_requested": indices + stock_batch,
                "symbols_completed": [item.get("underlying") for item in results],
                "result_count": len(results),
                "actionable_count": actionable_count,
                "rejection_counts": dict(rejection_counts.most_common(10)),
                "failure_count": len(failures),
                "failures": failures,
                "stock_universe": {
                    "source": universe.get("stock_universe_source"),
                    "total": len(stocks),
                    # candidates = spot-fresh names eligible for batch rotation;
                    # option-quote honesty is enforced per-batch AFTER the
                    # just-in-time refresh (see refresh_counts).
                    "candidates": len(candidate_stocks),
                    "batch": stock_batch,
                    "batch_size": len(stock_batch),
                    "refresh_counts": refresh_counts,
                    "skipped_unready_count": len(skipped_unready),
                    "skipped_reason_counts": dict(skip_reason_counts.most_common(10)),
                },
                "results": results,
            }

        async def _directional_positioning_runner() -> dict[str, Any]:
            """Refresh directional_positioning_daily for the directional universe.

            Once-per-session post-close (post_close_force_daily) so the positional
            lane reads FRESH PCR / oi_build / HTF next session instead of a stale
            snapshot (the feed previously only updated via a manual CLI). In-session
            passes are idempotent (upsert by (underlying, d))."""
            from directional_options.positioning_feed import _ensure_table, compute_and_store

            results: list[dict[str, Any]] = []
            failures: dict[str, str] = {}
            try:
                await asyncio.wait_for(_ensure_table(), timeout=30.0)
                for u in list(directional_service.config["universe"]):
                    try:
                        rep = await asyncio.wait_for(compute_and_store(u), timeout=180.0)
                        results.append(rep)
                    except Exception as exc:  # noqa: BLE001
                        failures[u] = str(exc)
            except asyncio.TimeoutError:
                return {"status": "timeout", "result_count": 0, "failure_count": 1,
                        "failures": {"ensure_table": "timed out"}, "results": []}
            stored = sum(int(r.get("stored") or 0) for r in results)
            return {"status": "ok" if not failures else "partial",
                    "result_count": stored,
                    "actionable_count": sum(1 for r in results if int(r.get("stored") or 0) > 0),
                    "failure_count": len(failures), "failures": failures, "results": results}

        async def _commodity_mp_history_runner() -> dict[str, Any]:
            """Write-once durable commodity MP history (MCX) + adaptive CVD/volume
            baselines. Derives per-session daily profiles from the durable 1-min
            MCX spot store at the per-instrument coarse value tick (so the live
            HTF gate reads non-degenerate value areas), persisting ONLY missing
            sessions. post_close_catchup appends the just-closed MCX session once
            after ~23:30 IST; the startup is-due fire does the one-time/gap
            backfill. Idempotent + write-once; builds all commodity roots."""
            from market_data.commodity_contract_specs import COMMODITY_CONTRACT_SPECS
            from paper_engine.commodity_mp_history import backfill_commodity_mp_history

            lookback = int(settings.COMMODITY_MP_HISTORY_BACKFILL_SESSIONS)
            results: list[dict[str, Any]] = []
            failures: dict[str, str] = {}
            persisted_total = 0
            for root in COMMODITY_CONTRACT_SPECS:
                try:
                    report = await asyncio.wait_for(
                        backfill_commodity_mp_history(root, lookback_sessions=lookback, reason="supervisor"),
                        timeout=120.0,
                    )
                    results.append(report)
                    persisted_total += int(report.get("missing_persisted") or 0)
                except Exception as exc:  # noqa: BLE001
                    failures[root] = str(exc)
            baseline_report: dict[str, Any] = {}
            try:
                from paper_engine.commodity_volume_baseline import backfill_all_baselines
                baseline_report = await asyncio.wait_for(
                    backfill_all_baselines(list(COMMODITY_CONTRACT_SPECS), lookback_sessions=lookback, reason="supervisor"),
                    timeout=120.0,
                )
            except Exception as exc:  # noqa: BLE001
                failures["_volume_baselines"] = str(exc)
            return {
                "status": "ok" if not failures else "partial",
                "result_count": len(results),
                "actionable_count": persisted_total,
                "failure_count": len(failures),
                "failures": failures,
                "results": results,
                "volume_baselines": baseline_report,
            }

        async def _macd_refined_runner() -> dict[str, Any]:
            """Fetch current + next monthly expiry chains, persist per-contract
            volume/turnover, and sync the MACD Refined paper book. This runner
            is registered with market_hours_fn=_in_nse_market_hours and
            post_close_catchup=False, so the supervisor only fires it INSIDE the
            NSE session — entries are therefore always session-gated by the
            supervisor (no separate wall-clock check needed)."""
            try:
                # Full F&O universe × current+next expiry → allow a wide budget.
                result = await asyncio.wait_for(
                    macd_refined_service.run_live_cycle(allow_entries=True),
                    timeout=1140.0,
                )
            except asyncio.TimeoutError:
                return {
                    "status": "timeout", "result_count": 0, "failure_count": 1,
                    "failures": {"macd_refined": "timed out after 1140s"}, "results": [],
                }
            paper_summary = dict(result.get("paper_summary") or {})
            result_count = int(result.get("snapshots_persisted") or 0)
            failures = dict(result.get("failures") or {})
            failure_count = len(failures)
            broker_ready = bool(result.get("broker_ready"))
            if not broker_ready:
                cycle_status = "broker_not_ready"
            elif failure_count and result_count == 0:
                cycle_status = "error"
            elif failure_count:
                cycle_status = "partial"
            else:
                cycle_status = "ok"
            first_failure = next(iter(failures.items()), None)
            message = None
            if first_failure:
                message = (
                    f"MACD Refined failed for {failure_count} target(s); "
                    f"{first_failure[0]}: {first_failure[1]}"
                )
            return {
                "status": cycle_status,
                "message": message,
                "result_count": result_count,
                "actionable_count": int(result.get("proposals") or 0),
                "failure_count": failure_count,
                "failure_samples": dict(list(failures.items())[:10]),
                "broker_ready": broker_ready,
                "storage_ready": bool(result.get("storage_ready")),
                "paper_summary": paper_summary,
                "fetched": result.get("fetched") or {},
                "funnel": result.get("funnel") or {},
            }

        async def _macd_refined_marks_runner() -> dict[str, Any]:
            """Seconds-cadence protective-exit heartbeat for open macd_refined
            positions (owner directive 2026-07-17: "position updates must be in
            SECONDS"). The 30m _macd_refined_runner sets the entry SIDE; this
            re-marks held positions off the REAL-TIME plane (Fyers-WS tick →
            Redis tick:{symbol} → shared oc: chain cache — never a broker REST /
            route_order fetch) and fires ONLY the existing stop/target/trail/
            window exits, so a breached stop is caught in seconds instead of
            waiting up to 30 minutes. Market-hours only (post_close_catchup=
            False); nothing to re-mark on frozen post-close bars."""
            try:
                result = await asyncio.wait_for(
                    macd_refined_service.refresh_paper_marks(),
                    timeout=60.0,
                )
            except asyncio.TimeoutError:
                return {
                    "status": "timeout", "result_count": 0, "failure_count": 1,
                    "failures": {"macd_refined_marks": "timed out after 60s"}, "results": [],
                }
            refreshed = int(result.get("refreshed") or 0)
            return {
                "status": "ok",
                "result_count": refreshed,
                "actionable_count": int(result.get("exits") or 0),
                "failure_count": 0,
                "positions": int(result.get("positions") or 0),
                "paper_summary": dict(result.get("paper_summary") or {}),
            }

        async def _cbe_runner() -> dict[str, Any]:
            """Run one CBE scan + sync the cash-equity paper book.

            The CBE scanner is end-of-day in design (its features look at
            daily OHLC, IV/PCR snapshots, sector momentum). Running it
            periodically through the day re-evaluates the watchlist against
            fresh prices, so bias flips are caught quickly and stale
            watchlist entries get dropped after FLAT_CONFIRMATION_SCANS."""
            from cbe_scanner.service import run_scan as _run_cbe_scan

            try:
                payload = await asyncio.wait_for(_run_cbe_scan(), timeout=180.0)
            except asyncio.TimeoutError:
                return {
                    "status": "timeout",
                    "result_count": 0,
                    "failure_count": 1,
                    "failures": {"cbe_scan": "timed out after 180s"},
                    "results": [],
                }
            watchlist = list(payload.get("watchlist") or [])
            paper_summary = dict(payload.get("paper_summary") or {})
            return {
                "status": "ok",
                "result_count": int(payload.get("scored_count") or 0),
                "actionable_count": len(watchlist),
                "watchlist_count": int(payload.get("watchlist_count") or 0),
                "failure_count": 0,
                "paper_summary": paper_summary,
                "scan_date": payload.get("scan_date"),
                "run_id": payload.get("run_id"),
            }

        async def _cbe_marks_runner() -> dict[str, Any]:
            """Lightweight 5-min LTP refresh for CBE open paper positions.

            Re-marks held cash-equity positions off the latest close WITHOUT
            re-running the heavy alpha pipeline — keeps the UI's LTP fresh
            between scans at a fraction of the CPU cost. Market-hours only
            (post_close_catchup=False); nothing to refresh after the close."""
            from cbe_scanner.service import refresh_paper_marks as _refresh_cbe_marks

            try:
                result = await asyncio.wait_for(_refresh_cbe_marks(), timeout=60.0)
            except asyncio.TimeoutError:
                return {
                    "status": "timeout",
                    "result_count": 0,
                    "failure_count": 1,
                    "failures": {"cbe_marks": "timed out after 60s"},
                    "results": [],
                }
            refreshed = int(result.get("refreshed") or 0)
            return {
                "status": "ok",
                "result_count": refreshed,
                "actionable_count": refreshed,
                "failure_count": 0,
                "paper_summary": dict(result.get("paper_summary") or {}),
                "symbols": result.get("symbols") or [],
            }

        async def _gann_runner() -> dict[str, Any]:
            try:
                result = await asyncio.wait_for(
                    gann_tp_delta_service.run_paper_agent_once(),
                    timeout=120.0,
                )
            except asyncio.TimeoutError:
                return {
                    "status": "timeout",
                    "result_count": 0,
                    "failure_count": 1,
                    "failures": {"paper_agent": "timed out after 120s"},
                    "results": [],
                }
            from collections import Counter as _Counter

            last_run = result.get("last_run") if isinstance(result, dict) else {}
            recent = (result or {}).get("recent_signals") or []
            rejection_counts: _Counter = _Counter()
            for r in recent:
                if str(r.get("decision")) == "skip":
                    rejection_counts[str(r.get("reason") or "unknown")[:80]] += 1
            actionable = int((last_run or {}).get("opened") or 0)
            return {
                "status": "ok",
                "result_count": int((last_run or {}).get("scanned") or 0),
                "actionable_count": actionable,
                "rejection_counts": dict(rejection_counts.most_common(10)),
                "failure_count": int((last_run or {}).get("errors") or 0),
                "result": result,
            }

        async def _lane_audit_runner() -> dict[str, Any]:
            """Post-close signal-correctness audit for every registered lane.

            Runs the audits framework (replay parity, gate attribution, trade
            reconciliation, edge persistence) once per session so live signals
            are mechanically checked against the strategy definition instead of
            only being eyeballed via P&L. Persists to lane_audit; results surface
            in /api/system/health. Auditors are added to audits.lanes.REGISTRY."""
            from datetime import timedelta as _td

            from audits.lane_audit import run_one as _audit_run_one
            from audits.lanes import REGISTRY as _AUDIT_REGISTRY

            audit_date = _now_ist().date()
            results: list[dict[str, Any]] = []
            failures: dict[str, str] = {}
            statuses: list[str] = []
            for lane in list(_AUDIT_REGISTRY):
                try:
                    res = await asyncio.wait_for(
                        _audit_run_one(lane, audit_date, lookback_days=30), timeout=180.0
                    )
                    status = str(getattr(res, "overall_status", "") or "unknown")
                    statuses.append(status)
                    results.append({"lane": lane, "overall_status": status})
                except Exception as exc:  # noqa: BLE001 — isolate one lane's failure
                    failures[lane] = str(exc)[:200]
            # A non-passing audit is actionable (surfaces a drifted lane).
            actionable = sum(1 for s in statuses if s not in {"pass", "ok", "unknown"})
            return {
                "status": "ok" if not failures else ("error" if not results else "partial"),
                "result_count": len(results),
                "actionable_count": actionable,
                "failure_count": len(failures),
                "failures": failures,
                "results": results,
            }

        return [
            RunnerConfig(
                key="option_flow_watchdog",
                label="Option-Flow Freshness Watchdog",
                interval_seconds=getattr(
                    settings, "OPTION_FLOW_WATCHDOG_INTERVAL_SECONDS", 60
                ),
                callback=_option_flow_watchdog_runner,
                # Default OFF (OPTION_FLOW_WATCHDOG_ENABLED) — opt-in monitor.
                enabled=getattr(settings, "OPTION_FLOW_WATCHDOG_ENABLED", False),
                # NSE session hours: option_premium_candles is the NSE-options
                # feed. broker_profile left DEFAULT so neither cadence-group kill
                # switch (SLOW/FAST_LANES_ENABLED) darks the monitor — it must
                # keep watching even when a lane group is disabled.
                market_hours_fn=_in_nse_market_hours,
                next_open_fn=_next_nse_market_open,
                # A stall watcher has nothing to catch up after the close.
                post_close_catchup=False,
                # Data-plane freshness monitor — watches option_premium_candles
                # persistence, which the CORE plane's feed pipeline owns.
                plane="core",
            ),
            RunnerConfig(
                key="stock_spot_sweep",
                label="F&O Stock Spot Post-Close Sweep",
                # Never reached in-session: market_hours_fn is _never_in_session,
                # so the ONLY dispatch path is the once-per-session post-close
                # catch-up. interval_seconds is therefore inert (kept non-zero
                # only because RunnerConfig requires a cadence). The sweep is
                # idempotent (ON CONFLICT DO NOTHING) so a retry is a no-op.
                interval_seconds=3600,
                callback=_stock_spot_sweep_runner,
                enabled=settings.STOCK_SPOT_SWEEP_ENABLED,
                # broker_profile left DEFAULT: this is data maintenance, not a
                # decision lane, so neither cadence-group kill switch should
                # dark it — its budget safety comes from CLASS_BULK instead.
                # _in_nse_market_hours here would make the supervisor ALSO fire
                # this 211-symbol sweep hourly DURING the session — and
                # immediately at 09:15, since last_started_at is None. That is
                # precisely the contention this lane exists to avoid, so the
                # window is always-closed and the post-close branch is the only
                # way in.
                market_hours_fn=_never_in_session,
                next_open_fn=_next_nse_market_open,
                # The whole point: run AFTER the close, once per session, so the
                # ~211-name sweep never competes with live decision traffic.
                post_close_catchup=True,
                post_close_force_daily=True,
                # ~211 names x 2 intervals at ~3 req/s ≈ 150s; allow headroom for
                # broker slowness. The sweep also self-limits via deadline_seconds.
                timeout_seconds=1200.0,
                # Spot ingestion belongs to the core data plane.
                plane="core",
            ),
            RunnerConfig(
                key="macd_preopen_watchlist",
                label="MACD Pre-open ATM Watchlist (expiry + freeze)",
                # 5-minute cadence: fine enough to land inside the ten-minute
                # 09:04-09:14 pre-open window, cheap enough that the pre-09:00
                # validation phase is not chatty. The build is idempotent — a
                # second pass inside the window sees the frozen ladder and
                # returns "already_frozen" rather than re-racing it.
                interval_seconds=300,
                callback=_macd_preopen_watchlist_runner,
                enabled=getattr(settings, "MACD_PREOPEN_WATCHLIST_ENABLED", False),
                # Pre-open only. There is no meaningful post-close "catch-up"
                # for a pre-open ladder: a 15:35 build would freeze a ladder for
                # a session that is already over.
                market_hours_fn=_in_token_readiness_window,
                next_open_fn=_next_token_readiness_open,
                post_close_catchup=False,
                # Phase 1 can walk the whole universe's warm-up under BULK.
                timeout_seconds=1200.0,
                plane="core",
            ),
            RunnerConfig(
                key="token_readiness",
                label="Pre-open Broker Token Readiness",
                interval_seconds=900,
                callback=_token_readiness_runner,
                enabled=getattr(settings, "TOKEN_READINESS_AUTO_ENABLED", True),
                market_hours_fn=_in_token_readiness_window,
                next_open_fn=_next_token_readiness_open,
                # The sweep only means anything BEFORE the 09:15 open — brokers
                # expire tokens daily (Upstox 03:30 IST), so a post-close
                # "catch-up" validation at 15:35+/22:26 is a false all-clear.
                # Observed running post-close 3× on 2026-07-15 via the generic
                # catch-up path; this pins it to its pre-open window only.
                post_close_catchup=False,
                # Auth/token plumbing lives with the broker-session owner (core).
                plane="core",
            ),
            RunnerConfig(
                key="market_intelligence",
                label="Market Intelligence Refresh",
                interval_seconds=settings.MARKET_INTELLIGENCE_REFRESH_INTERVAL_SECONDS,
                callback=_market_intelligence_runner,
                broker_profile="slow",
                enabled=settings.MARKET_INTELLIGENCE_AUTO_ENABLED,
                # The dominant SLOW-quota watchlist/chain REST consumer stays
                # with the WS/watchlist (core) plane per the Friday study.
                plane="core",
            ),
            RunnerConfig(
                key="auction_intelligence",
                label="Auction Intelligence Paper Cycle",
                interval_seconds=settings.AUCTION_INTELLIGENCE_AUTO_INTERVAL_SECONDS,
                callback=_auction_runner,
                broker_profile="fast",
                enabled=settings.AUCTION_INTELLIGENCE_AUTO_ENABLED,
                # This lane can create paper positions. Never run the generic
                # post-close recovery pass against frozen end-of-day bars.
                market_hours_fn=_in_nse_market_hours,
                next_open_fn=_next_nse_market_open,
                post_close_catchup=False,
                # The configured multi-index cycle can exceed the 300s global
                # ceiling on a cold option-chain cache. Keep the timeout
                # bounded, but let the cycle finish; the automation performs a
                # second market-hours check before any durable trade write.
                timeout_seconds=600.0,
                # Open stagger: let market_intelligence (offset 0) claim the
                # broker budget for the watchlist build before this lane joins.
                start_offset_seconds=90.0,
            ),
            RunnerConfig(
                key="auction_intelligence_commodity",
                label="Auction Intelligence Commodity Cycle",
                interval_seconds=settings.AUCTION_INTELLIGENCE_COMMODITY_INTERVAL_SECONDS,
                callback=_auction_commodity_runner,
                broker_profile="fast",
                enabled=settings.AUCTION_INTELLIGENCE_COMMODITY_ENABLED,
                # Same MP+OF machinery as the NSE index lane, but over the MCX
                # session (09:00-23:30) — the evening/extended hours when NSE is
                # closed. Creates commodity-futures paper positions in a SEPARATE
                # book; never runs the post-close recovery pass against frozen
                # end-of-session bars. Per-symbol MP CPU runs in asyncio.to_thread
                # inside the service, so this cannot seize the event loop.
                market_hours_fn=_in_mcx_market_hours,
                next_open_fn=_next_mcx_market_open,
                post_close_catchup=False,
                # The 3-root cycle can exceed the 300s global ceiling on a cold
                # commodity-store fetch; keep the timeout bounded but generous,
                # and the automation re-checks the MCX session before any write.
                timeout_seconds=600.0,
                # Let the IC-commodity lane (offset 0) claim the broker budget for
                # its universe resolution before this lane joins the MCX open.
                start_offset_seconds=90.0,
            ),
            RunnerConfig(
                key="institutional_convergence",
                label="Institutional Convergence Shadow Cycle",
                interval_seconds=settings.INSTITUTIONAL_CONVERGENCE_AUTO_INTERVAL_SECONDS,
                callback=_institutional_convergence_runner,
                broker_profile="fast",
                enabled=settings.INSTITUTIONAL_CONVERGENCE_AUTO_ENABLED,
                market_hours_fn=_in_institutional_convergence_window,
                next_open_fn=_next_institutional_convergence_open,
                post_close_catchup=False,
                timeout_seconds=600.0,
                start_offset_seconds=90.0,
            ),
            RunnerConfig(
                key="institutional_convergence_commodity",
                label="Institutional Convergence Commodity Cycle",
                interval_seconds=settings.INSTITUTIONAL_CONVERGENCE_COMMODITY_INTERVAL_SECONDS,
                callback=_institutional_convergence_commodity_runner,
                broker_profile="fast",
                enabled=settings.INSTITUTIONAL_CONVERGENCE_COMMODITY_ENABLED,
                market_hours_fn=_in_commodity_convergence_window,
                next_open_fn=_next_commodity_convergence_open,
                post_close_catchup=False,
                timeout_seconds=600.0,
                start_offset_seconds=90.0,
            ),
            RunnerConfig(
                key="fractal_market_profile",
                label="Fractal Market Profile Paper Cycle",
                interval_seconds=settings.FRACTAL_MARKET_PROFILE_AUTO_INTERVAL_SECONDS,
                callback=_fmp_runner,
                broker_profile="fast",
                enabled=settings.FRACTAL_MARKET_PROFILE_AUTO_ENABLED,
                market_hours_fn=_in_gann_market_hours,
                next_open_fn=_next_gann_market_open,
            ),
            RunnerConfig(
                key="directional_options",
                label="Directional Options Paper Cycle",
                interval_seconds=settings.DIRECTIONAL_OPTIONS_AUTO_INTERVAL_SECONDS,
                callback=_directional_runner,
                broker_profile="fast",
                enabled=settings.DIRECTIONAL_OPTIONS_AUTO_ENABLED,
                # NSE index + NIFTY-50 stock options — trade during the session,
                # never on the post-close frozen `live_tick` heartbeat (last
                # price re-stamped with 0 volume after 15:30 IST). No
                # after-hours catch-up either.
                market_hours_fn=_in_nse_market_hours,
                next_open_fn=_next_nse_market_open,
                post_close_catchup=False,
                # 2026-07-17 NIFTY-50 expansion: worst case index-serial +
                # stock-batch pass ≈ 325s, above the 300s global ceiling.
                # Typical passes stay well under one 180s cadence interval.
                timeout_seconds=600.0,
                start_offset_seconds=90.0,
            ),
            RunnerConfig(
                key="directional_positioning",
                label="Directional Positioning Feed Refresh",
                interval_seconds=getattr(
                    settings, "DIRECTIONAL_POSITIONING_REFRESH_INTERVAL_SECONDS", 3600
                ),
                callback=_directional_positioning_runner,
                broker_profile="fast",
                # Only run when the positional lane is live (else the feed is unused).
                enabled=settings.DIRECTIONAL_POSITIONAL_OPTIONS_ENABLED,
                # Guaranteed once-a-day post-15:35 EOD write so the feed is fresh
                # before the next session; in-session passes are idempotent upserts.
                market_hours_fn=_in_nse_market_hours,
                next_open_fn=_next_nse_market_open,
                post_close_catchup=True,
                post_close_force_daily=True,
            ),
            RunnerConfig(
                key="commodity_mp_history",
                label="Commodity MP Durable History",
                interval_seconds=getattr(
                    settings, "COMMODITY_MP_HISTORY_AUTO_INTERVAL_SECONDS", 21600
                ),
                callback=_commodity_mp_history_runner,
                broker_profile="fast",
                enabled=settings.COMMODITY_MP_HISTORY_AUTO_ENABLED,
                timeout_seconds=420.0,
                # Data-maintenance from the durable MCX 1-min spot store; MCX
                # hours + post-close catch-up appends the just-closed session,
                # startup is-due does the one-time/gap backfill. Write-once.
                market_hours_fn=_in_mcx_market_hours,
                next_open_fn=_next_mcx_market_open,
                post_close_catchup=True,
            ),
            RunnerConfig(
                key="macd_refined",
                label="MACD Refined Paper Cycle",
                interval_seconds=settings.MACD_REFINED_AUTO_INTERVAL_SECONDS,
                callback=_macd_refined_runner,
                broker_profile="slow",
                enabled=settings.MACD_REFINED_AUTO_ENABLED,
                # Full F&O universe × current+next expiry needs more than the
                # 300s global ceiling for a cold-start cycle.
                timeout_seconds=1200.0,
                # Long-premium stock + index book. Fetch current+next monthly
                # expiry and trade during the NSE session; no after-hours
                # frozen-heartbeat entries (post_close_catchup=False).
                market_hours_fn=_in_nse_market_hours,
                next_open_fn=_next_nse_market_open,
                post_close_catchup=False,
                # The heaviest bulk sweep gets the longest stagger: 30 min
                # after the observed open AND never before 09:45 IST, so the
                # watchlist build + first S1 scans own the open window (the
                # BULK quota class caps it after that).
                start_offset_seconds=1800.0,
                no_start_before=time(9, 45),
            ),
            RunnerConfig(
                key="macd_refined_marks",
                label="MACD Refined Paper Marks / Protective Exits",
                interval_seconds=settings.MACD_REFINED_MARKS_REFRESH_INTERVAL_SECONDS,
                callback=_macd_refined_marks_runner,
                enabled=settings.MACD_REFINED_AUTO_ENABLED,
                # broker_profile left DEFAULT deliberately: this pass reads the
                # real-time Fyers-WS plane (no Upstox / route_order fetch), so it
                # must NOT be darked by the SLOW_LANES_ENABLED cadence-group kill
                # switch — held positions still need seconds-cadence stop/target
                # protection even when the slow DECISION group is degraded. Same
                # reasoning as option_flow_watchdog. Runs from the open (no
                # start_offset / no_start_before) so a position opened early is
                # protected immediately; market-hours only.
                market_hours_fn=_in_nse_market_hours,
                next_open_fn=_next_nse_market_open,
                post_close_catchup=False,
                timeout_seconds=90.0,
            ),
            RunnerConfig(
                key="cbe_scanner",
                label="CBE Scanner Paper Cycle",
                interval_seconds=settings.CBE_SCANNER_AUTO_INTERVAL_SECONDS,
                callback=_cbe_runner,
                broker_profile="slow",
                enabled=settings.CBE_SCANNER_AUTO_ENABLED,
                # The hourly in-session passes use the previous completed
                # session (CBE's _completed_session_cutoff excludes today until
                # 15:35). post_close_force_daily makes a guaranteed once-a-day
                # pass fire after 15:35 — scoring on today's finalized close —
                # which is the canonical signal for the next session.
                market_hours_fn=_in_nse_market_hours,
                next_open_fn=_next_nse_market_open,
                post_close_catchup=True,
                post_close_force_daily=True,
            ),
            RunnerConfig(
                key="cbe_marks",
                label="CBE Paper Marks Refresh",
                interval_seconds=settings.CBE_MARKS_REFRESH_INTERVAL_SECONDS,
                callback=_cbe_marks_runner,
                broker_profile="slow",
                enabled=settings.CBE_SCANNER_AUTO_ENABLED,
                market_hours_fn=_in_nse_market_hours,
                next_open_fn=_next_nse_market_open,
                post_close_catchup=False,
            ),
            RunnerConfig(
                key="gann_tp_delta",
                label="Gann TP Delta Paper Cycle",
                interval_seconds=settings.GANN_TP_DELTA_AUTO_INTERVAL_SECONDS,
                callback=_gann_runner,
                broker_profile="slow",
                enabled=settings.GANN_TP_DELTA_AUTO_ENABLED,
                market_hours_fn=_in_gann_market_hours,
                next_open_fn=_next_gann_market_open,
            ),
            RunnerConfig(
                key="lane_audit",
                label="Lane Signal-Correctness Audit",
                # Runs once per session after the close (post_close_force_daily),
                # so it audits today's finalized signals against the strategy
                # definition. Hourly in-session interval is a harmless upper
                # bound; the guaranteed pass is the post-close one.
                interval_seconds=settings.LANE_AUDIT_INTERVAL_SECONDS,
                callback=_lane_audit_runner,
                enabled=settings.LANE_AUDIT_ENABLED,
                market_hours_fn=_in_nse_market_hours,
                next_open_fn=_next_nse_market_open,
                post_close_catchup=True,
                post_close_force_daily=True,
                timeout_seconds=600.0,
            ),
        ]

    async def start(self) -> None:
        if not self._enabled:
            logger.info("[MarketHoursSupervisor] disabled by config")
            return
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="market-hours-paper-supervisor")
        self._task.add_done_callback(self._on_loop_done)
        self._maybe_start_strategy_proxy_consumer()
        logger.info("[MarketHoursSupervisor] started")

    def _maybe_start_strategy_proxy_consumer(self) -> None:
        """Phase-2 ITEM 2: on the STRATEGY plane of a split boot, run the Redis
        command consumer so the core plane's proxied run-once / close /
        commodity-start calls reach the in-process agents. Started ONLY when
        is_split() and boots_strategies() and the proxy flag is on — so it never
        starts in the single-process boot (LANESET=all) nor on the core plane."""
        from core.laneset import boots_strategies, is_split

        if not (is_split() and boots_strategies()):
            return
        if not getattr(settings, "STRATEGY_PROXY_ENABLED", True):
            return
        if getattr(self, "_proxy_consumer_task", None) and not self._proxy_consumer_task.done():
            return
        from db.redis_client import consume_strategy_commands

        task = asyncio.create_task(
            consume_strategy_commands(_dispatch_strategy_command),
            name="strategy-proxy-consumer",
        )
        self._proxy_consumer_task = task
        self._maintenance_tasks.add(task)
        logger.info("[MarketHoursSupervisor] strategy-proxy consumer started (split strategy plane)")

    def _on_loop_done(self, task: asyncio.Task) -> None:
        # Runs when the scheduling loop task ends. A clean end or a deliberate
        # cancel (stop()) is fine; an EXCEPTION means the loop died and every
        # lane would go silent — alert loudly and auto-restart (bounded).
        if task.cancelled() or self._task is not task:
            return
        exc = task.exception()
        if exc is None:
            return
        self._loop_crash_count += 1
        logger.exception(
            "[MarketHoursSupervisor] loop crashed (#{n}): {e}",
            n=self._loop_crash_count, e=repr(exc),
        )

        async def _alert_and_restart() -> None:
            try:
                await self._emit_scan_audit(
                    "supervisor_loop", result=None,
                    error=f"scheduling loop crashed (#{self._loop_crash_count}): {exc!r}",
                )
            except Exception:  # noqa: BLE001 — never let alerting block a restart
                pass
            if not self._enabled:
                return
            if self._loop_crash_count > self._loop_max_restarts:
                logger.error(
                    "[MarketHoursSupervisor] loop crashed {n}× — NOT restarting; "
                    "all lanes are stopped until the backend is redeployed.",
                    n=self._loop_crash_count,
                )
                return
            logger.warning("[MarketHoursSupervisor] restarting scheduling loop after crash")
            self._task = asyncio.create_task(self._loop(), name="market-hours-paper-supervisor")
            self._task.add_done_callback(self._on_loop_done)

        try:
            asyncio.get_running_loop().create_task(_alert_and_restart())
        except RuntimeError:
            pass

    async def stop(self) -> None:
        task = self._task
        self._task = None
        tasks = [task] if task is not None else []
        tasks.extend(self._runner_tasks.values())
        tasks.extend(self._maintenance_tasks)
        for pending in tasks:
            pending.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._runner_tasks.clear()
        self._maintenance_tasks.clear()
        logger.info("[MarketHoursSupervisor] stopped")

    async def _loop(self) -> None:
        try:
            while True:
                # Dispatch only: no lane is allowed to hold the scheduler clock.
                await self._schedule_due_once()
                # Split boot only: mirror this plane's runner status to Redis and
                # cache the other plane's snapshot for merged status endpoints.
                # No-op (not even a Redis call) when LANESET=all.
                await self._publish_plane_status()
                now = self._now_fn()
                enabled_runners = [
                    runtime for runtime in self._runners.values()
                    if runtime.config.enabled
                ]
                if any(self._runtime_market_open(runtime, now) for runtime in enabled_runners):
                    await asyncio.sleep(max(int(settings.MARKET_HOURS_SUPERVISOR_LOOP_SECONDS), 5))
                else:
                    next_open = min(
                        (self._runtime_next_open(runtime, now) for runtime in enabled_runners),
                        default=self._next_open_fn(now),
                    )
                    seconds_until_open = max((next_open - now).total_seconds(), 60.0)
                    await asyncio.sleep(min(seconds_until_open, 300.0))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(f"[MarketHoursSupervisor] loop failed: {exc}")
            raise

    def _note_market_transitions(self, now: datetime) -> None:
        """Stamp each runner's closed→open transition so start_offset_seconds
        has an anchor; reset the stamp while the runner's window is closed so
        the stagger re-applies after EVERY market open."""
        for runtime in self._runners.values():
            if not runtime.config.enabled:
                runtime.market_open_since = None
                continue
            if self._runtime_market_open(runtime, now):
                if runtime.market_open_since is None:
                    runtime.market_open_since = now
            else:
                runtime.market_open_since = None

    async def _schedule_due_once(self) -> None:
        """Launch due lanes independently and return without awaiting scans."""
        if not self._enabled:
            return
        async with self._lock:
            now = self._now_fn()
            self._note_market_transitions(now)
            due_runners: list[RunnerRuntime] = []
            catchup_runners: list[RunnerRuntime] = []
            catchup_session_date = now.date()
            for runtime in self._runners.values():
                if (
                    not runtime.config.enabled
                    or runtime.running
                    or runtime.config.key in self._runner_tasks
                ):
                    continue
                runtime_market_open = self._runtime_market_open(runtime, now)
                if runtime_market_open and runtime.is_due(now):
                    due_runners.append(runtime)
                elif self._post_close_catchup_eligible(
                    runtime,
                    now=now,
                    session_date=catchup_session_date,
                    market_open=runtime_market_open,
                ):
                    catchup_runners.append(runtime)

            catchup_ids = {id(runtime) for runtime in catchup_runners}
            catchup_tasks: list[tuple[RunnerRuntime, asyncio.Task]] = []
            for runtime in due_runners + catchup_runners:
                runtime.running = True
                is_catchup = id(runtime) in catchup_ids
                if is_catchup:
                    # Stamp the attempt at DISPATCH so the next scheduler pass
                    # can never double-launch this catch-up while a slower
                    # batch peer is still running.
                    runtime.note_post_close_attempt(catchup_session_date)
                    coro = self._run_catchup_runner(
                        runtime, now=now, session_date=catchup_session_date
                    )
                else:
                    coro = self._run_runner(runtime, now=now)
                task = asyncio.create_task(
                    coro,
                    name=f"paper-lane-{runtime.config.key}",
                )
                self._runner_tasks[runtime.config.key] = task

                def _cleanup(
                    done: asyncio.Task,
                    *,
                    key: str = runtime.config.key,
                    scheduled_runtime: RunnerRuntime = runtime,
                ) -> None:
                    scheduled_runtime.running = False
                    if self._runner_tasks.get(key) is done:
                        self._runner_tasks.pop(key, None)

                task.add_done_callback(_cleanup)
                if is_catchup:
                    catchup_tasks.append((runtime, task))

            if catchup_tasks:
                maintenance = asyncio.create_task(
                    self._finalize_background_catchup(catchup_tasks, catchup_session_date),
                    name=f"paper-catchup-{catchup_session_date.isoformat()}",
                )
                self._maintenance_tasks.add(maintenance)
                maintenance.add_done_callback(self._maintenance_tasks.discard)

            for runtime in self._runners.values():
                if (
                    runtime.config.enabled
                    and not runtime.running
                    and runtime.last_message is None
                    and not self._runtime_market_open(runtime, now)
                ):
                    runtime.last_message = "Armed for the next market session."

    async def _run_catchup_runner(
        self,
        runtime: RunnerRuntime,
        *,
        now: datetime,
        session_date: date,
    ) -> None:
        """Run one post-close catch-up and stamp its success IMMEDIATELY.

        Stamping (and persisting) as soon as THIS runner finishes — instead of
        after the whole batch's gather — closes the window where a fast runner
        finished, its `running` flag cleared, and the next scheduler pass
        re-launched it because the batch was still waiting on a slower peer.
        """
        await self._run_runner(runtime, now=now)
        if runtime.last_error is None:
            runtime.last_post_close_success_date = session_date
            runtime.last_result_meta.setdefault("catchup_session_date", session_date.isoformat())
            runtime.last_message = (
                f"{runtime.last_message} Catch-up captured for {session_date.isoformat()}."
            )
            self._persist_catchup_state()

    async def _finalize_background_catchup(
        self,
        catchup_tasks: list[tuple[RunnerRuntime, asyncio.Task]],
        session_date: date,
    ) -> None:
        await asyncio.gather(*(task for _, task in catchup_tasks), return_exceptions=True)
        successful = [runtime for runtime, _ in catchup_tasks if runtime.last_error is None]
        if successful:
            try:
                from core.paper_trade_recorder import paper_trade_recorder

                await paper_trade_recorder.snapshot_daily(session_date=session_date.isoformat())
            except Exception as exc:
                logger.warning("[MarketHoursSupervisor] portfolio snapshot failed: {}", exc)
    async def run_due_once(self, *, force: bool = False) -> dict[str, Any]:
        if not self._enabled:
            return self.get_status()

        async with self._lock:
            now = self._now_fn()
            self._note_market_transitions(now)
            due_runners: list[RunnerRuntime] = []
            catchup_runners: list[RunnerRuntime] = []
            catchup_session_date = now.date()
            for runtime in self._runners.values():
                if (
                    not runtime.config.enabled
                    or runtime.running
                    or runtime.config.key in self._runner_tasks
                ):
                    continue
                runtime_market_open = self._runtime_market_open(runtime, now)
                if force or (runtime_market_open and runtime.is_due(now)):
                    due_runners.append(runtime)
                    continue
                if not force and self._post_close_catchup_eligible(
                    runtime,
                    now=now,
                    session_date=catchup_session_date,
                    market_open=runtime_market_open,
                ):
                    catchup_runners.append(runtime)

            if due_runners:
                await self._run_due_runners(due_runners, now=now)
            if catchup_runners:
                for runtime in catchup_runners:
                    runtime.note_post_close_attempt(catchup_session_date)
                await asyncio.gather(
                    *(
                        self._run_catchup_runner(
                            runtime, now=now, session_date=catchup_session_date
                        )
                        for runtime in catchup_runners
                    )
                )
                # End-of-session portfolio reconciliation snapshot.
                try:
                    from core.paper_trade_recorder import paper_trade_recorder

                    await paper_trade_recorder.snapshot_daily(
                        session_date=catchup_session_date.isoformat()
                    )
                except Exception as exc:
                    logger.warning(
                        "[MarketHoursSupervisor] portfolio snapshot failed: {}", exc
                    )
            for runtime in self._runners.values():
                if (
                    runtime.config.enabled
                    and not runtime.running
                    and runtime.last_message is None
                    and not self._runtime_market_open(runtime, now)
                ):
                    runtime.last_message = "Armed for the next market session."

        return self.get_status()

    async def _run_due_runners(self, due_runners: list[RunnerRuntime], *, now: datetime) -> None:
        await asyncio.gather(*(self._run_runner(runtime, now=now) for runtime in due_runners))

    async def _run_runner(self, runtime: RunnerRuntime, *, now: datetime) -> None:
        runtime.running = True
        runtime.last_started_at = now
        runtime.last_error = None
        timeout_s = float(
            runtime.config.timeout_seconds
            or settings.MARKET_HOURS_SUPERVISOR_RUNNER_TIMEOUT_SECONDS
        )
        try:
            try:
                # Per-lane broker routing: set the cadence profile for the whole
                # callback subtree (contextvars copy into child tasks/to_thread).
                # A no-op while LANE_BROKER_ROUTING_ENABLED is off — the READ
                # seams (source_policy.route_order, broker_circuit.cadence_datatype)
                # only consult the profile behind that flag. lane_broker_profile
                # coerces an unknown/empty profile to DEFAULT rather than raising,
                # so a mistyped RunnerConfig.broker_profile can never crash dispatch.
                from brokers.rate_limiter import lane_broker_profile

                with lane_broker_profile(runtime.config.broker_profile):
                    result = await asyncio.wait_for(
                        runtime.config.callback(), timeout=timeout_s
                    )
            except (asyncio.TimeoutError, TimeoutError) as _timeout:
                raise RuntimeError(
                    f"runner exceeded {timeout_s:g}s timeout — killed to protect the supervisor loop"
                ) from _timeout
        except Exception as exc:
            runtime.last_error = str(exc)
            runtime.last_finished_at = self._now_fn()
            runtime.last_message = str(exc)
            runtime.last_result_meta = {"error": str(exc)}
            logger.warning(f"[MarketHoursSupervisor] {runtime.config.key} failed: {exc}")
            await self._emit_scan_audit(runtime.config.key, result=None, error=str(exc))
        else:
            runtime.last_result_meta = result if isinstance(result, dict) else {"result": result}
            reported_status = str(
                result.get("status") if isinstance(result, dict) else ""
            ).strip().lower()
            if reported_status in {"error", "failed", "timeout", "broker_not_ready"}:
                runtime.last_finished_at = self._now_fn()
                runtime.last_error = str(
                    result.get("message")
                    or result.get("note")
                    or f"Runner reported {reported_status}."
                )
                runtime.last_message = runtime.last_error
                logger.warning(
                    f"[MarketHoursSupervisor] {runtime.config.key} reported {reported_status}: "
                    f"{runtime.last_error}"
                )
                await self._emit_scan_audit(
                    runtime.config.key,
                    result=result,
                    error=runtime.last_error,
                )
                return
            runtime.last_success_at = self._now_fn()
            runtime.last_finished_at = runtime.last_success_at
            completed = result.get("result_count") if isinstance(result, dict) else None
            failure_count = result.get("failure_count") if isinstance(result, dict) else None
            if completed is not None:
                runtime.last_message = f"Completed {completed} cycle(s)" + (
                    f" with {failure_count} failure(s)." if failure_count else "."
                )
            else:
                runtime.last_message = "Completed automated paper cycle."
            logger.info(f"[MarketHoursSupervisor] {runtime.config.key} completed")
            await self._emit_scan_audit(runtime.config.key, result=result, error=None)
        finally:
            runtime.running = False
            # WS-0.2 — per-lane scan wall-time (records on success and failure).
            try:
                from core.metrics import observe_scan

                if runtime.last_started_at is not None:
                    observe_scan(
                        runtime.config.key,
                        (self._now_fn() - runtime.last_started_at).total_seconds(),
                    )
            except Exception:
                pass
            finished_at = runtime.last_finished_at or self._now_fn()
            started_at = runtime.last_started_at or now
            meta = runtime.last_result_meta if isinstance(runtime.last_result_meta, dict) else {}
            runtime.cycle_history.append(
                {
                    "started_at": started_at.isoformat(),
                    "finished_at": finished_at.isoformat(),
                    "duration_seconds": round(max((finished_at - started_at).total_seconds(), 0.0), 3),
                    "status": "failed" if runtime.last_error else "completed",
                    "evaluated_count": meta.get("result_count"),
                    "actionable_count": meta.get("actionable_count"),
                    "failure_count": meta.get("failure_count"),
                }
            )
            del runtime.cycle_history[:-120]

    # State carried across scan cycles to dedupe audit emits — we only want
    # to push to the audit log on actionable signals, transitions, errors,
    # or a 30-minute heartbeat. Otherwise the log would store 1000+ "nothing
    # actionable" rows per day per desk.
    _last_audit_state: dict[str, dict[str, Any]] = {}

    async def _emit_scan_audit(
        self,
        runner_key: str,
        *,
        result: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        market_map = {
            "auction_intelligence": "auction_intelligence",
            "auction_intelligence_commodity": "auction_intelligence",
            "institutional_convergence": "institutional_convergence",
            "fractal_market_profile": "fmp",
            "directional_options": "directional_options",
            "cbe_scanner": "cbe_scanner",
            "macd_refined": "macd_refined",
            "market_intelligence": "market_intelligence",
            "gann_tp_delta": "gann_tp_delta",
        }
        market = market_map.get(runner_key)
        if market is None:
            return
        try:
            from agentic_rag.audit_agent import record_audit_event
        except Exception:  # noqa: BLE001
            return
        if error is not None:
            await record_audit_event(
                market=market,
                strategy_key=runner_key,
                event_type="scan_cycle_failed",
                actor="supervisor",
                severity="warning",
                message=error[:200],
            )
            self._last_audit_state[runner_key] = {
                "fingerprint": "error",
                "at": self._now_fn(),
            }
            return

        results = list((result or {}).get("results") or [])
        actionable_count = sum(
            1
            for r in results
            if (r.get("actionable") is True)
            or (r.get("actionable_count") is not None and int(r.get("actionable_count") or 0) > 0)
        )
        # Fingerprint = compact representation of what each underlying
        # decided this cycle. Changes when a new signal appears, a regime
        # flips, or an actionable transition happens.
        fingerprint_parts: list[str] = []
        per_symbol: list[dict[str, Any]] = []
        for r in results[:8]:
            sym = str(r.get("symbol_code") or r.get("underlying") or r.get("symbol") or "")
            action = (
                r.get("signal_action")
                or r.get("direction")
                or ("trade" if r.get("actionable") else "—")
            )
            fingerprint_parts.append(f"{sym}:{action}:{'1' if r.get('actionable') else '0'}")
            summary: dict[str, Any] = {"symbol": sym, "actionable": r.get("actionable")}
            for k in ("signal_action", "direction", "confidence", "regime_label", "setup"):
                if k in r:
                    summary[k] = r.get(k)
            rejection = r.get("rejection_reasons") or r.get("filters")
            if rejection:
                summary["rejection"] = (rejection[0] if isinstance(rejection, list) else rejection)
            per_symbol.append(summary)
        fingerprint = "|".join(fingerprint_parts)
        now = self._now_fn()

        prev = self._last_audit_state.get(runner_key, {})
        prev_fp = prev.get("fingerprint")
        prev_at = prev.get("at")
        heartbeat_due = (
            prev_at is None
            or (now - prev_at).total_seconds() >= 1800  # 30 min
        )
        state_changed = fingerprint != prev_fp
        should_emit = actionable_count > 0 or state_changed or heartbeat_due
        if not should_emit:
            return

        severity = "trade" if actionable_count else "info"
        if not actionable_count and heartbeat_due and not state_changed:
            event_type = "scan_cycle_heartbeat"
        elif state_changed and not actionable_count:
            event_type = "scan_cycle_change"
        else:
            event_type = "scan_cycle"

        await record_audit_event(
            market=market,
            strategy_key=runner_key,
            event_type=event_type,
            actor="supervisor",
            severity=severity,
            message=(
                f"{runner_key} scanned {len(results)} symbol(s); "
                f"{actionable_count} actionable"
            ),
            payload={
                "result_count": int(result.get("result_count") or 0) if result else 0,
                "failure_count": int(result.get("failure_count") or 0) if result else 0,
                "actionable_count": actionable_count,
                "per_symbol": per_symbol,
            },
        )
        self._last_audit_state[runner_key] = {
            "fingerprint": fingerprint,
            "at": now,
        }

    # ── Phase-1 split: cross-plane status mirroring (Redis) ──────────────────
    # Each plane's supervisor publishes its own get_status() payload to
    # supervisor:status:{laneset} every scheduler pass and caches the OTHER
    # plane's snapshot, so the status endpoints (which only core serves to the
    # UI) can present a MERGED runner map without touching api/routers/system.py.
    _PLANE_STATUS_KEY_PREFIX = "supervisor:status:"
    _PLANE_STATUS_TTL_SECONDS = 600

    async def _publish_plane_status(self) -> None:
        if self._laneset == "all":
            return
        try:
            from db.redis_client import get_redis

            redis = await get_redis()
            payload = self.get_status(merge_foreign=False)
            payload["plane"] = self._laneset
            payload["published_at"] = self._now_fn().isoformat()
            await redis.set(
                f"{self._PLANE_STATUS_KEY_PREFIX}{self._laneset}",
                json.dumps(payload, default=str),
                ex=self._PLANE_STATUS_TTL_SECONDS,
            )
            other = "core" if self._laneset == "strategies" else "strategies"
            raw = await redis.get(f"{self._PLANE_STATUS_KEY_PREFIX}{other}")
            if raw:
                self._foreign_plane_status = json.loads(
                    raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
                )
            else:
                self._foreign_plane_status = None
        except Exception as exc:  # noqa: BLE001 — status mirroring is best-effort
            logger.debug("[MarketHoursSupervisor] plane status publish failed: {}", exc)

    def _foreign_runner_entries(self, now: datetime) -> dict[str, dict[str, Any]]:
        """The other plane's runner payloads, staleness-tagged. {} when not split."""
        if self._laneset == "all":
            return {}
        foreign = self._foreign_plane_status or {}
        runners = foreign.get("runners")
        if not isinstance(runners, dict):
            return {}
        age_seconds: float | None = None
        published_raw = foreign.get("published_at")
        if published_raw:
            try:
                published_at = datetime.fromisoformat(str(published_raw))
                age_seconds = round((now - published_at).total_seconds(), 1)
            except (TypeError, ValueError):
                age_seconds = None
        plane = str(foreign.get("plane") or "unknown")
        out: dict[str, dict[str, Any]] = {}
        for key, item in runners.items():
            if not isinstance(item, dict):
                continue
            out[str(key)] = {
                **item,
                "plane": plane,
                "foreign": True,
                "snapshot_age_seconds": age_seconds,
                # The foreign snapshot's own loop_active refers to THAT process.
                "snapshot_stale": bool(age_seconds is None or age_seconds > 180.0),
            }
        return out

    def get_runner_status(self, key: str) -> dict[str, Any]:
        now = self._now_fn()
        runtime = self._runners.get(key)
        loop_active = bool(self._task and not self._task.done())
        if runtime is None:
            foreign = self._foreign_runner_entries(now).get(key)
            if foreign is not None:
                return foreign
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
            market_hours_fn=self._runtime_market_hours_fn(runtime),
            next_open_fn=self._runtime_next_open_fn(runtime),
        )

    def _runtime_market_hours_fn(self, runtime: RunnerRuntime) -> MarketHoursFn:
        return runtime.config.market_hours_fn or self._market_hours_fn

    def _runtime_next_open_fn(self, runtime: RunnerRuntime) -> NextOpenFn:
        return runtime.config.next_open_fn or self._next_open_fn

    def _runtime_market_open(self, runtime: RunnerRuntime, now: datetime) -> bool:
        return self._runtime_market_hours_fn(runtime)(now)

    def _runtime_next_open(self, runtime: RunnerRuntime, now: datetime) -> datetime:
        return self._runtime_next_open_fn(runtime)(now)

    def get_status(self, *, merge_foreign: bool = True) -> dict[str, Any]:
        now = self._now_fn()
        market_open = self._market_hours_fn(now)
        next_open = self._next_open_fn(now)
        loop_active = bool(self._task and not self._task.done())
        runners = {
            key: runtime.serialize(
                now,
                loop_active=loop_active,
                market_hours_fn=self._runtime_market_hours_fn(runtime),
                next_open_fn=self._runtime_next_open_fn(runtime),
            )
            for key, runtime in self._runners.items()
        }
        # Split boot: overlay the OTHER plane's runners (staleness-tagged) so
        # the existing status endpoints keep showing the full 17-runner map.
        # No-op when LANESET=all (payload stays byte-identical).
        if merge_foreign and self._laneset != "all":
            for key, item in self._foreign_runner_entries(now).items():
                runners.setdefault(key, item)
        any_runner_market_open = any(
            self._runtime_market_open(runtime, now)
            for runtime in self._runners.values()
            if runtime.config.enabled
        )
        # Healthy = enabled, no last_error, AND not stale (silently overdue).
        # Never-ran-idle runners (market closed / just armed) still count healthy.
        healthy_runner_count = sum(
            1 for item in runners.values()
            if item.get("last_error") is None and not item.get("stale")
        )
        stale_runner_count = sum(1 for item in runners.values() if item.get("stale"))
        return {
            # `laneset` appears ONLY in split boots so the LANESET=all payload
            # stays byte-identical to the pre-split shape.
            **({"laneset": self._laneset} if self._laneset != "all" else {}),
            "enabled": self._enabled,
            "loop_active": loop_active,
            "market_open": market_open,
            "any_runner_market_open": any_runner_market_open,
            "now_ist": now.isoformat(),
            "next_market_open_ist": next_open.isoformat(),
            "trading_calendar": trading_calendar.status_payload(now).get("status"),
            "runner_count": len(runners),
            "healthy_runner_count": healthy_runner_count,
            "stale_runner_count": stale_runner_count,
            "runners": runners,
        }


# The live singleton persists its post-close catch-up markers so a backend
# restart after 15:35 IST cannot re-run the "once per session" passes. Tests
# construct their own instances without a path (in-memory only).
#
# Phase-1 split: the runtime/ dir is bind-mounted into BOTH containers, so each
# plane gets its OWN marker file (post_close_catchup.{laneset}.json) — two
# supervisors last-writer-wins clobbering one JSON would re-fire "once per
# session" passes. LANESET=all keeps the exact legacy path.


def catchup_state_path_for(laneset: str) -> Path:
    base = Path(__file__).resolve().parents[1] / "runtime" / "supervisor"
    ls = str(laneset or "all").strip().lower()
    if ls in ("core", "strategies"):
        return base / f"post_close_catchup.{ls}.json"
    return base / "post_close_catchup.json"


def _singleton_catchup_state_path() -> Path:
    from core.laneset import normalized_laneset

    return catchup_state_path_for(normalized_laneset())


_CATCHUP_STATE_PATH = _singleton_catchup_state_path()

market_hours_paper_supervisor = MarketHoursPaperSupervisor(catchup_state_path=_CATCHUP_STATE_PATH)
