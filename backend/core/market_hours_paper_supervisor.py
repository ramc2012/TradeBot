from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
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
    # Gates post_close_force_daily runners so the guaranteed EOD pass fires
    # exactly once per session regardless of in-session successes.
    last_post_close_success_date: date | None = None

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
        if self.config.enabled and market_open_now and loop_active and not self.running:
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
    ) -> None:
        self._enabled = settings.MARKET_HOURS_PAPER_SUPERVISOR_ENABLED if enabled is None else bool(enabled)
        self._now_fn = now_fn or _now_ist
        self._market_hours_fn = market_hours_fn or _in_nse_market_hours
        self._next_open_fn = next_open_fn or _next_nse_market_open
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._runner_tasks: dict[str, asyncio.Task] = {}
        self._maintenance_tasks: set[asyncio.Task] = set()
        # Crash watchdog: if the scheduling loop ever raises it would otherwise
        # stop ALL lanes silently forever. Auto-restart, bounded so a hard crash
        # loop can't spin — after this many consecutive crashes we give up and
        # leave the loud audit/log trail instead of hammering.
        self._loop_crash_count = 0
        self._loop_max_restarts = 5
        self._runners: dict[str, RunnerRuntime] = {
            runner.key: RunnerRuntime(config=runner)
            for runner in (runners or self._default_runners())
        }

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

        async def _auction_runner() -> dict[str, Any]:
            return await run_auction_market_cycle()

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
                    results.append(
                        {
                            "underlying": underlying,
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
                    )
                except Exception as exc:
                    failures[underlying] = str(exc)
            if not results and failures:
                joined = "; ".join(f"{symbol}: {detail}" for symbol, detail in failures.items())
                raise RuntimeError(f"Directional options paper cycle failed: {joined}")
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
                "symbols_requested": list(directional_service.config["universe"]),
                "symbols_completed": [item.get("underlying") for item in results],
                "result_count": len(results),
                "actionable_count": actionable_count,
                "rejection_counts": dict(rejection_counts.most_common(10)),
                "failure_count": len(failures),
                "failures": failures,
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
                key="token_readiness",
                label="Pre-open Broker Token Readiness",
                interval_seconds=900,
                callback=_token_readiness_runner,
                enabled=getattr(settings, "TOKEN_READINESS_AUTO_ENABLED", True),
                market_hours_fn=_in_token_readiness_window,
                next_open_fn=_next_token_readiness_open,
            ),
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
            ),
            RunnerConfig(
                key="institutional_convergence",
                label="Institutional Convergence Shadow Cycle",
                interval_seconds=settings.INSTITUTIONAL_CONVERGENCE_AUTO_INTERVAL_SECONDS,
                callback=_institutional_convergence_runner,
                enabled=settings.INSTITUTIONAL_CONVERGENCE_AUTO_ENABLED,
                market_hours_fn=_in_institutional_convergence_window,
                next_open_fn=_next_institutional_convergence_open,
                post_close_catchup=False,
                timeout_seconds=600.0,
            ),
            RunnerConfig(
                key="institutional_convergence_commodity",
                label="Institutional Convergence Commodity Cycle",
                interval_seconds=settings.INSTITUTIONAL_CONVERGENCE_COMMODITY_INTERVAL_SECONDS,
                callback=_institutional_convergence_commodity_runner,
                enabled=settings.INSTITUTIONAL_CONVERGENCE_COMMODITY_ENABLED,
                market_hours_fn=_in_commodity_convergence_window,
                next_open_fn=_next_commodity_convergence_open,
                post_close_catchup=False,
                timeout_seconds=600.0,
            ),
            RunnerConfig(
                key="fractal_market_profile",
                label="Fractal Market Profile Paper Cycle",
                interval_seconds=settings.FRACTAL_MARKET_PROFILE_AUTO_INTERVAL_SECONDS,
                callback=_fmp_runner,
                enabled=settings.FRACTAL_MARKET_PROFILE_AUTO_ENABLED,
                market_hours_fn=_in_gann_market_hours,
                next_open_fn=_next_gann_market_open,
            ),
            RunnerConfig(
                key="directional_options",
                label="Directional Options Paper Cycle",
                interval_seconds=settings.DIRECTIONAL_OPTIONS_AUTO_INTERVAL_SECONDS,
                callback=_directional_runner,
                enabled=settings.DIRECTIONAL_OPTIONS_AUTO_ENABLED,
                # NSE index options only — trade during the session, never on the
                # post-close frozen `live_tick` heartbeat (last price re-stamped
                # with 0 volume after 15:30 IST). No after-hours catch-up either.
                market_hours_fn=_in_nse_market_hours,
                next_open_fn=_next_nse_market_open,
                post_close_catchup=False,
            ),
            RunnerConfig(
                key="directional_positioning",
                label="Directional Positioning Feed Refresh",
                interval_seconds=getattr(
                    settings, "DIRECTIONAL_POSITIONING_REFRESH_INTERVAL_SECONDS", 3600
                ),
                callback=_directional_positioning_runner,
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
            ),
            RunnerConfig(
                key="cbe_scanner",
                label="CBE Scanner Paper Cycle",
                interval_seconds=settings.CBE_SCANNER_AUTO_INTERVAL_SECONDS,
                callback=_cbe_runner,
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
        logger.info("[MarketHoursSupervisor] started")

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

    async def _schedule_due_once(self) -> None:
        """Launch due lanes independently and return without awaiting scans."""
        if not self._enabled:
            return
        async with self._lock:
            now = self._now_fn()
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
                elif (
                    runtime.config.post_close_catchup
                    and not runtime_market_open
                    and _should_run_post_close_catchup(now)
                    and (
                        (
                            runtime.last_post_close_success_date is None
                            or runtime.last_post_close_success_date < catchup_session_date
                        )
                        if runtime.config.post_close_force_daily
                        else (
                            runtime.last_success_at is None
                            or runtime.last_success_at.date() < catchup_session_date
                        )
                    )
                ):
                    catchup_runners.append(runtime)

            catchup_tasks: list[tuple[RunnerRuntime, asyncio.Task]] = []
            for runtime in due_runners + catchup_runners:
                runtime.running = True
                task = asyncio.create_task(
                    self._run_runner(runtime, now=now),
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
                if runtime in catchup_runners:
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

    async def _finalize_background_catchup(
        self,
        catchup_tasks: list[tuple[RunnerRuntime, asyncio.Task]],
        session_date: date,
    ) -> None:
        await asyncio.gather(*(task for _, task in catchup_tasks), return_exceptions=True)
        successful = [runtime for runtime, _ in catchup_tasks if runtime.last_error is None]
        for runtime in successful:
            runtime.last_post_close_success_date = session_date
            runtime.last_result_meta.setdefault("catchup_session_date", session_date.isoformat())
            runtime.last_message = (
                f"{runtime.last_message} Catch-up captured for {session_date.isoformat()}."
            )
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
                if (
                    not force
                    and runtime.config.post_close_catchup
                    and not runtime_market_open
                    and _should_run_post_close_catchup(now)
                    and (
                        # Guaranteed once-a-day EOD pass: fire after 15:35
                        # regardless of in-session successes, tracked by its
                        # own post-close date so it runs exactly once.
                        (
                            runtime.last_post_close_success_date is None
                            or runtime.last_post_close_success_date < catchup_session_date
                        )
                        if runtime.config.post_close_force_daily
                        # Recovery-only (default): only if no in-session pass
                        # succeeded for today's session.
                        else (
                            runtime.last_success_at is None
                            or runtime.last_success_at.date() < catchup_session_date
                        )
                    )
                ):
                    catchup_runners.append(runtime)

            if due_runners:
                await self._run_due_runners(due_runners, now=now)
            if catchup_runners:
                await self._run_due_runners(catchup_runners, now=now)
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
                for runtime in catchup_runners:
                    if runtime.last_error is None:
                        runtime.last_post_close_success_date = catchup_session_date
                        runtime.last_result_meta.setdefault(
                            "catchup_session_date",
                            catchup_session_date.isoformat(),
                        )
                        runtime.last_message = (
                            f"{runtime.last_message} Catch-up captured for "
                            f"{catchup_session_date.isoformat()}."
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
                result = await asyncio.wait_for(runtime.config.callback(), timeout=timeout_s)
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

    def get_status(self) -> dict[str, Any]:
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


market_hours_paper_supervisor = MarketHoursPaperSupervisor()
