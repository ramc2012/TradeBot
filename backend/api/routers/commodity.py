"""Commodity strategy routes — MP+OF futures-only sleeve.

The options sleeve and ATM-watchlist service have been deprecated; the
endpoints that backed them (`/atm-watchlist`, `/atm-watchlist/expiries`,
`PUT /strategy-agent/contracts`) are gone.

`/strategy-agent/contracts` and `/watchlist-snapshot` are retained as thin
catalog endpoints driven entirely from `COMMODITY_CONTRACT_SPECS`, so the
frontend's contract table keeps working without any expiry-discovery
backend behind it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from market_data.commodity_contract_specs import get_commodity_contract_spec
from paper_engine.commodity_strategy_agent import commodity_strategy_agent

router = APIRouter(prefix="/api/commodity", tags=["commodity"])


def _normalized_symbols(symbols: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_symbol in symbols:
        symbol = str(raw_symbol or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        normalized.append(symbol)
    return normalized


def _build_contract_catalog(symbols: list[str]) -> dict[str, object]:
    """Build the slim, options-free contract catalog the UI consumes.

    No broker calls — everything is sourced from the static specs.
    """
    contracts: list[dict[str, object]] = []
    for symbol in _normalized_symbols(symbols):
        spec = get_commodity_contract_spec(symbol)
        contracts.append(
            {
                "symbol": symbol,
                "underlying": spec.root,
                "display_name": spec.display_name,
                "lookup_symbol": symbol,
                "active_lookup_symbol": symbol,
                "default_lookup_symbol": symbol,
                "lot_size": spec.futures_lot_size,
                "tick_size": spec.mp_tick_size,
                "contract_unit_label": spec.contract_unit_label,
                "quote_unit_label": spec.quote_unit_label,
                "strategy_title": spec.futures_label,
                "has_options": False,
                "selection_policy": "futures_only",
                "selection_locked": True,
                "detail": "MP+OF futures-only sleeve; options were deprecated.",
            }
        )
    return {
        "source": "static_specs",
        "build_status": "ready",
        "detail": "MP+OF futures-only catalog (static specs; no broker discovery).",
        "contracts": contracts,
        "rows": contracts,
        "summary": {
            "total_symbols": len(contracts),
            "contracts_ready": len(contracts),
            "active_selections": len(contracts),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


class CommodityConfigRequest(BaseModel):
    symbols: list[str]
    # Legacy field accepted but ignored — options sleeve is deprecated.
    selected_option_expiries: dict[str, str] | None = None


class KillSwitchRequest(BaseModel):
    active: bool


class ResetPaperRequest(BaseModel):
    confirm: str
    actor: Optional[str] = None


@router.post("/strategy-agent/reset-paper")
async def reset_commodity_paper_account(body: ResetPaperRequest):
    if (body.confirm or "").strip().upper() != "RESET":
        raise HTTPException(
            status_code=400,
            detail=(
                "Paper reset is destructive. POST `{\"confirm\": \"RESET\"}` "
                "in the body to proceed."
            ),
        )
    actor = (body.actor or "manual").strip() or "manual"
    return await commodity_strategy_agent.archive_and_reset_paper_account(actor=actor)


@router.get("/strategy-agent/status")
async def commodity_strategy_status():
    return commodity_strategy_agent.get_status()


@router.get("/overview")
async def commodity_overview():
    return {
        "status": commodity_strategy_agent.get_status(),
        "kill_switch_state": commodity_strategy_agent.get_control_state(),
        "orders": commodity_strategy_agent.get_orders()[:40],
        "positions": commodity_strategy_agent.get_positions(),
        "reports": commodity_strategy_agent.get_reports()[:24],
    }


@router.post("/strategy-agent/start")
async def start_commodity_strategy_agent():
    return await commodity_strategy_agent.start_loop()


@router.post("/strategy-agent/run-once")
async def run_commodity_strategy_once(force: bool = True):
    return await commodity_strategy_agent.run_once(force=force)


@router.put("/strategy-agent/config")
async def update_commodity_strategy_config(body: CommodityConfigRequest):
    return commodity_strategy_agent.update_symbols(
        body.symbols,
        selected_option_expiries=body.selected_option_expiries,  # ignored
    )


@router.get("/strategy-agent/contracts")
async def commodity_strategy_contracts():
    symbols = commodity_strategy_agent.get_symbols()
    return _build_contract_catalog(symbols)


@router.get("/kill-switch")
async def commodity_kill_switch_state():
    return commodity_strategy_agent.get_control_state()


@router.put("/kill-switch")
async def update_commodity_kill_switch(body: KillSwitchRequest):
    return await commodity_strategy_agent.set_kill_switch(body.active)


@router.get("/watchlist-snapshot")
async def commodity_watchlist_snapshot(
    expiry: Optional[str] = Query(None),  # legacy arg, ignored
    live_refresh: bool = Query(False),  # legacy arg, ignored
):
    symbols = commodity_strategy_agent.get_symbols()
    return {
        "contract_catalog": _build_contract_catalog(symbols),
        # Field retained for frontend backwards-compat; always empty.
        "atm_watchlist": {
            "rows": [],
            "source": "deprecated",
            "detail": "Commodity options sleeve removed; this field is intentionally empty.",
            "summary": {"total_rows": 0, "ce_ready": 0, "pe_ready": 0},
        },
    }


@router.get("/orders")
async def commodity_orders(limit: Optional[int] = None):
    orders = commodity_strategy_agent.get_orders()
    if limit is not None and limit >= 0:
        return orders[:limit]
    return orders


@router.get("/positions")
async def commodity_positions():
    return commodity_strategy_agent.get_positions()


@router.get("/reports")
async def commodity_reports(limit: Optional[int] = None):
    reports = commodity_strategy_agent.get_reports()
    if limit is not None and limit >= 0:
        return reports[:limit]
    return reports
