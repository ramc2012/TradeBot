"""MACD Backtest API endpoints."""
from __future__ import annotations

import asyncio
import json
import math
import os
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, Response
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from analysis.backtest import MACDBacktester
from analysis.validation_live import (
    get_live_validation_report_artifact,
    get_live_validation_report_payload,
)
from db.database import AsyncSessionLocal

router = APIRouter(prefix="/api/analysis", tags=["analysis"])

# ── In-memory task registry ────────────────────────────────────────────────────
# Maps task_id → BacktestTask


@dataclass
class BacktestTask:
    task_id: str
    status: str = "pending"          # pending | running | done | error
    underlyings: list[str] = field(default_factory=list)
    from_date: str = ""
    to_date: str = ""
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: str = ""
    progress: dict = field(default_factory=dict)
    results: Optional[dict] = None

    @property
    def elapsed_secs(self) -> int:
        if self.started_at is None:
            return 0
        end = self.finished_at or datetime.utcnow()
        return int((end - self.started_at).total_seconds())

    def to_dict(self, include_results: bool = False) -> dict:
        d: dict[str, Any] = {
            "task_id": self.task_id,
            "status": self.status,
            "underlyings": self.underlyings,
            "from_date": self.from_date,
            "to_date": self.to_date,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "elapsed_secs": self.elapsed_secs,
            "error": self.error,
            "progress": self.progress,
        }
        if include_results and self.results is not None:
            # Exclude the potentially large all_trades list from status calls
            slim_results = {k: v for k, v in self.results.items() if k != "all_trades"}
            slim_results["trade_count"] = len(self.results.get("all_trades", []))
            d["results_summary"] = slim_results
        return d


# Keep only the last 100 tasks to avoid unbounded memory growth
_tasks: dict[str, BacktestTask] = {}
_MAX_TASKS = 100
_VALIDATION_REPORT_FILES = {
    "report.md",
    "summary.json",
    "trades.csv",
    "coverage.csv",
    "chain_summary.csv",
}
_VALIDATION_REPORT_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "reports" / "validation" / "nse-cache-current",
    Path(__file__).resolve().parents[3] / "reports" / "validation" / "nse-cache-current",
    Path(__file__).resolve().parents[2] / "reports" / "validation",
    Path(__file__).resolve().parents[3] / "reports" / "validation",
]
_RESEARCH_SYNC_STATE_FILE = (
    Path(__file__).resolve().parents[2] / "runtime" / "research_sync_status.json"
)


def _stage_for_symbol(row: dict[str, Any]) -> str:
    total_expiries = int(row.get("total_expiries") or 0)
    discovered_expiries = int(row.get("discovered_expiries") or 0)
    spot_candles = int(row.get("spot_candles") or 0)
    total_contracts = int(row.get("total_contracts") or 0)
    complete_contracts = int(row.get("complete_contracts") or 0)
    option_candles = int(row.get("option_candles") or 0)
    pending_contracts = int(row.get("pending_contracts") or 0)

    if option_candles > 0 and total_contracts > 0 and pending_contracts == 0:
        return "populated"
    if option_candles > 0 or complete_contracts > 0:
        return "populating"
    if total_contracts > 0 or discovered_expiries > 0:
        return "contracts"
    if spot_candles > 0:
        return "spot"
    if total_expiries > 0:
        return "metadata"
    return "queued"


def _symbol_progress_pct(row: dict[str, Any]) -> float:
    total_expiries = int(row.get("total_expiries") or 0)
    discovered_expiries = int(row.get("discovered_expiries") or 0)
    selection_spots_ready = int(row.get("selection_spots_ready") or 0)
    spot_candles = int(row.get("spot_candles") or 0)
    total_contracts = int(row.get("total_contracts") or 0)
    complete_contracts = int(row.get("complete_contracts") or 0)
    empty_contracts = int(row.get("empty_contracts") or 0)

    expiry_component = 0.0
    selection_component = 0.0
    if total_expiries > 0:
        expiry_component = 25.0 * (discovered_expiries / total_expiries)
        selection_component = 15.0 * (selection_spots_ready / total_expiries)

    spot_component = 20.0 if spot_candles > 0 else 0.0

    contract_component = 0.0
    if total_contracts > 0:
        processed_contracts = complete_contracts + empty_contracts
        contract_component = 40.0 * (processed_contracts / total_contracts)

    return round(min(100.0, expiry_component + selection_component + spot_component + contract_component), 1)


