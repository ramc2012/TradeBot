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


@router.get("/summary")
async def summary() -> dict[str, object]:
    return await asyncio.to_thread(_service.summary)


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
) -> dict[str, object]:
    """Option-chain analytics for the directional engine.

    Returns the same dict the RL policy receives as `chain` context —
    PCR, ATM IV, IV skew (25-delta), DEX, GEX, max-pain, gamma curve,
    top OI strikes, and chain-wide OI build classification. Returns
    `{"available": false}` when no chain is cached for the symbol or
    when the lookup times out (e.g. weekend / broker WS down).
    """
    from directional_options.chain_analytics import fetch_chain_analytics
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
    if not payload:
        return {"available": False, "underlying": underlying, "expiry": expiry}
    return {"available": True, **payload}


@router.get("/gex")
async def gex_analytics(
    underlying: str = Query("NIFTY"),
    expiries: str | None = Query(None, description="comma-separated; default = nearest available"),
    max_expiries: int = Query(3, ge=1, le=6),
) -> dict[str, object]:
    """Black-76 dealer-positioning analytics for the long-premium panel:
    per-expiry GEX-by-strike profile, Net GEX (₹Cr), gamma flip (zero-gamma
    spot), gamma density, DEX, max-pain, call/put walls, IV smile — plus the
    term structure across the nearest expiries. Additive: does not touch the
    legacy /chain-analytics payload the RL policy consumes."""
    from directional_options.gex_service import fetch_gex_analytics

    exp_list: list[str] | None = None
    if expiries:
        exp_list = [e.strip() for e in expiries.split(",") if e.strip()]
    else:
        try:
            from api.routers.market import _local_option_expiries
            from market_data.symbols import to_app_symbol

            app_symbol = to_app_symbol(underlying) or underlying
            exp_list = (await _local_option_expiries(app_symbol))[:max_expiries] or None
        except Exception:  # noqa: BLE001
            exp_list = None
    try:
        return await asyncio.wait_for(
            fetch_gex_analytics(underlying, exp_list, max_expiries=max_expiries),
            timeout=8.0,
        )
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        return {"available": False, "underlying": underlying,
                "expiries": exp_list or [], "per_expiry": [], "term": None}


@router.get("/gex-progression")
async def gex_progression_endpoint(
    underlying: str = Query("NIFTY"),
    expiry: str = Query(...),
    band: int = Query(3, ge=1, le=8),
    interval: str = Query("30minute"),
) -> dict[str, object]:
    """30-minute net-GEX / OI progression (regime-shaded) + strike×time
    gamma-density / OI heatmap matrices for one expiry. Heavy (history fetch);
    bounded and degrades to available=False when ingest is thin."""
    from directional_options.gex_progression import fetch_gex_progression

    try:
        return await asyncio.wait_for(
            fetch_gex_progression(underlying, expiry, band=band, interval=interval),
            timeout=20.0,
        )
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        return {"available": False, "underlying": underlying, "expiry": expiry}


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
    snap["enabled"] = True
    return snap
