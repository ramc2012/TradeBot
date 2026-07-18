from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any

from fastapi import APIRouter
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from api.routers.analysis import _load_research_sync_runtime_state, _parse_iso_datetime
from api.routers.auction_intelligence import summary as auction_intelligence_summary
from api.routers.auth import get_broker_connection_snapshot
from analytics.performance import PerformanceAnalytics
from api.routers.trading import _get_or_create_paper_session, _risk_manager
from auction_intelligence.config import clone_default_config
from core.config import settings
from core.market_hours_paper_supervisor import market_hours_paper_supervisor
from core.trading_calendar import trading_calendar
from db.database import AsyncSessionLocal
from db.redis_client import get_redis
from fractal_market_profile.service import fmp_service
from market_data.data_router import data_router
from market_data.validated_snapshots import validated_snapshot_store
from paper_engine.commodity_strategy_agent import commodity_strategy_agent, _in_commodity_hours
from paper_engine.strategy_agent import paper_strategy_agent, _in_market_hours


router = APIRouter(prefix="/api/system", tags=["system"])
_perf = PerformanceAnalytics()
IST = timezone(timedelta(hours=5, minutes=30))
_HEALTH_CACHE_TTL_SECONDS = 5.0
# WS-0.5b — per-sub-check hard timeout. Bounds the endpoint's worst case (the
# broker snapshot is a network call and was the long pole) so a slow/hung
# dependency degrades just that one service instead of stalling the whole probe.
_HEALTH_CHECK_TIMEOUT_SECONDS = 4.0
_OVERVIEW_CACHE_TTL_SECONDS = 5.0
_health_cache: dict[str, Any] = {"payload": None, "expires_at": 0.0}
_health_cache_lock = asyncio.Lock()
_overview_cache: dict[str, Any] = {"payload": None, "expires_at": 0.0}
_overview_cache_lock = asyncio.Lock()


class TradingCalendarSettingsRequest(BaseModel):
    enabled: bool = True
    exchanges: dict[str, Any]


def _service(
    *,
    key: str,
    label: str,
    status: str,
    detail: str,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "status": status,
        "detail": detail,
        "meta": meta or {},
    }


def _worst_status(current: str, candidate: str) -> str:
    order = {"healthy": 0, "idle": 0, "degraded": 1, "critical": 2}
    return candidate if order.get(candidate, 3) > order.get(current, 3) else current


def _lane_status_payload(
    *,
    parent: str,
    lane: dict[str, Any],
    positions: int,
    last_scan_at: str | None,
    stale_threshold_seconds: int | None = None,
    now_ist: datetime | None = None,
) -> dict[str, Any]:
    status = lane.get("status") or ("active" if positions else "idle")
    last_scan_dt = _parse_iso_datetime(last_scan_at)
    last_scan_age_seconds: float | None = None
    if last_scan_dt is not None and now_ist is not None:
        last_scan_age_seconds = max((now_ist - last_scan_dt).total_seconds(), 0.0)
    if (
        status in {"active", "healthy", "ready"}
        and stale_threshold_seconds is not None
        and last_scan_age_seconds is not None
        and last_scan_age_seconds > stale_threshold_seconds
    ):
        status = "stale"
    return {
        "key": f"{parent}:{lane.get('key')}",
        "parent": parent,
        "label": lane.get("label") or lane.get("title") or lane.get("key") or "Strategy",
        "status": status,
        "timeframe": lane.get("timeframe"),
        "scope": lane.get("scope") or lane.get("instrument"),
        "open_positions": positions,
        "last_scan_at": last_scan_at,
        "last_scan_age_seconds": last_scan_age_seconds,
        "scan_interval_seconds": lane.get("scan_interval_seconds"),
        "stale_threshold_seconds": stale_threshold_seconds,
        "notes": lane.get("notes"),
    }


def _strategy_metric(strategy: dict[str, Any], field: str, default: float = 0.0) -> float:
    summary = strategy.get("summary") or {}
    value = summary.get(field)
    if value is None:
        value = strategy.get(field)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _agent_status_snapshot(agent: Any) -> dict[str, Any]:
    try:
        return agent.get_status(refresh=False)
    except TypeError:
        return agent.get_status()


def _strategy_stale_threshold_seconds(scan_interval_seconds: Any) -> int:
    try:
        scan_interval = max(int(scan_interval_seconds or 0), 0)
    except (TypeError, ValueError):
        scan_interval = 0
    return max(300, scan_interval * 4)


def _collect_strategy_freshness_candidates(
    *,
    key: str,
    status: dict[str, Any],
    strategy_items: list[dict[str, Any]],
) -> list[tuple[datetime, str]]:
    candidates: list[tuple[datetime, str]] = []
    if key != "commodity_strategy":
        return candidates

    for strategy in strategy_items:
        raw_scan = strategy.get("last_scan_at")
        parsed_scan = _parse_iso_datetime(raw_scan)
        if parsed_scan is not None and raw_scan:
            candidates.append((parsed_scan, str(raw_scan)))

    for position in list(status.get("positions") or []):
        raw_review = position.get("last_reviewed_bar_time") or position.get("updated_at")
        parsed_review = _parse_iso_datetime(raw_review)
        if parsed_review is not None and raw_review:
            candidates.append((parsed_review, str(raw_review)))

    for row in list(status.get("futures_watchlist") or status.get("watchlist") or []):
        raw_bar = row.get("last_reviewed_bar_time") or row.get("bar_time") or row.get("as_of")
        parsed_bar = _parse_iso_datetime(raw_bar)
        if parsed_bar is not None and raw_bar:
            candidates.append((parsed_bar, str(raw_bar)))

    for row in list(status.get("signal_audit") or []):
        raw_time = row.get("time")
        parsed_time = _parse_iso_datetime(raw_time)
        if parsed_time is not None and raw_time:
            candidates.append((parsed_time, str(raw_time)))

    return candidates