def _serialise_ts(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_json_safe(inner) for inner in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _utc_naive(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _get_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _load_research_sync_runtime_state() -> dict[str, Any]:
    try:
        if not _RESEARCH_SYNC_STATE_FILE.exists():
            return {}
        return json.loads(_RESEARCH_SYNC_STATE_FILE.read_text())
    except Exception as exc:
        logger.debug(f"Could not load research sync runtime state: {exc}")
        return {}


def _build_research_scheduler_summary(
    *,
    now_utc: datetime,
    recent_activity_at: Optional[datetime],
    contracts_pending: int,
    active_recent_symbols: int,
    runtime_state: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    poll_minutes = max(1, _get_int_env("RESEARCH_POLL_MINUTES", 30))
    rate_limit_window_minutes = max(
        1,
        _get_int_env("UPSTOX_RATE_LIMIT_WINDOW_MINUTES", poll_minutes),
    )
    cooldown_minutes = max(
        1,
        min(
            poll_minutes,
            _get_int_env(
                "RESEARCH_RATE_LIMIT_COOLDOWN_MINUTES",
                max(1, rate_limit_window_minutes // 3),
            ),
        ),
    )
    active_grace_minutes = max(1, _get_int_env("RESEARCH_ACTIVE_GRACE_MINUTES", 2))

    state = "idle"
    label = "Aggregation idle"
    detail = "No pending contract backlog is waiting for the next sync pass."
    next_batch_at: Optional[datetime] = None
    seconds_until_next_batch = 0
    available_pct: Optional[float] = None
    used_pct: Optional[float] = None
    runtime_state = runtime_state or {}
    runtime_name = str(runtime_state.get("state") or "").strip().lower()
    runtime_started_at = _parse_iso_datetime(runtime_state.get("run_started_at"))
    runtime_completed_at = _parse_iso_datetime(runtime_state.get("run_completed_at"))
    runtime_next_run_at = _parse_iso_datetime(runtime_state.get("next_run_at"))
    runtime_error = str(runtime_state.get("error") or "").strip()

    if contracts_pending > 0 and runtime_name:
        if runtime_name == "running":
            state = "running"
            label = "Aggregation in progress"
            detail = (
                "Current Upstox aggregation batch is running."
                if runtime_started_at is None
                else f"Current Upstox aggregation batch started at {runtime_started_at.isoformat()}."
            )
        elif runtime_name == "waiting":
            state = "waiting"
            label = "Waiting for next aggregation pass"
            detail = "Last aggregation batch completed and the next scheduled pass is pending."
        elif runtime_name == "error":
            state = "waiting"
            label = "Aggregation waiting after error"
            detail = runtime_error or "The previous aggregation pass failed and the worker is waiting for the next scheduled retry."

        if runtime_next_run_at is not None:
            next_batch_at = runtime_next_run_at
            seconds_until_next_batch = max(0, int((runtime_next_run_at - now_utc).total_seconds()))

        window_started_at = runtime_started_at or recent_activity_at
        window_ends_at = runtime_next_run_at
        if window_started_at is not None and window_ends_at is not None:
            total_window_seconds = max(1.0, (window_ends_at - window_started_at).total_seconds())
            elapsed_window_seconds = min(
                total_window_seconds,
                max(0.0, (now_utc - window_started_at).total_seconds()),
            )
            available_pct = round(100.0 * (elapsed_window_seconds / total_window_seconds), 1)
            used_pct = round(100.0 - available_pct, 1)

        return {
            "state": state,
            "label": label,
            "detail": detail,
            "pause_assumed": runtime_name == "error",
            "poll_minutes": poll_minutes,
            "rate_limit_window_minutes": rate_limit_window_minutes,
            "cooldown_minutes": cooldown_minutes,
            "active_grace_minutes": active_grace_minutes,
            "next_batch_at": _serialise_ts(next_batch_at),
            "seconds_until_next_batch": seconds_until_next_batch,
            "estimated_window_available_pct": available_pct,
            "estimated_window_used_pct": used_pct,
            "last_batch_activity_at": _serialise_ts(recent_activity_at),
            "last_run_started_at": _serialise_ts(runtime_started_at),
            "last_run_completed_at": _serialise_ts(runtime_completed_at),
        }

    if contracts_pending > 0:
        state = "running"
        label = "Aggregation in progress"
        detail = (
            f"{active_recent_symbols} symbols updated in the last "
            f"{active_grace_minutes} minute{'s' if active_grace_minutes != 1 else ''}."
        )

    if contracts_pending > 0 and recent_activity_at is not None:
        seconds_since_recent_activity = max(
            0,
            int((now_utc - recent_activity_at).total_seconds()),
        )
        if active_recent_symbols == 0 and seconds_since_recent_activity < cooldown_minutes * 60:
            state = "rate_limit_cooldown"
            label = "Aggregation paused due to rate limit"
            detail = (
                f"Upstox request budget looks exhausted for this pass. "
                f"Next batch resumes after the {rate_limit_window_minutes}-minute window refills."
            )
            next_batch_at = recent_activity_at + timedelta(minutes=cooldown_minutes)
            seconds_until_next_batch = max(
                0,
                int((next_batch_at - now_utc).total_seconds()),
            )
            cooldown_total_seconds = max(1, cooldown_minutes * 60)
            available_pct = round(
                min(
                    100.0,
                    max(
                        0.0,
                        100.0 * (1 - (seconds_until_next_batch / cooldown_total_seconds)),
                    ),
                ),
                1,
            )
            used_pct = round(max(0.0, 100.0 - available_pct), 1)
        elif active_recent_symbols == 0:
            state = "waiting"
            label = "Waiting for next aggregation pass"
            detail = (
                "No cache writes were observed in the last few minutes, but a backlog remains."
            )
            next_batch_at = recent_activity_at + timedelta(minutes=poll_minutes)
            seconds_until_next_batch = max(
                0,
                int((next_batch_at - now_utc).total_seconds()),
            )

    return {
        "state": state,
        "label": label,
        "detail": detail,
        "pause_assumed": state == "rate_limit_cooldown",
        "poll_minutes": poll_minutes,
        "rate_limit_window_minutes": rate_limit_window_minutes,
        "cooldown_minutes": cooldown_minutes,
        "active_grace_minutes": active_grace_minutes,
        "next_batch_at": _serialise_ts(next_batch_at),
        "seconds_until_next_batch": seconds_until_next_batch,
        "estimated_window_available_pct": available_pct,
        "estimated_window_used_pct": used_pct,
        "last_batch_activity_at": _serialise_ts(recent_activity_at),
        "last_run_started_at": None,
        "last_run_completed_at": None,
    }


def _resolve_validation_report_dir() -> Optional[Path]:
    for base in _VALIDATION_REPORT_CANDIDATES:
        if not base.exists():
            continue
        if base.is_dir() and (base / "summary.json").exists():
            return base
        if not base.is_dir():
            continue
        candidates = sorted(
            [
                path for path in base.iterdir()
                if path.is_dir() and (path / "summary.json").exists()
            ],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]
    return None


def _load_validation_report_payload() -> dict[str, Any]:
    report_dir = _resolve_validation_report_dir()
    if report_dir is None:
        return {
            "available": False,
            "detail": (
                "No validation report is available yet. Generate one into "
                "backend/reports/validation/nse-cache-current."
            ),
        }

    summary_path = report_dir / "summary.json"
    report_path = report_dir / "report.md"

    if not summary_path.exists():
        return {
            "available": False,
            "detail": f"Validation report directory found but summary.json is missing in {report_dir}",
        }

    try:
        summary = _json_safe(json.loads(summary_path.read_text()))
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not parse validation summary.json: {exc}",
        ) from exc

    markdown_preview = ""
    if report_path.exists():
        markdown_preview = report_path.read_text()

    files = {
        "report_markdown_url": "/api/analysis/validation-report/latest/file/report.md",
        "summary_json_url": "/api/analysis/validation-report/latest/file/summary.json",
        "trades_csv_url": "/api/analysis/validation-report/latest/file/trades.csv",
        "coverage_csv_url": "/api/analysis/validation-report/latest/file/coverage.csv",
        "chain_summary_csv_url": "/api/analysis/validation-report/latest/file/chain_summary.csv",
    }

    return {
        "available": True,
        "report_key": report_dir.name,
        "report_dir": str(report_dir),
        "generated_at": summary.get("generated_at"),
        "summary": summary,
        "markdown_preview": markdown_preview,
        "files": files,
    }


# ── Request / Response models ─────────────────────────────────────────────────


class StartBacktestRequest(BaseModel):
    underlyings: list[str] = []  # empty = auto-discover all F&O underlyings
    from_date: str = ""          # YYYY-MM-DD; default = 1 year ago
    to_date: str = ""            # YYYY-MM-DD; default = today
    upstox_token: str = ""       # optional — auto-uses connected Upstox session


# ── Background worker ─────────────────────────────────────────────────────────


async def _run_backtest(task_id: str, req: StartBacktestRequest) -> None:
    """Background coroutine that executes the MACD backtest and updates the task."""
    task = _tasks.get(task_id)
    if task is None:
        logger.error(f"Backtest task {task_id} not found in registry")
        return

    task.status = "running"
    task.started_at = datetime.utcnow()

    try:
        from_date = (
            date.fromisoformat(req.from_date)
            if req.from_date
            else date.today() - timedelta(days=365)
        )
        to_date = (
            date.fromisoformat(req.to_date)
            if req.to_date
            else date.today()
        )

        if from_date > to_date:
            raise ValueError(f"from_date {from_date} is after to_date {to_date}")

        backtester = MACDBacktester(access_token=req.upstox_token)

        # Auto-discover all F&O underlyings if none specified
        underlyings = list(req.underlyings)
        if not underlyings:
            logger.info("No underlyings specified — auto-discovering current NSE F&O universe")
            task.progress = {"pct": 0, "current": "Fetching NSE F&O universe…"}
            universe = await backtester.fetch_fo_universe()
            underlyings = sorted(universe["indices"] + universe["stocks"])
            logger.info(f"Auto-discovered {len(underlyings)} F&O underlyings")
            task.progress = {"pct": 1, "current": f"Found {len(underlyings)} underlyings, starting analysis…"}

        def _progress_cb(progress_dict: dict) -> None:
            task.progress = progress_dict

        results = await backtester.run(
            underlyings=underlyings,
            from_date=from_date,
            to_date=to_date,
            progress_cb=_progress_cb,
        )

        task.results = results
        task.status = "done"
        task.finished_at = datetime.utcnow()

        total_opps = results.get("total_opportunities", 0)
        logger.info(
            f"Backtest {task_id} complete: {total_opps} opportunities found "
            f"in {task.elapsed_secs}s"
        )

    except Exception as exc:
        logger.error(f"Backtest task {task_id} failed: {exc}")
        task.status = "error"
        task.error = str(exc)
        task.finished_at = datetime.utcnow()


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/broker-status")
async def analysis_broker_status():
    """Return which brokers are currently connected and whether Upstox/Breeze are ready."""
    from api.routers.auth import (
        get_connected_brokers,
        get_broker_token,
        get_upstox_token_health,
    )
    connected = get_connected_brokers()
    upstox_token = get_broker_token("upstox")
    breeze_token = get_broker_token("icici_breeze")
    upstox_health = await get_upstox_token_health()
    upstox_ready = bool(upstox_health.get("valid"))
    note = upstox_health.get("message")
    if not note:
        note = (
            "Breeze connected — additional expired-contract history is available as fallback."
            if "icici_breeze" in connected
            else "Upstox expired-instruments APIs cover last-year expired options if the connected account has Plus access. "
            "Breeze remains a useful fallback for deeper history."
        )
    return {
        "connected_brokers": connected,
        "upstox_connected": "upstox" in connected,
        "upstox_ready": upstox_ready,
        "upstox_token_preview": f"{upstox_token[:12]}…" if upstox_token else None,
        "upstox_token_health": upstox_health,
        "breeze_connected": "icici_breeze" in connected,
        "breeze_token_preview": f"…{breeze_token[-6:]}" if breeze_token else None,
        "ready": upstox_ready,
        "data_sources": {
            "spot_prices": "upstox_public" if upstox_ready else "none",
            "options_history": (
                "breeze+upstox" if ("icici_breeze" in connected and upstox_ready)
                else "breeze" if "icici_breeze" in connected
                else "upstox_expired_api" if upstox_ready
                else "none"
            ),
            "note": note,
        },
    }


@router.get("/fo-underlyings")
async def get_fo_underlyings():
    """
    Return all unique underlying symbols in the NSE F&O segment.
    Fetches from Upstox instrument master using the connected Upstox session.
    """
    from api.routers.auth import get_broker_token
    token = get_broker_token("upstox") or ""
    if not token:
        # Return the static list if Upstox not connected
        from analysis.instruments import NSE_FO_INDICES
        stocks: list[str] = []
        return {
            "source": "static",
            "indices": NSE_FO_INDICES,
            "stocks": sorted(stocks),
            "total": len(NSE_FO_INDICES) + len(stocks),
        }
    try:
        backtester = MACDBacktester(access_token=token)
        universe = await backtester.fetch_fo_universe()
        indices = universe["indices"]
        stocks = universe["stocks"]

        return {
            "source": "nse_underlying_information",
            "indices": sorted(indices),
            "stocks": stocks,
            "total": len(indices) + len(stocks),
        }
    except Exception as exc:
        logger.warning(f"Could not fetch F&O underlyings: {exc}")
        from analysis.instruments import NSE_FO_INDICES
        stocks: list[str] = []
        return {
            "source": "static_fallback",
            "indices": NSE_FO_INDICES,
            "stocks": sorted(stocks),
            "total": len(NSE_FO_INDICES) + len(stocks),
            "error": str(exc),
        }


@router.get("/research-cache-status")
async def get_research_cache_status():
    """
    Return a live view of local research-cache population progress.

    The response is derived from Timescale tables populated by the recurring
    Upstox research-sync job and is intended for UI polling.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                WITH expiry_stats AS (
                    SELECT
                        underlying,
                        COUNT(*) AS total_expiries,
                        COUNT(*) FILTER (WHERE contracts_discovered_at IS NOT NULL) AS discovered_expiries,
                        COUNT(*) FILTER (WHERE selection_spot_price IS NOT NULL) AS selection_spots_ready,
                        MAX(
                            GREATEST(
                                COALESCE(contracts_discovered_at, TIMESTAMPTZ 'epoch'),
                                COALESCE(selection_spot_time, TIMESTAMPTZ 'epoch'),
                                COALESCE(updated_at, TIMESTAMPTZ 'epoch')
                            )
                        ) AS last_expiry_activity
                    FROM fo_expiry_catalog
                    GROUP BY underlying
                ),
                contract_stats AS (
                    SELECT
                        underlying,
                        COUNT(*) AS total_contracts,
                        COUNT(*) FILTER (WHERE sync_status = 'complete') AS complete_contracts,
                        COUNT(*) FILTER (WHERE sync_status = 'pending') AS pending_contracts,
                        COUNT(*) FILTER (WHERE sync_status = 'empty') AS empty_contracts,
                        MAX(
                            GREATEST(
                                COALESCE(last_synced_at, TIMESTAMPTZ 'epoch'),
                                COALESCE(updated_at, TIMESTAMPTZ 'epoch')
                            )
                        ) AS last_contract_activity
                    FROM fo_contract_catalog
                    GROUP BY underlying
                ),
                spot_stats AS (
                    SELECT
                        underlying,
                        COUNT(*) AS spot_candles,
                        MAX(synced_at) AS last_spot_activity
                    FROM underlying_spot_candles
                    GROUP BY underlying
                ),
                option_stats AS (
                    SELECT
                        underlying,
                        COUNT(*) AS option_candles,
                        COUNT(DISTINCT instrument_key) AS option_contracts,
                        MAX(synced_at) AS last_option_activity
                    FROM option_premium_candles
                    WHERE instrument_key IS NOT NULL
                    GROUP BY underlying
                )
                SELECT
                    u.symbol,
                    u.kind,
                    COALESCE(e.total_expiries, 0) AS total_expiries,
                    COALESCE(e.discovered_expiries, 0) AS discovered_expiries,
                    COALESCE(e.selection_spots_ready, 0) AS selection_spots_ready,
                    COALESCE(s.spot_candles, 0) AS spot_candles,
                    COALESCE(c.total_contracts, 0) AS total_contracts,
                    COALESCE(c.complete_contracts, 0) AS complete_contracts,
                    COALESCE(c.pending_contracts, 0) AS pending_contracts,
                    COALESCE(c.empty_contracts, 0) AS empty_contracts,
                    COALESCE(o.option_candles, 0) AS option_candles,
                    COALESCE(o.option_contracts, 0) AS option_contracts,
                    u.expiries_synced_at,
                    u.spot_synced_at,
                    GREATEST(
                        COALESCE(u.expiries_synced_at, TIMESTAMPTZ 'epoch'),
                        COALESCE(u.spot_synced_at, TIMESTAMPTZ 'epoch'),
                        COALESCE(e.last_expiry_activity, TIMESTAMPTZ 'epoch'),
                        COALESCE(c.last_contract_activity, TIMESTAMPTZ 'epoch'),
                        COALESCE(s.last_spot_activity, TIMESTAMPTZ 'epoch'),
                        COALESCE(o.last_option_activity, TIMESTAMPTZ 'epoch')
                    ) AS last_activity_at
                FROM fo_underlying_catalog u
                LEFT JOIN expiry_stats e
                  ON e.underlying = u.symbol
                LEFT JOIN contract_stats c
                  ON c.underlying = u.symbol
                LEFT JOIN spot_stats s
                  ON s.underlying = u.symbol
                LEFT JOIN option_stats o
                  ON o.underlying = u.symbol
                ORDER BY u.symbol
            """)
        )
        raw_rows = [dict(row) for row in result.mappings().all()]
        freshness_result = await session.execute(
            text("""
                SELECT
                    MAX(synced_at) AS last_successful_option_sync_at,
                    COUNT(*) FILTER (
                        WHERE synced_at >= NOW() - INTERVAL '30 minutes'
                    ) AS option_candles_added_last_30m
                FROM option_premium_candles
                WHERE instrument_key IS NOT NULL
            """)
        )
        contract_touch_result = await session.execute(
            text("""
                SELECT
                    MAX(CASE WHEN sync_status = 'complete' THEN last_synced_at END) AS last_complete_contract_sync_at,
                    MAX(CASE WHEN sync_status = 'empty' THEN last_synced_at END) AS last_empty_contract_touch_at,
                    COUNT(*) FILTER (
                        WHERE sync_status = 'complete'
                          AND last_synced_at >= NOW() - INTERVAL '30 minutes'
                    ) AS complete_contracts_touched_last_30m,
                    COUNT(*) FILTER (
                        WHERE sync_status = 'empty'
                          AND last_synced_at >= NOW() - INTERVAL '30 minutes'
                    ) AS empty_contracts_touched_last_30m
                FROM fo_contract_catalog
            """)
        )

    freshness_row = freshness_result.mappings().one()
    contract_touch_row = contract_touch_result.mappings().one()

    now_utc = datetime.utcnow()
    recent_activity_grace = timedelta(
        minutes=max(1, _get_int_env("RESEARCH_ACTIVE_GRACE_MINUTES", 2))
    )
    symbols: list[dict[str, Any]] = []
    stage_counts = {
        "queued": 0,
        "metadata": 0,
        "spot": 0,
        "contracts": 0,
        "populating": 0,
        "populated": 0,
    }
    active_recent_symbols = 0
    recent_activity_at_dt: Optional[datetime] = None

    for row in raw_rows:
        stage = _stage_for_symbol(row)
        stage_counts[stage] += 1

        last_activity_at_value = _utc_naive(row.get("last_activity_at"))
        active_now = False
        if last_activity_at_value is not None:
            active_now = last_activity_at_value >= now_utc - timedelta(minutes=20)
            if last_activity_at_value >= now_utc - recent_activity_grace:
                active_recent_symbols += 1
            if recent_activity_at_dt is None or last_activity_at_value > recent_activity_at_dt:
                recent_activity_at_dt = last_activity_at_value

        symbol_row = {
            "symbol": row["symbol"],
            "kind": row["kind"],
            "stage": stage,
            "progress_pct": _symbol_progress_pct(row),
            "active_now": active_now,
            "total_expiries": int(row.get("total_expiries") or 0),
            "discovered_expiries": int(row.get("discovered_expiries") or 0),
            "selection_spots_ready": int(row.get("selection_spots_ready") or 0),
            "spot_candles": int(row.get("spot_candles") or 0),
            "total_contracts": int(row.get("total_contracts") or 0),
            "complete_contracts": int(row.get("complete_contracts") or 0),
            "pending_contracts": int(row.get("pending_contracts") or 0),
            "empty_contracts": int(row.get("empty_contracts") or 0),
            "option_contracts": int(row.get("option_contracts") or 0),
            "option_candles": int(row.get("option_candles") or 0),
            "expiries_synced_at": _serialise_ts(row.get("expiries_synced_at")),
            "spot_synced_at": _serialise_ts(row.get("spot_synced_at")),
            "last_activity_at": _serialise_ts(last_activity_at_value),
        }
        symbols.append(symbol_row)

    def _count(predicate: Any) -> int:
        return sum(1 for row in symbols if predicate(row))

    universe_total = len(symbols)
    expiry_total = sum(row["total_expiries"] for row in symbols)
    discovered_expiries = sum(row["discovered_expiries"] for row in symbols)
    selection_spots_ready = sum(row["selection_spots_ready"] for row in symbols)
    contracts_total = sum(row["total_contracts"] for row in symbols)
    contracts_complete = sum(row["complete_contracts"] for row in symbols)
    contracts_pending = sum(row["pending_contracts"] for row in symbols)
    contracts_empty = sum(row["empty_contracts"] for row in symbols)
    option_contracts = sum(row["option_contracts"] for row in symbols)
    option_candles = sum(row["option_candles"] for row in symbols)
    recent_activity_at = _serialise_ts(recent_activity_at_dt)
    runtime_state = _load_research_sync_runtime_state()
    scheduler = _build_research_scheduler_summary(
        now_utc=now_utc,
        recent_activity_at=recent_activity_at_dt,
        contracts_pending=contracts_pending,
        active_recent_symbols=active_recent_symbols,
        runtime_state=runtime_state,
    )

    symbols.sort(
        key=lambda row: (
            0 if row["active_now"] else 1,
            0 if row["stage"] in {"populating", "contracts", "spot", "metadata"} else 1,
            -(row["option_candles"]),
            row["symbol"],
        )
    )

    return _json_safe({
        "summary": {
            "universe_total": universe_total,
            "underlyings_with_expiries": _count(lambda row: row["total_expiries"] > 0),
            "underlyings_with_spot": _count(lambda row: row["spot_candles"] > 0),
            "selection_spots_ready": selection_spots_ready,
            "expiry_total": expiry_total,
            "expiries_discovered": discovered_expiries,
            "contracts_total": contracts_total,
            "contracts_complete": contracts_complete,
            "contracts_pending": contracts_pending,
            "contracts_empty": contracts_empty,
            "option_contracts": option_contracts,
            "option_candles": option_candles,
            "active_symbols": _count(lambda row: row["active_now"]),
            "active_recent_symbols": active_recent_symbols,
            "populated_symbols": _count(lambda row: row["option_candles"] > 0),
            "symbols_in_progress": _count(
                lambda row: row["stage"] in {"metadata", "spot", "contracts", "populating"}
            ),
            "stage_counts": stage_counts,
            "recent_activity_at": recent_activity_at,
            "last_successful_option_sync_at": _serialise_ts(
                freshness_row.get("last_successful_option_sync_at")
            ),
            "last_complete_contract_sync_at": _serialise_ts(
                contract_touch_row.get("last_complete_contract_sync_at")
            ),
            "last_empty_contract_touch_at": _serialise_ts(
                contract_touch_row.get("last_empty_contract_touch_at")
            ),
            "option_candles_added_last_30m": int(
                freshness_row.get("option_candles_added_last_30m") or 0
            ),
            "complete_contracts_touched_last_30m": int(
                contract_touch_row.get("complete_contracts_touched_last_30m") or 0
            ),
            "empty_contracts_touched_last_30m": int(
                contract_touch_row.get("empty_contracts_touched_last_30m") or 0
            ),
        },
        "scheduler": scheduler,
        "symbols": symbols,
    })


@router.get("/validation-report/latest")
async def get_latest_validation_report():
    """
    Return the latest live NSE cache validation report summary.
    """
    try:
        return await asyncio.to_thread(get_live_validation_report_payload)
    except Exception as exc:
        logger.exception(f"Live validation report generation failed: {exc}")
        return _load_validation_report_payload()


@router.get("/validation-report/latest/file/{file_name}")
async def download_latest_validation_report_file(file_name: str):
    """
    Download a whitelisted artifact from the latest validation report.
    """
    if file_name not in _VALIDATION_REPORT_FILES:
        raise HTTPException(status_code=404, detail=f"Unsupported report file '{file_name}'")

    try:
        content, media_type = await asyncio.to_thread(
            get_live_validation_report_artifact,
            file_name,
        )
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
        )
    except Exception as exc:
        logger.exception(f"Live validation report artifact failed for {file_name}: {exc}")

    report_dir = _resolve_validation_report_dir()
    if report_dir is None:
        raise HTTPException(
            status_code=404,
            detail="No validation report is available yet.",
        )

    file_path = report_dir / file_name
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Report file '{file_name}' not found in {report_dir.name}.",
        )

    media_type = "text/plain; charset=utf-8"
    if file_name.endswith(".json"):
        media_type = "application/json"
    elif file_name.endswith(".csv"):
        media_type = "text/csv; charset=utf-8"
    elif file_name.endswith(".md"):
        media_type = "text/markdown; charset=utf-8"

    return FileResponse(
        path=file_path,
        media_type=media_type,
        filename=file_name,
    )


