"""LANESET boot-plane helpers — Phase-1 process split (2026-07-18).

One image, multiple boot modes. Two processes share ONE Postgres + ONE Redis +
ONE broker layer + ONE Fyers WS (owned by the core plane):

    LANESET=all         (default) byte-identical single-process boot.
    LANESET=core        market-data ingest + API + health + daemons.
    LANESET=strategies  supervisor strategy runners + S1/commodity own-loop
                        agents; broker WS is gated OFF (Redis/PG reads only).

Everything here is pure and side-effect-free except one CRITICAL log when an
unknown LANESET value is coerced to "all" (fail-safe: a typo boots the full
single process rather than silently darking a plane).

``planned_subsystems`` is the declarative inventory the no-op proof tests pin:
planned("all") == planned("core") | planned("strategies"), and the core∩strategies
overlap is exactly the deliberately-shared subsystems.
"""
from __future__ import annotations

from loguru import logger

from core.config import settings

VALID_LANESETS = frozenset({"all", "core", "strategies"})

_warned_invalid: set[str] = set()


def normalized_laneset() -> str:
    """The effective laneset for this process; unknown values coerce to "all"."""
    raw = str(getattr(settings, "LANESET", "all") or "all").strip().lower()
    if raw in VALID_LANESETS:
        return raw
    if raw not in _warned_invalid:
        _warned_invalid.add(raw)
        logger.critical(
            f"LANESET={raw!r} is not one of {sorted(VALID_LANESETS)} — "
            "failing SAFE to 'all' (full single-process boot)."
        )
    return "all"


def boots_core() -> bool:
    """True when this process runs the data/API plane (all or core)."""
    return normalized_laneset() in ("all", "core")


def boots_strategies() -> bool:
    """True when this process runs the strategy plane (all or strategies)."""
    return normalized_laneset() in ("all", "strategies")


def is_split() -> bool:
    """True only when this process is one half of a split boot."""
    return normalized_laneset() != "all"


def is_core_only() -> bool:
    """True only for LANESET=core (the plane WITHOUT the strategy loops)."""
    return normalized_laneset() == "core"


# ── Declarative subsystem inventory (main.py lifespan, supervisor runners) ──
# Shared: started in BOTH planes.
SHARED_SUBSYSTEMS = frozenset(
    {
        "security_guardrails",
        "event_loop_lag_monitor",
        "redis_ping",
        "credential_restore",  # auto_restore_sessions; its WS resync no-ops via the data_router gate
        "market_hours_paper_supervisor",  # runs in both planes with disjoint runner sets
        "api_routers_and_websockets",  # both processes serve them; only core's port is published
    }
)

# Core plane only: everything needing the in-process WS callback stream, the
# broker WS itself, and the data-maintenance daemons.
CORE_SUBSYSTEMS = frozenset(
    {
        "market_profile_builder_tick_wiring",
        "live_candle_store",
        "quote_bus",
        "broker_ws_subscribe",  # data_router set_broker/subscribe + sector/stock/directional subs
        "option_ws_subscription_manager",
        "held_position_subscription_refresh",
        "commodity_mark_refresh",
        "embedded_research_sync",
        "macd_diffusion_daemon",
        "greeks_enrichment_daemon",
        "chain_candle_builder",
        # Supervisor runners tagged plane="core":
        "runner:option_flow_watchdog",
        "runner:token_readiness",
        "runner:market_intelligence",
        "runner:stock_spot_sweep",
        "runner:macd_preopen_watchlist",
    }
)

# Strategy plane only: the own-loop agents + RL trainer + strategy runners.
STRATEGY_SUBSYSTEMS = frozenset(
    {
        "paper_strategy_agent",
        "commodity_strategy_agent",
        "paper_bootstrap",
        "rl_qtable_cache",
        "rl_auto_trainer",
        # Supervisor runners tagged plane="strategies":
        "runner:auction_intelligence",
        "runner:auction_intelligence_commodity",
        "runner:institutional_convergence",
        "runner:institutional_convergence_commodity",
        "runner:fractal_market_profile",
        "runner:directional_options",
        "runner:directional_positioning",
        "runner:commodity_mp_history",
        "runner:macd_refined",
        "runner:macd_refined_marks",
        "runner:cbe_scanner",
        "runner:cbe_marks",
        "runner:gann_tp_delta",
        "runner:lane_audit",
    }
)


def planned_subsystems(laneset: str) -> frozenset[str]:
    """Which subsystems a process with this LANESET boots (pure function)."""
    ls = str(laneset or "all").strip().lower()
    if ls not in VALID_LANESETS:
        ls = "all"
    if ls == "core":
        return SHARED_SUBSYSTEMS | CORE_SUBSYSTEMS
    if ls == "strategies":
        return SHARED_SUBSYSTEMS | STRATEGY_SUBSYSTEMS
    return SHARED_SUBSYSTEMS | CORE_SUBSYSTEMS | STRATEGY_SUBSYSTEMS


def require_strategy_plane(action: str) -> None:
    """Guard strategy-mutating API paths that act on IN-PROCESS agent state.

    On LANESET=core those endpoints would act on the WRONG process (the agents
    live in backend-strategies), so return 409 with a pointer instead of a
    silent misfire. No-op for LANESET=all/strategies — strictly inert in the
    default single-process boot.
    """
    if not is_core_only():
        return
    from fastapi import HTTPException  # local import: keep this module framework-light

    raise HTTPException(
        status_code=409,
        detail=(
            f"{action} runs in-process on the strategy plane and this process is "
            "LANESET=core. Use the backend-strategies service (its internal :8000), "
            "or the shared control endpoints (kill-switch / auto-run), which "
            "propagate via the shared state store."
        ),
    )