async def _manual_book_summary() -> dict[str, Any]:
    _, _, portfolio = await _get_or_create_paper_session()
    trades = [
        {
            "symbol": trade.symbol,
            "action": trade.action,
            "qty": trade.qty,
            "entry_price": trade.entry_price,
            "exit_price": trade.exit_price,
            "pnl": trade.pnl,
            "entry_time": trade.entry_time.isoformat(),
            "exit_time": trade.exit_time.isoformat(),
            "instrument_type": trade.instrument_type,
        }
        for trade in portfolio._trade_history
    ]
    summary = _perf.summary(trades, "all")
    positions = portfolio.get_positions_list()
    open_pnl = sum(float(position.get("unrealized_pnl") or 0.0) for position in positions)

    return {
        "total_pnl": summary.total_pnl,
        "total_trades": summary.total_trades,
        "win_rate": summary.win_rate,
        "profit_factor": summary.profit_factor,
        "open_positions": len(positions),
        "open_pnl": round(open_pnl, 2),
    }


def _service_bucket(status: str) -> str:
    if status in {"healthy", "active", "ready"}:
        return "healthy"
    if status in {"critical", "error"}:
        return "critical"
    if status in {"degraded", "warning", "stale"}:
        return "degraded"
    return "idle"


async def _postgres_service() -> dict[str, Any]:
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(text("SELECT 1"))
            result.scalar_one_or_none()
        return _service(
            key="postgres",
            label="PostgreSQL",
            status="healthy",
            detail="Primary application database reachable.",
        )
    except Exception as exc:
        logger.debug(f"[System] PostgreSQL health check failed: {exc}")
        return _service(
            key="postgres",
            label="PostgreSQL",
            status="critical",
            detail=f"Database check failed: {exc}",
        )


async def _redis_service() -> dict[str, Any]:
    try:
        redis = await get_redis()
        await redis.ping()
        return _service(
            key="redis",
            label="Redis",
            status="healthy",
            detail="Cache and pub/sub broker reachable.",
        )
    except Exception as exc:
        logger.debug(f"[System] Redis health check failed: {exc}")
        return _service(
            key="redis",
            label="Redis",
            status="critical",
            detail=f"Redis ping failed: {exc}",
        )


def _research_sync_service(now_utc: datetime) -> dict[str, Any]:
    if not settings.RESEARCH_SYNC_AUTO_ENABLED:
        return _service(
            key="research_sync",
            label="Research Sync",
            status="idle",
            detail="Broker research sync is disabled until explicitly started.",
            meta={"state": "disabled", "enabled": False},
        )

    runtime = _load_research_sync_runtime_state()
    state = str(runtime.get("state") or "idle")
    completed_at = _parse_iso_datetime(runtime.get("run_completed_at"))
    next_run_at = _parse_iso_datetime(runtime.get("next_run_at"))
    error = runtime.get("error")
    runtime_detail = str(runtime.get("detail") or "").strip()

    if "valid saved Upstox token" in runtime_detail:
        status = "critical"
        detail = runtime_detail
    elif error:
        status = "critical"
        detail = str(error)
    elif state == "running":
        status = "healthy"
        detail = "Research sync cycle is running."
    elif completed_at and now_utc - completed_at <= timedelta(hours=6):
        status = "healthy"
        detail = "Recent research sync output is available."
    elif runtime:
        status = "degraded"
        detail = "Research sync state is stale or waiting for the next cycle."
    else:
        status = "idle"
        detail = "Research sync runtime state file not found yet."

    return _service(
        key="research_sync",
        label="Research Sync",
        status=status,
        detail=detail,
        meta={
            "state": state,
            "run_started_at": runtime.get("run_started_at"),
            "run_completed_at": runtime.get("run_completed_at"),
            "next_run_at": next_run_at.isoformat() if next_run_at else None,
            "detail": runtime_detail or None,
            "last_result": runtime.get("last_result") or {},
        },
    )


def _next_market_open_ist(now_utc: datetime) -> datetime:
    next_open = now_utc.astimezone(IST).replace(hour=9, minute=15, second=0, microsecond=0)
    if now_utc.astimezone(IST) >= next_open:
        next_open += timedelta(days=1)
    while next_open.weekday() >= 5:
        next_open += timedelta(days=1)
    return next_open


