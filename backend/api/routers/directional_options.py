"""API surface for the directional long-options module."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from directional_options.service import directional_options_service


router = APIRouter(prefix="/api/directional-options", tags=["directional-options"])
_service = directional_options_service


class DirectionalResetRequest(BaseModel):
    confirm: str = Field(..., description="Must equal 'RESET' to proceed (destructive).")
    actor: str | None = None


class DirectionalPositionCloseRequest(BaseModel):
    position_id: str = Field(..., description="Open directional paper position id to close.")
    premium: float | None = Field(None, gt=0, description="Optional operator mark. Uses live/stored mark when omitted.")
    spot: float | None = Field(None, gt=0, description="Optional underlying spot mark.")
    reason: str = Field("operator_close", description="Close reason recorded in the paper journal.")
    actor: str | None = None


@router.get("/summary")
async def summary() -> dict[str, object]:
    return await asyncio.to_thread(_service.summary)


@router.get("/universe")
async def universe() -> dict[str, object]:
    return await _service.universe()


@router.get("/workspace")
async def workspace(
    underlying: str = Query("NIFTY"),
    timeframe: str = Query("5minute"),
    lookback_sessions: int = Query(16, ge=4, le=90),
) -> dict[str, object]:
    return await asyncio.to_thread(_service.workspace, underlying, timeframe, lookback_sessions)


@router.get("/live-snapshot")
async def live_snapshot(
    underlying: str = Query("NIFTY"),
    timeframe: str = Query("5minute"),
    lookback_sessions: int = Query(16, ge=4, le=90),
) -> dict[str, object]:
    return await _service.live_snapshot(underlying, timeframe, lookback_sessions)


@router.post("/paper-proposal")
async def paper_proposal(
    underlying: str = Query("NIFTY"),
    timeframe: str = Query("5minute"),
    lookback_sessions: int = Query(16, ge=4, le=90),
) -> dict[str, object]:
    return await _service.record_paper_snapshot(underlying, timeframe, lookback_sessions)


@router.get("/paper-journal")
async def paper_journal(
    symbol: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, object]:
    return await _service.paper_journal(symbol=symbol, limit=limit)


@router.get("/paper-positions")
async def paper_positions(
    symbol: str | None = Query(None),
    status: str = Query("all"),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, object]:
    return await _service.paper_positions(symbol=symbol, status=status, limit=limit)


@router.get("/paper-summary")
async def paper_summary() -> dict[str, object]:
    """Capital + P&L snapshot for the portfolio panel."""
    return await _service.paper_summary()


@router.post("/paper-positions/close")
async def close_paper_position(body: DirectionalPositionCloseRequest) -> dict[str, object]:
    try:
        return await _service.close_paper_position(
            body.position_id,
            premium=body.premium,
            spot=body.spot,
            reason=body.reason,
            actor=body.actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/reset-paper")
async def reset_paper_account(body: DirectionalResetRequest) -> dict[str, object]:
    if (body.confirm or "").strip().upper() != "RESET":
        raise HTTPException(
            status_code=400,
            detail=(
                "Paper reset is destructive. POST `{\"confirm\": \"RESET\"}` "
                "to confirm."
            ),
        )
    return await _service.reset_paper_account(actor=body.actor)


@router.get("/backtest")
async def backtest(
    underlying: str = Query("NIFTY"),
    timeframe: str = Query("5minute"),
    lookback_sessions: int = Query(16, ge=4, le=90),
) -> dict[str, object]:
    payload = await asyncio.to_thread(_service.workspace, underlying, timeframe, lookback_sessions)
    return payload["backtest"]


@router.get("/chain-analytics")
async def chain_analytics(
    underlying: str = Query("NIFTY"),
    expiry: str | None = Query(None),
    refresh: bool = Query(False),
) -> dict[str, object]:
    """Option-chain analytics for the directional engine.

    Returns the same dict the RL policy receives as `chain` context —
    PCR, ATM IV, IV skew (25-delta), DEX, GEX, max-pain, gamma curve,
    top OI strikes, and chain-wide OI build classification. Returns
    `{"available": false}` when no chain is cached for the symbol or
    when the lookup times out (e.g. weekend / broker WS down).
    """
    from directional_options.chain_analytics import chain_cache_status, fetch_chain_analytics, warm_chain_cache
    try:
        # Outer wait_for is a safety belt — fetch_chain_analytics already
        # bounds its Redis call with its own 2s timeout, but a 5s wall
        # guarantees this endpoint never hangs the API server even if
        # the inner timeout were ever bypassed.
        payload = await asyncio.wait_for(
            fetch_chain_analytics(underlying, expiry=expiry),
            timeout=5.0,
        )
    except (asyncio.TimeoutError, Exception):
        payload = None
    refresh_status = None
    if not payload and refresh:
        try:
            refresh_status = await asyncio.wait_for(
                warm_chain_cache(underlying, expiry=expiry),
                timeout=12.0,
            )
            payload = await asyncio.wait_for(
                fetch_chain_analytics(underlying, expiry=expiry),
                timeout=5.0,
            )
        except (asyncio.TimeoutError, Exception) as exc:
            refresh_status = {"warmed": False, "reason": str(exc)}
    if not payload:
        status = await chain_cache_status(underlying, expiry)
        return {
            "available": False,
            "underlying": underlying,
            "expiry": expiry,
            "cache_status": status,
            "refresh_status": refresh_status,
        }
    return {"available": True, **payload, "refresh_status": refresh_status}


@router.get("/policy")
async def policy_state() -> dict[str, object]:
    """Global RL policy state — n_seen, per-size-bucket Mean R, pending positions.

    Surfaced so the UI's "Policy & Learning" tab can render the
    posterior's training progress and size-bucket convergence
    without having to call live-snapshot per underlying.
    """
    if _service.policy is None:
        return {
            "enabled": False,
            "reason": "RL policy disabled in config.",
        }
    snap = _service.policy.snapshot()
    risk_cfg = _service.config.get("risk", {}) or {}
    paper_cfg = _service.config.get("paper_trading", {}) or {}
    size_buckets = snap.get("size_buckets") or {}
    bucket_rows: list[dict[str, object]] = []
    total_bucket_trades = 0
    weighted_r = 0.0
    for multiplier, bucket in size_buckets.items():
        if not isinstance(bucket, dict):
            continue
        n = int(bucket.get("n") or 0)
        mean_r = bucket.get("mean_R")
        total_bucket_trades += n
        if mean_r is not None:
            weighted_r += float(mean_r) * n
        bucket_rows.append(
            {
                "multiplier": multiplier,
                "n": n,
                "mean_R": mean_r,
            }
        )
    trained_rows = [row for row in bucket_rows if int(row["n"] or 0) > 0 and row.get("mean_R") is not None]
    best_bucket = max(trained_rows, key=lambda row: float(row.get("mean_R") or 0.0), default=None)
    worst_bucket = min(trained_rows, key=lambda row: float(row.get("mean_R") or 0.0), default=None)
    # Mirror the strategy knobs the UI needs to render — these are the
    # things the user (and the agent) need to see at a glance.
    snap["strategy_params"] = {
        "universe": list(_service.config.get("universe") or []),
        "risk_pct": risk_cfg.get("risk_pct"),
        "premium_cap_pct": risk_cfg.get("premium_cap_pct"),
        "planned_stop_pct": risk_cfg.get("planned_stop_pct"),
        "profit_target_pct": risk_cfg.get("profit_target_pct"),
        "trail_giveback_pct": risk_cfg.get("trail_giveback_pct"),
        "daily_loss_cap_r": risk_cfg.get("daily_loss_cap_r"),
        "weekly_loss_cap_r": risk_cfg.get("weekly_loss_cap_r"),
        "starting_equity": risk_cfg.get("starting_equity"),
        "min_hold_bars": paper_cfg.get("min_hold_bars"),
        "one_position_per_symbol": paper_cfg.get("one_position_per_symbol"),
    }
    snap["learning_summary"] = {
        "bucket_trades": total_bucket_trades,
        "mean_R": (weighted_r / total_bucket_trades) if total_bucket_trades else None,
        "best_multiplier": best_bucket.get("multiplier") if best_bucket else None,
        "best_mean_R": best_bucket.get("mean_R") if best_bucket else None,
        "worst_multiplier": worst_bucket.get("multiplier") if worst_bucket else None,
        "worst_mean_R": worst_bucket.get("mean_R") if worst_bucket else None,
        "pending_rewards": len(snap.get("pending_positions") or []),
        "untrained_buckets": sum(1 for row in bucket_rows if int(row["n"] or 0) == 0),
    }
    snap["paper"] = await _service.paper_summary()
    snap["enabled"] = True
    return snap
