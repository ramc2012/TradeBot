"""
F&O Historical Data Download API
=================================
Endpoints to trigger, monitor, and query Upstox NSE F&O options data downloads.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel
from loguru import logger

from api.routers.auth import ensure_upstox_session, get_broker_token
from data.index_analytics_collector import (
    INDEX_ANALYTICS_DATA_ROOT,
    DEFAULT_INTERVAL as INDEX_ANALYTICS_DEFAULT_INTERVAL,
    IndexAnalyticsCollector,
    IndexAnalyticsProgress,
    SUPPORTED_INDEX_ANALYTICS_UNDERLYINGS,
    load_index_analytics_summary,
)
from data.upstox_downloader import UpstoxFODownloader, DownloadProgress, get_stored_stats

router = APIRouter(prefix="/api/fo-data", tags=["fo-data"])

# ── In-memory task registry ────────────────────────────────────────────────────
# Maps task_id → DownloadProgress
_tasks: dict[str, DownloadProgress] = {}
_index_analytics_tasks: dict[str, IndexAnalyticsProgress] = {}


# ── Models ─────────────────────────────────────────────────────────────────────

class StartDownloadRequest(BaseModel):
    underlyings: list[str] = ["NIFTY", "BANKNIFTY"]
    from_date: str = ""          # YYYY-MM-DD; default = 1 year ago
    to_date: str = ""            # YYYY-MM-DD; default = today
    interval: str = "30minute"   # "1minute" | "30minute" | "day"
    option_types: list[str] = ["CE", "PE"]
    min_strike: Optional[float] = None
    max_strike: Optional[float] = None
    upstox_token: str           # required — Upstox access token


class StartIndexAnalyticsRequest(BaseModel):
    underlyings: list[str] = list(SUPPORTED_INDEX_ANALYTICS_UNDERLYINGS)
    from_date: str = ""
    to_date: str = ""
    interval: str = INDEX_ANALYTICS_DEFAULT_INTERVAL


# ── Background task runner ─────────────────────────────────────────────────────

async def _run_download(task_id: str, req: StartDownloadRequest) -> None:
    progress = _tasks[task_id]
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
        strike_range = (
            (req.min_strike, req.max_strike)
            if req.min_strike is not None and req.max_strike is not None
            else None
        )
        downloader = UpstoxFODownloader(access_token=req.upstox_token)
        await downloader.run(
            underlyings=req.underlyings,
            from_date=from_date,
            to_date=to_date,
            interval=req.interval,
            option_types=req.option_types,
            strike_range=strike_range,
            progress=progress,
        )
    except Exception as exc:
        logger.error(f"Download task {task_id} failed: {exc}")
        progress.status = "error"
        progress.error = str(exc)


def _index_task_file(task_id: str) -> Path:
    return INDEX_ANALYTICS_DATA_ROOT / "tasks" / f"{task_id}.json"


def _load_index_task_snapshot(task_id: str) -> Optional[dict]:
    path = _index_task_file(task_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return None
    return _normalize_index_task_snapshot(task_id, payload)


def _normalize_index_task_snapshot(task_id: str, payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    if task_id in _index_analytics_tasks:
        return _index_analytics_tasks[task_id].to_dict()
    if payload.get("status") in {"running", "pending"}:
        payload = dict(payload)
        payload["status"] = "error"
        payload["error"] = payload.get("error") or "Task was interrupted by a backend restart."
        payload["finished_at"] = payload.get("finished_at") or datetime.now(UTC).isoformat()
    return payload


def _list_index_task_snapshots() -> list[dict]:
    task_dir = INDEX_ANALYTICS_DATA_ROOT / "tasks"
    if not task_dir.exists():
        return []
    rows: dict[str, dict] = {
        task_id: progress.to_dict()
        for task_id, progress in _index_analytics_tasks.items()
    }
    for path in sorted(task_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text())
        except Exception:
            continue
        if isinstance(payload, dict):
            task_id = str(payload.get("task_id") or path.stem)
            rows[task_id] = _normalize_index_task_snapshot(task_id, payload)
    ordered_rows = list(rows.values())
    ordered_rows.sort(key=lambda row: str(row.get("started_at") or ""), reverse=True)
    return ordered_rows


async def _run_index_analytics_download(task_id: str, req: StartIndexAnalyticsRequest) -> None:
    progress = _index_analytics_tasks[task_id]
    try:
        if not await ensure_upstox_session(force_validate=False):
            raise RuntimeError("Upstox is not connected. Reconnect Upstox in Settings before starting the dataset.")
        access_token = get_broker_token("upstox")
        if not access_token:
            raise RuntimeError("Upstox access token is unavailable. Reconnect Upstox in Settings.")

        from_date = (
            date.fromisoformat(req.from_date)
            if req.from_date
            else date.today() - timedelta(days=365)
        )
        to_date = date.fromisoformat(req.to_date) if req.to_date else date.today()
        collector = IndexAnalyticsCollector(access_token=access_token)
        await collector.run(
            underlyings=req.underlyings,
            from_date=from_date,
            to_date=to_date,
            interval=req.interval,
            progress=progress,
        )
    except Exception as exc:
        logger.error(f"Index analytics download task {task_id} failed: {exc}")
        progress.status = "error"
        progress.error = str(exc)
        progress.finished_at = datetime.now(UTC)
        _index_task_file(task_id).parent.mkdir(parents=True, exist_ok=True)
        _index_task_file(task_id).write_text(json.dumps(progress.to_dict(), indent=2))


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/start")
async def start_download(req: StartDownloadRequest, background_tasks: BackgroundTasks):
    """
    Kick off a background F&O data download from Upstox.

    Provide your Upstox Bearer token (without 'Bearer ' prefix) in `upstox_token`.
    Returns a `task_id` to poll for progress.
    """
    if not req.upstox_token:
        raise HTTPException(400, "upstox_token is required — connect Upstox first")

    if not req.underlyings:
        raise HTTPException(400, "At least one underlying required")

    valid_intervals = {"1minute", "30minute", "day", "week", "month"}
    if req.interval not in valid_intervals:
        raise HTTPException(400, f"interval must be one of {valid_intervals}")

    task_id = str(uuid.uuid4())
    progress = DownloadProgress(task_id=task_id, status="pending")
    _tasks[task_id] = progress

    background_tasks.add_task(_run_download, task_id, req)

    return {
        "task_id": task_id,
        "message": f"Download started for {req.underlyings}",
        "poll_url": f"/api/fo-data/status/{task_id}",
    }


@router.post("/index-analytics/start")
async def start_index_analytics_download(req: StartIndexAnalyticsRequest, background_tasks: BackgroundTasks):
    underlyings = [str(value or "").strip().upper() for value in req.underlyings]
    invalid = [value for value in underlyings if value not in SUPPORTED_INDEX_ANALYTICS_UNDERLYINGS]
    if invalid:
        raise HTTPException(400, f"Only {', '.join(SUPPORTED_INDEX_ANALYTICS_UNDERLYINGS)} are supported.")

    if req.interval != INDEX_ANALYTICS_DEFAULT_INTERVAL:
        raise HTTPException(400, "Index analytics dataset currently supports only 1minute candles.")

    task_id = str(uuid.uuid4())
    progress = IndexAnalyticsProgress(
        task_id=task_id,
        underlyings=underlyings or list(SUPPORTED_INDEX_ANALYTICS_UNDERLYINGS),
        interval=req.interval,
        data_root=str(INDEX_ANALYTICS_DATA_ROOT),
    )
    _index_analytics_tasks[task_id] = progress
    background_tasks.add_task(_run_index_analytics_download, task_id, req)

    return {
        "task_id": task_id,
        "message": f"Index analytics dataset started for {progress.underlyings}",
        "poll_url": f"/api/fo-data/index-analytics/status/{task_id}",
        "data_root": str(INDEX_ANALYTICS_DATA_ROOT),
    }


@router.get("/status/{task_id}")
async def get_download_status(task_id: str):
    """Poll download progress by task_id."""
    if task_id not in _tasks:
        raise HTTPException(404, f"Task {task_id} not found")
    return _tasks[task_id].to_dict()


@router.get("/index-analytics/status/{task_id}")
async def get_index_analytics_status(task_id: str):
    progress = _index_analytics_tasks.get(task_id)
    if progress:
        return progress.to_dict()
    payload = _load_index_task_snapshot(task_id)
    if payload is None:
        raise HTTPException(404, f"Task {task_id} not found")
    return payload


@router.get("/tasks")
async def list_tasks():
    """List all download tasks (last 50)."""
    tasks = list(_tasks.values())[-50:]
    return [t.to_dict() for t in reversed(tasks)]


@router.get("/index-analytics/tasks")
async def list_index_analytics_tasks():
    return _list_index_task_snapshots()[:50]


@router.get("/stats")
async def stored_data_stats():
    """Return summary stats of data stored in option_premium_candles."""
    return await get_stored_stats()


@router.get("/index-analytics/stats")
async def index_analytics_stats():
    return load_index_analytics_summary()


@router.get("/instruments")
async def list_instruments(
    underlying: str = "NIFTY",
    max_results: int = 500,
    upstox_token: str = "",
):
    """
    Preview instruments for a given underlying.
    Pass upstox_token to authenticate the instrument master fetch.
    Returns count estimate even without a token (uses cached data if available).
    """
    try:
        downloader = UpstoxFODownloader(access_token=upstox_token)
        instruments = await downloader.get_fo_instruments(underlyings=[underlying])
        total = len(instruments)
        sample = instruments[:max_results]
        return {
            "underlying": underlying,
            "total_instruments": total,
            "sample": [
                {
                    "symbol": i.get("tradingsymbol"),
                    "expiry": i.get("expiry"),
                    "strike": i.get("strike"),
                    "type": i.get("instrument_type"),
                    "lot_size": i.get("lot_size"),
                    "instrument_key": i.get("instrument_key"),
                }
                for i in sample
            ],
        }
    except Exception as exc:
        # Return a helpful message instead of 500 if token is missing
        return {
            "underlying": underlying,
            "total_instruments": -1,
            "error": str(exc),
            "note": "Provide upstox_token query param to fetch instrument list",
        }


@router.delete("/tasks/{task_id}")
async def cancel_task(task_id: str):
    """Remove a completed/failed task from the registry."""
    if task_id in _tasks:
        del _tasks[task_id]
    return {"deleted": task_id}


@router.delete("/index-analytics/tasks/{task_id}")
async def delete_index_analytics_task(task_id: str):
    _index_analytics_tasks.pop(task_id, None)
    path = _index_task_file(task_id)
    if path.exists():
        path.unlink()
    return {"deleted": task_id}