def _broker_rollover_meta(snapshot: dict[str, Any], now_utc: datetime) -> dict[str, Any]:
    next_open_ist = _next_market_open_ist(now_utc)
    warnings: list[str] = []

    upstox = snapshot.get("upstox_token_health") or {}
    upstox_expiry = _parse_iso_datetime(upstox.get("expires_at_ist"))
    fyers = snapshot.get("fyers_token_health") or {}

    if not snapshot.get("broker_ready"):
        warnings.append("No valid broker session is active before the next market open.")
    if snapshot.get("upstox_ready") and upstox_expiry and upstox_expiry <= next_open_ist:
        warnings.append("Upstox access tokens expire before the next market open.")
    elif upstox.get("needs_reconnect"):
        warnings.append("Upstox must be reconnected before the next market open.")

    if snapshot.get("fyers_ready") and fyers.get("refresh_available"):
        pass
    elif snapshot.get("fyers_ready"):
        warnings.append("Fyers requires re-authentication before a new trading day.")
    elif fyers.get("needs_reconnect"):
        warnings.append("Fyers must be reconnected before the next market open.")

    return {
        "next_market_open_ist": next_open_ist.isoformat(),
        "reconnect_required_before_next_market_open": bool(warnings),
        "reconnect_warnings": warnings,
    }


async def _broker_service(now_utc: datetime) -> dict[str, Any]:
    snapshot = await get_broker_connection_snapshot(force_validate=False)
    connected = snapshot.get("connected_brokers") or []
    upstox = snapshot.get("upstox_token_health") or {}
    fyers = snapshot.get("fyers_token_health") or {}
    rollover_meta = _broker_rollover_meta(snapshot, now_utc)

    if snapshot.get("broker_ready"):
        status = "healthy"
        detail = "At least one broker session is valid for market data and strategy scans."
    elif connected:
        status = "degraded"
        detail = "Broker adapters are present, but no broker token is currently valid."
    else:
        status = "critical"
        detail = "No valid broker session is active."

    return _service(
        key="brokers",
        label="Broker Connectivity",
        status=status,
        detail=detail,
        meta={
            "connected_brokers": connected,
            "broker_ready": snapshot.get("broker_ready"),
            "upstox_ready": snapshot.get("upstox_ready"),
            "fyers_ready": snapshot.get("fyers_ready"),
            "upstox_token_health": upstox,
            "fyers_token_health": fyers,
            **rollover_meta,
        },
    )


def _market_data_service() -> dict[str, Any]:
    status = data_router.get_status()
    mode = status.get("mode")
    subscribed = int(status.get("subscribed_symbol_count") or 0)
    ws_connected = bool(status.get("ws_connected"))
    last_tick_age_seconds = status.get("last_tick_age_seconds")
    now_ist = datetime.now(timezone.utc).astimezone(IST)
    market_open = _in_market_hours(now_ist) or _in_commodity_hours(now_ist)

    if mode == "mock":
        service_status = "healthy"
        detail = f"Mock market feed active for {subscribed} symbols."
    elif mode == "broker" and subscribed > 0 and not market_open:
        service_status = "healthy"
        detail = f"Broker feed subscribed for {subscribed} symbols; waiting for market hours."
    elif mode == "broker" and subscribed > 0 and ws_connected:
        service_status = "healthy"
        detail = f"Broker feed subscribed for {subscribed} symbols."
    elif mode == "broker" and subscribed > 0:
        service_status = "degraded"
        if last_tick_age_seconds is None:
            detail = "Broker feed is subscribed, but no fresh ticks have been observed yet."
        else:
            detail = (
                "Broker feed is subscribed, but the latest tick is stale "
                f"({last_tick_age_seconds:.0f}s ago)."
            )
    elif mode == "broker" and not market_open:
        service_status = "idle"
        detail = "Broker selected; websocket intentionally idle until the next NSE/MCX stream window."
    elif mode == "broker":
        service_status = "degraded"
        detail = "Broker selected, but no live symbol subscriptions are active."
    else:
        service_status = "idle"
        detail = "No live market data feed is active."

    return _service(
        key="market_data",
        label="Market Data Router",
        status=service_status,
        detail=detail,
        meta=status,
    )


