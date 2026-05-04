"""Market-data source routing policy."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.config import settings


_CAPABILITIES: dict[str, set[str]] = {
    "postgres": {
        "analytics",
        "durable_history",
        "historical_candles",
        "market_intelligence_cache",
    },
    "fyers": {
        "historical_candles",
        "live_ticks",
        "ltp",
        "market_profile",
        "option_chain",
        "order_flow",
    },
    "upstox": {
        "contract_metadata",
        "historical_candles",
        "live_ticks",
        "ltp",
        "option_chain",
    },
    "upstox_analytics": {
        "analytics",
        "backfill",
        "contract_metadata",
        "historical_candles",
    },
    "catalog": {
        "contract_metadata",
        "expiry_metadata",
        "offline_fallback",
    },
}

_QUALITY_NOTES: dict[str, str] = {
    "postgres": "Durable first-read store for candles, contracts, snapshots, sector state and Market Intelligence context.",
    "fyers": "Preferred real-time tick source for market profile and order-flow style modules when connected.",
    "upstox": "Fallback live quote/chain source and regular broker historical source when connected.",
    "upstox_analytics": "Preferred research/backfill token source for historical candles and contract metadata.",
    "catalog": "Offline metadata fallback; not a live-price source.",
}

_PURPOSE_SETTING: dict[str, str] = {
    "live_ticks": "MARKET_DATA_LIVE_TICK_ORDER",
    "market_profile": "MARKET_DATA_MARKET_PROFILE_ORDER",
    "order_flow": "MARKET_DATA_ORDER_FLOW_ORDER",
    "option_chain": "MARKET_DATA_OPTION_CHAIN_ORDER",
    "historical": "MARKET_DATA_HISTORICAL_ORDER",
    "analytics": "MARKET_DATA_ANALYTICS_ORDER",
}


def _parse_order(raw: str | None) -> list[str]:
    seen: set[str] = set()
    order: list[str] = []
    for part in str(raw or "").split(","):
        source = part.strip().lower()
        if not source or source in seen:
            continue
        seen.add(source)
        order.append(source)
    return order


def route_order(purpose: str) -> list[str]:
    setting_name = _PURPOSE_SETTING.get(str(purpose or "").strip().lower(), "")
    value = getattr(settings, setting_name, "") if setting_name else ""
    return _parse_order(value)


def choose_active_adapter(
    purpose: str,
    adapters: Mapping[str, Any],
) -> tuple[Any | None, str | None, list[dict[str, Any]]]:
    decisions: list[dict[str, Any]] = []
    for source in route_order(purpose):
        adapter = adapters.get(source)
        if source in {"postgres", "upstox_analytics", "catalog"}:
            decisions.append({"source": source, "selected": False, "reason": "not_a_live_adapter"})
            continue
        if adapter is not None:
            decisions.append({"source": source, "selected": True, "reason": "active_adapter"})
            return adapter, source, decisions
        decisions.append({"source": source, "selected": False, "reason": "adapter_not_connected"})
    return None, None, decisions


def source_policy_snapshot(
    *,
    active_brokers: list[str] | None = None,
    selected_live_source: str | None = None,
    route_decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    active = {str(item).lower() for item in (active_brokers or []) if item}
    routes: dict[str, dict[str, Any]] = {}
    for purpose in _PURPOSE_SETTING:
        order = route_order(purpose)
        active_live = [source for source in order if source in {"fyers", "upstox"} and source in active]
        if purpose in {"historical", "analytics"}:
            selected = order[0] if order else None
        elif purpose == "live_ticks":
            selected = selected_live_source
        else:
            selected = active_live[0] if active_live else (order[0] if order else None)
        routes[purpose] = {
            "order": order,
            "active_live_sources": active_live,
            "selected": selected,
        }
    return {
        "version": "2026-05-03.source-policy.v1",
        "routes": routes,
        "capabilities": {source: sorted(values) for source, values in _CAPABILITIES.items()},
        "quality_notes": dict(_QUALITY_NOTES),
        "durable_storage": {
            "enabled": True,
            "first_read_source": "postgres",
            "history_behavior": "append_or_upsert_gap_fill",
            "market_intelligence_scope": [
                "underlying_spot_candles",
                "option_premium_candles",
                "fo_contract_catalog",
                "fo_underlying_catalog",
                "atm_option_watchlist_snapshots",
                "sector_interaction_state",
                "macro_research_state",
            ],
            "note": "Broker/API calls should fill missing gaps; existing candle and catalog history is not intentionally repopulated every run.",
        },
        "route_decisions": list(route_decisions or []),
    }
