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
    status_source: str  # supervisor | nse_agent | commodity_agent | research_sync | flag_only | always_on
    cadence_seconds: float | None = None
    broker_profile: str | None = None  # slow | fast | default | None (no broker REST)
    exchange_session: str | None = None
    enabled_flag_name: str | None = None
    runner_keys: tuple[str, ...] = ()
    paper_book_source: str | None = None
    status_endpoint: str | None = None
    audit_lane_key: str | None = None  # key into audits.lanes.REGISTRY
    agent_strategy_key: str | None = None  # for nse_agent/commodity_agent sources
    risk_book_source: str | None = None  # key into _BOOK_PROBES for best-effort breach visibility
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
    nse = "NSE 09:15-15:30 IST (cash) · derivatives to 15:40"
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
            key="stock_spot_sweep",
            label="F&O Stock Spot Post-Close Sweep",
            kind="data",
            execution_mode="none",
            status_source="supervisor",
            cadence_seconds=3600.0,
            broker_profile="default",
            exchange_session=nse,
            enabled_flag_name="STOCK_SPOT_SWEEP_ENABLED",
            runner_keys=("stock_spot_sweep",),
            status_endpoint="/api/system/automation-status",
            notes=(
                "Post-close (once/session) sweep of the F&O stock universe for "
                "intraday spot. Stock 30m had no durable live writer — "
                "upstox_research_sync covers 25 of ~211 names per pass — so full "
                "coverage previously required a manual backfill. CLASS_BULK + "
                "post-close only, so it never competes with live decision traffic."
            ),
        ),
        LaneSpec(
            key="stock_spot_intraday",
            label="F&O Stock Spot Intraday Sweep",
            kind="data", execution_mode="none", status_source="supervisor",
            cadence_seconds=float(settings.STOCK_SPOT_INTRADAY_SECONDS),
            broker_profile="default", exchange_session=nse,
            enabled_flag_name="STOCK_SPOT_INTRADAY_ENABLED",
            runner_keys=("stock_spot_intraday",),
            status_endpoint="/api/system/automation-status",
            notes="Public candle collection for the current stock decision grid; no orders.",
        ),
        LaneSpec(
            key="sector_ingestion",
            label="Sector-Interaction Durable Ingestion",
            kind="data",
            execution_mode="none",
            status_source="supervisor",
            cadence_seconds=3600.0,
            broker_profile="default",
            exchange_session=nse,
            enabled_flag_name="SECTOR_INGESTION_AUTO_ENABLED",
            runner_keys=("sector_ingestion",),
            status_endpoint="/api/system/automation-status",
            notes=(
                "Post-close (once/session) durable sector-interaction ingestion: "
                "IN public open-data collectors (PPAC; NPCI bot-blocked upstream) "
                "+ live-market sector observations + niftyindices constituent "
                "CSV sync. Fills the 24-distinct-date runtime handoff that flips "
                "/api/sector-interaction/signals off synthetic. No broker REST."
            ),
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
            key="macd_preopen_watchlist",
            label="MACD Pre-open ATM Watchlist",
            kind="scheduler-runner",
            execution_mode="none",
            status_source="supervisor",
            cadence_seconds=300.0,
            broker_profile="default",
            exchange_session=nse,
            enabled_flag_name="MACD_PREOPEN_WATCHLIST_ENABLED",
            runner_keys=("macd_preopen_watchlist",),
            status_endpoint="/api/system/automation-status",
            notes=(
                "Owner spec 2026-07-20. Pre-open only, two phases: expiry validation "
                "+ history warm-up before 09:00, then the anchor sample / liquid strike "
                "pick / FREEZE in the 09:04-09:14 window. Shared by s1_atm_30m_macd and "
                "macd_refined — they have ONE watchlist. Takes no position itself."
            ),
        ),
        LaneSpec(
            key="preopen_spot_snapshot",
            label="Pre-open Spot Snapshot + Activeness",
            kind="scheduler-runner",
            execution_mode="none",
            status_source="supervisor",
            cadence_seconds=300.0,
            broker_profile=None,
            exchange_session=nse,
            enabled_flag_name="PREOPEN_SPOT_SNAPSHOT_ENABLED",
            runner_keys=("preopen_spot_snapshot",),
            status_endpoint="/api/market/preopen/coverage",
            notes=(
                "Owner spec 2026-07-27. Fires 09:12-09:30 IST, AFTER the NSE cash "
                "call auction matches, and reads only ticks already committed to "
                "Postgres — no broker REST, no competition with the 09:04-09:14 "
                "MACD ladder build. Writes one bounded row per (session, underlying) "
                "to preopen_spot_snapshots with the equilibrium print, matched "
                "quantity, gap vs prior close and an activeness verdict. MCX is "
                "excluded (no call auction exists). Observation only — takes no "
                "position and touches no strategy math."
            ),
        ),
        LaneSpec(
            key="candidate_capture",
            label="Candidate Capture (research observer)",
            kind="scheduler-runner",
            execution_mode="none",
            status_source="supervisor",
            cadence_seconds=float(settings.CANDIDATE_CAPTURE_INTERVAL_SECONDS),
            broker_profile=None,
            exchange_session=nse,
            enabled_flag_name="CANDIDATE_CAPTURE_ENABLED",
            runner_keys=("candidate_capture",),
            notes=(
                "Prospective training-set capture. Records EVERY evaluated option "
                "contract inside a DTE/moneyness envelope — plus one explicit "
                "NO_TRADE candidate per decision set — to candidate_snapshots, so "
                "the contracts that were passed over stop being invisible. Every "
                "other book here only ever sees a contract after something chose "
                "to act on it, which makes honest supervised learning impossible. "
                "Read-only by construction: it reads the option chain already "
                "cached in Redis by OptionChainService, never calls track() or "
                "the broker, and imports no order path (enforced by "
                "tests/test_candidate_capture_service.py). Takes no position and "
                "touches no strategy math."
            ),
        ),
        LaneSpec(
            key="candidate_labelling",
            label="Candidate Outcome Labelling (post-close)",
            kind="scheduler-runner",
            execution_mode="none",
            status_source="supervisor",
            cadence_seconds=3600.0,
            broker_profile=None,
            exchange_session=nse,
            enabled_flag_name="CANDIDATE_LABELLING_ENABLED",
            runner_keys=("candidate_labelling",),
            notes=(
                "Post-close outcome resolution for candidate_capture. Two stages, "
                "because the data supports them very differently: the SPOT outcome "
                "is exactly computable from the ~0.25s index tick tape (barrier "
                "first-touch, true MFE/MAE), while the OPTION outcome is a "
                "nearest-sample mark on an irregular ~2-3 minute cadence, so every "
                "option field carries its REALIZED lag and sample count and an "
                "unmarkable row is stored as unlabellable_* rather than dropped. "
                "Costs are computed into the label (never re-subtracted at "
                "evaluation) using each row's OWN measured spread; the measured "
                "entry half-spread and the assumed exit half-spread are kept as "
                "separate columns. Reads committed rows only — no broker call, no "
                "position."
            ),
        ),
        LaneSpec(
            key="candidate_training",
            label="Candidate Model Training (post-close)",
            kind="scheduler-runner",
            execution_mode="none",
            status_source="supervisor",
            cadence_seconds=3600.0,
            broker_profile=None,
            exchange_session=nse,
            enabled_flag_name="CANDIDATE_TRAINING_ENABLED",
            runner_keys=("candidate_training",),
            notes=(
                "Fits, gates and versions the baseline ranker specialists from "
                "the labelled candidate set. Walk-forward (expanding-window) "
                "prequential evaluation with SESSION-CLUSTERED standard errors, "
                "and ten promotion gates that fail CLOSED — a metric that cannot "
                "be computed is a failed gate, never a skipped one. Refuses to "
                "promote anything until there is enough data, which is the "
                "normal outcome early on. Reads its own tables and writes only "
                "model versions; no broker call, no position."
            ),
        ),
        LaneSpec(
            key="auction_intelligence",
            audit_lane_key="auction_intelligence",
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
            audit_lane_key="institutional_convergence",
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
            status_endpoint="/api/institutional-convergence/status",
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
            status_endpoint="/api/institutional-convergence/commodity/status",
            notes=(
                "real_tick_cvd honest-gate blocks MCX entries until futures stream on the WS. "
                "The gate name is historical: it requires CVD derived from the observed QUOTE-tick "
                "stream rather than from bar shape — no broker sends an aggressor trade tape, so "
                "the buy/sell sides are inferred either way (analytics/orderflow.py)."
            ),
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
            audit_lane_key="directional_options",
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
            audit_lane_key="macd_refined",
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
            risk_book_source="macd_refined_paper",
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
            risk_book_source="macd_refined_paper",
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
            status_endpoint="/api/cbe/paper-summary",
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
            status_endpoint="/api/cbe/paper-summary",
        ),
        LaneSpec(
            key="gann_tp_delta",
            label="Gann TP Delta Paper Cycle",
            kind="scheduler-runner",
            execution_mode="parked",
            status_source="supervisor",
            cadence_seconds=float(settings.GANN_TP_DELTA_AUTO_INTERVAL_SECONDS),
            broker_profile="slow",
            exchange_session=nse,
            enabled_flag_name="GANN_TP_DELTA_AUTO_ENABLED",
            runner_keys=("gann_tp_delta",),
            paper_book_source="gann_tp_delta paper book",
            status_endpoint="/api/gann-tp-delta/summary",
            notes=(
                "Parked 2026-07-21 (owner call): lane stops running, ALL code/tables/"
                "tests/research kept. Flag default False; offline watchlist sidecar "
                "(gann_tp_delta/watchlist_runner.py) is manual-invoke only, never wired "
                "into the app. Same precedent as fractal_market_profile."
            ),
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
            # Own-loop seam wraps run_once in lane_broker_profile(LANE_PROFILE_SLOW)
            # (strategy_agent.py ~:1118) — decision REST rides the SLOW profile.
            broker_profile="slow",
            exchange_session=nse,
            enabled_flag_name=None,
            paper_book_source="paper_engine S1 book",
            status_endpoint="/api/trading/strategy-agent/status",
            audit_lane_key="s1",
            notes="Own-loop agent started unconditionally in main.py lifespan (no enable flag).",
        ),
        # RETIRED 2026-07-20 (owner spec): "For MACD, MACD refined - only these
        # two lanes for these strategy, other variants to be removed."
        # s2_index_mp_macd was already exec=parked/enabled=False. Its LaneSpec is
        # gone; the paper_engine `index_mp_strategy` RUNTIME stays only so
        # persisted state files still deserialize (it has never scanned since
        # 2026-06-02), and all historical data is kept.
        # NOTE: its status_endpoint /api/trading/strategy-agent/status is SHARED
        # with S1 — the lane entry is removed, the endpoint is NOT.
        LaneSpec(
            key="commodity_mp_orderflow",
            audit_lane_key="commodity_mp_orderflow",
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
        # RETIRED 2026-07-20 (owner spec, same instruction as s2 above).
        # us_macd_refined was exec=parked and brokers.alpaca does not exist on
        # this deployment, so it could never run. Registry entry, router,
        # service clone and UI desk removed; historical data kept.
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


# The ONE authoritative lane total. The pre-review memory note said "31 lanes";
# the assembler returned 32 because the registry grew by one entry the same day
# (US-MACD parked lane + research_sync were both added after that note). There is
# NO double-count: every key is unique AND no lane key collides with any other
# lane's runner_key (see registry_counts + test_lane_registry). This constant is
# the drift guard — adding/removing a LaneSpec must update it (a test asserts it).
# 2026-07-19: 32 -> 33 with stock_spot_sweep, the post-close F&O stock spot
# writer (stock 30m previously had no durable live writer at all).
# 2026-07-20: 33 -> 31, retiring the two PARKED MACD variants s2_index_mp_macd
# and us_macd_refined (owner: only MACD and MACD-refined survive). The two
# lanes that KEEP running are s1_atm_30m_macd and macd_refined; macd_refined_marks
# (exit/marks machinery) and macd_diffusion (a CE/PE breadth indicator) are NOT
# variants and are untouched.
# 2026-07-20: 31 -> 32 with macd_preopen_watchlist, the pre-open expiry +
# frozen-ATM-ladder runner the two surviving MACD lanes share.
# 2026-07-27: 32 -> 33 with preopen_spot_snapshot, the post-auction pre-open
# spot + activeness recorder (owner spec). Observation only, default OFF, and
# it makes no broker call.
# 2026-08-02: 33 -> 34 with sector_ingestion, the post-close sector-interaction
# durable ingestion + NSE constituent sync (data lane, no broker call) — the
# manual ingestion endpoints were never scheduled, so the 24-date runtime
# handoff could never be reached.
# 2026-08-26: 34 -> 35 with candidate_capture, the prospective training-set
# observer. Records every evaluated contract (plus an explicit NO_TRADE
# candidate) so the passed-over set stops being invisible to learning. Default
# OFF, no broker call, no position.
# 2026-08-27: 35 -> 36 with candidate_labelling, the post-close outcome
# resolver for the capture lane. Default OFF, no broker call, no position.
# 2026-08-27: 36 -> 37 with candidate_training, the post-close model fit +
# promotion-gate pass. Default OFF, no broker call, no position.
EXPECTED_LANE_TOTAL = 38


def registry_counts() -> dict[str, int]:
    """Bucketed lane counts that MUST sum to len(get_registry()).

    A lane belongs to exactly ONE bucket by status_source, so the buckets are a
    partition — this is how we prove there is no entry counted twice. Also
    surfaces cross-bucket key collisions (a lane key that is also another lane's
    runner_key) as ``key_runnerkey_collisions`` so a future two-bucket duplicate
    fails loudly instead of silently inflating the count.
    """
    specs = get_registry()
    supervisor = sum(1 for s in specs if s.status_source == "supervisor")
    own_loop = sum(1 for s in specs if s.status_source in ("nse_agent", "commodity_agent"))
    product_daemon = len(specs) - supervisor - own_loop
    keys = {s.key for s in specs}
    runner_keys: set[str] = set()
    for s in specs:
        runner_keys.update(rk for rk in s.runner_keys if rk not in keys or rk == s.key)
    # A runner_key that is ALSO a *different* lane's standalone key = a lane
    # represented in two buckets. Should always be empty.
    collisions = {
        rk
        for s in specs
        for rk in s.runner_keys
        if rk in keys and rk != s.key
    }
    return {
        "supervisor": supervisor,
        "own_loop": own_loop,
        "product_daemon": product_daemon,
        "total": len(specs),
        "key_runnerkey_collisions": len(collisions),
    }


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
    probed: bool = True,
) -> str:
    """Honest lane status.

    ``probed`` distinguishes a lane whose liveness was actually observed (a
    supervisor runner status, an agent get_status, a durable runtime marker)
    from one whose state is inferred from a config flag alone. An unprobed,
    config-on lane reports ``"enabled"`` (config-on, unverified) rather than
    ``"ready"`` (probed-live) — we never claim ready without evidence.
    """
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
    if not probed:
        return "enabled"
    return "ready"