def _strategy_service(
    *,
    key: str,
    label: str,
    status: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    enabled = bool(status.get("enabled", True))
    auto_run_enabled = bool(status.get("auto_run_enabled", True))
    running = bool(status.get("running"))
    loop_active = bool(status.get("loop_active"))
    kill_switch = bool(status.get("kill_switch_active"))
    last_error = status.get("last_error")
    last_message = status.get("last_message") or "Waiting for next scan."
    start_required = bool(status.get("start_required"))
    lane_payloads = status.get("strategy_agents") or []
    strategy_items = status.get("strategies") or []
    now_ist = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=5, minutes=30)))
    market_open = _in_market_hours(now_ist) if key == "nse_strategy" else _in_commodity_hours(now_ist)
    stale_threshold_seconds = _strategy_stale_threshold_seconds(status.get("scan_interval_seconds"))
    last_run_at = status.get("last_run_at")
    last_run_dt = _parse_iso_datetime(last_run_at)
    last_run_age_seconds: float | None = None
    if last_run_dt is not None:
        last_run_age_seconds = max((now_ist - last_run_dt).total_seconds(), 0.0)
    freshness_reference_at = last_run_at
    freshness_age_seconds = last_run_age_seconds
    freshness_candidates = _collect_strategy_freshness_candidates(
        key=key,
        status=status,
        strategy_items=strategy_items,
    )
    if freshness_candidates:
        freshest_dt, freshest_at = max(freshness_candidates, key=lambda item: item[0])
        freshness_reference_at = str(freshest_at)
        freshness_age_seconds = max((now_ist - freshest_dt).total_seconds(), 0.0)
    mode = "idle"

    if last_error:
        service_status = "critical"
        mode = "error"
        detail = str(last_error)
    elif kill_switch:
        service_status = "idle"
        mode = "kill_switch"
        detail = "Kill switch is active."
    elif not enabled:
        service_status = "idle"
        mode = "disabled"
        detail = "Supervisor disabled."
    elif not auto_run_enabled:
        service_status = "idle"
        mode = "paused"
        detail = "Auto-run disabled. Manual scans only."
    elif key == "commodity_strategy" and start_required:
        service_status = "idle"
        mode = "start_required"
        detail = "Commodity supervisor is armed but waiting for an explicit start."
    elif not market_open:
        service_status = "idle"
        mode = "market_closed"
        detail = "Market closed. Supervisor waiting for the next session."
    elif freshness_age_seconds is not None and freshness_age_seconds > stale_threshold_seconds:
        service_status = "degraded"
        mode = "stale"
        detail = (
            f"Supervisor status is stale during market hours: latest activity "
            f"{int(freshness_age_seconds)}s ago."
        )
    elif loop_active:
        service_status = "healthy"
        mode = "active"
        detail = last_message
    elif running:
        service_status = "healthy"
        mode = "scanning"
        detail = last_message
    elif enabled:
        service_status = "degraded"
        mode = "loop_inactive"
        detail = "Supervisor should be active during market hours, but its loop is not running."

    lanes: list[dict[str, Any]] = []
    for lane, strategy in zip(lane_payloads, strategy_items):
        positions = int(_strategy_metric(strategy, "open_positions", 0))
        lane_last_scan_at = strategy.get("last_scan_at")
        if not lane_last_scan_at and key == "commodity_strategy":
            lane_last_scan_at = freshness_reference_at or status.get("last_run_at")
        lanes.append(
            _lane_status_payload(
                parent=key,
                lane=lane,
                positions=positions,
                last_scan_at=lane_last_scan_at or status.get("last_run_at"),
                stale_threshold_seconds=stale_threshold_seconds,
                now_ist=now_ist,
            )
        )

    service = _service(
        key=key,
        label=label,
        status=service_status,
        detail=detail,
        meta={
            "enabled": enabled,
            "running": running,
            "auto_run_enabled": auto_run_enabled,
            "loop_active": loop_active,
            "mode": mode,
            "kill_switch_active": kill_switch,
            "scan_interval_seconds": status.get("scan_interval_seconds"),
            "last_run_at": last_run_at,
            "last_run_age_seconds": last_run_age_seconds,
            "freshness_reference_at": freshness_reference_at,
            "freshness_age_seconds": freshness_age_seconds,
            "stale_threshold_seconds": stale_threshold_seconds,
            "next_scan_at": status.get("next_scan_at"),
            "strategy_lane_count": len(lanes),
            "open_positions": sum(
                int(_strategy_metric(item, "open_positions", 0))
                for item in strategy_items
            ),
            "data_health": status.get("data_health"),
        },
    )
    return service, lanes


async def _auction_service() -> dict[str, Any]:
    summary = await auction_intelligence_summary()
    live_ready = bool(summary.get("live_ready"))
    config = clone_default_config()
    automation = market_hours_paper_supervisor.get_runner_status("auction_intelligence")
    auto_enabled = bool(automation.get("enabled") and automation.get("loop_active"))
    market_open = bool(market_hours_paper_supervisor.get_status().get("market_open"))
    status = "healthy" if live_ready else "idle"
    if auto_enabled and automation.get("last_error") and market_open and not automation.get("last_success_at"):
        status = "degraded"

    return _service(
        key="auction_intelligence",
        label="Auction Intelligence",
        status=status,
        detail=(
            "Automated paper cycle is armed for market hours."
            if live_ready and auto_enabled
            else (
                "Validation module is available; auto paper cycle is not armed."
                if live_ready
                else "Validation module is available; live readiness depends on broker connectivity."
            )
        ),
        meta={
            "live_ready": live_ready,
            "connected_brokers": summary.get("connected_brokers") or [],
            "deployable_first_sleeve": summary.get("deployable_first_sleeve"),
            "validation_gates": summary.get("validation_gates") or [],
            "symbols": [
                *list(config.get("mvp_scope", {}).get("primary_underlyings") or []),
                *list(config.get("mvp_scope", {}).get("secondary_underlyings") or []),
            ],
            "automation": automation,
        },
    )


async def _fractal_market_profile_service() -> dict[str, Any]:
    automation = market_hours_paper_supervisor.get_runner_status("fractal_market_profile")
    try:
        summary = await fmp_service.summary()
        auto_started = bool(summary.get("auto_started"))
        paper_summary = summary.get("paper_summary") or {}
        return _service(
            key="fractal_market_profile",
            label="Fractal Market Profile",
            status="healthy" if auto_started else "idle",
            detail=(
                "Fractal MP paper automation is armed and recent replay summaries are available."
                if auto_started
                else "Fractal MP module is loaded, but automation is not armed."
            ),
            meta={
                "supported_symbols": summary.get("supported_symbols") or [],
                "paper_summary": paper_summary,
                "replay_report_count": len(summary.get("replay_reports") or []),
                "automation": automation,
            },
        )
    except RuntimeError as exc:
        return _service(
            key="fractal_market_profile",
            label="Fractal Market Profile",
            status="degraded",
            detail=f"Fractal MP summary unavailable: {exc}",
            meta={"automation": automation},
        )
    except Exception as exc:
        logger.debug(f"[System] Fractal Market Profile health check failed: {exc}")
        return _service(
            key="fractal_market_profile",
            label="Fractal Market Profile",
            status="critical",
            detail=f"FMP health check failed: {exc}",
            meta={"automation": automation},
        )