@router.post("/macd-backtest/start")
async def start_backtest(
    req: StartBacktestRequest,
    background_tasks: BackgroundTasks,
):
    """
    Start a MACD zero-line crossover backtest for NSE F&O options.

    - Iterates over each monthly expiry in the given date range
    - For each expiry, identifies ATM strike and fetches 30-min candles
    - Runs MACD(12,26,9) and detects zero-line buy crossovers
    - Analyses max potential move from each crossover signal

    Returns a `task_id` to poll for status and results.
    """
    # Auto-resolve Upstox token from active broker session if not provided
    from api.routers.auth import get_broker_token
    token = req.upstox_token.strip() or get_broker_token("upstox") or ""
    if not token:
        raise HTTPException(
            status_code=400,
            detail="Upstox is not connected. Go to Settings → Upstox and connect first.",
        )
    # Patch token onto a copy of the request so it flows to the background task
    req = StartBacktestRequest(
        underlyings=req.underlyings,
        from_date=req.from_date,
        to_date=req.to_date,
        upstox_token=token,
    )

    # Validate date formats if provided
    if req.from_date:
        try:
            date.fromisoformat(req.from_date)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid from_date format: '{req.from_date}' — expected YYYY-MM-DD",
            )
    if req.to_date:
        try:
            date.fromisoformat(req.to_date)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid to_date format: '{req.to_date}' — expected YYYY-MM-DD",
            )

    task_id = str(uuid.uuid4())
    task = BacktestTask(
        task_id=task_id,
        status="pending",
        underlyings=req.underlyings,
        from_date=req.from_date or (date.today() - timedelta(days=365)).isoformat(),
        to_date=req.to_date or date.today().isoformat(),
        created_at=datetime.utcnow(),
    )
    _tasks[task_id] = task

    # Trim registry if too large
    if len(_tasks) > _MAX_TASKS:
        oldest_key = next(iter(_tasks))
        del _tasks[oldest_key]

    background_tasks.add_task(_run_backtest, task_id, req)

    return {
        "task_id": task_id,
        "message": (
            f"MACD backtest started for {req.underlyings} "
            f"({task.from_date} → {task.to_date})"
        ),
        "status_url": f"/api/analysis/macd-backtest/status/{task_id}",
        "results_url": f"/api/analysis/macd-backtest/results/{task_id}",
    }