async def _supervisor_state(spec: LaneSpec) -> dict[str, Any]:
    from core.market_hours_paper_supervisor import market_hours_paper_supervisor

    runner_key = spec.runner_keys[0]
    status = market_hours_paper_supervisor.get_runner_status(runner_key)

    # Foreign-plane staleness (split boot): when the OTHER process stops
    # publishing supervisor:status:{laneset} to Redis, the supervisor tags that
    # plane's runner entries with snapshot_stale. Reading only the runner's own
    # `stale` would let a dead/restart-looping strategy container keep reporting
    # its lanes READY off a frozen snapshot. Fold snapshot_stale into effective
    # staleness so a stale foreign snapshot downgrades the lane to "stale".
    own_stale = status.get("stale")
    snapshot_stale = status.get("snapshot_stale")  # None for local runners
    effective_stale = bool(own_stale) or bool(snapshot_stale)

    # Plane identity: foreign entries carry the source plane; local entries get
    # THIS supervisor's laneset so every lane exposes which process owns it.
    plane = status.get("plane") or getattr(market_hours_paper_supervisor, "_laneset", None)

    return {
        "enabled": status.get("enabled"),
        "running": status.get("running"),
        "stale": effective_stale,
        "last_error": status.get("last_error"),
        "last_success_at": status.get("last_success_at"),
        "last_message": status.get("last_message"),
        "next_run_at": status.get("next_run_at"),
        "loop_active": status.get("loop_active"),
        "plane": plane,
        "foreign_plane": bool(status.get("foreign")),
        "snapshot_stale": bool(snapshot_stale) if snapshot_stale is not None else None,
        "snapshot_age_seconds": status.get("snapshot_age_seconds"),
        "probed": True,
    }


