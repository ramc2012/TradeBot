"""Commodity strategy routes for Fyers-first MCX paper trading."""
from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from market_data.commodity_atm_watchlist import commodity_atm_watchlist_service
from paper_engine.commodity_strategy_agent import commodity_strategy_agent

router = APIRouter(prefix="/api/commodity", tags=["commodity"])


def _degraded_contract_catalog(detail: str) -> dict[str, object]:
    return {
        "source": "degraded",
        "build_status": "degraded",
        "detail": detail,
        "rows": [],
        "summary": {
            "total_symbols": 0,
            "contracts_ready": 0,
            "active_selections": 0,
        },
    }


def _degraded_atm_watchlist(detail: str) -> dict[str, object]:
    return {
        "source": "degraded",
        "build_status": "degraded",
        "detail": detail,
        "rows": [],
        "summary": {
            "total_rows": 0,
            "ce_ready": 0,
            "pe_ready": 0,
        },
    }


async def _bounded_contract_catalog(
    symbols: list[str],
    selected_option_expiries: dict[str, str],
    selected_option_lookup_symbols: dict[str, str],
    *,
    timeout: float = 4.0,
) -> dict[str, object]:
    try:
        return await asyncio.wait_for(
            commodity_atm_watchlist_service.get_contract_catalog(
                symbols,
                selected_option_expiries,
                selected_option_lookup_symbols,
            ),
            timeout=timeout,
        )
    except Exception as exc:
        return _degraded_contract_catalog(f"Commodity contract catalog refresh timed out or failed: {exc}")


async def _bounded_atm_watchlist(
    symbols: list[str],
    selected_option_expiries: dict[str, str],
    selected_option_lookup_symbols: dict[str, str],
    expiry: Optional[str],
    *,
    timeout: float = 4.0,
) -> dict[str, object]:
    try:
        return await asyncio.wait_for(
            commodity_atm_watchlist_service.get_watchlist(
                symbols,
                selected_option_expiries,
                selected_option_lookup_symbols,
                expiry,
            ),
            timeout=timeout,
        )
    except Exception as exc:
        return _degraded_atm_watchlist(f"Commodity ATM watchlist refresh timed out or failed: {exc}")


class CommodityConfigRequest(BaseModel):
    symbols: list[str]
    selected_option_expiries: dict[str, str] | None = None


class CommodityExpirySelectionRequest(BaseModel):
    selected_option_expiries: dict[str, str]


class KillSwitchRequest(BaseModel):
    active: bool


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
        selected_option_expiries=body.selected_option_expiries,
    )


@router.get("/strategy-agent/contracts")
async def commodity_strategy_contracts():
    await commodity_strategy_agent.ensure_selected_option_setup_locks()
    return await _bounded_contract_catalog(
        commodity_strategy_agent.get_symbols(),
        commodity_strategy_agent.get_selected_option_expiries(),
        commodity_strategy_agent.get_selected_option_lookup_symbols(),
    )


@router.put("/strategy-agent/contracts")
async def update_commodity_strategy_contracts(body: CommodityExpirySelectionRequest):
    return await commodity_strategy_agent.update_selected_option_expiries(body.selected_option_expiries)


@router.get("/kill-switch")
async def commodity_kill_switch_state():
    return commodity_strategy_agent.get_control_state()


@router.put("/kill-switch")
async def update_commodity_kill_switch(body: KillSwitchRequest):
    return await commodity_strategy_agent.set_kill_switch(body.active)


@router.get("/atm-watchlist/expiries")
async def commodity_atm_watchlist_expiries():
    return await commodity_atm_watchlist_service.get_expiries(
        commodity_strategy_agent.get_symbols(),
    )


@router.get("/atm-watchlist")
async def commodity_atm_watchlist(expiry: Optional[str] = Query(None)):
    await commodity_strategy_agent.ensure_selected_option_setup_locks()
    return await _bounded_atm_watchlist(
        commodity_strategy_agent.get_symbols(),
        commodity_strategy_agent.get_selected_option_expiries(),
        commodity_strategy_agent.get_selected_option_lookup_symbols(),
        expiry,
    )


@router.get("/watchlist-snapshot")
async def commodity_watchlist_snapshot(
    expiry: Optional[str] = Query(None),
    live_refresh: bool = Query(False),
):
    await commodity_strategy_agent.ensure_selected_option_setup_locks()
    symbols = commodity_strategy_agent.get_symbols()
    selected_option_expiries = commodity_strategy_agent.get_selected_option_expiries()
    selected_option_lookup_symbols = commodity_strategy_agent.get_selected_option_lookup_symbols()
    contract_catalog = None if live_refresh else commodity_atm_watchlist_service.get_cached_contract_catalog(
        symbols,
        selected_option_expiries,
        selected_option_lookup_symbols,
    )
    atm_watchlist = None if live_refresh else commodity_atm_watchlist_service.get_cached_watchlist(
        symbols,
        selected_option_expiries,
        selected_option_lookup_symbols,
        expiry,
    )
    return {
        "contract_catalog": contract_catalog or await _bounded_contract_catalog(
            symbols,
            selected_option_expiries,
            selected_option_lookup_symbols,
        ),
        "atm_watchlist": atm_watchlist or await _bounded_atm_watchlist(
            symbols,
            selected_option_expiries,
            selected_option_lookup_symbols,
            expiry,
        ),
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