@router.get("/automation-status")
async def automation_status() -> dict[str, Any]:
    return market_hours_paper_supervisor.get_status()


@router.get("/trading-calendar")
async def get_trading_calendar() -> dict[str, Any]:
    return trading_calendar.status_payload()


@router.put("/trading-calendar")
async def update_trading_calendar(body: TradingCalendarSettingsRequest) -> dict[str, Any]:
    trading_calendar.update(body.model_dump())
    _health_cache["expires_at"] = 0.0
    _overview_cache["expires_at"] = 0.0
    return trading_calendar.status_payload()


async def _timed_service(
    coro,
    *,
    key: str,
    label: str,
    timeout: float = _HEALTH_CHECK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run a health sub-check under a hard timeout (WS-0.5b).

    On timeout/error, returns a degraded service entry instead of letting a slow
    or hung dependency stall the whole /health probe.
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        return _service(
            key=key,
            label=label,
            status="degraded",
            detail=f"Health check timed out after {timeout:.0f}s.",
        )
    except Exception as exc:  # pragma: no cover - defensive
        return _service(
            key=key,
            label=label,
            status="degraded",
            detail=f"Health check error: {exc}",
        )


def _notifications_service() -> dict[str, Any]:
    """Telegram delivery health — a dead bot token must show up here, not as a
    silent zero-notification day."""
    try:
        from notifications.telegram_agent import telegram_agent

        health = telegram_agent.get_health()
        if health.get("auth_failed"):
            status = "critical"
            detail = (
                f"Telegram bot token rejected (HTTP {health.get('last_error_status')}); "
                "alerts are NOT being delivered."
            )
        elif int(health.get("consecutive_failures") or 0) >= 3:
            status = "degraded"
            detail = (
                f"{health.get('consecutive_failures')} consecutive Telegram send failures "
                f"(last status {health.get('last_error_status')})."
            )
        elif not health.get("last_success_at") and not health.get("last_failure_at"):
            status = "idle"
            detail = "No Telegram sends attempted this process lifetime."
        else:
            status = "healthy"
            detail = f"Last successful Telegram delivery {health.get('last_success_at')}."
        return _service(
            key="notifications",
            label="Telegram Notifications",
            status=status,
            detail=detail,
            meta=health,
        )
    except Exception as exc:  # noqa: BLE001
        return _service(
            key="notifications",
            label="Telegram Notifications",
            status="degraded",
            detail=f"Telegram health unavailable: {exc}",
            meta={},
        )


@router.get("/health")
async def system_health() -> dict[str, Any]:
    cached = _health_cache.get("payload")
    if isinstance(cached, dict) and float(_health_cache.get("expires_at") or 0.0) > monotonic():
        return cached

    async with _health_cache_lock:
        cached = _health_cache.get("payload")
        if isinstance(cached, dict) and float(_health_cache.get("expires_at") or 0.0) > monotonic():
            return cached

        now_utc = datetime.now(timezone.utc)

        backend_service = _service(
            key="backend",
            label="Backend API",
            status="healthy",
            detail="FastAPI application is serving requests.",
            meta={"version": "1.0.0", "generated_at": now_utc.isoformat()},
        )

        # Sync helpers run first (cheap, no I/O).
        research_sync_service = _research_sync_service(now_utc)
        market_service = _market_data_service()
        notifications_service = _notifications_service()
        nse_strategy_service, nse_lanes = _strategy_service(
            key="nse_strategy",
            label="NSE Strategy Supervisor",
            status=paper_strategy_agent.get_status(),
        )
        commodity_strategy_service, commodity_lanes = _strategy_service(
            key="commodity_strategy",
            label="Commodity Strategy Supervisor",
            status=commodity_strategy_agent.get_status(),
        )

        # Async checks run in parallel. Previously they were sequential awaits,
        # multiplying the worst-case latency by the number of services. With
        # gather the wall time becomes max(per-service) instead of sum.
        (
            postgres_service,
            redis_service,
            broker_service,
            auction_service,
            fractal_market_profile_service,
        ) = await asyncio.gather(
            _timed_service(_postgres_service(), key="postgres", label="PostgreSQL"),
            _timed_service(_redis_service(), key="redis", label="Redis"),
            _timed_service(_broker_service(now_utc), key="brokers", label="Broker Sessions"),
            _timed_service(_auction_service(), key="auction_intelligence", label="Auction Intelligence"),
            _timed_service(
                _fractal_market_profile_service(),
                key="fractal_market_profile",
                label="Fractal Market Profile",
            ),
        )

        services = [
            backend_service,
            postgres_service,
            redis_service,
            research_sync_service,
            broker_service,
            market_service,
            nse_strategy_service,
            commodity_strategy_service,
            auction_service,
            fractal_market_profile_service,
            notifications_service,
        ]

        overall = "healthy"
        counts = {"healthy": 0, "idle": 0, "degraded": 0, "critical": 0}
        for item in services:
            state = str(item.get("status") or "critical")
            bucket = _service_bucket(state)
            counts[bucket] = counts.get(bucket, 0) + 1
            overall = _worst_status(overall, state)

        payload = {
            "generated_at": now_utc.isoformat(),
            "summary": {
                "status": overall,
                "service_counts": counts,
                "degraded_services": counts.get("degraded", 0),
                "critical_services": counts.get("critical", 0),
            },
            "services": services,
            "strategy_lanes": nse_lanes + commodity_lanes,
        }
        _health_cache["payload"] = payload
        _health_cache["expires_at"] = monotonic() + _HEALTH_CACHE_TTL_SECONDS
        return payload


@router.get("/overview")
async def system_overview() -> dict[str, Any]:
    cached = _overview_cache.get("payload")
    if isinstance(cached, dict) and float(_overview_cache.get("expires_at") or 0.0) > monotonic():
        return cached

    async with _overview_cache_lock:
        cached = _overview_cache.get("payload")
        if isinstance(cached, dict) and float(_overview_cache.get("expires_at") or 0.0) > monotonic():
            return cached

        health = await system_health()
        nse_status = _agent_status_snapshot(paper_strategy_agent)
        commodity_status = _agent_status_snapshot(commodity_strategy_agent)
        manual = await _manual_book_summary()
        auction = await auction_intelligence_summary()
        risk = _risk_manager.get_status()

        nse_strategies = nse_status.get("strategies") or []
        commodity_strategies = commodity_status.get("strategies") or []
        nse_realized = sum(_strategy_metric(item, "realized_pnl", 0.0) for item in nse_strategies)
        nse_open = sum(_strategy_metric(item, "unrealized_pnl", 0.0) for item in nse_strategies)
        nse_equity = sum(_strategy_metric(item, "total_equity", 0.0) for item in nse_strategies)
        commodity_realized = sum(_strategy_metric(item, "realized_pnl", 0.0) for item in commodity_strategies)
        commodity_open = sum(_strategy_metric(item, "unrealized_pnl", 0.0) for item in commodity_strategies)
        commodity_equity = sum(_strategy_metric(item, "total_equity", 0.0) for item in commodity_strategies)

        payload = {
            "generated_at": health["generated_at"],
            "health": health,
            "books": {
                "combined": {
                    "equity": round(nse_equity + commodity_equity, 2),
                    "realized_pnl": round(nse_realized + commodity_realized + float(manual.get("total_pnl") or 0.0), 2),
                    "open_pnl": round(nse_open + commodity_open, 2),
                    "open_positions": int(sum(_strategy_metric(item, "open_positions", 0) for item in nse_strategies))
                    + int(sum(_strategy_metric(item, "open_positions", 0) for item in commodity_strategies)),
                },
                "manual": manual,
                "nse_strategy": {
                    "equity": round(nse_equity, 2),
                    "realized_pnl": round(nse_realized, 2),
                    "open_pnl": round(nse_open, 2),
                    "strategies": nse_strategies,
                    "status": {
                        "auto_run_enabled": nse_status.get("auto_run_enabled"),
                        "loop_active": nse_status.get("loop_active"),
                        "running": nse_status.get("running"),
                        "last_run_at": nse_status.get("last_run_at"),
                    },
                },
                "commodity_strategy": {
                    "equity": round(commodity_equity, 2),
                    "realized_pnl": round(commodity_realized, 2),
                    "open_pnl": round(commodity_open, 2),
                    "strategies": commodity_strategies,
                    "status": {
                        "auto_run_enabled": commodity_status.get("auto_run_enabled"),
                        "loop_active": commodity_status.get("loop_active"),
                        "running": commodity_status.get("running"),
                        "last_run_at": commodity_status.get("last_run_at"),
                    },
                },
            },
            "risk": risk,
            "auction_intelligence": {
                "live_ready": bool(auction.get("live_ready")),
                "connected_brokers": auction.get("connected_brokers") or [],
                "deployable_first_sleeve": auction.get("deployable_first_sleeve"),
                "validation_gates": auction.get("validation_gates") or [],
                "paper_trading": auction.get("paper_trading") or {},
            },
            "blockers": [
                item for item in health.get("services", [])
                if item.get("status") in {"critical", "degraded"}
            ],
        }
        _overview_cache["payload"] = payload
        _overview_cache["expires_at"] = monotonic() + _OVERVIEW_CACHE_TTL_SECONDS
        return payload


def _replay_mismatch_count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        total = 0
        for nested in value.values():
            if isinstance(nested, (list, dict)):
                total += _replay_mismatch_count(nested)
            elif isinstance(nested, bool):
                total += int(not nested)
            elif isinstance(nested, (int, float)):
                total += int(max(nested, 0))
            elif nested:
                total += 1
        return total
    return int(bool(value))


def _agent_validation_runner(
    status: dict[str, Any],
    *,
    key: str,
    label: str,
    strategy_key: str | None = None,
) -> dict[str, Any]:
    strategies = status.get("strategies") or status.get("strategy_agents") or []
    strategy = next(
        (item for item in strategies if not strategy_key or item.get("key") == strategy_key),
        {},
    )
    signals = strategy.get("signals") if isinstance(strategy.get("signals"), list) else []
    run_summary = strategy.get("last_run_summary") or {}
    requested = (
        status.get("instrument_universe")
        or (strategy.get("meta") or {}).get("instrument_universe")
        or []
    )
    if not requested:
        requested = [
            item.get("symbol") or item.get("underlying")
            for item in (status.get("futures_watchlist") or status.get("watchlist") or [])
            if isinstance(item, dict) and (item.get("symbol") or item.get("underlying"))
        ]
    evaluated = int(
        run_summary.get("evaluated")
        or run_summary.get("scanned")
        or status.get("active_windows")
        or len(requested)
        or len(signals)
    )
    actionable = sum(
        1
        for item in signals
        if str(item.get("status") or "").lower()
        in {"actionable", "approved", "entry-ready", "triggered", "open"}
    )
    blocked = dict(run_summary.get("blocked_reasons") or run_summary.get("rejection_counts") or {})
    last_error = status.get("last_error")
    return {
        "key": key,
        "label": label,
        "enabled": status.get("enabled", True),
        "running": status.get("running", False),
        "interval_seconds": status.get("scan_interval_seconds"),
        "last_started_at": status.get("last_run_at"),
        "last_success_at": status.get("last_run_at") if not last_error else None,
        "last_finished_at": status.get("last_run_at"),
        "last_error": last_error,
        "last_message": status.get("last_message") or strategy.get("last_message"),
        "last_result_meta": {
            "result_count": evaluated,
            "actionable_count": actionable,
            "failure_count": int(bool(last_error)),
            "rejection_counts": blocked,
            "symbols_requested": requested,
            "symbols_completed": requested if evaluated else [],
        },
        "cycle_stats": {},
    }


def build_signal_validation_payload(
    supervisor_status: dict[str, Any],
    audits: list[dict[str, Any]],
    snapshot_status: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Normalize runtime evidence around signal correctness, not paper P/L."""
    generated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    audit_by_lane = {str(item.get("lane")): item for item in audits}
    audit_aliases = {
        "macd_refined": "s1",
        "directional_options": "directional_options",
        "auction_intelligence": "auction_intelligence",
        "fractal_market_profile": "fmp",
        "cbe_scanner": "cbe_scanner",
    }
    lanes: list[dict[str, Any]] = []
    total_evaluated = total_actionable = total_failures = total_stale_missing = 0
    for key, runner in (supervisor_status.get("runners") or {}).items():
        meta = runner.get("last_result_meta") or {}
        evaluated = int(meta.get("result_count") or 0)
        actionable = int(meta.get("actionable_count") or 0)
        failures = int(meta.get("failure_count") or 0)
        requested = meta.get("symbols_requested") or meta.get("requested") or []
        completed = meta.get("symbols_completed") or meta.get("completed") or []
        coverage_pct = meta.get("coverage_pct")
        if isinstance(requested, list) and requested:
            coverage_pct = round(len(set(map(str, completed))) / len(set(map(str, requested))) * 100.0, 2)
        rejection_counts = dict(meta.get("rejection_counts") or {})
        funnel = dict(meta.get("funnel") or {})
        stale_missing = sum(
            int(count or 0)
            for reason, count in rejection_counts.items()
            if any(token in str(reason).lower() for token in ("stale", "missing", "no_data", "no data", "fresh"))
        )
        audit = audit_by_lane.get(audit_aliases.get(str(key), str(key)))
        mismatch_count = _replay_mismatch_count((audit or {}).get("replay_mismatches")) if audit else None
        cycle_stats = runner.get("cycle_stats") or {}
        lanes.append(
            {
                "key": key,
                "label": runner.get("label") or key,
                "status": "running" if runner.get("running") else "failed" if runner.get("last_error") else "ready",
                "interval_seconds": runner.get("interval_seconds"),
                "last_success_at": runner.get("last_success_at"),
                "evaluated_count": evaluated,
                "actionable_count": actionable,
                "failure_count": failures,
                "coverage_pct": coverage_pct,
                "rejection_counts": rejection_counts,
                "rejection_funnel": funnel,
                "stale_or_missing_count": stale_missing,
                "median_latency_seconds": cycle_stats.get("median_duration_seconds"),
                "frequency_drift_pct": cycle_stats.get("frequency_drift_pct"),
                "replay_mismatch_count": mismatch_count,
                "replay_parity_pass": (audit or {}).get("replay_parity_pass"),
                "audit_date": (audit or {}).get("audit_date"),
                "last_error": runner.get("last_error"),
            }
        )
        total_evaluated += evaluated
        total_actionable += actionable
        total_failures += failures
        total_stale_missing += stale_missing
    return {
        "generated_at": generated_at.isoformat(),
        "summary": {
            "lane_count": len(lanes),
            "evaluated_count": total_evaluated,
            "actionable_count": total_actionable,
            "failure_count": total_failures,
            "stale_or_missing_count": total_stale_missing,
            "degraded_feed_count": int(snapshot_status.get("degraded_count") or 0)
            + int(snapshot_status.get("rejected_count") or 0),
        },
        "lanes": lanes,
        "validated_snapshots": snapshot_status,
        "audits": audits,
    }


@router.get("/signal-validation")
async def signal_validation() -> dict[str, Any]:
    audits: list[dict[str, Any]] = []
    try:
        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT DISTINCT ON (lane)
                            lane, audit_date, replay_parity_pass, replay_mismatches,
                            data_integrity_pass, freshness_violations,
                            gate_block_breakdown, overall_status, created_at
                        FROM lane_audit
                        ORDER BY lane, audit_date DESC, created_at DESC
                        """
                    )
                )
            ).mappings().all()
        for row in rows:
            item = dict(row)
            for key, value in list(item.items()):
                if hasattr(value, "isoformat"):
                    item[key] = value.isoformat()
            audits.append(item)
    except Exception as exc:
        # Older/dev databases may not have the audit migration yet. Runtime
        # signal telemetry remains useful and the gap is explicit in `audits`.
        logger.debug(f"[System] lane audit read unavailable: {exc}")
    supervisor_status = market_hours_paper_supervisor.get_status()
    runners = dict(supervisor_status.get("runners") or {})
    nse_status = _agent_status_snapshot(paper_strategy_agent)
    runners["macd_strategy"] = _agent_validation_runner(
        nse_status,
        key="macd_strategy",
        label="Strategy 1 · 30m ATM MACD",
        strategy_key="macd_strategy",
    )
    commodity_status = _agent_status_snapshot(commodity_strategy_agent)
    runners["commodity_strategy"] = _agent_validation_runner(
        commodity_status,
        key="commodity_strategy",
        label="Commodity MP + Order Flow",
        strategy_key="commodity_futures",
    )
    try:
        from macd_refined.service import us_macd_refined_service

        us_summary, us_signals = await asyncio.gather(
            asyncio.to_thread(us_macd_refined_service.summary),
            asyncio.to_thread(us_macd_refined_service.signals, limit=100),
        )
        automation = dict(us_summary.get("automation") or {})
        rows = list(us_signals.get("signals") or [])
        rejection_counts: dict[str, int] = {}
        for row in rows:
            if row.get("accepted") is False:
                reason = str(row.get("gate_reason") or row.get("reason") or "rejected")
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
        runners["us_macd_refined"] = {
            **automation,
            "key": "us_macd_refined",
            "label": us_summary.get("label") or "US MACD Refined",
            "interval_seconds": automation.get("interval_seconds"),
            "last_result_meta": {
                "result_count": int(us_signals.get("count") or len(rows)),
                "actionable_count": int(us_signals.get("accepted") or 0),
                "failure_count": int(bool(automation.get("last_error"))),
                "rejection_counts": rejection_counts,
                "symbols_requested": list(us_summary.get("live_universe") or []),
                "symbols_completed": list(us_summary.get("live_universe") or []) if rows else [],
            },
            "cycle_stats": automation.get("cycle_stats") or {},
        }
    except Exception as exc:
        logger.debug(f"[System] US MACD validation status unavailable: {exc}")
    supervisor_status = {**supervisor_status, "runners": runners}
    return build_signal_validation_payload(
        supervisor_status,
        audits,
        validated_snapshot_store.status(),
    )