_BOOK_KEYS = ("initial_capital", "available_capital", "max_drawdown", "total_equity")


def _extract_book(*candidates: Any) -> dict[str, Any] | None:
    """First candidate dict that carries capital fields → normalized book."""
    for cand in candidates:
        if isinstance(cand, dict) and any(k in cand for k in _BOOK_KEYS):
            return {k: cand.get(k) for k in _BOOK_KEYS}
    return None


def _agent_state(agent: Any, strategy_key: str | None) -> dict[str, Any]:
    try:
        status = agent.get_status(refresh=False)
    except TypeError:
        status = agent.get_status()
    strategies = status.get("strategies") or status.get("strategy_agents") or []
    matched: dict[str, Any] | None
    if strategy_key:
        matched = next((item for item in strategies if item.get("key") == strategy_key), None)
    else:
        matched = strategies[0] if strategies else None

    # Parked/deleted own-loop lane (e.g. the retired index_mp_strategy runtime,
    # excluded from get_status strategies[]): the requested strategy_key is absent.
    # NEVER borrow the LIVE agent's top-level enabled/running/last_error/
    # last_run_at/last_message — a parked lane must not show another lane's live
    # state. Return blanked dynamic fields (execution_mode="parked" still labels it).
    if strategy_key and matched is None:
        return {
            "enabled": False,
            "running": False,
            "stale": None,
            "last_error": None,
            "last_success_at": None,
            "last_message": "Parked: strategy absent from the live agent (no borrowed state).",
            "next_run_at": None,
            "open_positions": None,
            "loop_active": None,
            "book": None,
            "probed": True,
        }

    strategy = matched or {}
    last_error = status.get("last_error")
    enabled = bool(status.get("enabled", True))

    # Own-loop liveness: the agent answers loop_active from its scan-loop task
    # (or, in a split boot, a fresh persisted heartbeat via _loop_alive). A lane
    # that SHOULD be looping (enabled) whose loop is dead reads stale/not-ready,
    # instead of inheriting "ready" from persisted book state after the own-loop
    # died or restart-looped.
    loop_active = status.get("loop_active")
    stale: bool | None = None
    if enabled and loop_active is False:
        stale = True

    book = _extract_book(strategy.get("summary"), status.get("summary"))
    open_positions = (
        (strategy.get("summary") or {}).get("open_positions")
        if isinstance(strategy.get("summary"), dict)
        else strategy.get("open_positions")
    )
    return {
        "enabled": enabled,
        "running": bool(status.get("running")),
        "stale": stale,
        "last_error": str(last_error) if last_error else None,
        "last_success_at": status.get("last_run_at") if not last_error else None,
        "last_message": status.get("last_message") or strategy.get("last_message"),
        "next_run_at": status.get("next_scan_at"),
        "loop_active": loop_active,
        "open_positions": open_positions,
        "book": book,
        "probed": True,
    }


