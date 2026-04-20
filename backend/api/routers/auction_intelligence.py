"""Isolated API for the Market Profile + order-flow strategy module."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import date, datetime, time
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.encoders import jsonable_encoder
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
    return symbol.upper().replace(" FUT", "").strip()


def _journal_matches_symbol(record: dict, symbol: str | None) -> bool:
    normalized = _normalize_symbol_filter(symbol)
    if not normalized:
        return True

    symbol_field = str(record.get("symbol") or "").upper().replace(" FUT", "").strip()
    underlying_field = str(record.get("underlying_symbol") or "").upper().replace(" FUT", "").strip()
    trading_symbol = str(record.get("trading_symbol") or "").upper().strip()

    return (
        normalized == symbol_field
        or normalized == underlying_field
        or trading_symbol.startswith(normalized)
    )


def _shadow_records_from_snapshot(snapshot: dict, options: ShadowCaptureOptions) -> list[dict]:
    return build_shadow_records_from_snapshot(snapshot, options.model_dump())


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
        return await build_live_analysis(symbol_code=symbol)
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
    symbol_key = f"{symbol.upper()} FUT"
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
import math
from collections import defaultdict
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
# Primary: compact mp_data/ directory shipped inside the Docker image (1.5 MB)
# Fallback: full runtime/ tree used during local development
_MP_DATA_ROOT = _BACKEND_ROOT / "mp_data"
_DATA_ROOT = _BACKEND_ROOT / "runtime" / "index_analytics_data"

_SUPPORTED_UNDERLYINGS = ("NIFTY", "BANKNIFTY", "SENSEX")


def _mp_enr_path(underlying: str) -> Path:
    # Try compact ship-in-image path first
    compact = _MP_DATA_ROOT / f"underlying={underlying}" / "enriched_mp_with_failures.csv"
    if compact.exists():
        return compact
    # Fallback to full runtime tree
    sub = _DATA_ROOT / "market_profile" / f"underlying={underlying}" / "enriched_mp_with_failures.csv"
    if sub.exists():
        return sub
    # Legacy root-level file (SENSEX only)
    return _DATA_ROOT / "market_profile" / "enriched_mp_with_failures.csv"


def _mp_params_path(underlying: str) -> Path:
    compact = _MP_DATA_ROOT / f"underlying={underlying}" / "daily_mp_params.csv"
    if compact.exists():
        return compact
    sub = _DATA_ROOT / "market_profile" / f"underlying={underlying}" / "daily_mp_params.csv"
    if sub.exists():
        return sub
    return _DATA_ROOT / "market_profile" / "daily_mp_params.csv"


def _spot_path(underlying: str) -> Path:
    return _DATA_ROOT / "spot" / f"underlying={underlying}" / "1minute.csv.gz"


def _safe_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def _flt(row: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(row.get(key, default))
    except (ValueError, TypeError):
        return default


def _bool(row: dict, key: str) -> bool:
    return str(row.get(key, "")).lower() == "true"


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
    sources: list[dict] = []

    import pandas as pd

    for ul in _SUPPORTED_UNDERLYINGS:
        # Spot candles
        sp = _spot_path(ul)
        if sp.exists():
            df = pd.read_csv(gzip.open(sp, "rt"), usecols=["time"])
            last = str(df["time"].iloc[-1])[:10] if len(df) else "—"
            sources.append({
                "name": f"{ul} Spot 1-min",
                "status": "ok",
                "rows": len(df),
                "last_date": last,
                "detail": f"{len(df):,} candles",
            })
        else:
            sources.append({
                "name": f"{ul} Spot 1-min",
                "status": "missing",
                "rows": 0,
                "last_date": "—",
                "detail": "Fetch via broker API",
            })

        # Daily MP params
        mp = _mp_params_path(ul)
        mp_rows = _safe_csv(mp)
        if mp_rows:
            sources.append({
                "name": f"{ul} Daily MP",
                "status": "ok",
                "rows": len(mp_rows),
                "last_date": mp_rows[-1].get("date", "—"),
                "detail": f"{len(mp_rows)} sessions",
            })
        else:
            sources.append({
                "name": f"{ul} Daily MP",
                "status": "missing",
                "rows": 0,
                "last_date": "—",
                "detail": "Run build_nifty_mp.py",
            })

        # Enriched failure scores
        enr = _mp_enr_path(ul)
        enr_rows = _safe_csv(enr)
        if enr_rows:
            sources.append({
                "name": f"{ul} Failure Scores",
                "status": "ok",
                "rows": len(enr_rows),
                "last_date": enr_rows[-1].get("date", "—"),
                "detail": f"Buyer/seller scores — {len(enr_rows)} days",
            })
        else:
            sources.append({
                "name": f"{ul} Failure Scores",
                "status": "warning",
                "rows": 0,
                "last_date": "—",
                "detail": "Run build_nifty_mp.py",
            })

    return sources


@router.get("/mp-signals")
async def mp_signals(underlying: str = "NIFTY", limit: int = 20) -> dict:
    """Recent MP day signals with failure scores and direction for the given underlying."""
    enr_path = _mp_enr_path(underlying)
    rows = _safe_csv(enr_path)
    if not rows:
        return {"underlying": underlying, "signals": [], "message": f"No MP data for {underlying}"}

    signals = [_build_mp_signal_record(row) for row in rows[-limit:]]

    return {
        "underlying": underlying,
        "signals": signals,
        "latest": signals[-1] if signals else None,
    }


@router.get("/mp-open-signal")
async def mp_open_signal(underlying: str = "NIFTY") -> dict:
    """
    Next-session actionable signal for the MP+Order-Flow strategy.
    Direction from: day_type + IB extension + failure scores.
    Entry method: wait for price > VWAP after 09:30; VWAP stop with 60-min grace; hard SL -50%.
    """
    enr_path = _mp_enr_path(underlying)
    rows = _safe_csv(enr_path)
    if not rows:
        return {"underlying": underlying, "signals": [], "skip_reason": f"No MP data for {underlying}"}
    return _build_mp_open_signal_payload(underlying, rows[-1])


@router.get("/mp-agent-context")
async def mp_agent_context(underlying: str = "NIFTY", limit: int = 10) -> list[dict]:
    """
    Contextual agent reasoning for the MP+Order-Flow strategy.
    Returns structured comments explaining the latest MP structure.
    """
    enr_path = _mp_enr_path(underlying)
    rows = _safe_csv(enr_path)
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
    enr_path = _mp_enr_path(underlying)
    rows = _safe_csv(enr_path)
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
        "context": _build_mp_agent_context_payload(underlying, latest_row, limit=6),
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
    composite_60d: bool = Query(True),
) -> dict:
    """
    Full MP Intelligence bundle:
    - Composite (20d / 60d) multi-timeframe profiles
    - Weekly profile aggregates
    - Value migration trend (POC shift, VA center, VA width over time)
    - Regime history (day-type sequence, transition matrix, streaks)
    - Setup performance matrix (day_type × direction → win rate, expectancy)
    - Concept drift detection (Page-Hinkley on rolling win rate)
    - Orderflow proxy CVD series
    """
    enr_path = _mp_enr_path(underlying)
    rows = _safe_csv(enr_path)
    if not rows:
        # Fall back to daily_mp_params for lighter requests
        rows = _safe_csv(_mp_params_path(underlying))
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
        }

    result = _mp_analytics_engine.full_analytics(
        rows=rows,
        lookback=lookback,
        composite_20d=composite_20d,
        composite_60d=composite_60d,
    )
    result["underlying"] = underlying
    result["lookback"] = lookback
    result["total_sessions"] = len(rows)
    return result


@router.get("/mp-multi-tf-profile")
async def mp_multi_tf_profile(
    underlying: str = Query("NIFTY"),
) -> dict:
    """
    Multi-timeframe profile snapshot: composite_20d, composite_60d, weekly,
    plus today's daily profile from FMP if available.

    Designed to power the multi-TF stacked profile panel in the UI.
    """
    enr_path = _mp_enr_path(underlying)
    rows = _safe_csv(enr_path) or _safe_csv(_mp_params_path(underlying))
    if not rows:
        return {"underlying": underlying, "profiles": {}, "weekly_profiles": []}

    rows_sorted = sorted(rows, key=lambda r: r.get("date", ""))
    profiles = {
        "composite_20d": _mp_analytics_engine.build_composite_profile(rows_sorted, lookback=20, label="Composite 20D"),
        "composite_60d": _mp_analytics_engine.build_composite_profile(rows_sorted, lookback=60, label="Composite 60D"),
    }
    weekly = _mp_analytics_engine.build_weekly_profiles(rows_sorted)

    return {
        "underlying": underlying,
        "profiles": profiles,
        "weekly_profiles": weekly[-8:],
        "latest_daily": _build_mp_signal_record(rows_sorted[-1]) if rows_sorted else None,
    }


@router.get("/mp-regime-history")
async def mp_regime_history(
    underlying: str = Query("NIFTY"),
    lookback: int = Query(60, ge=10, le=250),
) -> dict:
    """Day-type sequence, transition matrix, and streak analysis."""
    rows = _safe_csv(_mp_enr_path(underlying)) or _safe_csv(_mp_params_path(underlying))
    if not rows:
        return {"underlying": underlying, "sessions": [], "distribution": [], "transition_matrix": {}, "streaks": []}
    result = _mp_analytics_engine.regime_history(rows, lookback=lookback)
    result["underlying"] = underlying
    return result


@router.get("/mp-setup-performance")
async def mp_setup_performance(underlying: str = Query("NIFTY")) -> dict:
    """Setup performance matrix with win rates and expectancy by day_type × direction."""
    rows = _safe_csv(_mp_enr_path(underlying))
    if not rows:
        return {"underlying": underlying, "total_signals": 0, "cells": [], "calibration": []}
    result = _mp_analytics_engine.setup_performance(rows)
    result["underlying"] = underlying
    return result


@router.get("/mp-concept-drift")
async def mp_concept_drift(
    underlying: str = Query("NIFTY"),
    window: int = Query(20, ge=10, le=60),
    threshold: float = Query(8.0, ge=2.0, le=30.0),
) -> dict:
    """Page-Hinkley concept drift detection on rolling signal win rate."""
    rows = _safe_csv(_mp_enr_path(underlying))
    if not rows:
        return {"underlying": underlying, "drift_detected": False, "series": [], "current_state": "no_data"}
    result = _mp_analytics_engine.concept_drift(rows, window=window, threshold=threshold)
    result["underlying"] = underlying
    return result


@router.get("/mp-orderflow-proxy")
async def mp_orderflow_proxy(
    underlying: str = Query("NIFTY"),
    lookback: int = Query(60, ge=10, le=250),
) -> dict:
    """Approximate CVD series derived from daily auction structure."""
    rows = _safe_csv(_mp_enr_path(underlying)) or _safe_csv(_mp_params_path(underlying))
    if not rows:
        return {"underlying": underlying, "series": [], "summary": {}}
    result = _mp_analytics_engine.orderflow_proxy(rows, lookback=lookback)
    result["underlying"] = underlying
    return result