@router.get("/ws-chain-probe")
async def ws_chain_probe_status() -> dict[str, Any]:
    """WS-first chain design P0 probe results (empty unless WS_CHAIN_PROBE_ENABLED
    was on for a live session). Answers: Upstox WS greeks/iv presence + unit,
    Fyers oi/pdoi cadence. See market_data/ws_chain_probe.py."""
    from market_data.ws_chain_probe import ws_chain_probe
    return {
        "enabled": settings.WS_CHAIN_PROBE_ENABLED,
        "sources": ws_chain_probe.snapshot(),
    }


@router.get("/rate-budget")
async def rate_budget_status() -> dict[str, Any]:
    """Live broker REST rate-limiter telemetry: per-window used/allowed, day
    count, plus admit/429/queue-wait counters and the process circuit-breaker
    state. Makes broker-budget headroom + throttling observable at a glance."""
    from brokers.rate_limiter import FYERS_DATA_LIMITER, UPSTOX_DATA_LIMITER
    payload: dict[str, Any] = {
        "fyers": FYERS_DATA_LIMITER.snapshot(),
        "upstox": UPSTOX_DATA_LIMITER.snapshot(),
    }
    try:
        from market_data.broker_circuit import broker_circuit
        payload["circuit"] = broker_circuit.snapshot()
    except Exception:  # noqa: BLE001 — circuit module optional / not yet imported
        pass
    try:
        from market_data.chain_candle_builder import chain_candle_builder
        payload["chain_builder"] = chain_candle_builder.status()
    except Exception:  # noqa: BLE001
        pass
    return payload


@router.get("/pools")
async def connection_pools() -> dict[str, Any]:
    """Connection-pool telemetry (Redis P0, 2026-07-18).

    Redis: both bounded blocking pools (command + pub/sub) with in_use /
    available / max plus wait-and-timeout counters — makes Monday-open
    connection demand observable instead of a mystery "Too many connections"
    storm. Postgres: the SQLAlchemy engine pool's checked-out/size/overflow.
    """
    from db.database import engine
    from db.redis_client import redis_pool_stats

    payload: dict[str, Any] = {"redis": redis_pool_stats()}
    try:
        pool = engine.pool
        payload["database"] = {
            "size": pool.size(),
            "checked_out": pool.checkedout(),
            "checked_in": pool.checkedin(),
            "overflow": pool.overflow(),
        }
    except Exception as exc:  # noqa: BLE001 — telemetry must never 500
        payload["database"] = {"error": str(exc)}
    return payload