async def _nse_agent_state(spec: LaneSpec) -> dict[str, Any]:
    from paper_engine.strategy_agent import paper_strategy_agent

    return _agent_state(paper_strategy_agent, spec.agent_strategy_key)


async def _commodity_agent_state(spec: LaneSpec) -> dict[str, Any]:
    from paper_engine.commodity_strategy_agent import commodity_strategy_agent

    return _agent_state(commodity_strategy_agent, spec.agent_strategy_key)


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
        # A durable runtime marker was read (or its absence observed) — this is
        # evidence, so the lane may report "ready" when its marker says running.
        "probed": True,
    }


async def _flag_only_state(spec: LaneSpec) -> dict[str, Any]:
    # No heartbeat/runtime object: state is the config flag ONLY, so this lane
    # can never honestly claim "ready". probed=False → status "enabled" when on.
    return {
        "enabled": _flag(spec.enabled_flag_name),
        "running": None,
        "stale": None,
        "last_error": None,
        "last_success_at": None,
        "last_message": "No runtime status probe; state derived from config flag only.",
        "probed": False,
    }


async def _always_on_state(spec: LaneSpec) -> dict[str, Any]:
    # Started unconditionally in the lifespan but with no runtime probe — config-on,
    # unverified. probed=False → status "enabled" (not "ready").
    return {
        "enabled": True,
        "running": None,
        "stale": None,
        "last_error": None,
        "last_success_at": None,
        "last_message": "Started unconditionally in main.py lifespan; no runtime probe.",
        "probed": False,
    }


