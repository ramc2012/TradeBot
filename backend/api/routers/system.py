from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter
from loguru import logger
from sqlalchemy import text

from api.routers.analysis import _load_research_sync_runtime_state, _parse_iso_datetime
from api.routers.auction_intelligence import summary as auction_intelligence_summary
from api.routers.auth import get_broker_connection_snapshot
from analytics.performance import PerformanceAnalytics
from api.routers.trading import _get_or_create_paper_session, _risk_manager
from auction_intelligence.config import clone_default_config
from core.config import settings
from db.database import AsyncSessionLocal
from db.redis_client import get_redis
from market_data.data_router import data_router
from paper_engine.commodity_strategy_agent import commodity_strategy_agent, _in_commodity_hours
from paper_engine.strategy_agent import paper_strategy_agent, _in_market_hours


router = APIRouter(prefix="/api/system", tags=["system"])
_perf = PerformanceAnalytics()


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
) -> dict[str, Any]:
    return {
        "key": f"{parent}:{lane.get('key')}",
        "parent": parent,
        "label": lane.get("label") or lane.get("title") or lane.get("key") or "Strategy",
        "status": lane.get("status") or ("active" if positions else "idle"),
        "timeframe": lane.get("timeframe"),
        "scope": lane.get("scope") or lane.get("instrument"),
        "open_positions": positions,
        "last_scan_at": last_scan_at,
        "scan_interval_seconds": lane.get("scan_interval_seconds"),
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

    if error:
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
            "last_result": runtime.get("last_result") or {},
        },
    )


async def _broker_service() -> dict[str, Any]:
    snapshot = await get_broker_connection_snapshot(force_validate=False)
    connected = snapshot.get("connected_brokers") or []
    upstox = snapshot.get("upstox_token_health") or {}
    fyers = snapshot.get("fyers_token_health") or {}

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
        },
    )


def _market_data_service() -> dict[str, Any]:
    status = data_router.get_status()
    mode = status.get("mode")
    subscribed = int(status.get("subscribed_symbol_count") or 0)

    if mode == "mock":
        service_status = "healthy"
        detail = f"Mock market feed active for {subscribed} symbols."
    elif mode == "broker" and subscribed > 0:
        service_status = "healthy"
        detail = f"Broker feed subscribed for {subscribed} symbols."
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
        lanes.append(
            _lane_status_payload(
                parent=key,
                lane=lane,
                positions=positions,
                last_scan_at=strategy.get("last_scan_at") or status.get("last_run_at"),
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
            "last_run_at": status.get("last_run_at"),
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

    return _service(
        key="auction_intelligence",
        label="Auction Intelligence",
        status="healthy" if live_ready else "idle",
        detail=(
            "Validation and paper-trading module is broker-ready."
            if live_ready
            else "Validation module is available; live readiness depends on broker connectivity."
        ),
        meta={
            "live_ready": live_ready,
            "connected_brokers": summary.get("connected_brokers") or [],
            "deployable_first_sleeve": summary.get("deployable_first_sleeve"),
            "validation_gates": summary.get("validation_gates") or [],
            "symbols": config.get("mvp_scope", {}).get("underlyings") or [],
        },
    )


@router.get("/health")
async def system_health() -> dict[str, Any]:
    now_utc = datetime.now(timezone.utc)

    backend_service = _service(
        key="backend",
        label="Backend API",
        status="healthy",
        detail="FastAPI application is serving requests.",
        meta={"version": "1.0.0", "generated_at": now_utc.isoformat()},
    )

    postgres_service = await _postgres_service()
    redis_service = await _redis_service()
    research_sync_service = _research_sync_service(now_utc)
    broker_service = await _broker_service()
    market_service = _market_data_service()
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
    auction_service = await _auction_service()

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
    ]

    overall = "healthy"
    counts = {"healthy": 0, "idle": 0, "degraded": 0, "critical": 0}
    for item in services:
        state = str(item.get("status") or "critical")
        bucket = _service_bucket(state)
        counts[bucket] = counts.get(bucket, 0) + 1
        overall = _worst_status(overall, state)

    return {
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


@router.get("/overview")
async def system_overview() -> dict[str, Any]:
    health = await system_health()
    nse_status = paper_strategy_agent.get_status()
    commodity_status = commodity_strategy_agent.get_status()
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

    return {
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
        },
        "blockers": [
            item for item in health.get("services", [])
            if item.get("status") in {"critical", "degraded"}
        ],
    }
