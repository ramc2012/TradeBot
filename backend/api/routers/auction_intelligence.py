"""Isolated API for the Market Profile + order-flow strategy module."""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date, datetime, time, timezone
from time import monotonic
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from loguru import logger
from pydantic import BaseModel, Field

from auction_intelligence import AuctionIntelligenceService
from auction_intelligence.automation import build_shadow_records_from_snapshot
from auction_intelligence.analytics import MPAnalyticsEngine
from auction_intelligence.config import clone_default_config
from auction_intelligence.demo import (
    available_scenarios as get_available_demo_scenarios,
    available_symbols as get_available_demo_symbols,
    build_demo_analysis,
    build_demo_validation_series,
)
from auction_intelligence.live import (
    available_live_symbols as get_available_live_symbols,
    build_live_analysis,
    build_shadow_backfill_snapshots,
    build_live_validation_series,
)
from auction_intelligence.market_profile.engine import MarketProfileEngine
from auction_intelligence.paper import PaperPositionBook
from auction_intelligence.paper.journal import JournalReader
from auction_intelligence.shadow import ShadowPersistenceService
from auction_intelligence.validation.gate_b import GateBValidator
from auction_intelligence.validation.gate_c import GateCValidator
from auction_intelligence.validation.persistence import ValidationPersistenceService
from auction_intelligence.schemas import (
    DepthLevel,
    DepthSnapshot,
    MarketBar,
    PortfolioSnapshot,
    QuoteSnapshot,
    SessionContext,
    TradePrint,
)
from agentic_rag import ContextGateRequest, rag_service
from api.routers.auth import get_connected_brokers


router = APIRouter(prefix="/api/auction-intelligence", tags=["auction-intelligence"])
_validation_store = ValidationPersistenceService()
_shadow_store = ShadowPersistenceService()
_paper_journal = JournalReader(clone_default_config()["paper_trading"]["journal_root"])
_paper_book = PaperPositionBook(clone_default_config()["paper_trading"]["journal_root"])