_SOURCES = {
    "supervisor": _supervisor_state,
    "nse_agent": _nse_agent_state,
    "commodity_agent": _commodity_agent_state,
    "research_sync": _research_sync_state,
    "flag_only": _flag_only_state,
    "always_on": _always_on_state,
}


# ---------------------------------------------------------------------------
# Risk-breach VISIBILITY (never enforcement)
# ---------------------------------------------------------------------------
# SIGNAL_VALIDATION_UNCAPPED=True is a STANDING OWNER DIRECTIVE: signals are
# validated with NO entry caps. This layer only SURFACES a breach flag on the
# snapshot so a served paper book gone deep-negative is visible; it adds no
# block, no allocation prevention. Best-effort: if a book number isn't readily
# available, risk_breach stays None (unknown) and this code never raises.

# Default drawdown fraction at/above which we surface a breach. Configurable via
# settings without requiring the key to exist (getattr default).
_DEFAULT_DRAWDOWN_SURFACING_PCT = 0.30


def _risk_breach_from_book(book: dict[str, Any] | None) -> tuple[bool | None, str | None]:
    """(risk_breach, reason) from already-served book numbers. None = unknown.

    Breach when the paper book's available_capital has gone negative OR realized
    drawdown meets/exceeds the surfacing threshold. VISIBILITY ONLY.
    """
    if not isinstance(book, dict):
        return None, None
    avail = book.get("available_capital")
    drawdown = book.get("max_drawdown")  # fraction (0.5 == 50%)
    have_avail = isinstance(avail, (int, float)) and not isinstance(avail, bool)
    have_dd = isinstance(drawdown, (int, float)) and not isinstance(drawdown, bool)
    if not have_avail and not have_dd:
        return None, None  # book present but no usable numbers → unknown
    threshold = float(getattr(settings, "RISK_BREACH_DRAWDOWN_SURFACING_PCT", _DEFAULT_DRAWDOWN_SURFACING_PCT))
    reasons: list[str] = []
    if have_avail and avail < 0:
        reasons.append(f"available_capital negative ({avail:,.0f})")
    if have_dd and drawdown >= threshold:
        reasons.append(f"drawdown {drawdown * 100:.1f}% >= surfacing threshold {threshold * 100:.0f}%")
    return (bool(reasons), "; ".join(reasons) if reasons else None)


