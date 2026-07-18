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


def _prefer(order: list[str], broker: str) -> list[str]:
    """Stable move-to-front: hoist ``broker`` to the head if present, keeping the
    relative order of every other source. Reorder ONLY — never drops a source, so
    failover is preserved and a broker not in the list leaves ``order`` unchanged.
    """
    if broker not in order:
        return list(order)
    return [broker] + [source for source in order if source != broker]


# Only DECISION-data purposes are cadence-rerouted. The REAL-TIME plane
# (live_ticks / market_profile / order_flow — held-position marks, quote-bus
# tape, tick MP/OF construction) stays on the GLOBAL order for EVERY lane,
# including slow ones (owner refinement 2026-07-17: "market watch, position
# updates need to be in seconds" → always Fyers-WS, never rerouted to Upstox
# REST). Scoping the reorder here makes that guarantee STRUCTURAL rather than
# incidental — a slow lane can never drag its second-level marks onto Upstox.
_DECISION_PURPOSES: frozenset[str] = frozenset({"option_chain", "historical"})


def _profile_reorder(order: list[str], normalized: str) -> list[str]:
    """Reorder the failover list by the active lane broker profile (SLOW →
    upstox-first, FAST → fyers-first), but ONLY for decision-data purposes
    (option_chain / historical). The real-time plane (live_ticks / market_profile
    / order_flow) is never rerouted — see _DECISION_PURPOSES. Gated by
    LANE_BROKER_ROUTING_ENABLED: flag-off returns ``order`` unchanged (provable
    no-op regardless of profile). Fail-safe: any error → the unmodified order."""
    try:
        if normalized not in _DECISION_PURPOSES:
            return order
        if not bool(getattr(settings, "LANE_BROKER_ROUTING_ENABLED", False)):
            return order
        from brokers.rate_limiter import (
            current_lane_profile,
            LANE_PROFILE_SLOW,
            LANE_PROFILE_FAST,
        )

        prof = current_lane_profile()
        if prof == LANE_PROFILE_SLOW:
            return _prefer(order, "upstox")
        if prof == LANE_PROFILE_FAST:
            return _prefer(order, "fyers")
    except Exception:  # noqa: BLE001
        pass
    return order


def route_order(purpose: str) -> list[str]:
    normalized = str(purpose or "").strip().lower()
    setting_name = _PURPOSE_SETTING.get(normalized, "")
    value = getattr(settings, setting_name, "") if setting_name else ""
    order = _parse_order(value)
    # (1) Per-lane cadence routing FIRST: SLOW lanes prefer Upstox, FAST lanes
    # prefer Fyers (owner directive 2026-07-17). Reorder only; flag-off no-op.
    # Scoped to DECISION purposes (option_chain / historical) — the real-time
    # plane (live_ticks / market_profile / order_flow) is passed through
    # unchanged so slow-lane marks/quote-tape stay Fyers-WS at seconds cadence.
    order = _profile_reorder(order, normalized)
    # (2) Circuit-aware failover SECOND: when a broker's chain REST is circuit-
    # OPEN (sustained 429s/errors), prefer the healthy broker. Applied HERE (not
    # just in choose_active_adapter, which is only called for live_ticks) so the
    # real option-chain consumers that iterate route_order() directly — the
    # market router's chain endpoint — actually fail over. Reorder only; never
    # drops a source, and a fully-healthy circuit leaves the order unchanged.
    # ORDER MATTERS: circuit runs AFTER the profile so an OPEN preferred broker
    # (e.g. a slow lane pinned to Upstox while Upstox is circuit-OPEN) still
    # fails over to the healthy broker — the circuit wins over the profile. The
    # circuit datatype is cadence-scoped (fast_chain / slow_chain) so a Fyers
    # degradation trips only the fast group and an Upstox degradation only the
    # slow group (structural isolation) — matching the record site in
    # brokers/{fyers,upstox}.py.
    if normalized == "option_chain":
        try:
            from market_data.broker_circuit import broker_circuit, cadence_datatype
            order = broker_circuit.preferred_order(order, cadence_datatype("chain"))
        except Exception:  # noqa: BLE001
            pass
    return order


def choose_active_adapter(
    purpose: str,
    adapters: Mapping[str, Any],
) -> tuple[Any | None, str | None, list[dict[str, Any]]]:
    decisions: list[dict[str, Any]] = []
    # route_order() already applies the circuit-aware failover reorder for
    # option_chain, so callers here inherit it automatically.
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


def ordered_live_adapters(
    purpose: str,
    adapters: Mapping[str, Any],
) -> list[tuple[str, Any]]:
    """Resolve ``route_order(purpose)`` into an ordered ``(source, adapter)`` list
    limited to the two LIVE brokers (fyers/upstox) that actually have a connected
    adapter, dropping metadata/offline sources.

    This is the single seam that lets a shared chain WRITER honour the active lane
    broker profile without threading any signature: because ``route_order`` applies
    the (flag-gated) cadence reorder + circuit failover, a writer that iterates
    THIS instead of a hardcoded ``(upstox, fyers)`` tuple inherits SLOW→upstox /
    FAST→fyers automatically, and falls back to a circuit-healthy broker on an
    OPEN preferred one. Flag-off ⇒ ``route_order`` returns the global order, so the
    iteration is byte-identical to the pre-routing literal for callers whose
    literal already matched the configured order (e.g. option_chain = upstox,fyers).
    """
    ordered: list[tuple[str, Any]] = []
    for source in route_order(purpose):
        if source not in {"fyers", "upstox"}:
            continue
        adapter = adapters.get(source)
        if adapter is not None:
            ordered.append((source, adapter))
    return ordered


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