class BarPayload(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class TradePayload(BaseModel):
    timestamp: datetime
    price: float
    quantity: float
    aggressor_side: str = "unknown"


class QuotePayload(BaseModel):
    timestamp: datetime
    bid: float
    ask: float
    bid_size: float = 0.0
    ask_size: float = 0.0


class DepthLevelPayload(BaseModel):
    price: float
    quantity: float


class DepthPayload(BaseModel):
    timestamp: datetime
    bids: list[DepthLevelPayload] = Field(default_factory=list)
    asks: list[DepthLevelPayload] = Field(default_factory=list)


class SessionPayload(BaseModel):
    symbol: str = "NIFTY FUT"
    session_date: date = Field(default_factory=date.today)
    last_price: float
    stale_data_seconds: float = 0.0
    minutes_to_close: int = 120
    broker_connected: bool = True


class PortfolioPayload(BaseModel):
    net_liquidation: float = 1_000_000.0
    daily_realized_pnl: float = 0.0
    open_positions: int = 0
    symbol_exposure: dict[str, float] = Field(default_factory=dict)
    agent_drawdowns: dict[str, float] = Field(default_factory=dict)
    correlated_exposure: float = 0.0


class AnalysisRequest(BaseModel):
    session: SessionPayload
    quote: QuotePayload
    quote_history: list[QuotePayload] = Field(default_factory=list)
    bars: list[BarPayload]
    prior_bars: list[BarPayload] = Field(default_factory=list)
    trades: list[TradePayload] = Field(default_factory=list)
    depth: Optional[DepthPayload] = None
    portfolio: PortfolioPayload = Field(default_factory=PortfolioPayload)


class ShadowCaptureOptions(BaseModel):
    reconciliation_status: str = "matched"
    mismatch_duration_seconds: float = 0.0
    kill_switch_tested: bool = False
    kill_switch_passed: bool = False
    dashboard_checked: bool = False
    alerts_checked: bool = False
    manual_override_tested: bool = False
    record_flat_decisions: bool = True


def _service() -> AuctionIntelligenceService:
    return AuctionIntelligenceService()


def _bars(payload: list[BarPayload]) -> list[MarketBar]:
    return [MarketBar(**item.model_dump()) for item in payload]


def _trades(payload: list[TradePayload]) -> list[TradePrint]:
    return [TradePrint(**item.model_dump()) for item in payload]


def _quotes(payload: list[QuotePayload]) -> list[QuoteSnapshot]:
    return [QuoteSnapshot(**item.model_dump()) for item in payload]


def _depth(payload: Optional[DepthPayload]) -> Optional[DepthSnapshot]:
    if payload is None:
        return None
    return DepthSnapshot(
        timestamp=payload.timestamp,
        bids=[DepthLevel(**item.model_dump()) for item in payload.bids],
        asks=[DepthLevel(**item.model_dump()) for item in payload.asks],
    )


def _serialize(value: object) -> dict:
    return jsonable_encoder(asdict(value))


def _parse_time_value(raw: str | None) -> time | None:
    if not raw:
        return None
    return time.fromisoformat(raw)


def _normalize_symbol_filter(symbol: str | None) -> str | None:
    if not symbol:
        return None
    return symbol.upper().replace(" FUT", "").replace(" INDEX", "").strip()


def _journal_matches_symbol(record: dict, symbol: str | None) -> bool:
    normalized = _normalize_symbol_filter(symbol)
    if not normalized:
        return True

    symbol_field = str(record.get("symbol") or "").upper().replace(" FUT", "").replace(" INDEX", "").strip()
    underlying_field = str(record.get("underlying_symbol") or "").upper().replace(" FUT", "").replace(" INDEX", "").strip()
    trading_symbol = str(record.get("trading_symbol") or "").upper().strip()

    return (
        normalized == symbol_field
        or normalized == underlying_field
        or trading_symbol.startswith(normalized)
    )


def _shadow_records_from_snapshot(snapshot: dict, options: ShadowCaptureOptions) -> list[dict]:
    return build_shadow_records_from_snapshot(snapshot, options.model_dump())


async def _safe_context_gate(request: ContextGateRequest) -> dict:
    try:
        result = await asyncio.to_thread(rag_service.context_gate, request)
        return result.model_dump()
    except Exception as exc:
        return {
            "decision": "warn",
            "confidence": 0.0,
            "summary": f"RAG context unavailable: {exc}",
            "reason_codes": ["rag_unavailable"],
            "case_stats": {"matched_cases": 0, "resolved_cases": 0},
            "retrievals": [],
            "audit_bundle": {},
        }


async def _build_live_rag_context(snapshot: dict) -> dict:
    request_payload = snapshot.get("request") or {}
    analysis = snapshot.get("analysis") or {}
    session = request_payload.get("session") or {}
    decisions = analysis.get("agent_decisions") or []
    actionable = [row for row in decisions if str(row.get("action") or "FLAT").upper() != "FLAT"]
    primary_decision = max(actionable, key=lambda row: float(row.get("confidence") or 0.0), default={})
    order_flow = analysis.get("order_flow") or {}
    market_profile = analysis.get("market_profile") or {}
    regime = analysis.get("regime") or {}
    allowed_directions = regime.get("allowed_directions") or [None]
    symbol = str(session.get("symbol") or snapshot.get("symbol_code") or "").upper()
    underlying = str(snapshot.get("symbol_code") or symbol.replace(" FUT", "") or "NIFTY").upper()
    return await _safe_context_gate(
        ContextGateRequest(
            strategy_key="auction_intelligence",
            underlying=underlying,
            symbol=symbol,
            signal_direction=primary_decision.get("action") or allowed_directions[0],
            setup_name=primary_decision.get("agent_name") or regime.get("label"),
            regime=regime.get("label"),
            event_tags=["live_snapshot", "market_profile", "orderflow"],
            numeric_context={
                "last_price": session.get("last_price"),
                "poc": market_profile.get("poc"),
                "vah": market_profile.get("vah"),
                "val": market_profile.get("val"),
                "timing_confidence": order_flow.get("timing_confidence"),
                "queue_pressure": order_flow.get("queue_pressure"),
                "toxicity_score": order_flow.get("toxicity_score") or order_flow.get("adverse_selection_risk"),
            },
            hard_risk_passed=bool((analysis.get("risk") or {}).get("allowed", True)),
        )
    )


@router.get("/summary")
async def summary() -> dict:
    config = clone_default_config()
    from core.market_hours_paper_supervisor import market_hours_paper_supervisor

    automation = market_hours_paper_supervisor.get_runner_status("auction_intelligence")
    return {
        "module": "auction_intelligence",
        "description": "Separate Market Profile + order-flow strategy stack",
        "auto_started": bool(automation.get("enabled") and automation.get("loop_active")),
        "automation": automation,
        "mvp_scope": config["mvp_scope"],
        "deployable_first_sleeve": "swing",
        "validation_gates": [
            {"id": "gate_a", "label": "Data and feature engine", "status": "available"},
            {"id": "gate_b", "label": "Rule engine and walk-forward", "status": "available"},
            {"id": "gate_c", "label": "Shadow mode and auto paper trading", "status": "available"},
            {"id": "gate_d", "label": "Live canary", "status": "planned"},
        ],
        "implementation_plan": [
            "Automate Gate A against broker-backed snapshots and deterministic demo sessions.",
            "Run Gate B walk-forward and setup-level expectancy validation on a deeper BANKNIFTY futures replay set.",
            "Promote only after shadow-mode reconciliation and automated paper-trading stability are verified.",
        ],
        "demo_symbols": get_available_demo_symbols(),
        "live_symbols": get_available_live_symbols(),
        "demo_scenarios": get_available_demo_scenarios(),
        "connected_brokers": get_connected_brokers(),
        "live_ready": bool(get_connected_brokers()),
        "endpoints": [
            "/api/auction-intelligence/summary",
            "/api/auction-intelligence/default-config",
            "/api/auction-intelligence/demo-scenario",
            "/api/auction-intelligence/live-snapshot",
            "/api/auction-intelligence/analyze",
            "/api/auction-intelligence/paper-proposal",
            "/api/auction-intelligence/validate-gate-a",
            "/api/auction-intelligence/validate-gate-b",
            "/api/auction-intelligence/shadow-record-live",
            "/api/auction-intelligence/shadow-backfill",
            "/api/auction-intelligence/shadow-records",
            "/api/auction-intelligence/validate-gate-c",
            "/api/auction-intelligence/canary-readiness",
            "/api/auction-intelligence/validation-runs/latest",
            "/api/auction-intelligence/validation-runs/{run_id}/artifacts",
            "/api/auction-intelligence/rl-policy",
            "/api/auction-intelligence/rl-cycle",
            "/api/auction-intelligence/rl-versions",
        ],
    }


@router.get("/default-config")
async def default_config() -> dict:
    return clone_default_config()


@router.get("/demo-scenario")
async def demo_scenario(symbol: str = "NIFTY", scenario: str = "acceptance_up") -> dict:
    try:
        return build_demo_analysis(symbol_code=symbol, scenario=scenario)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/live-snapshot")
async def live_snapshot(symbol: str = "NIFTY") -> dict:
    try:
        snapshot = await build_live_analysis(symbol_code=symbol)
        snapshot["rag_context"] = await _build_live_rag_context(snapshot)
        return snapshot
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/shadow-record-live")
async def shadow_record_live(
    symbol: str = "BANKNIFTY",
    options: ShadowCaptureOptions = Body(default_factory=ShadowCaptureOptions),
) -> dict:
    try:
        snapshot = await build_live_analysis(symbol_code=symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    records = _shadow_records_from_snapshot(snapshot, options)
    storage = await _shadow_store.record_records(records)
    return {
        "symbol_code": snapshot.get("symbol_code"),
        "session_date": snapshot.get("session_date"),
        "snapshot_mode": snapshot.get("request", {}).get("metadata", {}).get("snapshot_mode"),
        "record_count": len(records),
        "storage": storage,
        "records_preview": records[:6],
    }


@router.post("/shadow-backfill")
async def shadow_backfill(
    symbol: str = "BANKNIFTY",
    session_limit: int = 20,
    lookback_days: int = 45,
    observation_bars: int = 4,
    snapshot_cutoff: str = "11:15",
    shadow_net_liquidation: float = 1_000_000.0,
    options: ShadowCaptureOptions = Body(default_factory=ShadowCaptureOptions),
) -> dict:
    try:
        backfill = await build_shadow_backfill_snapshots(
            symbol_code=symbol,
            max_sessions=session_limit,
            lookback_days=lookback_days,
            observation_bars=observation_bars,
            snapshot_cutoff=_parse_time_value(snapshot_cutoff),
            shadow_net_liquidation=shadow_net_liquidation,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    records: list[dict] = []
    for snapshot in backfill["snapshots"]:
        records.extend(_shadow_records_from_snapshot(snapshot, options))
    storage = await _shadow_store.record_records(records)
    return {
        "symbol_code": backfill["symbol_code"],
        "source": backfill["source"],
        "history_symbol": backfill["history_symbol"],
        "snapshot_count": backfill["snapshot_count"],
        "skipped_sessions": backfill["skipped_sessions"],
        "observation_bars": observation_bars,
        "snapshot_cutoff": snapshot_cutoff,
        "shadow_net_liquidation": shadow_net_liquidation,
        "record_count": len(records),
        "storage": storage,
        "records_preview": records[:8],
    }


@router.get("/shadow-records")
async def shadow_records(symbol: str = "BANKNIFTY", limit: int = 50) -> dict:
    symbol_base = _normalize_symbol_filter(symbol) or "BANKNIFTY"
    symbol_key = f"{symbol_base} FUT"
    records = await _shadow_store.list_records(symbol=symbol_key, limit=limit)
    return {
        "symbol": symbol_key,
        "count": len(records),
        "records": records,
    }


@router.get("/paper-journal")
async def paper_journal(symbol: str | None = None, limit: int = 50) -> dict:
    limit = max(1, min(limit, 500))
    filtered = [
        record
        for record in _paper_journal.iter_records()
        if _journal_matches_symbol(record, symbol)
    ]
    filtered.sort(key=lambda item: str(item.get("recorded_at") or ""), reverse=True)
    records = filtered[:limit]

    action_breakdown = Counter(str(item.get("action") or "UNKNOWN") for item in filtered)
    style_breakdown = Counter(str(item.get("execution_style") or "unknown") for item in filtered)
    agent_breakdown = Counter(str(item.get("agent_name") or "unknown") for item in filtered)
    premiums = [
        float(item["premium"])
        for item in filtered
        if item.get("premium") is not None
    ]
    confidences = [
        float(item["confidence"])
        for item in filtered
        if item.get("confidence") is not None
    ]

    return {
        "symbol_filter": _normalize_symbol_filter(symbol),
        "count": len(records),
        "total_records": len(filtered),
        "summary": {
            "latest_recorded_at": records[0].get("recorded_at") if records else None,
            "avg_confidence": round(sum(confidences) / len(confidences), 4) if confidences else None,
            "avg_premium": round(sum(premiums) / len(premiums), 4) if premiums else None,
            "action_breakdown": dict(action_breakdown),
            "style_breakdown": dict(style_breakdown),
            "agent_breakdown": dict(agent_breakdown),
        },
        "records": records,
    }


@router.get("/paper-positions")
async def paper_positions(symbol: str | None = None, status: str = "all", limit: int = 50) -> dict:
    normalized_status = str(status or "all").lower()
    if normalized_status not in {"all", "open", "closed"}:
        raise HTTPException(status_code=400, detail="status must be one of: all, open, closed")
    return await _paper_book.list_positions(
        symbol=symbol,
        status=normalized_status,
        limit=max(1, min(limit, 200)),
    )


@router.post("/analyze")
async def analyze(request: AnalysisRequest) -> dict:
    service = _service()
    bundle = await service.analyze_with_options(
        session=SessionContext(**request.session.model_dump()),
        bars=_bars(request.bars),
        quote=QuoteSnapshot(**request.quote.model_dump()),
        trades=_trades(request.trades),
        prior_bars=_bars(request.prior_bars),
        depth=_depth(request.depth),
        portfolio=PortfolioSnapshot(**request.portfolio.model_dump()),
        quote_history=_quotes(request.quote_history),
    )
    return _serialize(bundle)


@router.post("/paper-proposal")
async def paper_proposal(request: AnalysisRequest) -> dict:
    service = _service()
    bundle, journal_paths, paper_positions = await service.analyze_and_record_option_paper(
        session=SessionContext(**request.session.model_dump()),
        bars=_bars(request.bars),
        quote=QuoteSnapshot(**request.quote.model_dump()),
        trades=_trades(request.trades),
        prior_bars=_bars(request.prior_bars),
        depth=_depth(request.depth),
        portfolio=PortfolioSnapshot(**request.portfolio.model_dump()),
        quote_history=_quotes(request.quote_history),
    )
    payload = _serialize(bundle)
    payload["journal_paths"] = journal_paths
    payload["paper_positions"] = paper_positions
    return payload


@router.post("/validate-gate-a")
async def validate_gate_a(request: AnalysisRequest) -> dict:
    service = _service()
    report = service.validate_gate_a(
        session=SessionContext(**request.session.model_dump()),
        bars=_bars(request.bars),
        prior_bars=_bars(request.prior_bars),
    )
    storage = await _validation_store.record_report(
        report,
        gate="gate_a",
        symbol=request.session.symbol,
        mode="request",
        source="manual_payload",
        context={"session_date": request.session.session_date.isoformat()},
    )
    payload = jsonable_encoder(asdict(report))
    payload["storage"] = storage
    return payload


@router.get("/validate-gate-b")
async def validate_gate_b(
    symbol: str = "BANKNIFTY",
    mode: str = "live",
    scenario: str = "acceptance_up",
    session_limit: int = 20,
    lookback_days: int = 45,
) -> dict:
    if mode not in {"live", "demo"}:
        raise HTTPException(status_code=400, detail="mode must be 'live' or 'demo'")

    if mode == "live":
        series = await build_live_validation_series(
            symbol_code=symbol,
            max_sessions=session_limit,
            lookback_days=lookback_days,
        )
    else:
        series = build_demo_validation_series(symbol_code=symbol, scenario=scenario, session_count=session_limit)

    sessions = [
        [
            MarketBar(
                timestamp=datetime.fromisoformat(str(item["timestamp"])),
                open=item["open"],
                high=item["high"],
                low=item["low"],
                close=item["close"],
                volume=item.get("volume", 0.0),
            )
            for item in session["bars"]
        ]
        for session in series["sessions"]
    ]
    report = GateBValidator().validate(
        symbol=f"{symbol.upper()} FUT",
        sessions=sessions,
        mode=mode,
        source=str(series.get("source", mode)),
    )
    storage = await _validation_store.record_report(
        report,
        gate="gate_b",
        symbol=symbol.upper(),
        mode=mode,
        scenario=scenario if mode == "demo" else None,
        source=str(series.get("source", mode)),
        context={"session_limit": session_limit, "session_count": len(series["sessions"])},
    )
    payload = jsonable_encoder(asdict(report))
    payload["series_metadata"] = {
        "symbol_code": series["symbol_code"],
        "source": series.get("source"),
        "session_count": len(series["sessions"]),
        "session_dates": [session["session_date"] for session in series["sessions"]],
    }
    payload["storage"] = storage
    payload["artifact_count"] = len(report.artifacts)
    payload["artifacts_preview"] = jsonable_encoder([asdict(item) for item in report.artifacts[:8]])
    return payload


@router.get("/validate-gate-c")
async def validate_gate_c(
    symbol: str = "BANKNIFTY",
    session_limit: int = 30,
    record_limit: int = 500,
) -> dict:
    symbol_key = f"{symbol.upper()} FUT"
    records = await _shadow_store.list_records(symbol=symbol_key, limit=record_limit)
    report = GateCValidator().validate(
        symbol=symbol_key,
        records=records,
        session_limit=session_limit,
    )
    storage = await _validation_store.record_report(
        report,
        gate="gate_c",
        symbol=symbol.upper(),
        mode="live_shadow",
        source="shadow_observations",
        context={
            "session_limit": session_limit,
            "record_limit": record_limit,
            "record_count": len(records),
        },
    )
    payload = jsonable_encoder(asdict(report))
    payload["series_metadata"] = {
        "symbol": symbol_key,
        "record_count": len(records),
        "session_limit": session_limit,
    }
    payload["storage"] = storage
    payload["artifact_count"] = len(report.artifacts)
    payload["artifacts_preview"] = jsonable_encoder([asdict(item) for item in report.artifacts[:8]])
    return payload


@router.get("/canary-readiness")
async def canary_readiness(symbol: str = "BANKNIFTY") -> dict:
    normalized_symbol = symbol.upper()
    config = clone_default_config()
    canary_config = config.get("live_canary", {})
    gate_b = await _validation_store.latest_report(gate="gate_b", symbol=normalized_symbol)
    gate_c = await _validation_store.latest_report(gate="gate_c", symbol=normalized_symbol)

    blockers: list[str] = []
    if normalized_symbol not in canary_config.get("allowed_symbols", []):
        blockers.append(f"{normalized_symbol} is not in the approved canary symbol list.")
    if not gate_b or not bool(gate_b.get("passed")):
        blockers.append("Gate B is not passing for this symbol.")
    if not gate_c or not bool(gate_c.get("passed")):
        blockers.append("Gate C is not passing for this symbol.")

    ready = not blockers
    return {
        "symbol": normalized_symbol,
        "ready": ready,
        "stage": "gate_d_canary",
        "blockers": blockers,
        "requirements": {
            "manual_approval_required": bool(canary_config.get("manual_approval_required", True)),
            "allowed_agents": list(canary_config.get("allowed_agents", ["swing"])),
            "max_live_lots": int(canary_config.get("max_live_lots", 1)),
            "daily_loss_limit": float(canary_config.get("daily_loss_limit", 25_000.0)),
            "max_size_multiplier": float(canary_config.get("max_size_multiplier", 0.35)),
        },
        "gate_b": gate_b,
        "gate_c": gate_c,
        "next_step": (
            "Prepare the smallest-size live canary with manual approval still enabled."
            if ready
            else "Clear the listed blockers before enabling any live canary route."
        ),
    }


@router.get("/validation-runs/latest")
async def latest_validation_run(gate: str | None = None, symbol: str | None = None) -> dict:
    payload = await _validation_store.latest_report(gate=gate, symbol=symbol)
    if payload is None:
        raise HTTPException(status_code=404, detail="No persisted validation run found.")
    return payload


@router.get("/validation-runs/{run_id}/artifacts")
async def validation_run_artifacts(
    run_id: str,
    artifact_type: str | None = None,
    limit: int = 50,
) -> dict:
    artifacts = await _validation_store.list_artifacts(
        run_id,
        artifact_type=artifact_type,
        limit=limit,
    )
    return {
        "run_id": run_id,
        "artifact_type": artifact_type,
        "count": len(artifacts),
        "artifacts": artifacts,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  RL Q-learning policy endpoints
# ══════════════════════════════════════════════════════════════════════════════


@router.get("/rl-policy")
async def rl_policy_summary() -> dict:
    """Return the current RL Q-table summary: learned policy per MP state."""
    from auction_intelligence.rl.policy import rl_policy as _rl

    if not _rl._cache_loaded:
        await _rl.load_cache()
    return _rl.get_policy_summary()


@router.post("/rl-train")
async def rl_train(
    max_trades: int = 500,
    symbol: str | None = None,
    use_proxy_reward: bool = True,
) -> dict:
    """Train the RL Q-table from shadow observations stored in the DB.

    Args:
        max_trades:       Max number of shadow observation records to train on.
        symbol:           If set, restrict training to one symbol (e.g. "BANKNIFTY FUT").
        use_proxy_reward: Use proxy reward based on R:R ratio (True) or actual outcomes (False).
    """
    from auction_intelligence.rl.trainer import train_from_journal

    result = await train_from_journal(
        max_trades=max_trades,
        use_proxy_reward=use_proxy_reward,
        symbol=symbol,
    )
    return result


@router.post("/rl-cycle")
async def rl_cycle(
    max_trades: int | None = None,
    symbol: str | None = None,
    use_proxy_reward: bool | None = None,
    promote_if_eligible: bool = True,
) -> dict:
    """Run the guarded offline RL cycle: train candidate, evaluate, then promote if eligible."""
    from auction_intelligence.rl.automation import rl_auto_trainer

    return await rl_auto_trainer.run_cycle(
        source="manual",
        symbol=symbol,
        max_trades=max_trades,
        use_proxy_reward=use_proxy_reward,
        promote_if_eligible=promote_if_eligible,
    )


@router.get("/rl-versions")
async def rl_versions(limit: int = 20, status: str | None = None) -> dict:
    """List persisted RL policy versions and their promotion status."""
    from auction_intelligence.rl.versions import RLPolicyVersionStore

    store = RLPolicyVersionStore()
    versions = await store.list_versions(limit=limit, status=status)
    active = await store.latest_version(status="active")
    return {
        "count": len(versions),
        "active_version": active,
        "versions": versions,
    }


@router.delete("/rl-reset")
async def rl_reset() -> dict:
    """Wipe the Q-table from DB and in-memory cache. Resets all learning."""
    from auction_intelligence.rl.policy import rl_policy as _rl

    await _rl.reset()
    return {"reset": True, "message": "Q-table wiped. Agent reverts to config defaults."}


# ══════════════════════════════════════════════════════════════════════════════
#  MP-based signal layer — NIFTY / BANKNIFTY / SENSEX
#  These endpoints serve the new MP+Order-Flow strategy dashboard panels.
#  Data sourced from pre-computed enriched_mp_with_failures.csv per underlying.
# ══════════════════════════════════════════════════════════════════════════════

import csv
import gzip
import json
import math
from collections import defaultdict
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
# Primary: compact mp_data/ directory shipped inside the Docker image (1.5 MB)
# Fallback: full runtime/ tree used during local development
_MP_DATA_ROOT = _BACKEND_ROOT / "mp_data"
_DATA_ROOT = _BACKEND_ROOT / "runtime" / "index_analytics_data"

_SUPPORTED_UNDERLYINGS = ("NIFTY", "BANKNIFTY", "SENSEX", "CRUDEOIL")
_MP_DATA_STATUS_CACHE_TTL_SECONDS = 120.0
_mp_data_status_cache: dict[str, object] = {"payload": None, "expires_at": 0.0}
_mp_data_status_cache_lock = asyncio.Lock()
_mp_collection_tasks: dict[str, asyncio.Task] = {}
_mp_collection_tasks_lock = asyncio.Lock()
_AUCTION_LIVE_MP_TIMEOUT_SECONDS = 30.0
_FMP_LIVE_MP_TIMEOUT_SECONDS = 20.0
_DURABLE_DAILY_MP_TIMEFRAME = "auction_daily"
_DURABLE_DAILY_MP_SPOOL_ROOT = _BACKEND_ROOT / "runtime" / "auction_intelligence" / "daily_mp_spool"
_MP_PRICE_BANDS: dict[str, tuple[float, float]] = {
    "NIFTY": (10_000.0, 50_000.0),
    "BANKNIFTY": (20_000.0, 100_000.0),
    "FINNIFTY": (10_000.0, 60_000.0),
    "MIDCPNIFTY": (5_000.0, 40_000.0),
    "SENSEX": (30_000.0, 150_000.0),
}


def _mp_tick_size(underlying: str) -> float:
    normalized = underlying.upper()
    if normalized == "CRUDEOIL":
        return 10.0
    return 0.5


def _mp_enr_path(underlying: str) -> Path:
    # Try compact ship-in-image path first
    compact = _MP_DATA_ROOT / f"underlying={underlying}" / "enriched_mp_with_failures.csv"
    if compact.exists():
        return compact
    # Fallback to full runtime tree
    sub = _DATA_ROOT / "market_profile" / f"underlying={underlying}" / "enriched_mp_with_failures.csv"
    if sub.exists():
        return sub
    # Legacy root-level file is SENSEX only; other symbols should surface as
    # live-only or no-data instead of borrowing the wrong packaged profile.
    if underlying.upper() == "SENSEX":
        return _DATA_ROOT / "market_profile" / "enriched_mp_with_failures.csv"
    return sub


def _mp_params_path(underlying: str) -> Path:
    compact = _MP_DATA_ROOT / f"underlying={underlying}" / "daily_mp_params.csv"
    if compact.exists():
        return compact
    sub = _DATA_ROOT / "market_profile" / f"underlying={underlying}" / "daily_mp_params.csv"
    if sub.exists():
        return sub
    if underlying.upper() == "SENSEX":
        return _DATA_ROOT / "market_profile" / "daily_mp_params.csv"
    return sub


def _spot_path(underlying: str) -> Path:
    return _DATA_ROOT / "spot" / f"underlying={underlying}" / "1minute.csv.gz"


async def _load_spot_status(underlying: str) -> dict:
    """Return 1-minute spot availability from durable DB first, then files."""
    normalized = underlying.upper()
    try:
        from sqlalchemy import text
        from db.database import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    SELECT
                        time AS latest_time,
                        synced_at AS latest_sync,
                        source AS source
                    FROM underlying_spot_candles
                    WHERE underlying = :underlying
                      AND interval = '1minute'
                    ORDER BY time DESC
                    LIMIT 1
                    """
                ),
                {"underlying": normalized},
            )
            row = result.mappings().first()
            latest_time = (row or {}).get("latest_time")
            if latest_time:
                return {
                    "name": f"{normalized} Spot 1-min",
                    "status": "ok",
                    "rows": 1,
                    "last_date": str(latest_time)[:10] if latest_time else "—",
                    "detail": "Latest candle available · source=underlying_spot_candles",
                    "source": "underlying_spot_candles",
                    "latest_time": str(latest_time) if latest_time else None,
                    "latest_sync": str((row or {}).get("latest_sync")) if (row or {}).get("latest_sync") else None,
                    "count_mode": "latest_only",
                }
    except Exception as exc:
        logger.debug(f"[Auction MP] DB spot status unavailable for {normalized}: {exc}")

    sp = _spot_path(normalized)
    if sp.exists():
        import pandas as pd

        df = pd.read_csv(gzip.open(sp, "rt"), usecols=["time"])
        last = str(df["time"].iloc[-1])[:10] if len(df) else "—"
        return {
            "name": f"{normalized} Spot 1-min",
            "status": "ok",
            "rows": len(df),
            "last_date": last,
            "detail": f"{len(df):,} candles · source=packaged_file",
            "source": "packaged_file",
        }

    return {
        "name": f"{normalized} Spot 1-min",
        "status": "missing",
        "rows": 0,
        "last_date": "—",
        "detail": "No DB/file 1-minute spot rows; live MP bridge may still build a daily profile.",
        "source": "none",
    }


def _safe_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def _merge_mp_rows(base_rows: list[dict], extra_rows: list[dict]) -> tuple[list[dict], int]:
    by_date: dict[date, dict] = {}
    order: list[date] = []
    for row in [*base_rows, *extra_rows]:
        row_date = _parse_row_date(row.get("date"))
        if not row_date:
            continue
        if row_date not in by_date:
            order.append(row_date)
        by_date[row_date] = row
    merged = [by_date[row_date] for row_date in sorted(order)]
    base_dates = {_parse_row_date(row.get("date")) for row in base_rows}
    added = sum(1 for row_date in by_date if row_date not in base_dates)
    return merged, added


def _flt(row: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(row.get(key, default))
    except (ValueError, TypeError):
        return default


def _bool(row: dict, key: str) -> bool:
    return str(row.get(key, "")).lower() == "true"


def _parse_row_date(value: str | None) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _price_in_underlying_band(underlying: str, value: object, *, required: bool = True) -> bool:
    band = _MP_PRICE_BANDS.get(underlying.upper())
    try:
        price = float(value or 0.0)
    except (TypeError, ValueError):
        return not required
    if price <= 0:
        return not required
    if not band:
        return True
    return band[0] <= price <= band[1]


def _plausible_mp_row(underlying: str, row: dict | None) -> bool:
    if not row:
        return False
    required_keys = ("open_price", "close_price", "session_high", "session_low", "poc", "vah", "val")
    if not all(_price_in_underlying_band(underlying, row.get(key), required=True) for key in required_keys):
        return False
    high = _flt(row, "session_high")
    low = _flt(row, "session_low")
    close = _flt(row, "close_price")
    vah = _flt(row, "vah")
    val = _flt(row, "val")
    if high < low or not (low <= close <= high):
        return False
    return vah >= val


def _valid_durable_mp_rows(underlying: str, rows: list[dict]) -> list[dict]:
    valid: dict[date, dict] = {}
    for row in rows:
        if not _plausible_mp_row(underlying, row):
            continue
        row_date = _parse_row_date(row.get("date"))
        if not row_date:
            continue
        valid[row_date] = row
    return [valid[row_date] for row_date in sorted(valid)]


def _durable_mp_spool_path(underlying: str) -> Path:
    return _DURABLE_DAILY_MP_SPOOL_ROOT / f"{underlying.upper()}.jsonl"


def _load_spooled_durable_mp_rows(underlying: str) -> list[dict]:
    path = _durable_mp_spool_path(underlying)
    if not path.exists():
        return []

    rows: list[dict] = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                row = payload.get("row") if isinstance(payload, dict) else None
                if isinstance(row, dict):
                    rows.append(row)
    except Exception as exc:
        logger.debug(f"[Auction MP] Durable MP spool read failed for {underlying}: {exc}")
        return []
    return _valid_durable_mp_rows(underlying, rows)


def _spool_durable_mp_rows(underlying: str, rows: list[dict], *, reason: str) -> int:
    valid_rows = _valid_durable_mp_rows(underlying, rows)
    if not valid_rows:
        return 0

    try:
        _DURABLE_DAILY_MP_SPOOL_ROOT.mkdir(parents=True, exist_ok=True)
        path = _durable_mp_spool_path(underlying)
        spooled_at = datetime.now(timezone.utc).isoformat()
        with open(path, "a") as f:
            for row in valid_rows:
                f.write(
                    json.dumps(
                        {
                            "date": row.get("date"),
                            "reason": reason,
                            "spooled_at": spooled_at,
                            "row": row,
                        },
                        default=str,
                    )
                    + "\n"
                )
        return len(valid_rows)
    except Exception as exc:
        logger.warning(f"[Auction MP] Durable MP spool write failed for {underlying}: {exc}")
        return 0


def _drop_spooled_durable_mp_dates(underlying: str, row_dates: set[date]) -> None:
    if not row_dates:
        return
    path = _durable_mp_spool_path(underlying)
    if not path.exists():
        return

    kept_lines: list[str] = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                row = payload.get("row") if isinstance(payload, dict) else None
                row_date = _parse_row_date(row.get("date")) if isinstance(row, dict) else None
                if row_date not in row_dates:
                    kept_lines.append(line)
        if kept_lines:
            with open(path, "w") as f:
                f.writelines(kept_lines)
        else:
            path.unlink(missing_ok=True)
    except Exception as exc:
        logger.debug(f"[Auction MP] Durable MP spool cleanup failed for {underlying}: {exc}")


def _extract_live_failure_scores(snapshot: dict) -> tuple[float, float]:
    buyer_fail = 0.0
    seller_fail = 0.0
    decisions = snapshot.get("analysis", {}).get("agent_decisions", [])
    for decision in decisions:
        metadata = decision.get("metadata", {}) or {}
        buyer_fail = max(buyer_fail, _flt(metadata, "buyer_fail_bin"))
        seller_fail = max(seller_fail, _flt(metadata, "seller_fail_bin"))
    return buyer_fail, seller_fail


def _metric_flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value) > 0
    return bool(value)


def _build_live_mp_row(snapshot: dict) -> dict | None:
    analysis = snapshot.get("analysis", {}) or {}
    profile = analysis.get("market_profile", {}) or {}
    session_date = str(profile.get("session_date") or snapshot.get("session_date") or "").strip()
    if not session_date:
        return None

    open_price = float(profile.get("open_price") or 0.0)
    high_price = float(profile.get("high_price") or 0.0)
    low_price = float(profile.get("low_price") or 0.0)
    close_price = float(profile.get("close_price") or 0.0)
    initial_balance_high = float(profile.get("initial_balance_high") or 0.0)
    initial_balance_low = float(profile.get("initial_balance_low") or 0.0)
    initial_balance_range = float(profile.get("initial_balance_range") or 0.0)
    buyer_fail, seller_fail = _extract_live_failure_scores(snapshot)

    row = {
        "date": session_date,
        "poc": profile.get("poc", 0.0),
        "vah": profile.get("vah", 0.0),
        "val": profile.get("val", 0.0),
        "var": max(float(profile.get("vah") or 0.0) - float(profile.get("val") or 0.0), 0.0),
        "ibh": initial_balance_high,
        "ibl": initial_balance_low,
        "ibr": initial_balance_range,
        "ib_broken_up": float(profile.get("range_extension_up") or 0.0) > 0.0,
        "ib_broken_dn": float(profile.get("range_extension_down") or 0.0) > 0.0,
        "fa_up": False,
        "fa_dn": False,
        "session_high": high_price,
        "session_low": low_price,
        "open_price": open_price,
        "close_price": close_price,
        "total_tpos": profile.get("sample_count", 0),
        "daily_move": close_price - open_price,
        "daily_pct": ((close_price - open_price) / open_price * 100.0) if open_price else 0.0,
        "close_pct_range": ((close_price - low_price) / max(high_price - low_price, 1.0)) if high_price > low_price else 0.5,
        "day_type": "",
        "ib_ext_up_fail": False,
        "ib_ext_dn_fail": False,
        "ib_ext_up_reversal": False,
        "ib_ext_dn_reversal": False,
        "poor_high": bool(profile.get("poor_high")),
        "poor_low": bool(profile.get("poor_low")),
        "excess_high": _metric_flag(profile.get("excess_high")),
        "excess_low": _metric_flag(profile.get("excess_low")),
        "tail_high_buckets": len(list(profile.get("selling_tail") or [])),
        "tail_low_buckets": len(list(profile.get("buying_tail") or [])),
        "buyer_fail_score": buyer_fail,
        "seller_fail_score": seller_fail,
        "net_failure": seller_fail - buyer_fail,
        "next_day_move": "",
        "next_3d_move": "",
    }
    row["day_type"] = _classify_day_type(row)
    return row


def _build_live_mp_row_from_fmp(snapshot: dict, profile_key: str = "daily_profile") -> dict | None:
    profile = snapshot.get(profile_key, {}) or {}
    session_date = str(profile.get("session_date") or snapshot.get("session", {}).get("session_date") or "").strip()
    if not session_date:
        return None

    open_price = float(profile.get("open_price") or 0.0)
    high_price = float(profile.get("high_price") or 0.0)
    low_price = float(profile.get("low_price") or 0.0)
    close_price = float(profile.get("close_price") or snapshot.get("session", {}).get("last_price") or 0.0)
    initial_balance_high = float(profile.get("initial_balance_high") or 0.0)
    initial_balance_low = float(profile.get("initial_balance_low") or 0.0)
    initial_balance_range = float(profile.get("initial_balance_range") or 0.0)
    return {
        "date": session_date,
        "poc": profile.get("poc", 0.0),
        "vah": profile.get("vah", 0.0),
        "val": profile.get("val", 0.0),
        "var": max(float(profile.get("vah") or 0.0) - float(profile.get("val") or 0.0), 0.0),
        "ibh": initial_balance_high,
        "ibl": initial_balance_low,
        "ibr": initial_balance_range,
        "ib_broken_up": float(profile.get("range_extension_up") or 0.0) > 0.0,
        "ib_broken_dn": float(profile.get("range_extension_down") or 0.0) > 0.0,
        "fa_up": False,
        "fa_dn": False,
        "session_high": high_price,
        "session_low": low_price,
        "open_price": open_price,
        "close_price": close_price,
        "total_tpos": profile.get("sample_count", 0),
        "daily_move": close_price - open_price,
        "daily_pct": ((close_price - open_price) / open_price * 100.0) if open_price else 0.0,
        "close_pct_range": ((close_price - low_price) / max(high_price - low_price, 1.0)) if high_price > low_price else 0.5,
        "day_type": profile.get("day_type") or "",
        "ib_ext_up_fail": False,
        "ib_ext_dn_fail": False,
        "ib_ext_up_reversal": False,
        "ib_ext_dn_reversal": False,
        "poor_high": bool(profile.get("poor_high")),
        "poor_low": bool(profile.get("poor_low")),
        "excess_high": _metric_flag(profile.get("excess_high")),
        "excess_low": _metric_flag(profile.get("excess_low")),
        "buyer_fail": 0.0,
        "seller_fail": 0.0,
        "buyer_fail_score": 0.0,
        "seller_fail_score": 0.0,
        "net_failure": 0.0,
        "range_factor": float(profile.get("daily_ib_ratio") or 0.0),
    }


def _build_mp_row_from_bars(normalized: str, bars: list[MarketBar]) -> dict | None:
    if len(bars) < 20:
        return None

    profile = MarketProfileEngine(
        {
            "period_minutes": 30,
            "tick_size": _mp_tick_size(normalized),
            "value_area_pct": 0.70,
            "initial_balance_periods": 2,
        }
    ).build_profile(normalized, bars)

    row = {
        "date": profile.session_date,
        "poc": profile.poc,
        "vah": profile.vah,
        "val": profile.val,
        "var": max(profile.vah - profile.val, 0.0),
        "ibh": profile.initial_balance_high,
        "ibl": profile.initial_balance_low,
        "ibr": profile.initial_balance_range,
        "ib_broken_up": profile.range_extension_up > 0.0,
        "ib_broken_dn": profile.range_extension_down > 0.0,
        "fa_up": False,
        "fa_dn": False,
        "session_high": profile.high_price,
        "session_low": profile.low_price,
        "open_price": profile.open_price,
        "close_price": profile.close_price,
        "total_tpos": sum(profile.tpo_counts.values()),
        "daily_move": profile.close_price - profile.open_price,
        "daily_pct": ((profile.close_price - profile.open_price) / profile.open_price * 100.0) if profile.open_price else 0.0,
        "close_pct_range": (
            (profile.close_price - profile.low_price) / max(profile.high_price - profile.low_price, _mp_tick_size(normalized))
            if profile.high_price > profile.low_price
            else 0.5
        ),
        "day_type": "",
        "ib_ext_up_fail": False,
        "ib_ext_dn_fail": False,
        "ib_ext_up_reversal": False,
        "ib_ext_dn_reversal": False,
        "poor_high": profile.poor_high,
        "poor_low": profile.poor_low,
        "excess_high": _metric_flag(profile.excess_high),
        "excess_low": _metric_flag(profile.excess_low),
        "tail_high_buckets": len(profile.selling_tail),
        "tail_low_buckets": len(profile.buying_tail),
        "buyer_fail_score": 0.0,
        "seller_fail_score": 0.0,
        "net_failure": 0.0,
        "range_factor": profile.day_range / max(profile.initial_balance_range, _mp_tick_size(normalized)),
    }
    row["day_type"] = _classify_day_type(row)
    return row


async def _build_db_spot_mp_rows(underlying: str, *, limit: int = 60) -> list[dict]:
    """Build recent daily MP rows directly from durable 1-minute spot candles."""
    normalized = underlying.upper()
    try:
        from sqlalchemy import text
        from db.database import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    WITH recent_sessions AS (
                        SELECT timezone('Asia/Kolkata', time)::date AS session_date
                        FROM underlying_spot_candles
                        WHERE underlying = :underlying
                          AND interval = '1minute'
                        GROUP BY timezone('Asia/Kolkata', time)::date
                        HAVING COUNT(*) >= 20
                        ORDER BY session_date DESC
                        LIMIT :limit
                    )
                    SELECT timezone('Asia/Kolkata', time)::date AS session_date,
                           time, open, high, low, close, volume
                    FROM underlying_spot_candles
                    WHERE underlying = :underlying
                      AND interval = '1minute'
                      AND timezone('Asia/Kolkata', time)::date IN (SELECT session_date FROM recent_sessions)
                    ORDER BY session_date ASC, time ASC
                    """
                ),
                {"underlying": normalized, "limit": limit},
            )
            db_rows = result.mappings().all()
    except Exception as exc:
        logger.debug(f"[Auction MP] DB spot MP fallback unavailable for {normalized}: {exc}")
        return []

    bars_by_session: dict[date, list[MarketBar]] = defaultdict(list)
    for row in db_rows:
        session_date = row.get("session_date")
        if not isinstance(session_date, date):
            continue
        timestamp = row.get("time")
        if not isinstance(timestamp, datetime):
            continue
        try:
            bars_by_session[session_date].append(
                MarketBar(
                    timestamp=timestamp,
                    open=float(row.get("open") or 0.0),
                    high=float(row.get("high") or 0.0),
                    low=float(row.get("low") or 0.0),
                    close=float(row.get("close") or 0.0),
                    volume=float(row.get("volume") or 0.0),
                )
            )
        except (TypeError, ValueError):
            continue

    mp_rows: list[dict] = []
    for session_date, bars in sorted(bars_by_session.items()):
        try:
            row = _build_mp_row_from_bars(normalized, bars)
            if row and _plausible_mp_row(normalized, row):
                mp_rows.append(row)
        except Exception as exc:
            logger.debug(f"[Auction MP] DB spot MP fallback failed for {normalized} {session_date}: {exc}")
    return mp_rows


async def _build_db_spot_mp_row(underlying: str) -> dict | None:
    rows = await _build_db_spot_mp_rows(underlying, limit=1)
    return rows[-1] if rows else None


async def _collect_live_mp_candidate_rows(
    underlying: str,
) -> tuple[list[tuple[str, dict]], dict[str, object]]:
    status: dict[str, object] = {
        "live_latest_date": None,
        "live_rejected": False,
        "live_bridge": None,
        "live_error": None,
    }
    candidate_rows: list[tuple[str, dict]] = []
    rejected_sources: list[str] = []
    live_snapshot_ok = False

    if underlying.upper() != "CRUDEOIL":
        try:
            live_snapshot = await asyncio.wait_for(
                build_live_analysis(symbol_code=underlying),
                timeout=_AUCTION_LIVE_MP_TIMEOUT_SECONDS,
            )
            live_row = _build_live_mp_row(live_snapshot)
            if _plausible_mp_row(underlying, live_row):
                candidate_rows.append(("live_snapshot", live_row))
                live_snapshot_ok = True
            elif live_row:
                rejected_sources.append("live_snapshot")
        except Exception as exc:
            status["live_error"] = str(exc)

    if not live_snapshot_ok:
        try:
            from fractal_market_profile.service import fmp_service

            fmp_snapshot = await asyncio.wait_for(
                fmp_service.live_snapshot(underlying),
                timeout=_FMP_LIVE_MP_TIMEOUT_SECONDS,
            )
            for profile_key in ("prior_daily_profile", "daily_profile"):
                fmp_row = _build_live_mp_row_from_fmp(fmp_snapshot, profile_key=profile_key)
                if _plausible_mp_row(underlying, fmp_row):
                    candidate_rows.append((f"fractal_market_profile:{profile_key}", fmp_row))
                elif fmp_row:
                    rejected_sources.append(f"fractal_market_profile:{profile_key}")
        except Exception as exc:
            if not status["live_error"]:
                status["live_error"] = str(exc)
            else:
                status["live_fmp_error"] = str(exc)

    db_spot_row = await _build_db_spot_mp_row(underlying)
    if _plausible_mp_row(underlying, db_spot_row):
        candidate_rows.append(("db_spot_1minute_profile", db_spot_row))
    elif db_spot_row:
        rejected_sources.append("db_spot_1minute_profile")

    if rejected_sources:
        status["live_rejected"] = True
        status["live_rejected_sources"] = rejected_sources

    if candidate_rows:
        status["live_bridge"] = [source_name for source_name, _ in candidate_rows]
        latest_candidate_date = max(
            (
                row_date
                for _, candidate in candidate_rows
                if (row_date := _parse_row_date(candidate.get("date"))) is not None
            ),
            default=None,
        )
        if latest_candidate_date:
            status["live_latest_date"] = latest_candidate_date.isoformat()

    return candidate_rows, status


async def _load_durable_mp_rows(underlying: str) -> list[dict]:
    """Load live-appended daily MP rows persisted in Timescale/Postgres."""
    normalized = underlying.upper()
    spooled_rows = _load_spooled_durable_mp_rows(normalized)
    try:
        from sqlalchemy import text
        from db.database import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    SELECT DISTINCT ON ((tpo_data->>'date'))
                        tpo_data
                    FROM market_profiles
                    WHERE symbol = :symbol
                      AND timeframe = :timeframe
                      AND tpo_data ? 'date'
                    ORDER BY (tpo_data->>'date') ASC, time DESC
                    """
                ),
                {"symbol": normalized, "timeframe": _DURABLE_DAILY_MP_TIMEFRAME},
            )
            payloads = [row[0] for row in result.all()]
    except Exception as exc:
        logger.debug(f"[Auction MP] Durable daily MP cache unavailable for {normalized}: {exc}")
        return spooled_rows

    rows: list[dict] = []
    for payload in payloads:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                continue
        if isinstance(payload, dict) and _plausible_mp_row(normalized, payload):
            rows.append(payload)
    merged_rows, _ = _merge_mp_rows(rows, spooled_rows)
    return merged_rows


async def _persist_durable_mp_rows(underlying: str, rows: list[dict]) -> int:
    """Persist live/db/FMP daily MP rows so future cold calls do not regress."""
    normalized = underlying.upper()
    valid_rows = _valid_durable_mp_rows(normalized, rows)
    spooled_rows = _load_spooled_durable_mp_rows(normalized)
    rows, _ = _merge_mp_rows(spooled_rows, valid_rows)
    payloads: list[dict] = []
    for row in rows:
        row_date = _parse_row_date(row.get("date"))
        if not row_date:
            continue
        payloads.append(
            {
                "time": datetime.combine(row_date, time(15, 30)),
                "poc": _flt(row, "poc"),
                "vah": _flt(row, "vah"),
                "val": _flt(row, "val"),
                "ib_high": _flt(row, "ibh"),
                "ib_low": _flt(row, "ibl"),
                "payload": json.dumps(row, default=str),
            }
        )

    if not payloads:
        return 0

    persisted_dates = {
        row_date
        for row in rows
        if (row_date := _parse_row_date(row.get("date"))) is not None
    }

    try:
        from sqlalchemy import text
        from db.database import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    """
                    DELETE FROM market_profiles
                    WHERE symbol = :symbol
                      AND timeframe = :timeframe
                      AND time = :time
                    """
                ),
                [
                    {
                        "symbol": normalized,
                        "timeframe": _DURABLE_DAILY_MP_TIMEFRAME,
                        "time": payload["time"],
                    }
                    for payload in payloads
                ],
            )
            await session.execute(
                text(
                    """
                    INSERT INTO market_profiles
                        (time, symbol, timeframe, poc, vah, val, ib_high, ib_low, tpo_data)
                    VALUES
                        (:time, :symbol, :timeframe, :poc, :vah, :val, :ib_high, :ib_low, CAST(:payload AS JSONB))
                    """
                ),
                [
                    {
                        "time": payload["time"],
                        "symbol": normalized,
                        "timeframe": _DURABLE_DAILY_MP_TIMEFRAME,
                        "poc": payload["poc"],
                        "vah": payload["vah"],
                        "val": payload["val"],
                        "ib_high": payload["ib_high"],
                        "ib_low": payload["ib_low"],
                        "payload": payload["payload"],
                    }
                    for payload in payloads
                ],
            )
            await session.commit()
            _drop_spooled_durable_mp_dates(normalized, persisted_dates)
            return len(payloads)
    except Exception as exc:
        logger.warning(f"[Auction MP] Failed to persist durable daily MP cache for {normalized}: {exc}")
        _spool_durable_mp_rows(normalized, valid_rows, reason="postgres_unavailable")
        return 0


async def _refresh_durable_mp_collection(underlying: str, *, reason: str) -> dict[str, object]:
    normalized = underlying.upper()
    candidate_rows, live_status = await _collect_live_mp_candidate_rows(normalized)
    candidate_payloads = [row for _, row in candidate_rows]
    persisted = await _persist_durable_mp_rows(normalized, candidate_payloads)
    return {
        "underlying": normalized,
        "reason": reason,
        "candidate_rows": len(candidate_payloads),
        "durable_persisted": persisted,
        **live_status,
    }


async def _ensure_mp_collection_task(underlying: str, *, reason: str) -> None:
    normalized = underlying.upper()

    def _finalize(task: asyncio.Task) -> None:
        _mp_collection_tasks.pop(normalized, None)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(f"[Auction MP] Background durable collection failed for {normalized}: {exc}")

    async with _mp_collection_tasks_lock:
        current = _mp_collection_tasks.get(normalized)
        if current and not current.done():
            return
        try:
            task = asyncio.create_task(
                _refresh_durable_mp_collection(normalized, reason=reason),
                name=f"auction-mp-collect-{normalized}",
            )
            task.add_done_callback(_finalize)
            _mp_collection_tasks[normalized] = task
        except RuntimeError:
            logger.debug(f"[Auction MP] No running loop; skipped background collection for {normalized}")


async def _load_mp_rows(
    underlying: str,
    *,
    allow_params_fallback: bool = False,
    live_refresh: bool = True,
) -> tuple[list[dict], dict[str, object]]:
    source = "enriched_mp_with_failures"
    rows = _safe_csv(_mp_enr_path(underlying))
    if not rows and allow_params_fallback:
        rows = _safe_csv(_mp_params_path(underlying))
        source = "daily_mp_params"

    packaged_latest = rows[-1].get("date") if rows else None
    durable_rows = await _load_durable_mp_rows(underlying)
    durable_added = 0
    if durable_rows:
        rows, durable_added = _merge_mp_rows(rows, durable_rows)
        if durable_added:
            source = f"{source}+durable_cache"

    db_spot_added = 0
    if len(rows) < 30 and not live_refresh:
        db_spot_rows = await _build_db_spot_mp_rows(underlying, limit=90)
        if db_spot_rows:
            rows, db_spot_added = _merge_mp_rows(rows, db_spot_rows)
            if db_spot_added:
                source = f"{source}+db_spot_cache"
                await _persist_durable_mp_rows(underlying, db_spot_rows)

    latest_row_date = rows[-1].get("date") if rows else packaged_latest
    status: dict[str, object] = {
        "source": source,
        "packaged_latest_date": packaged_latest,
        "latest_date": latest_row_date,
        "live_appended": False,
        "live_refreshed": False,
        "durable_appended": durable_added > 0,
        "db_spot_appended": db_spot_added > 0,
        "durable_merged": bool(durable_rows),
        "durable_rows": len(durable_rows),
        "db_spot_rows": db_spot_added,
        "live_latest_date": None,
        "live_rejected": False,
        "live_bridge": None,
        "live_error": None,
        "collection_mode": "blocking" if live_refresh else "background",
    }

    if not live_refresh:
        await _ensure_mp_collection_task(underlying, reason="lightweight_read")
        packaged_latest_date = _parse_row_date(packaged_latest)
        status["stale_days"] = None
        if packaged_latest_date:
            status["stale_days"] = max((date.today() - packaged_latest_date).days, 0)
        return rows, status

    try:
        candidate_rows, live_status = await asyncio.wait_for(
            _collect_live_mp_candidate_rows(underlying),
            timeout=5.0,
        )
    except asyncio.TimeoutError:
        candidate_rows = []
        live_status = {
            "live_rejected": True,
            "live_bridge": [],
            "live_error": "live MP collection timed out; using durable/cache rows",
        }
    status.update(live_status)

    if not candidate_rows:
        packaged_latest_date = _parse_row_date(packaged_latest)
        status["stale_days"] = None
        if packaged_latest_date:
            status["stale_days"] = max((date.today() - packaged_latest_date).days, 0)
        return rows, status

    packaged_latest_date = _parse_row_date(packaged_latest)
    existing_dates = {_parse_row_date(row.get("date")) for row in rows}
    latest_existing_date = max((row_date for row_date in existing_dates if row_date), default=packaged_latest_date)
    appended_sources: list[str] = []
    refreshed_sources: list[str] = []
    latest_candidate_date: date | None = None
    persisted = await _persist_durable_mp_rows(
        underlying,
        [candidate for _, candidate in candidate_rows],
    )
    status["durable_persisted"] = persisted

    for source_name, candidate in sorted(
        candidate_rows,
        key=lambda item: str(item[1].get("date", "")),
    ):
        candidate_date = _parse_row_date(candidate.get("date"))
        if not candidate_date:
            continue
        latest_candidate_date = max(latest_candidate_date, candidate_date) if latest_candidate_date else candidate_date
        if candidate_date in existing_dates:
            rows, _ = _merge_mp_rows(rows, [candidate])
            refreshed_sources.append(source_name)
            latest_refreshed_date = max(
                (row_date for row in rows if (row_date := _parse_row_date(row.get("date"))) is not None),
                default=candidate_date,
            )
            status["latest_date"] = latest_refreshed_date.isoformat()
            continue
        if latest_existing_date is not None and candidate_date <= latest_existing_date:
            continue
        rows = [*rows, candidate]
        existing_dates.add(candidate_date)
        latest_existing_date = candidate_date
        appended_sources.append(source_name)
        status["latest_date"] = candidate.get("date")

    rows = sorted(rows, key=lambda row: str(row.get("date") or ""))

    if appended_sources:
        source_suffix = "live_snapshot" if appended_sources == ["live_snapshot"] else "live_bridge"
        status["source"] = f"{source}+{source_suffix}"
        status["live_appended"] = True
        status["live_bridge"] = appended_sources
        status["live_latest_date"] = rows[-1].get("date")
    elif refreshed_sources:
        status["source"] = f"{source}+live_refresh"
        status["live_refreshed"] = True
        status["live_bridge"] = refreshed_sources
        status["live_latest_date"] = rows[-1].get("date")
    elif latest_candidate_date:
        status["live_latest_date"] = latest_candidate_date.isoformat()

    final_latest_date = _parse_row_date(str(status.get("latest_date") or ""))
    if packaged_latest_date and final_latest_date:
        status["stale_days"] = max((final_latest_date - packaged_latest_date).days, 0)
    else:
        status["stale_days"] = None

    return rows, status


def _classify_day_type(r: dict) -> str:
    fa_up = str(r.get("fa_up", "")).lower() == "true"
    fa_dn = str(r.get("fa_dn", "")).lower() == "true"
    ib_up = str(r.get("ib_broken_up", "")).lower() == "true"
    ib_dn = str(r.get("ib_broken_dn", "")).lower() == "true"
    # Use pre-computed day_type if available
    dt = r.get("day_type", "")
    if dt and dt not in ("", "UNKNOWN"):
        return dt
    # Fall back to derivation
    sh = _flt(r, "session_high")
    sl = _flt(r, "session_low")
    ibr = _flt(r, "ibr")
    close = _flt(r, "close_price") or _flt(r, "close")
    sr = sh - sl
    if sr <= 0 or ibr <= 0:
        return "UNKNOWN"
    rr = sr / ibr
    cp = (close - sl) / sr if sr > 0 else 0.5
    if ib_up != ib_dn and rr >= 2.0:
        if ib_up and cp >= 0.70:
            return "TREND_UP"
        if ib_dn and cp <= 0.30:
            return "TREND_DN"
    if ib_up and ib_dn and rr >= 1.5:
        return "DOUBLE_DIST"
    if ib_up != ib_dn and rr >= 1.2:
        return "NORMAL_VAR_UP" if ib_up else "NORMAL_VAR_DN"
    if fa_up or fa_dn:
        return "FAILED_AUCTION"
    return "NORMAL"


def _signal_direction(day_type: str, buyer_fail: float, seller_fail: float) -> str:
    if buyer_fail >= 4 and seller_fail < 2:
        return "PE"
    if seller_fail >= 4 and buyer_fail < 2:
        return "CE"
    if day_type == "TREND_UP":
        return "CE"
    if day_type == "TREND_DN":
        return "PE"
    if buyer_fail >= 2 and seller_fail >= 2:
        return "CONFLICT"
    return "NEUTRAL"


def _signal_strength(direction: str, buyer_fail: float, seller_fail: float) -> str:
    if direction == "CONFLICT":
        return "conflict"
    if direction in {"CE", "PE"} and max(buyer_fail, seller_fail) >= 4:
        return "strong"
    if direction in {"CE", "PE"}:
        return "moderate"
    return "neutral"


def _build_mp_signal_record(row: dict) -> dict:
    buyer_fail = _flt(row, "buyer_fail_score")
    seller_fail = _flt(row, "seller_fail_score")
    day_type = _classify_day_type(row)
    direction = _signal_direction(day_type, buyer_fail, seller_fail)
    close = _flt(row, "close_price") or _flt(row, "close")
    vah = _flt(row, "vah")
    val = _flt(row, "val")
    poc = _flt(row, "poc")
    session_high = _flt(row, "session_high")
    session_low = _flt(row, "session_low")
    ibr = _flt(row, "ibr")
    session_range = max(session_high - session_low, 0.0)
    close_location = (
        (close - session_low) / session_range
        if session_range > 0
        else 0.5
    )
    value_shift = close - poc
    inside_value = val <= close <= vah if vah >= val else False
    above_value = close > vah if vah >= val else False
    below_value = close < val if vah >= val else False

    return {
        "date": row.get("date", ""),
        "day_type": day_type,
        "direction": direction,
        "signal_strength": _signal_strength(direction, buyer_fail, seller_fail),
        "poc": poc,
        "vah": vah,
        "val": val,
        "ibh": _flt(row, "ibh"),
        "ibl": _flt(row, "ibl"),
        "ibr": ibr,
        "session_high": session_high,
        "session_low": session_low,
        "day_range": round(session_range, 2),
        "buyer_fail": buyer_fail,
        "seller_fail": seller_fail,
        "net_failure": round(seller_fail - buyer_fail, 4),
        "close": close,
        "daily_move": _flt(row, "daily_move"),
        "daily_pct": _flt(row, "daily_pct"),
        "close_location": round(close_location, 4),
        "value_shift": round(value_shift, 2),
        "range_factor": round(session_range / max(ibr, 1.0), 4) if session_range > 0 else 0.0,
        "inside_value": inside_value,
        "above_value": above_value,
        "below_value": below_value,
        "poor_high": _bool(row, "poor_high"),
        "poor_low": _bool(row, "poor_low"),
        "fa_up": _bool(row, "fa_up"),
        "fa_dn": _bool(row, "fa_dn"),
    }


def _build_mp_open_signal_payload(underlying: str, latest: dict) -> dict:
    buyer_fail = _flt(latest, "buyer_fail_score")
    seller_fail = _flt(latest, "seller_fail_score")
    day_type = _classify_day_type(latest)
    signal_date = latest.get("date", "")

    direction = None
    strength = "base"
    reason = day_type
    fa_up = _bool(latest, "fa_up")
    fa_dn = _bool(latest, "fa_dn")

    if day_type == "TREND_UP":
        direction, strength = "CE", "strong"
    elif day_type == "TREND_DN":
        direction, strength = "PE", "strong"
    elif day_type == "NORMAL_VAR_UP":
        direction = "CE"
    elif day_type == "NORMAL_VAR_DN":
        direction = "PE"
    elif day_type == "FAILED_AUCTION":
        if fa_up and not fa_dn:
            direction, reason = "PE", "FA_UP"
        elif fa_dn and not fa_up:
            direction, reason = "CE", "FA_DN"

    if buyer_fail >= 4 and seller_fail < 2:
        direction, strength, reason = "PE", "strong", f"{reason}+BF{buyer_fail:.0f}"
    elif seller_fail >= 4 and buyer_fail < 2:
        direction, strength, reason = "CE", "strong", f"{reason}+SF{seller_fail:.0f}"

    if buyer_fail >= 2 and seller_fail >= 2 and day_type not in ("TREND_UP", "TREND_DN"):
        direction = None
        reason = f"{reason}+CONFLICT"

    allocation = 0.35 if strength == "strong" else 0.20
    signals: list[dict] = []
    if direction:
        signals.append(
            {
                "signal_date": signal_date,
                "trade_date": "next session",
                "underlying": underlying,
                "direction": direction,
                "reason": reason,
                "strength": strength,
                "alloc": allocation,
                "buyer_fail": buyer_fail,
                "seller_fail": seller_fail,
                "day_type": day_type,
                "status": "pending_vwap_confirm",
                "instruction": (
                    f"Enter {direction} ATM when premium > VWAP after 09:30 IST. "
                    f"60-min grace period before VWAP stop activates. "
                    f"Hard SL at -50%. Target +50%."
                ),
            }
        )

    return {
        "as_of": signal_date,
        "underlying": underlying,
        "signals": signals,
        "skip_reason": reason if not direction else None,
    }


async def _build_mp_rag_context(
    underlying: str,
    latest: dict,
    *,
    open_signal_payload: dict | None = None,
    orderflow_proxy: dict | None = None,
) -> dict:
    signal_record = _build_mp_signal_record(latest)
    open_signal = (open_signal_payload or {}).get("signals", [{}])
    primary_signal = open_signal[0] if open_signal else {}
    direction = primary_signal.get("direction") or signal_record.get("direction")
    setup_name = primary_signal.get("reason") or signal_record.get("day_type")
    orderflow_summary = (orderflow_proxy or {}).get("summary") or {}
    return await _safe_context_gate(
        ContextGateRequest(
            strategy_key="auction_intelligence",
            underlying=underlying.upper(),
            symbol=f"{underlying.upper()} FUT",
            signal_direction=direction,
            setup_name=setup_name,
            regime=signal_record.get("day_type"),
            event_tags=[
                "mp_composite",
                "orderflow_proxy",
                "options_flow_proxy",
                "context_gate",
            ],
            numeric_context={
                "date": signal_record.get("date"),
                "close": signal_record.get("close"),
                "poc": signal_record.get("poc"),
                "vah": signal_record.get("vah"),
                "val": signal_record.get("val"),
                "daily_move": signal_record.get("daily_move"),
                "buyer_fail": signal_record.get("buyer_fail"),
                "seller_fail": signal_record.get("seller_fail"),
                "net_failure": signal_record.get("net_failure"),
                "close_location": signal_record.get("close_location"),
                "range_factor": signal_record.get("range_factor"),
                "current_cvd": (orderflow_proxy or {}).get("current_cvd"),
                "cvd_divergences": len((orderflow_proxy or {}).get("divergences") or []),
                "total_bull_days": orderflow_summary.get("total_bull_days"),
                "total_bear_days": orderflow_summary.get("total_bear_days"),
            },
            hard_risk_passed=True,
        )
    )


async def _build_mp_rag_context_fast(
    underlying: str,
    latest: dict,
    *,
    open_signal_payload: dict | None = None,
    orderflow_proxy: dict | None = None,
    timeout_seconds: float = 3.0,
) -> dict:
    try:
        return await asyncio.wait_for(
            _build_mp_rag_context(
                underlying,
                latest,
                open_signal_payload=open_signal_payload,
                orderflow_proxy=orderflow_proxy,
            ),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        return {
            "decision": "warn",
            "confidence": 0.0,
            "summary": "RAG context timed out; MP numeric signal returned without blocking the dashboard.",
            "reason_codes": ["rag_timeout"],
            "case_stats": {"matched_cases": 0, "resolved_cases": 0},
            "retrievals": [],
            "audit_bundle": {},
        }


def _build_mp_agent_context_payload(underlying: str, latest: dict, *, limit: int = 10) -> list[dict]:
    comments: list[dict] = []
    buyer_fail = _flt(latest, "buyer_fail_score")
    seller_fail = _flt(latest, "seller_fail_score")
    signal_date = latest.get("date", "")
    move = _flt(latest, "daily_move")
    day_type = _classify_day_type(latest)

    comments.append(
        {
            "time": signal_date,
            "type": "day_summary",
            "level": "info",
            "message": (
                f"{signal_date}: {day_type} day on {underlying}. "
                f"Move: {move:+.0f} pts. "
                f"Buyer fail={buyer_fail:.0f}, Seller fail={seller_fail:.0f}."
            ),
        }
    )

    fa_up = _bool(latest, "fa_up")
    fa_dn = _bool(latest, "fa_dn")
    close = _flt(latest, "close_price")
    ibh = _flt(latest, "ibh")
    ibl = _flt(latest, "ibl")

    if fa_up:
        comments.append(
            {
                "time": signal_date,
                "type": "auction",
                "level": "bearish",
                "message": (
                    f"Failed Auction UP — {underlying} broke IB high (₹{ibh:.0f}) "
                    f"but closed at ₹{close:.0f}. Buyers exhausted. PE bias for next session."
                ),
            }
        )
    if fa_dn:
        comments.append(
            {
                "time": signal_date,
                "type": "auction",
                "level": "bullish",
                "message": (
                    f"Failed Auction DOWN — {underlying} broke IB low (₹{ibl:.0f}) "
                    f"but closed at ₹{close:.0f}. Sellers rejected. CE bias for next session."
                ),
            }
        )

    direction = _signal_direction(day_type, buyer_fail, seller_fail)
    if direction == "PE":
        comments.append(
            {
                "time": signal_date,
                "type": "signal",
                "level": "bearish",
                "message": f"Strong buyer failure (score {buyer_fail:.0f}). PE entry next session — wait for ATM premium > VWAP.",
            }
        )
    elif direction == "CE":
        comments.append(
            {
                "time": signal_date,
                "type": "signal",
                "level": "bullish",
                "message": f"Strong seller failure (score {seller_fail:.0f}). CE entry next session — wait for ATM premium > VWAP.",
            }
        )
    elif direction == "CONFLICT":
        comments.append(
            {
                "time": signal_date,
                "type": "signal",
                "level": "warning",
                "message": f"CONFLICT — BF={buyer_fail:.0f} and SF={seller_fail:.0f} both elevated. Choppy auction. Skip or reduce size.",
            }
        )
    else:
        comments.append(
            {
                "time": signal_date,
                "type": "signal",
                "level": "neutral",
                "message": "Balanced auction. No strong failure signal. Await clearer MP structure before entry.",
            }
        )

    if _bool(latest, "poor_high"):
        comments.append(
            {
                "time": signal_date,
                "type": "profile",
                "level": "bearish",
                "message": f"Poor High on {underlying} — single-print at top. No buyer acceptance. Likely to revisit.",
            }
        )
    if _bool(latest, "poor_low"):
        comments.append(
            {
                "time": signal_date,
                "type": "profile",
                "level": "bullish",
                "message": f"Poor Low on {underlying} — single-print at bottom. No seller acceptance. Likely to revisit.",
            }
        )

    return comments[-limit:]


@router.get("/mp-data-status")
async def mp_data_status() -> list[dict]:
    """Pipeline health for the MP+Order-Flow strategy — per supported underlying."""
    cached = _mp_data_status_cache.get("payload")
    if isinstance(cached, list) and float(_mp_data_status_cache.get("expires_at") or 0.0) > monotonic():
        return cached

    async with _mp_data_status_cache_lock:
        cached = _mp_data_status_cache.get("payload")
        if isinstance(cached, list) and float(_mp_data_status_cache.get("expires_at") or 0.0) > monotonic():
            return cached

        sources = await _build_mp_data_status()
        _mp_data_status_cache["payload"] = sources
        _mp_data_status_cache["expires_at"] = monotonic() + _MP_DATA_STATUS_CACHE_TTL_SECONDS
        return sources


async def _build_mp_data_status() -> list[dict]:
    """Build uncached MP pipeline health for the readiness panel."""
    sources: list[dict] = []

    for ul in _SUPPORTED_UNDERLYINGS:
        # Spot candles
        sources.append(await _load_spot_status(ul))

        # Daily MP params / enriched rows. Use the same live bridge as the
        # strategy endpoints so the readiness panel reflects broker-backed FMP
        # snapshots, not only static files shipped in the container.
        mp_rows, data_status = await _load_mp_rows(ul, allow_params_fallback=True, live_refresh=False)
        source = str(data_status.get("source") or "none")
        stale_days = data_status.get("stale_days")
        live_bridge = data_status.get("live_bridge") or []
        live_appended = bool(data_status.get("live_appended"))
        live_refreshed = bool(data_status.get("live_refreshed"))
        durable_appended = bool(data_status.get("durable_appended"))
        db_spot_appended = bool(data_status.get("db_spot_appended"))
        latest_date = str(data_status.get("latest_date") or "—")
        if mp_rows:
            status = "ok"
            if not live_appended and not live_refreshed and not durable_appended and not db_spot_appended and isinstance(stale_days, int) and stale_days > 7:
                status = "warning"
            detail_parts = [f"{len(mp_rows)} sessions", f"source={source}"]
            if live_bridge:
                detail_parts.append(f"live_bridge={','.join(str(item) for item in live_bridge)}")
            if live_refreshed:
                detail_parts.append("live_refreshed=true")
            if durable_appended:
                detail_parts.append(f"durable_cache={data_status.get('durable_rows')}")
            if db_spot_appended:
                detail_parts.append(f"db_spot_cache={data_status.get('db_spot_rows')}")
            if isinstance(stale_days, int) and stale_days > 0:
                detail_parts.append(f"packaged_lag={stale_days}d")
            sources.append({
                "name": f"{ul} Daily MP",
                "status": status,
                "rows": len(mp_rows),
                "last_date": latest_date,
                "detail": " · ".join(detail_parts),
                "source": source,
                "live_appended": live_appended,
                "live_refreshed": live_refreshed,
                "durable_appended": durable_appended,
                "db_spot_appended": db_spot_appended,
                "durable_rows": data_status.get("durable_rows"),
                "db_spot_rows": data_status.get("db_spot_rows"),
                "live_latest_date": data_status.get("live_latest_date"),
            })
        else:
            detail = "Fetch via broker/FMP live bridge"
            if data_status.get("live_error"):
                detail = f"{detail}; last error: {data_status.get('live_error')}"
            sources.append({
                "name": f"{ul} Daily MP",
                "status": "missing",
                "rows": 0,
                "last_date": "—",
                "detail": detail,
                "source": source,
                "live_appended": False,
            })

        # Failure-score availability is separate from MP availability because
        # FMP live profiles can keep the auction page usable even when the
        # historical buyer/seller failure-score feature set is not packaged.
        failure_rows = [
            row
            for row in mp_rows
            if row.get("buyer_fail_score") not in {None, ""}
            or row.get("seller_fail_score") not in {None, ""}
        ]
        if failure_rows:
            sources.append({
                "name": f"{ul} Failure Scores",
                "status": "ok",
                "rows": len(failure_rows),
                "last_date": failure_rows[-1].get("date", latest_date),
                "detail": f"Buyer/seller scores available · source={source}",
                "source": source,
            })
        elif mp_rows:
            sources.append({
                "name": f"{ul} Failure Scores",
                "status": "warning",
                "rows": 0,
                "last_date": latest_date,
                "detail": "MP profile available, but buyer/seller failure scores are unavailable in the current source.",
                "source": source,
            })
        else:
            sources.append({
                "name": f"{ul} Failure Scores",
                "status": "missing",
                "rows": 0,
                "last_date": "—",
                "detail": "No MP rows available for failure-score features.",
                "source": source,
            })

    return sources


@router.get("/mp-signals")
async def mp_signals(underlying: str = "NIFTY", limit: int = 20) -> dict:
    """Recent MP day signals with failure scores and direction for the given underlying."""
    rows, data_status = await _load_mp_rows(underlying, live_refresh=False)
    if not rows:
        return {
            "underlying": underlying,
            "signals": [],
            "message": f"No MP data for {underlying}",
            "data_status": data_status,
        }

    signals = [_build_mp_signal_record(row) for row in rows[-limit:]]

    return {
        "underlying": underlying,
        "signals": signals,
        "latest": signals[-1] if signals else None,
        "data_status": data_status,
    }


@router.get("/mp-open-signal")
async def mp_open_signal(underlying: str = "NIFTY") -> dict:
    """
    Next-session actionable signal for the MP+Order-Flow strategy.
    Direction from: day_type + IB extension + failure scores.
    Entry method: wait for price > VWAP after 09:30; VWAP stop with 60-min grace; hard SL -50%.
    """
    rows, data_status = await _load_mp_rows(underlying)
    if not rows:
        return {
            "underlying": underlying,
            "signals": [],
            "skip_reason": f"No MP data for {underlying}",
            "data_status": data_status,
        }
    payload = _build_mp_open_signal_payload(underlying, rows[-1])
    payload["data_status"] = data_status
    payload["rag_context"] = await _build_mp_rag_context_fast(
        underlying,
        rows[-1],
        open_signal_payload=payload,
    )
    return payload


@router.get("/mp-agent-context")
async def mp_agent_context(underlying: str = "NIFTY", limit: int = 10) -> list[dict]:
    """
    Contextual agent reasoning for the MP+Order-Flow strategy.
    Returns structured comments explaining the latest MP structure.
    """
    rows, _ = await _load_mp_rows(underlying, live_refresh=False)
    if not rows:
        return [{"time": "", "type": "system", "level": "warning",
                 "message": f"No MP failure data found for {underlying}."}]
    return _build_mp_agent_context_payload(underlying, rows[-1], limit=limit)


@router.get("/mp-dashboard")
async def mp_dashboard(
    underlying: str = "NIFTY",
    lookback: int = Query(30, ge=5, le=120),
) -> dict:
    """Aggregated MP structure, failure-pressure history, and next-session bias."""
    rows, data_status = await _load_mp_rows(underlying)
    if not rows:
        return {
            "underlying": underlying,
            "lookback": lookback,
            "overview": {},
            "structure_summary": {},
            "day_type_distribution": [],
            "direction_distribution": [],
            "sessions": [],
            "latest": None,
            "open_signal": None,
            "skip_reason": f"No MP data for {underlying}",
            "data_status": data_status,
            "context": [
                {
                    "time": "",
                    "type": "system",
                    "level": "warning",
                    "message": f"No MP failure data found for {underlying}.",
                }
            ],
        }

    sessions = [_build_mp_signal_record(row) for row in rows[-lookback:]]
    latest_row = rows[-1]
    open_signal_payload = _build_mp_open_signal_payload(underlying, latest_row)
    rag_context = await _build_mp_rag_context_fast(
        underlying,
        latest_row,
        open_signal_payload=open_signal_payload,
    )

    day_type_distribution = [
        {"day_type": day_type, "count": count}
        for day_type, count in Counter(session["day_type"] for session in sessions).most_common()
    ]
    direction_distribution = [
        {"direction": direction, "count": count}
        for direction, count in Counter(session["direction"] for session in sessions).most_common()
    ]

    session_count = len(sessions)
    average_divisor = max(session_count, 1)
    overview = {
        "session_count": session_count,
        "strong_signal_count": sum(1 for session in sessions if session["signal_strength"] == "strong"),
        "conflict_count": sum(1 for session in sessions if session["direction"] == "CONFLICT"),
        "ce_count": sum(1 for session in sessions if session["direction"] == "CE"),
        "pe_count": sum(1 for session in sessions if session["direction"] == "PE"),
        "neutral_count": sum(1 for session in sessions if session["direction"] == "NEUTRAL"),
        "avg_buyer_fail": round(sum(float(session["buyer_fail"]) for session in sessions) / average_divisor, 2),
        "avg_seller_fail": round(sum(float(session["seller_fail"]) for session in sessions) / average_divisor, 2),
        "avg_net_failure": round(sum(float(session["net_failure"]) for session in sessions) / average_divisor, 2),
        "avg_range_factor": round(sum(float(session["range_factor"]) for session in sessions) / average_divisor, 2),
        "avg_close_location": round(sum(float(session["close_location"]) for session in sessions) / average_divisor, 4),
        "value_acceptance_ratio": round(
            sum(1 for session in sessions if bool(session["inside_value"])) / average_divisor,
            4,
        ),
    }
    structure_summary = {
        "above_value_count": sum(1 for session in sessions if bool(session["above_value"])),
        "inside_value_count": sum(1 for session in sessions if bool(session["inside_value"])),
        "below_value_count": sum(1 for session in sessions if bool(session["below_value"])),
        "poor_high_count": sum(1 for session in sessions if bool(session["poor_high"])),
        "poor_low_count": sum(1 for session in sessions if bool(session["poor_low"])),
        "failed_auction_count": sum(
            1 for session in sessions if bool(session["fa_up"]) or bool(session["fa_dn"])
        ),
    }

    return {
        "underlying": underlying,
        "lookback": lookback,
        "overview": overview,
        "structure_summary": structure_summary,
        "day_type_distribution": day_type_distribution,
        "direction_distribution": direction_distribution,
        "sessions": sessions,
        "latest": sessions[-1] if sessions else None,
        "open_signal": open_signal_payload["signals"][0] if open_signal_payload["signals"] else None,
        "skip_reason": open_signal_payload["skip_reason"],
        "data_status": data_status,
        "context": _build_mp_agent_context_payload(underlying, latest_row, limit=6),
        "rag_context": rag_context,
    }


# ---------------------------------------------------------------------------
# MP Intelligence Analytics — multi-TF profiles, regime history, setup perf
# ---------------------------------------------------------------------------

_mp_analytics_engine = MPAnalyticsEngine()


@router.get("/mp-analytics")
async def mp_analytics(
    underlying: str = Query("NIFTY"),
    lookback: int = Query(60, ge=10, le=250),
    composite_20d: bool = Query(True),
    composite_50d: bool = Query(True),
) -> dict:
    """
    Full MP Intelligence bundle:
    - Composite (20d / 50d) multi-timeframe profiles
    - Weekly profile aggregates
    - Value migration trend (POC shift, VA center, VA width over time)
    - Regime history (day-type sequence, transition matrix, streaks)
    - Setup performance matrix (day_type × direction → win rate, expectancy)
    - Concept drift detection (Page-Hinkley on rolling win rate)
    - Orderflow proxy CVD series
    """
    rows, data_status = await _load_mp_rows(underlying, allow_params_fallback=True)
    if not rows:
        return {
            "underlying": underlying,
            "error": f"No MP data found for {underlying}",
            "profiles": {},
            "weekly_profiles": [],
            "value_migration": {"sessions": [], "summary": {}},
            "regime_history": {"sessions": [], "distribution": [], "transition_matrix": {}, "streaks": []},
            "setup_performance": {"total_signals": 0, "cells": [], "calibration": []},
            "concept_drift": {"drift_detected": False, "series": [], "drift_events": [], "current_state": "no_data"},
            "orderflow_proxy": {"series": [], "summary": {}},
            "data_status": data_status,
        }

    result = await asyncio.to_thread(
        _mp_analytics_engine.full_analytics,
        rows=rows,
        lookback=lookback,
        composite_20d=composite_20d,
        composite_50d=composite_50d,
    )
    result["underlying"] = underlying
    result["lookback"] = lookback
    result["total_sessions"] = len(rows)
    result["data_status"] = data_status
    return result


@router.get("/mp-multi-tf-profile")
async def mp_multi_tf_profile(
    underlying: str = Query("NIFTY"),
) -> dict:
    """
    Multi-timeframe profile snapshot: composite_20d, composite_50d, weekly,
    plus today's daily profile from FMP if available.

    Designed to power the multi-TF stacked profile panel in the UI.
    """
    rows, data_status = await _load_mp_rows(underlying, allow_params_fallback=True)
    if not rows:
        return {"underlying": underlying, "profiles": {}, "weekly_profiles": [], "data_status": data_status}

    rows_sorted = sorted(rows, key=lambda r: r.get("date", ""))
    profiles = {
        "composite_20d": _mp_analytics_engine.build_composite_profile(rows_sorted, lookback=20, label="Composite 20D"),
        "composite_50d": _mp_analytics_engine.build_composite_profile(rows_sorted, lookback=50, label="Composite 50D"),
    }
    weekly = _mp_analytics_engine.build_weekly_profiles(rows_sorted)

    return {
        "underlying": underlying,
        "profiles": profiles,
        "weekly_profiles": weekly[-8:],
        "latest_daily": _build_mp_signal_record(rows_sorted[-1]) if rows_sorted else None,
        "data_status": data_status,
    }


@router.get("/mp-regime-history")
async def mp_regime_history(
    underlying: str = Query("NIFTY"),
    lookback: int = Query(60, ge=10, le=250),
) -> dict:
    """Day-type sequence, transition matrix, and streak analysis."""
    rows, data_status = await _load_mp_rows(underlying, allow_params_fallback=True)
    if not rows:
        return {
            "underlying": underlying,
            "sessions": [],
            "distribution": [],
            "transition_matrix": {},
            "streaks": [],
            "data_status": data_status,
        }
    result = _mp_analytics_engine.regime_history(rows, lookback=lookback)
    result["underlying"] = underlying
    result["data_status"] = data_status
    return result


@router.get("/mp-setup-performance")
async def mp_setup_performance(underlying: str = Query("NIFTY")) -> dict:
    """Setup performance matrix with win rates and expectancy by day_type × direction."""
    rows, data_status = await _load_mp_rows(underlying)
    if not rows:
        return {"underlying": underlying, "total_signals": 0, "cells": [], "calibration": [], "data_status": data_status}
    result = _mp_analytics_engine.setup_performance(rows)
    result["underlying"] = underlying
    result["data_status"] = data_status
    return result


@router.get("/mp-concept-drift")
async def mp_concept_drift(
    underlying: str = Query("NIFTY"),
    window: int = Query(20, ge=10, le=60),
    threshold: float = Query(8.0, ge=2.0, le=30.0),
) -> dict:
    """Page-Hinkley concept drift detection on rolling signal win rate."""
    rows, data_status = await _load_mp_rows(underlying)
    if not rows:
        return {
            "underlying": underlying,
            "drift_detected": False,
            "series": [],
            "current_state": "no_data",
            "data_status": data_status,
        }
    result = _mp_analytics_engine.concept_drift(rows, window=window, threshold=threshold)
    result["underlying"] = underlying
    result["data_status"] = data_status
    return result


@router.get("/mp-orderflow-proxy")
async def mp_orderflow_proxy(
    underlying: str = Query("NIFTY"),
    lookback: int = Query(60, ge=10, le=250),
) -> dict:
    """Approximate CVD series derived from daily auction structure."""
    rows, data_status = await _load_mp_rows(underlying, allow_params_fallback=True)
    if not rows:
        return {"underlying": underlying, "series": [], "summary": {}, "data_status": data_status}
    result = _mp_analytics_engine.orderflow_proxy(rows, lookback=lookback)
    result["underlying"] = underlying
    result["data_status"] = data_status
    result["rag_context"] = await _build_mp_rag_context(
        underlying,
        rows[-1],
        open_signal_payload=_build_mp_open_signal_payload(underlying, rows[-1]),
        orderflow_proxy=result,
    )
    return result
