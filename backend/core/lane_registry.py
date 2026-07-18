"""Single source of truth for every lane / runner / product surface (Task C, 2026-07-18).

The system previously had FIVE different lane enumerations that counted
different things (frontend overview ~6 lanes, /api/system/health ~2 strategy
lanes, automation-status ~17 supervisor runners, signal-validation ~20,
audits.lanes 1). This module is the ONE declarative registry: every supervisor
runner, every own-loop agent/daemon started in main.py's lifespan, and every
parked product lane, each entry verified against the code that starts it.

Additive only — /api/system/lanes serves LaneSnapshots from here; the existing
health/overview endpoints keep their shapes (consolidation is Phase 2, owner
sign-off required).

Design rules:
  * Registry entries are DECLARATIVE (what the lane is); the snapshot
    assembler gathers CURRENT state (what the lane is doing) resiliently —
    a lane whose status source raises reports status="unknown" with the
    error string, it never breaks the endpoint.
  * ``audit_coverage`` is True only for lanes present in
    ``audits.lanes.REGISTRY`` (s1 only today) — the visible gap is the point.
  * Heavy imports (agents, services) happen lazily inside the status
    sources, never at module import.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from core.config import settings

LaneKind = Literal["strategy-engine", "scheduler-runner", "product-lane", "monitor"]
ExecutionMode = Literal["paper", "live", "parked", "none"]

# Per-source hard timeout so one hung status provider cannot stall the
# whole /api/system/lanes assembly.
_SOURCE_TIMEOUT_SECONDS = 4.0


@dataclass(frozen=True)
class LaneSpec:
    key: str
    label: str
    kind: LaneKind
    execution_mode: ExecutionMode
    status_source: str  # supervisor | nse_agent | commodity_agent | us_macd | research_sync | flag_only | always_on
    cadence_seconds: float | None = None
    broker_profile: str | None = None  # slow | fast | default | None (no broker REST)
    exchange_session: str | None = None
    enabled_flag_name: str | None = None
    runner_keys: tuple[str, ...] = ()
    paper_book_source: str | None = None
    status_endpoint: str | None = None
    audit_lane_key: str | None = None  # key into audits.lanes.REGISTRY
    agent_strategy_key: str | None = None  # for nse_agent/commodity_agent sources
    notes: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _audit_registry_keys() -> set[str]:
    try:
        from audits.lanes import REGISTRY

        return set(REGISTRY)
    except Exception:  # noqa: BLE001 — audits package must never break the registry
        return set()


def _flag(name: str | None, default: bool = False) -> bool | None:
    if not name:
        return None
    return bool(getattr(settings, name, default))


def get_registry() -> tuple[LaneSpec, ...]:
    """Build the declarative registry. Values that mirror settings are read
    fresh on every call so the snapshot reflects the current process config."""
    nse = "NSE 09:15-15:30 IST"
    mcx = "MCX 09:00-23:30 IST"

    supervisor_runners: list[LaneSpec] = [
        LaneSpec(
            key="option_flow_watchdog",
            label="Option-Flow Freshness Watchdog",
            kind="monitor",
            execution_mode="none",
            status_source="supervisor",
            cadence_seconds=float(getattr(settings, "OPTION_FLOW_WATCHDOG_INTERVAL_SECONDS", 60)),
            broker_profile="default",
            exchange_session=nse,
            enabled_flag_name="OPTION_FLOW_WATCHDOG_ENABLED",
            runner_keys=("option_flow_watchdog",),
            status_endpoint="/api/system/automation-status",
            notes="Detection-only monitor for a frozen REST premium feed (default OFF).",
        ),
        LaneSpec(
            key="token_readiness",
            label="Pre-open Broker Token Readiness",
            kind="monitor",
            execution_mode="none",
            status_source="supervisor",
            cadence_seconds=900.0,
            broker_profile="default",
            exchange_session="Pre-open readiness window (IST)",
            enabled_flag_name="TOKEN_READINESS_AUTO_ENABLED",
            runner_keys=("token_readiness",),
            status_endpoint="/api/system/automation-status",
            notes="Morning token sweep; deliberately never runs post-close.",
        ),
        LaneSpec(
            key="market_intelligence",
            label="Market Intelligence Refresh",
            kind="scheduler-runner",
            execution_mode="none",
            status_source="supervisor",
            cadence_seconds=float(settings.MARKET_INTELLIGENCE_REFRESH_INTERVAL_SECONDS),
            broker_profile="slow",
            exchange_session=nse,
            enabled_flag_name="MARKET_INTELLIGENCE_AUTO_ENABLED",
            runner_keys=("market_intelligence",),
            status_endpoint="/api/system/automation-status",
            notes="ATM watchlist / premium feed refresh — the data plane S1 scans from.",
        ),
        LaneSpec(
            key="auction_intelligence",
            label="Auction Intelligence Paper Cycle",
            kind="scheduler-runner",
            execution_mode="paper",
            status_source="supervisor",
            cadence_seconds=float(settings.AUCTION_INTELLIGENCE_AUTO_INTERVAL_SECONDS),
            broker_profile="fast",
            exchange_session=nse,
            enabled_flag_name="AUCTION_INTELLIGENCE_AUTO_ENABLED",
            runner_keys=("auction_intelligence",),
            paper_book_source="auction_intelligence paper book (PG)",
            status_endpoint="/api/auction-intelligence/summary",
        ),
        LaneSpec(
            key="auction_intelligence_commodity",
            label="Auction Intelligence Commodity Cycle",
            kind="scheduler-runner",
            execution_mode="paper",
            status_source="supervisor",
            cadence_seconds=float(settings.AUCTION_INTELLIGENCE_COMMODITY_INTERVAL_SECONDS),
            broker_profile="fast",
            exchange_session=mcx,
            enabled_flag_name="AUCTION_INTELLIGENCE_COMMODITY_ENABLED",
            runner_keys=("auction_intelligence_commodity",),
            paper_book_source="commodity futures paper book (separate from NSE)",
            status_endpoint="/api/auction-intelligence/summary",
        ),
        LaneSpec(
            key="institutional_convergence",
            label="Institutional Convergence Shadow Cycle",
            kind="scheduler-runner",
            execution_mode="paper",
            status_source="supervisor",
            cadence_seconds=float(settings.INSTITUTIONAL_CONVERGENCE_AUTO_INTERVAL_SECONDS),
            broker_profile="fast",
            exchange_session=nse,
            enabled_flag_name="INSTITUTIONAL_CONVERGENCE_AUTO_ENABLED",
            runner_keys=("institutional_convergence",),
            paper_book_source="institutional_convergence shadow book",
            status_endpoint="/api/institutional-convergence/summary",
        ),
        LaneSpec(
            key="institutional_convergence_commodity",
            label="Institutional Convergence Commodity Cycle",
            kind="scheduler-runner",
            execution_mode="paper",
            status_source="supervisor",
            cadence_seconds=float(settings.INSTITUTIONAL_CONVERGENCE_COMMODITY_INTERVAL_SECONDS),
            broker_profile="fast",
            exchange_session=mcx,
            enabled_flag_name="INSTITUTIONAL_CONVERGENCE_COMMODITY_ENABLED",
            runner_keys=("institutional_convergence_commodity",),
            paper_book_source="institutional_convergence commodity book",
            status_endpoint="/api/institutional-convergence/summary",
            notes="real_tick_cvd honest-gate blocks MCX entries until futures stream on the WS.",
        ),
        LaneSpec(
            key="fractal_market_profile",
            label="Fractal Market Profile Paper Cycle",
            kind="scheduler-runner",
            execution_mode="parked",
            status_source="supervisor",
            cadence_seconds=float(settings.FRACTAL_MARKET_PROFILE_AUTO_INTERVAL_SECONDS),
            broker_profile="fast",
            exchange_session=nse,
            enabled_flag_name="FRACTAL_MARKET_PROFILE_AUTO_ENABLED",
            runner_keys=("fractal_market_profile",),
            paper_book_source="fmp paper snapshots",
            status_endpoint="/api/fractal-market-profile/summary",
            notes="Parked out of production 2026-07-07 (owner call); flag default False.",
        ),
        LaneSpec(
            key="directional_options",
            label="Directional Options Paper Cycle",
            kind="scheduler-runner",
            execution_mode="paper",
            status_source="supervisor",
            cadence_seconds=float(settings.DIRECTIONAL_OPTIONS_AUTO_INTERVAL_SECONDS),
            broker_profile="fast",
            exchange_session=nse,
            enabled_flag_name="DIRECTIONAL_OPTIONS_AUTO_ENABLED",
            runner_keys=("directional_options",),
            paper_book_source="directional_options paper book",
            status_endpoint="/api/directional-options/summary",
            notes="3 indices + rotating NIFTY-50 stock batch (2026-07-17 expansion).",
        ),
        LaneSpec(
            key="directional_positioning",
            label="Directional Positioning Feed Refresh",
            kind="scheduler-runner",
            execution_mode="none",
            status_source="supervisor",
            cadence_seconds=float(getattr(settings, "DIRECTIONAL_POSITIONING_REFRESH_INTERVAL_SECONDS", 3600)),
            broker_profile="fast",
            exchange_session=f"{nse} + guaranteed post-close pass",
            enabled_flag_name="DIRECTIONAL_POSITIONAL_OPTIONS_ENABLED",
            runner_keys=("directional_positioning",),
            status_endpoint="/api/system/automation-status",
            notes="OI-positioning feed for the positional sleeve; only runs when the positional lane flag is on (default False).",
        ),
        LaneSpec(
            key="commodity_mp_history",
            label="Commodity MP Durable History",
            kind="scheduler-runner",
            execution_mode="none",
            status_source="supervisor",
            cadence_seconds=float(getattr(settings, "COMMODITY_MP_HISTORY_AUTO_INTERVAL_SECONDS", 21600)),
            broker_profile="fast",
            exchange_session=mcx,
            enabled_flag_name="COMMODITY_MP_HISTORY_AUTO_ENABLED",
            runner_keys=("commodity_mp_history",),
            status_endpoint="/api/system/automation-status",
            notes="Write-once TPO history maintenance feeding the live HTF gate.",
        ),
        LaneSpec(
            key="macd_refined",
            label="MACD Refined Paper Cycle",
            kind="scheduler-runner",
            execution_mode="paper",
            status_source="supervisor",
            cadence_seconds=float(settings.MACD_REFINED_AUTO_INTERVAL_SECONDS),
            broker_profile="slow",
            exchange_session=f"{nse} (never before 09:45)",
            enabled_flag_name="MACD_REFINED_AUTO_ENABLED",
            runner_keys=("macd_refined",),
            paper_book_source="macd_refined paper book",
            status_endpoint="/api/macd-refined/summary",
        ),
        LaneSpec(
            key="macd_refined_marks",
            label="MACD Refined Paper Marks / Protective Exits",
            kind="scheduler-runner",
            execution_mode="paper",
            status_source="supervisor",
            cadence_seconds=float(settings.MACD_REFINED_MARKS_REFRESH_INTERVAL_SECONDS),
            broker_profile="default",
            exchange_session=nse,
            enabled_flag_name="MACD_REFINED_AUTO_ENABLED",
            runner_keys=("macd_refined_marks",),
            paper_book_source="macd_refined paper book (marks/exits on the same book)",
            status_endpoint="/api/macd-refined/summary",
            notes="Reads the Fyers-WS plane; deliberately NOT under the slow-lane kill switch.",
        ),
        LaneSpec(
            key="cbe_scanner",
            label="CBE Scanner Paper Cycle",
            kind="scheduler-runner",
            execution_mode="paper",
            status_source="supervisor",
            cadence_seconds=float(settings.CBE_SCANNER_AUTO_INTERVAL_SECONDS),
            broker_profile="slow",
            exchange_session=f"{nse} + guaranteed post-close pass",
            enabled_flag_name="CBE_SCANNER_AUTO_ENABLED",
            runner_keys=("cbe_scanner",),
            paper_book_source="cbe cash-equity paper book",
            status_endpoint="/api/cbe/summary",
        ),
        LaneSpec(
            key="cbe_marks",
            label="CBE Paper Marks Refresh",
            kind="scheduler-runner",
            execution_mode="paper",
            status_source="supervisor",
            cadence_seconds=float(settings.CBE_MARKS_REFRESH_INTERVAL_SECONDS),
            broker_profile="slow",
            exchange_session=nse,
            enabled_flag_name="CBE_SCANNER_AUTO_ENABLED",
            runner_keys=("cbe_marks",),
            paper_book_source="cbe cash-equity paper book",
            status_endpoint="/api/cbe/summary",
        ),
        LaneSpec(
            key="gann_tp_delta",
            label="Gann TP Delta Paper Cycle",
            kind="scheduler-runner",
            execution_mode="paper",
            status_source="supervisor",
            cadence_seconds=float(settings.GANN_TP_DELTA_AUTO_INTERVAL_SECONDS),
            broker_profile="slow",
            exchange_session=nse,
            enabled_flag_name="GANN_TP_DELTA_AUTO_ENABLED",
            runner_keys=("gann_tp_delta",),
            paper_book_source="gann_tp_delta paper book",
            status_endpoint="/api/gann-tp-delta/summary",
        ),
        LaneSpec(
            key="lane_audit",
            label="Lane Signal-Correctness Audit",
            kind="monitor",
            execution_mode="none",
            status_source="supervisor",
            cadence_seconds=float(settings.LANE_AUDIT_INTERVAL_SECONDS),
            broker_profile="default",
            exchange_session=f"{nse} + guaranteed post-close pass",
            enabled_flag_name="LANE_AUDIT_ENABLED",
            runner_keys=("lane_audit",),
            status_endpoint="/api/system/signal-validation",
            notes="Runs every auditor in audits.lanes.REGISTRY (s1 only today).",
        ),
    ]

    own_loop_engines: list[LaneSpec] = [
        LaneSpec(
            key="s1_atm_30m_macd",
            label="Strategy 1 · 30m ATM MACD",
            kind="strategy-engine",
            execution_mode="paper",
            status_source="nse_agent",
            agent_strategy_key="macd_strategy",
            cadence_seconds=60.0,  # PaperStrategyAgent.scan_interval_seconds
            broker_profile="default",
            exchange_session=nse,
            enabled_flag_name=None,
            paper_book_source="paper_engine S1 book",
            status_endpoint="/api/strategy/status",
            audit_lane_key="s1",
            notes="Own-loop agent started unconditionally in main.py lifespan (no enable flag).",
        ),
        LaneSpec(
            key="s2_index_mp_macd",
            label="Strategy 2 · 15m Index MACD + MP",
            kind="strategy-engine",
            execution_mode="parked",
            status_source="nse_agent",
            agent_strategy_key="index_mp_strategy",
            cadence_seconds=None,
            broker_profile=None,
            exchange_session=nse,
            enabled_flag_name=None,
            paper_book_source="paper_engine S2 book (frozen state-file back-compat)",
            status_endpoint="/api/strategy/status",
            notes=(
                "DELETED 2026-06-02 (owner instruction): removed from the agent's "
                "_strategy_agents so it never scans; runtime kept only so persisted "
                "state files deserialize. Excluded from get_status strategies[]."
            ),
        ),
        LaneSpec(
            key="commodity_mp_orderflow",
            label="Commodity MP + Order Flow",
            kind="strategy-engine",
            execution_mode="paper",
            status_source="commodity_agent",
            agent_strategy_key="commodity_futures",
            cadence_seconds=None,  # agent-managed scan interval (see get_status)
            broker_profile="default",
            exchange_session=mcx,
            enabled_flag_name=None,
            paper_book_source="commodity futures paper book",
            status_endpoint="/api/commodity/overview",
            notes="Own-loop CommodityStrategyAgent started in main.py lifespan.",
        ),
    ]

    product_and_daemons: list[LaneSpec] = [
        LaneSpec(
            key="us_macd_refined",
            label="US MACD Refined (Alpaca)",
            kind="product-lane",
            execution_mode="parked",
            status_source="us_macd",
            cadence_seconds=None,
            broker_profile=None,
            exchange_session="US market (parked)",
            enabled_flag_name=None,
            paper_book_source="us_macd_refined paper book (frozen)",
            status_endpoint="/api/us/macd-refined/summary",
            notes="Honestly PARKED (audit 2026-07-18): brokers.alpaca does not exist on this deployment; /summary reports status=unavailable.",
        ),
        LaneSpec(
            key="research_sync",
            label="Upstox Research Sync",
            kind="scheduler-runner",
            execution_mode="none",
            status_source="research_sync",
            cadence_seconds=None,  # poll-minutes driven, env-configured per container
            broker_profile="default",
            exchange_session="post-close IST window",
            enabled_flag_name="RESEARCH_SYNC_EMBEDDED_ENABLED",
            status_endpoint="/api/analysis/research-cache-status",
            notes=(
                "Runs as a SEPARATE container (nomadcurie_research_sync) by default; the "
                "embedded flag (default False) runs it in-process instead. Exempt from the "
                "default DB statement timeout in standalone mode."
            ),
        ),
        LaneSpec(
            key="macd_diffusion",
            label="MACD Diffusion Breadth Daemon",
            kind="monitor",
            execution_mode="none",
            status_source="flag_only",
            cadence_seconds=float(getattr(settings, "MACD_DIFFUSION_POLL_MINUTES", 60)) * 60.0,
            broker_profile=None,
            exchange_session=nse,
            enabled_flag_name="MACD_DIFFUSION_ENABLED",
            notes="Own-loop daemon in main.py lifespan; no runtime status object (flag-only state).",
        ),
        LaneSpec(
            key="greeks_enrichment",
            label="Greeks Enrichment Daemon",
            kind="monitor",
            execution_mode="none",
            status_source="flag_only",
            cadence_seconds=float(getattr(settings, "GREEKS_ENRICHMENT_POLL_MINUTES", 30)) * 60.0,
            broker_profile=None,
            exchange_session=nse,
            enabled_flag_name="GREEKS_ENRICHMENT_ENABLED",
            notes="Copies real broker greeks from chain snapshots onto greeks-null candles.",
        ),
        LaneSpec(
            key="chain_candle_builder",
            label="Chain Candle Builder (F1 3m CE+PE)",
            kind="scheduler-runner",
            execution_mode="parked",
            status_source="flag_only",
            cadence_seconds=None,
            broker_profile="slow",
            exchange_session=nse,
            enabled_flag_name="CHAIN_CANDLE_BUILDER_ENABLED",
            status_endpoint="/api/system/rate-budget",
            notes="OFF until open-window priority is built (s1 watchlist freeze 2026-07-08).",
        ),
        LaneSpec(
            key="option_ws_subscription_manager",
            label="Option WS Subscription Manager",
            kind="monitor",
            execution_mode="none",
            status_source="flag_only",
            cadence_seconds=300.0,
            broker_profile=None,
            exchange_session=nse,
            enabled_flag_name="OPTION_WS_SUBSCRIPTIONS_ENABLED",
            notes="DRY-RUN unless the flag is on; reconciles ATM option symbols against the WS.",
        ),
        LaneSpec(
            key="held_position_marks_refresh",
            label="Held-Position WS Marks Refresh",
            kind="monitor",
            execution_mode="none",
            status_source="always_on",
            cadence_seconds=45.0,
            broker_profile=None,
            exchange_session=nse,
            notes="Keeps open option legs WS-subscribed so P&L marks stream per-tick.",
        ),
        LaneSpec(
            key="commodity_mark_refresh",
            label="Commodity Mark Refresh (REST bridge)",
            kind="monitor",
            execution_mode="none",
            status_source="always_on",
            cadence_seconds=12.0,
            broker_profile="default",
            exchange_session=mcx,
            notes="MCX futures have no WS feed; REST LTP poll bridges marks into the tick cache.",
        ),
        LaneSpec(
            key="rl_auto_trainer",
            label="Auction RL Auto-Trainer",
            kind="scheduler-runner",
            execution_mode="none",
            status_source="always_on",
            cadence_seconds=86400.0,
            broker_profile=None,
            exchange_session="post-close (close + ~45m)",
            notes="APScheduler job training the auction RL policy once per session.",
        ),
        LaneSpec(
            key="event_loop_lag_monitor",
            label="Event-Loop Lag Monitor",
            kind="monitor",
            execution_mode="none",
            status_source="always_on",
            cadence_seconds=None,
            notes="Pure observation (WS-0.2); first task started in the lifespan.",
        ),
        LaneSpec(
            key="live_candle_store",
            label="Live Candle Store (tick→bar)",
            kind="monitor",
            execution_mode="none",
            status_source="always_on",
            cadence_seconds=None,
            notes="Global tick fan-in building 1m/3m spot bars; feeds readiness gates.",
        ),
        LaneSpec(
            key="quote_bus",
            label="Quote Bus (WS fan-out)",
            kind="monitor",
            execution_mode="none",
            status_source="always_on",
            cadence_seconds=None,
            notes="Coalesces ticks into ~150ms frames on Redis quotes:bus for /ws/quotes.",
        ),
    ]

    return tuple(supervisor_runners + own_loop_engines + product_and_daemons)


def supervisor_runner_keys() -> set[str]:
    """All supervisor runner keys claimed by the registry."""
    keys: set[str] = set()
    for spec in get_registry():
        if spec.status_source == "supervisor":
            keys.update(spec.runner_keys)
    return keys


# ---------------------------------------------------------------------------
# Snapshot assembly
# ---------------------------------------------------------------------------

def _derive_status(
    *,
    enabled: bool | None,
    running: bool | None,
    stale: bool | None,
    last_error: str | None,
    execution_mode: str,
) -> str:
    if execution_mode == "parked":
        return "parked"
    if last_error:
        return "error"
    if stale:
        return "stale"
    if running:
        return "running"
    if enabled is False:
        return "disabled"
    if enabled is None:
        return "configured"
    return "ready"


async def _supervisor_state(spec: LaneSpec) -> dict[str, Any]:
    from core.market_hours_paper_supervisor import market_hours_paper_supervisor

    runner_key = spec.runner_keys[0]
    status = market_hours_paper_supervisor.get_runner_status(runner_key)
    return {
        "enabled": status.get("enabled"),
        "running": status.get("running"),
        "stale": status.get("stale"),
        "last_error": status.get("last_error"),
        "last_success_at": status.get("last_success_at"),
        "last_message": status.get("last_message"),
        "next_run_at": status.get("next_run_at"),
    }


def _agent_state(agent: Any, strategy_key: str | None) -> dict[str, Any]:
    try:
        status = agent.get_status(refresh=False)
    except TypeError:
        status = agent.get_status()
    strategies = status.get("strategies") or status.get("strategy_agents") or []
    strategy = next(
        (item for item in strategies if not strategy_key or item.get("key") == strategy_key),
        {},
    )
    last_error = status.get("last_error")
    return {
        "enabled": bool(status.get("enabled", True)),
        "running": bool(status.get("running")),
        "stale": None,
        "last_error": str(last_error) if last_error else None,
        "last_success_at": status.get("last_run_at") if not last_error else None,
        "last_message": status.get("last_message") or strategy.get("last_message"),
        "open_positions": (strategy.get("summary") or {}).get("open_positions")
        if isinstance(strategy.get("summary"), dict)
        else strategy.get("open_positions"),
    }


async def _nse_agent_state(spec: LaneSpec) -> dict[str, Any]:
    from paper_engine.strategy_agent import paper_strategy_agent

    return _agent_state(paper_strategy_agent, spec.agent_strategy_key)


async def _commodity_agent_state(spec: LaneSpec) -> dict[str, Any]:
    from paper_engine.commodity_strategy_agent import commodity_strategy_agent

    return _agent_state(commodity_strategy_agent, spec.agent_strategy_key)


async def _us_macd_state(spec: LaneSpec) -> dict[str, Any]:
    from macd_refined.service import us_macd_refined_service

    summary = await asyncio.to_thread(us_macd_refined_service.summary)
    automation = dict(summary.get("automation") or {})
    return {
        "enabled": bool(automation.get("enabled", False)),
        "running": bool(automation.get("running", False)),
        "stale": None,
        "last_error": automation.get("last_error"),
        "last_success_at": automation.get("last_success_at") or automation.get("last_run_at"),
        "last_message": "Parked: Alpaca data source not configured on this deployment.",
    }


async def _research_sync_state(spec: LaneSpec) -> dict[str, Any]:
    # Durable runtime marker written by the daemon (shared volume) — works for
    # both the standalone container and the embedded mode.
    from api.routers.analysis import _load_research_sync_runtime_state

    state = _load_research_sync_runtime_state() or {}
    last = state.get("last_result") or {}
    return {
        "enabled": bool(getattr(settings, "RESEARCH_SYNC_AUTO_ENABLED", False))
        or bool(getattr(settings, "RESEARCH_SYNC_EMBEDDED_ENABLED", False))
        or bool(state),
        "running": bool(state.get("running")) if "running" in state else None,
        "stale": None,
        "last_error": state.get("last_error") or last.get("error"),
        "last_success_at": state.get("last_success_at") or state.get("updated_at"),
        "last_message": state.get("phase") or state.get("status"),
    }


async def _flag_only_state(spec: LaneSpec) -> dict[str, Any]:
    return {
        "enabled": _flag(spec.enabled_flag_name),
        "running": None,
        "stale": None,
        "last_error": None,
        "last_success_at": None,
        "last_message": "No runtime status probe; state derived from config flag only.",
    }


async def _always_on_state(spec: LaneSpec) -> dict[str, Any]:
    return {
        "enabled": True,
        "running": None,
        "stale": None,
        "last_error": None,
        "last_success_at": None,
        "last_message": "Started unconditionally in main.py lifespan; no runtime probe.",
    }


_SOURCES = {
    "supervisor": _supervisor_state,
    "nse_agent": _nse_agent_state,
    "commodity_agent": _commodity_agent_state,
    "us_macd": _us_macd_state,
    "research_sync": _research_sync_state,
    "flag_only": _flag_only_state,
    "always_on": _always_on_state,
}


def _spec_payload(spec: LaneSpec, audit_keys: set[str]) -> dict[str, Any]:
    return {
        "key": spec.key,
        "label": spec.label,
        "kind": spec.kind,
        "execution_mode": spec.execution_mode,
        "cadence_seconds": spec.cadence_seconds,
        "broker_profile": spec.broker_profile,
        "exchange_session": spec.exchange_session,
        "enabled_flag_name": spec.enabled_flag_name,
        "runner_keys": list(spec.runner_keys),
        "paper_book_source": spec.paper_book_source,
        "status_endpoint": spec.status_endpoint,
        "audit_coverage": bool(spec.audit_lane_key and spec.audit_lane_key in audit_keys),
        "audit_lane_key": spec.audit_lane_key,
        "notes": spec.notes,
    }


async def build_lane_snapshot(spec: LaneSpec, audit_keys: set[str] | None = None) -> dict[str, Any]:
    """Declarative spec + current state. NEVER raises: a failing source yields
    status="unknown" with the error attached."""
    payload = _spec_payload(spec, _audit_registry_keys() if audit_keys is None else audit_keys)
    source = _SOURCES.get(spec.status_source)
    state: dict[str, Any]
    if source is None:
        payload.update({"status": "unknown", "error": f"unknown status source: {spec.status_source}"})
        return payload
    try:
        state = await asyncio.wait_for(source(spec), timeout=_SOURCE_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — resilience is the contract
        payload.update(
            {
                "status": "unknown",
                "error": f"{type(exc).__name__}: {exc}"[:300],
                "enabled": _flag(spec.enabled_flag_name),
            }
        )
        return payload
    payload.update(state)
    payload["status"] = _derive_status(
        enabled=state.get("enabled"),
        running=state.get("running"),
        stale=state.get("stale"),
        last_error=state.get("last_error"),
        execution_mode=spec.execution_mode,
    )
    payload["error"] = None
    return payload


async def build_lane_snapshots() -> list[dict[str, Any]]:
    audit_keys = _audit_registry_keys()
    specs = get_registry()
    return list(
        await asyncio.gather(*(build_lane_snapshot(spec, audit_keys) for spec in specs))
    )


def summarize(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_execution_mode: dict[str, int] = {}
    audited = 0
    for snap in snapshots:
        by_kind[str(snap.get("kind"))] = by_kind.get(str(snap.get("kind")), 0) + 1
        by_status[str(snap.get("status"))] = by_status.get(str(snap.get("status")), 0) + 1
        mode = str(snap.get("execution_mode"))
        by_execution_mode[mode] = by_execution_mode.get(mode, 0) + 1
        if snap.get("audit_coverage"):
            audited += 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(snapshots),
        "by_kind": by_kind,
        "by_status": by_status,
        "by_execution_mode": by_execution_mode,
        "audit_covered": audited,
        "audit_uncovered": len(snapshots) - audited,
    }