async def _macd_refined_book_probe() -> dict[str, Any] | None:
    """Best-effort paper book for the macd_refined lanes (supervisor-scheduled,
    so their book isn't in the supervisor runner status). Guarded + off-thread."""
    from macd_refined.service import macd_refined_service

    summary = await asyncio.to_thread(macd_refined_service.summary)
    return _extract_book(summary.get("paper_summary"))


# Lane-key/book-source → probe. Only lanes whose book is cheaply + safely
# reachable are wired; every other lane leaves risk_breach=None (honest unknown).
_BOOK_PROBES: dict[str, Any] = {
    "macd_refined_paper": _macd_refined_book_probe,
}


async def _resolve_book(spec: LaneSpec, state: dict[str, Any]) -> dict[str, Any] | None:
    """Book from the state source if present, else the spec's declared probe.
    Never raises — returns None on any failure so risk stays 'unknown'."""
    book = state.get("book")
    if isinstance(book, dict):
        return book
    probe = _BOOK_PROBES.get(spec.risk_book_source or "")
    if probe is None:
        return None
    try:
        return await asyncio.wait_for(probe(), timeout=_SOURCE_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 — visibility is best-effort, never fatal
        return None


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
        payload.update(
            {
                "status": "unknown",
                "error": f"unknown status source: {spec.status_source}",
                "risk_breach": None,
                "risk_breach_reason": None,
            }
        )
        return payload
    try:
        state = await asyncio.wait_for(source(spec), timeout=_SOURCE_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — resilience is the contract
        payload.update(
            {
                "status": "unknown",
                "error": f"{type(exc).__name__}: {exc}"[:300],
                "enabled": _flag(spec.enabled_flag_name),
                "risk_breach": None,  # unknown, never raise
                "risk_breach_reason": None,
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
        probed=bool(state.get("probed", True)),
    )
    # Risk-breach VISIBILITY: best-effort, never blocks (SIGNAL_VALIDATION_UNCAPPED).
    book = await _resolve_book(spec, state)
    breach, reason = _risk_breach_from_book(book)
    payload["book"] = book
    payload["risk_breach"] = breach
    payload["risk_breach_reason"] = reason
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
    risk_breached = 0
    risk_breached_keys: list[str] = []
    risk_unknown = 0
    for snap in snapshots:
        by_kind[str(snap.get("kind"))] = by_kind.get(str(snap.get("kind")), 0) + 1
        by_status[str(snap.get("status"))] = by_status.get(str(snap.get("status")), 0) + 1
        mode = str(snap.get("execution_mode"))
        by_execution_mode[mode] = by_execution_mode.get(mode, 0) + 1
        if snap.get("audit_coverage"):
            audited += 1
        breach = snap.get("risk_breach")
        if breach is True:
            risk_breached += 1
            risk_breached_keys.append(str(snap.get("key")))
        elif breach is None:
            risk_unknown += 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(snapshots),
        "by_kind": by_kind,
        "by_status": by_status,
        "by_execution_mode": by_execution_mode,
        "audit_covered": audited,
        "audit_uncovered": len(snapshots) - audited,
        # VISIBILITY ONLY — a breach flag is surfaced, never enforced.
        "risk_breached": risk_breached,
        "risk_breached_keys": risk_breached_keys,
        "risk_unknown": risk_unknown,
    }
