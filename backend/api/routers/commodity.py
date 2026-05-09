"""Commodity strategy routes for Fyers-first MCX paper trading."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from market_data.commodity_contract_specs import get_commodity_contract_spec
from market_data.commodity_atm_watchlist import commodity_atm_watchlist_service
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


def _degraded_contract_catalog(
    detail: str,
    *,
    symbols: Optional[list[str]] = None,
    selected_option_expiries: Optional[dict[str, str]] = None,
    selected_option_lookup_symbols: Optional[dict[str, str]] = None,
) -> dict[str, object]:
    selected_expiries = {
        str(symbol).strip().upper(): str(expiry).strip()
        for symbol, expiry in dict(selected_option_expiries or {}).items()
        if str(symbol).strip() and str(expiry).strip()
    }
    selected_lookup_symbols = {
        str(symbol).strip().upper(): str(lookup_symbol).strip().upper()
        for symbol, lookup_symbol in dict(selected_option_lookup_symbols or {}).items()
        if str(symbol).strip() and str(lookup_symbol).strip()
    }
    contracts: list[dict[str, object]] = []
    for symbol in _normalized_symbols(symbols or []):
        spec = get_commodity_contract_spec(symbol)
        selected_expiry = selected_expiries.get(symbol)
        selected_lookup_symbol = selected_lookup_symbols.get(symbol) or symbol
        expiry_mappings = (
            [{"expiry": selected_expiry, "lookup_symbol": selected_lookup_symbol}]
            if selected_expiry
            else []
        )
        contracts.append(
            {
                "symbol": symbol,
                "underlying": spec.root,
                "lookup_symbol": symbol,
                "expiries": [selected_expiry] if selected_expiry else [],
                "selected_expiry": selected_expiry,
                "suggested_expiry": selected_expiry,
                "active_expiry": selected_expiry,
                "has_options": bool(selected_expiry),
                "active_lookup_symbol": selected_lookup_symbol,
                "default_lookup_symbol": symbol,
                "expiry_mappings": expiry_mappings,
                "selected_lookup_symbol": selected_lookup_symbol if selected_expiry else None,
                "selection_policy": "saved_static_fallback" if selected_expiry else "static_metadata_only",
                "selection_locked": bool(selected_expiry),
                "lot_size": spec.futures_lot_size,
                "contract_unit_label": spec.contract_unit_label,
                "quote_unit_label": spec.quote_unit_label,
                "strategy_title": spec.options_label,
                "detail": "Live MCX expiry discovery is unavailable; showing saved/static contract metadata.",
            }
        )
    fallback_detail = detail
    if contracts:
        fallback_detail = (
            f"{detail} Showing saved MCX futures with static contract metadata; "
            "live expiry discovery will retry on the next refresh."
        )
    return {
        "source": "degraded_static",
        "build_status": "degraded",
        "detail": fallback_detail,
        "contracts": contracts,
        "rows": contracts,
        "summary": {
            "total_symbols": len(contracts),
            "contracts_ready": sum(1 for item in contracts if item.get("has_options")),
            "active_selections": sum(1 for item in contracts if item.get("active_expiry")),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
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
        payload = await asyncio.wait_for(
            commodity_atm_watchlist_service.get_contract_catalog(
                symbols,
                selected_option_expiries,
                selected_option_lookup_symbols,
            ),
            timeout=timeout,
        )
        if symbols and not list(payload.get("contracts") or []):
            return _degraded_contract_catalog(
                str(payload.get("detail") or "Commodity contract catalog is unavailable."),
                symbols=symbols,
                selected_option_expiries=selected_option_expiries,
                selected_option_lookup_symbols=selected_option_lookup_symbols,
            )
        return payload
    except Exception as exc:
        return _degraded_contract_catalog(
            f"Commodity contract catalog refresh timed out or failed: {exc}",
            symbols=symbols,
            selected_option_expiries=selected_option_expiries,
            selected_option_lookup_symbols=selected_option_lookup_symbols,
        )


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
