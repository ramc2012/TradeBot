"""
F&O Historical Data Download API
=================================
Endpoints to trigger, monitor, and query Upstox NSE F&O options data downloads.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel
from loguru import logger

from data.upstox_downloader import UpstoxFODownloader, DownloadProgress, get_stored_stats

router = APIRouter(prefix="/api/fo-data", tags=["fo-data"])

# ── In-memory task registry ────────────────────────────────────────────────────
# Maps task_id → DownloadProgress
_tasks: dict[str, DownloadProgress] = {}


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


@router.get("/status/{task_id}")
async def get_download_status(task_id: str):
    """Poll download progress by task_id."""
    if task_id not in _tasks:
        raise HTTPException(404, f"Task {task_id} not found")
    return _tasks[task_id].to_dict()


@router.get("/tasks")
async def list_tasks():
    """List all download tasks (last 50)."""
    tasks = list(_tasks.values())[-50:]
    return [t.to_dict() for t in reversed(tasks)]


@router.get("/stats")
async def stored_data_stats():
    """Return summary stats of data stored in option_premium_candles."""
    return await get_stored_stats()


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