@router.get("/macd-backtest/status/{task_id}")
async def get_backtest_status(task_id: str):
    """
    Poll the status of a running or completed backtest by task_id.

    Returns status, elapsed time, and a compact results summary once done.
    Use the `/results/{task_id}` endpoint to retrieve the full trade list.
    """
    task = _tasks.get(task_id)
    if task is None:
        raise HTTPException(
            status_code=404,
            detail=f"Task '{task_id}' not found. It may have expired or never existed.",
        )
    return task.to_dict(include_results=True)


@router.get("/macd-backtest/results/{task_id}")
async def get_backtest_results(task_id: str):
    """
    Retrieve the full results of a completed MACD backtest.

    Returns the complete results dict including `all_trades` with individual
    trade records for every MACD crossover signal found.
    """
    task = _tasks.get(task_id)
    if task is None:
        raise HTTPException(
            status_code=404,
            detail=f"Task '{task_id}' not found. It may have expired or never existed.",
        )
    if task.status == "pending":
        raise HTTPException(
            status_code=202,
            detail="Backtest is queued but has not started yet. Retry in a few seconds.",
        )
    if task.status == "running":
        pct = task.progress.get("pct", 0)
        raise HTTPException(
            status_code=202,
            detail=f"Backtest is still running ({pct}% complete). Retry when status is 'done'.",
        )
    if task.status == "error":
        raise HTTPException(
            status_code=500,
            detail=f"Backtest failed: {task.error}",
        )
    if task.results is None:
        raise HTTPException(
            status_code=404,
            detail="No results available — task may still be running.",
        )

    return {
        "task_id": task_id,
        "status": task.status,
        "elapsed_secs": task.elapsed_secs,
        "from_date": task.from_date,
        "to_date": task.to_date,
        "underlyings": task.underlyings,
        "results": task.results,
    }


@router.get("/macd-backtest/tasks")
async def list_backtest_tasks():
    """
    List recent MACD backtest tasks (up to last 50), newest first.

    Returns a summary of each task without full trade data.
    """
    tasks = list(_tasks.values())[-50:]
    return [t.to_dict(include_results=False) for t in reversed(tasks)]
