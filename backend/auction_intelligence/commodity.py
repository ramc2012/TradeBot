"""Auction-Intelligence COMMODITY sleeve (MCX evening / extended session).

Runs the SAME Market-Profile + order-flow auction machinery the NSE index lane
uses (``AuctionIntelligenceService`` — market profile, order flow, regime, the
positional/swing/scalp agents, the risk governor) over a small, configurable set
of liquid MCX roots during the extended MCX session (09:00-23:30 IST) — the hours
when NSE is closed. It is a strictly ADDITIVE extension: it never touches the NSE
auction path or its paper book.

Key differences from the NSE lane, and why:

  * Universe = configured MCX roots (default GOLD, SILVERM, CRUDEOIL), each
    resolved to the ACTIVE front-month futures contract via
    ``resolve_active_upstox_mcx_future`` — the same resolver the IC-commodity and
    commodity-strategy lanes use, so every commodity lane always trades the same
    contract.
  * Market profile bars = the unified 1-minute MCX store
    (``load_commodity_history_rows`` → ``underlying_spot_candles`` keyed by root,
    with broker top-up), grouped into MCX sessions (09:00-23:30) and aggregated
    to the 30-minute auction bar size. The profile is built at the per-root
    COARSE value tick (``CommodityContractSpec.mp_profile_tick()``) so POC/VAH/VAL
    concentrate instead of smearing across thousands of one-rupee TPO levels.
  * Order flow = the REAL MCX tick tape (``market_ticks`` keyed by the resolved
    futures symbol, which this lane subscribes on the shared WS router), fed
    through the same tick-reconstruction path as the NSE book path — tick-first,
    degrading to bar inference only when the tape is thin.
  * Instrument traded = the futures contract DIRECTLY (no options remap — the NSE
    option mapper only knows index/stock underlyings, so it would empty every
    commodity plan). The auction agents already emit price-based LONG/SHORT
    decisions with entry/stop/target; those feed a direction-aware futures paper
    book (the same ``ConvergencePaperBook`` the IC-commodity lane uses), so PnL,
    square-off and the circuit breaker are correct for both sides.
  * Window = MCX session (09:00-23:30); intraday square-off at 23:15 exchange
    local. No NSE stale/broker-connectivity gates (paper mode bypasses them).
  * SEPARATE paper book + state file (runtime/auction_intelligence_commodity/…)
    so the NSE index auction book and the commodity book never collide.

The heavy per-symbol MP/OF CPU is offloaded to ``asyncio.to_thread`` (via the
service's own ``analyze`` bulkhead) so the recurring event-loop-seizure class
that has repeatedly wedged this backend cannot recur here.
"""
from __future__ import annotations

import asyncio
import functools
from dataclasses import asdict
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any

from fastapi.encoders import jsonable_encoder
from loguru import logger

from auction_intelligence.config import clone_default_config
from auction_intelligence.live import (
    COMMODITY_SESSION_CLOSE,
    COMMODITY_SESSION_OPEN,
    _aggregate_rows,
    _append_latest_market_tick_as_minute_row,
    _build_live_data_status,
    _build_order_flow_inputs,
    _group_rows_by_session,
    _parse_bar,
    _parse_quote,
    _parse_trade,
    _select_snapshot_rows,
)
from auction_intelligence.schemas import (
    AnalysisBundle,
    DepthLevel,
    DepthSnapshot,
    MarketBar,
    PortfolioSnapshot,
    QuoteSnapshot,
    SessionContext,
    TradePrint,
)
from auction_intelligence.service import AuctionIntelligenceService
from core.config import settings
from core.trading_calendar import trading_calendar
from institutional_convergence.paper import ConvergencePaperBook
from market_data import data_router as market_data_router
from market_data.commodity_contract_specs import (
    COMMODITY_CONTRACT_SPECS,
    canonicalize_commodity_root,
    extract_commodity_root,
    get_commodity_contract_spec,
)
from market_data.commodity_runtime_history import load_commodity_history_rows
from market_data.upstox_commodity import resolve_active_upstox_mcx_future
from paper_engine.base_strategy_agent import _now_ist


# Notional paper capital for the commodity auction sleeve, and per-trade risk
# fraction used by the direction-aware futures paper book (mirrors the IC lane's
# risk-based sizing). Kept separate from the NSE lane's AI_INITIAL_CAPITAL.
COMMODITY_AUCTION_RISK_FRACTION = 0.01

# Separate durable state — never shares a directory with the NSE index book.
_STATE_ROOT = Path(__file__).resolve().parents[1] / "runtime" / "auction_intelligence_commodity"
_PAPER_FILE = _STATE_ROOT / "commodity_paper.json"

# Evening-session square-off (exchange-local). Matches the IC-commodity lane so
# both commodity books flatten at the same 23:15 boundary; no entry quarantine
# (the NSE noon quarantine is a cash-equity concept, irrelevant to MCX).
_SQUAREOFF = time(23, 15)

# Direction-aware futures paper book (separate file). Reused from the
# institutional-convergence lane so PnL is correct for LONG *and* SHORT.
commodity_auction_paper_book = ConvergencePaperBook(
    _PAPER_FILE, squareoff=_SQUAREOFF, entry_quarantine=None
)


# ── Universe ────────────────────────────────────────────────────────────────


def configured_commodity_auction_roots() -> list[str]:
    """Canonical MCX roots for the commodity auction sleeve (config-driven)."""
    raw = str(getattr(settings, "AUCTION_INTELLIGENCE_COMMODITY_SYMBOLS", "GOLD,SILVERM,CRUDEOIL"))
    roots: list[str] = []
    for item in raw.split(","):
        token = item.strip()
        if not token:
            continue
        root = canonicalize_commodity_root(extract_commodity_root(token) or token)
        if root and root in COMMODITY_CONTRACT_SPECS:
            roots.append(root)
    return list(dict.fromkeys(roots))


async def resolve_commodity_auction_universe(now: datetime | None = None) -> dict[str, Any]:
    """Resolve each configured root to its active front-month MCX contract."""
    now = now or _now_ist()
    session_date = trading_calendar.next_exchange_open("MCX", now).date()
    roots = configured_commodity_auction_roots()
    contracts: dict[str, dict[str, Any]] = {}
    for root in roots:
        try:
            resolved = await resolve_active_upstox_mcx_future(root, session_date=session_date)
        except Exception as exc:  # noqa: BLE001 — one root must not break resolution
            logger.debug(f"[AuctionCommodity] resolve failed for {root}: {exc}")
            resolved = None
        if resolved and resolved.get("symbol"):
            contracts[root] = resolved
    return {
        "roots": roots,
        "contracts": contracts,
        "resolved_count": len(contracts),
        "unresolved": [root for root in roots if root not in contracts],
        "session_date": session_date.isoformat(),
    }


# ── Per-root config ─────────────────────────────────────────────────────────


def _commodity_config(root: str) -> dict[str, Any]:
    """Auction config tuned for one MCX root: options remap OFF, coarse MP tick,
    futures contract spec, own journal root. Sizing/exposure stay on the small
    option-buy fraction so the enormous futures notional never trips the risk
    governor's symbol-exposure cap (the paper book does risk-based sizing)."""
    config = clone_default_config()
    spec = get_commodity_contract_spec(root)
    config["options_mapping"]["enabled"] = False
    config["market_profile"]["tick_size"] = float(spec.mp_profile_tick())
    config["contract_specs"] = {
        root: {
            "lot_size": int(spec.futures_lot_size or 1),
            "margin_fraction_per_lot": 0.18,
        }
    }
    config["paper_trading"]["journal_root"] = str(_STATE_ROOT)
    return config


# ── Data prep (monkeypatch seam for tests) ──────────────────────────────────


async def _load_commodity_minute_rows(
    root: str,
    contract_symbol: str,
    *,
    lookback_days: int = 7,
) -> list[dict[str, Any]]:
    """Unified 1-minute MCX rows for a root: durable store + broker top-up
    (``load_commodity_history_rows``) with the freshest market tick appended as a
    trailing partial-minute bar. Isolated so tests can inject synthetic bars."""
    rows, _selected = await load_commodity_history_rows(
        root, interval="1minute", lookback_days=lookback_days, persist=True
    )
    rows = await _append_latest_market_tick_as_minute_row(rows, app_symbol=contract_symbol)
    return rows


async def _build_commodity_inputs(
    root: str,
    contract: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Assemble the schema-ready analysis inputs for one commodity root.

    Returns the ``SessionContext`` + 30-minute current/prior bars + tick-first
    order-flow quote/trades/depth/history + metadata, all built from the unified
    1-minute MCX store and the real MCX tick tape.
    """
    contract_symbol = str(contract.get("symbol") or "")
    if not contract_symbol:
        raise RuntimeError("commodity contract has no resolved futures symbol")
    spec = get_commodity_contract_spec(root)
    tick_size = float(spec.mp_profile_tick())

    rows = await _load_commodity_minute_rows(root, contract_symbol)
    if not rows:
        raise RuntimeError("no 1-minute commodity rows available for the requested root")

    sessions = _group_rows_by_session(rows, allow_partial_live_session=True, symbol_code=root)
    session_dates = sorted(sessions.keys())
    if len(session_dates) < 2:
        raise RuntimeError("at least two MCX sessions are required for commodity auction analysis")

    latest_date = session_dates[-1]
    prior_date = session_dates[-2]
    current_session_rows = sessions[latest_date]
    prior_session_rows = sessions[prior_date]

    current_rows, snapshot_time_local, snapshot_mode = _select_snapshot_rows(
        current_session_rows, symbol_code=root
    )
    if len(current_rows) < 120:
        raise RuntimeError("the selected commodity snapshot does not have enough 1-minute history yet")

    current_bars = _aggregate_rows(
        current_rows, interval_minutes=30, session_open=COMMODITY_SESSION_OPEN
    )
    prior_bars = _aggregate_rows(
        prior_session_rows, interval_minutes=30, session_open=COMMODITY_SESSION_OPEN
    )
    if len(current_bars) < 4 or len(prior_bars) < 4:
        raise RuntimeError("insufficient 30-minute commodity bars were built from the MCX store")

    current_tick = market_data_router.get_latest_tick(contract_symbol)
    order_flow_inputs = await _build_order_flow_inputs(
        app_symbol=contract_symbol,
        current_rows=current_rows,
        current_tick=current_tick,
        quote_override=None,
        tick_size=tick_size,
        snapshot_mode=snapshot_mode,
        symbol_code=root,
    )
    quote_payload = order_flow_inputs["quote"]
    depth_payload = order_flow_inputs["depth"]
    trades_payload = order_flow_inputs["trades"]
    quote_history_payload = order_flow_inputs["quote_history"]
    quote_source = str(order_flow_inputs["quote_source"])
    order_flow_source = str(order_flow_inputs["order_flow_source"])
    stale_data_seconds = float(order_flow_inputs["stale_data_seconds"])

    data_status = _build_live_data_status(
        current_rows=current_rows,
        snapshot_mode=snapshot_mode,
        quote_source=quote_source,
        order_flow_source=order_flow_source,
        quote_history_payload=quote_history_payload,
        trades_payload=trades_payload,
        stale_data_seconds=stale_data_seconds,
    )

    session_date = latest_date
    spot = float(quote_payload.get("last_price") or 0.0)
    if spot <= 0 and current_bars:
        spot = float(current_bars[-1]["close"])
    session_close_dt = datetime.combine(session_date, COMMODITY_SESSION_CLOSE, tzinfo=snapshot_time_local.tzinfo)
    minutes_to_close = max(0, int((session_close_dt - snapshot_time_local).total_seconds() // 60))

    request = {
        "session": {
            "symbol": f"{root} FUT",
            "session_date": session_date.isoformat(),
            "last_price": round(spot, 2),
            # Paper mode bypasses the live stale/broker gates (see NSE lane) so the
            # regime + agents are judged on structure, not tick freshness.
            "stale_data_seconds": 0.0,
            "minutes_to_close": minutes_to_close,
            "broker_connected": True,
        },
        "portfolio": {
            "net_liquidation": float(_commodity_paper_initial_capital()),
            "daily_realized_pnl": 0.0,
            "open_positions": 0,
            "symbol_exposure": {f"{root} FUT": 0.0},
            "agent_drawdowns": {"positional": 0.0, "swing": 0.0, "scalp": 0.0},
            "correlated_exposure": 0.0,
        },
        "quote": {
            "timestamp": quote_payload["timestamp"],
            "bid": quote_payload["bid"],
            "ask": quote_payload["ask"],
            "bid_size": quote_payload["bid_size"],
            "ask_size": quote_payload["ask_size"],
        },
        "depth": depth_payload,
        "quote_history": quote_history_payload,
        "bars": current_bars,
        "prior_bars": prior_bars,
        "trades": trades_payload,
        "metadata": {
            "symbol_code": root,
            "market": "MCX",
            "futures_contract": contract_symbol,
            "lot_size": int(spec.futures_lot_size or 1),
            "tick_size": tick_size,
            "quote_source": quote_source,
            "order_flow_source": order_flow_source,
            "snapshot_mode": snapshot_mode,
            "snapshot_time": snapshot_time_local.isoformat(),
            "data_status": data_status,
        },
    }
    return {
        "request": request,
        "session_date": session_date,
        "snapshot_mode": snapshot_mode,
        "snapshot_time": snapshot_time_local,
        "spot": spot,
        "contract_symbol": contract_symbol,
        "lot_size": int(spec.futures_lot_size or 1),
        "data_status": data_status,
        "order_flow_source": order_flow_source,
    }


def _commodity_paper_initial_capital() -> float:
    """Notional paper capital anchor for the commodity auction sleeve. Read from
    the shared ConvergencePaperBook module constant so equity accounting matches
    the IC-commodity book (₹10L)."""
    from institutional_convergence.paper import INITIAL_CAPITAL

    return float(INITIAL_CAPITAL)


# ── Analysis ────────────────────────────────────────────────────────────────


async def build_commodity_analysis(
    root: str,
    *,
    contract: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the auction MP+OF machinery for one commodity root and return the
    bundle + request + metadata. The heavy analyze() CPU runs in a worker thread.
    """
    now = now or _now_ist()
    normalized_root = canonicalize_commodity_root(root)
    if normalized_root not in COMMODITY_CONTRACT_SPECS:
        raise ValueError(f"Unsupported commodity root: {root}")

    if contract is None:
        session_date = trading_calendar.next_exchange_open("MCX", now).date()
        contract = await resolve_active_upstox_mcx_future(normalized_root, session_date=session_date)
        if not contract or not contract.get("symbol"):
            raise RuntimeError(f"active MCX contract could not be resolved for {normalized_root}")

    inputs = await _build_commodity_inputs(normalized_root, contract, now=now)
    request = inputs["request"]

    config = _commodity_config(normalized_root)
    service = AuctionIntelligenceService(config, paper_mode=True)

    bundle: AnalysisBundle = await asyncio.to_thread(
        functools.partial(
            service.analyze,
            session=SessionContext(**request["session"]),
            bars=[MarketBar(**_parse_bar(item)) for item in request["bars"]],
            quote=QuoteSnapshot(**_parse_quote(request["quote"])),
            trades=[TradePrint(**_parse_trade(item)) for item in request["trades"]],
            prior_bars=[MarketBar(**_parse_bar(item)) for item in request["prior_bars"]],
            depth=_depth_snapshot(request.get("depth")),
            portfolio=PortfolioSnapshot(**request["portfolio"]),
            quote_history=[QuoteSnapshot(**_parse_quote(item)) for item in request["quote_history"]],
        )
    )
    return {"bundle": bundle, "request": request, **{k: v for k, v in inputs.items() if k != "request"}}


def _depth_snapshot(payload: dict[str, Any] | None) -> DepthSnapshot | None:
    if not payload:
        return None
    return DepthSnapshot(
        timestamp=datetime.fromisoformat(str(payload["timestamp"])),
        bids=[DepthLevel(**item) for item in list(payload.get("bids") or [])],
        asks=[DepthLevel(**item) for item in list(payload.get("asks") or [])],
    )


def bundle_to_result_row(
    root: str,
    *,
    bundle: AnalysisBundle,
    contract_symbol: str,
    spot: float,
    snapshot_time: datetime,
    lot_size: int,
) -> dict[str, Any]:
    """Translate an auction analysis bundle into the IC-style result row the
    direction-aware futures paper book consumes. Picks the single best actionable
    (risk-allowed, non-FLAT, executable) decision and maps its price-based
    entry/stop/target onto a futures order."""
    decisions = list(bundle.agent_decisions or [])
    exec_by_agent = {
        execution.agent_name: execution
        for execution in (bundle.execution_plan or [])
        if getattr(execution, "action", "FLAT") != "FLAT"
    }
    risk_allowed = bool(getattr(bundle.risk, "allowed", False))
    non_flat = [d for d in decisions if getattr(d, "action", "FLAT") != "FLAT"]

    candidates = [
        d
        for d in decisions
        if d.agent_name in exec_by_agent
        and str(d.action).upper() in {"LONG", "SHORT"}
        and d.stop_price is not None
        and d.target_price is not None
    ]
    chosen = max(candidates, key=lambda d: float(d.confidence or 0.0), default=None)

    regime_label = str(getattr(bundle.regime, "label", "") or "")
    entry = float(spot)
    row: dict[str, Any] = {
        "kind": "commodity",
        "symbol": root,
        "sector": "COMMODITY",
        "market": "MCX",
        "spot": round(entry, 4) if entry > 0 else None,
        "futures_contract": contract_symbol,
        "regime_label": regime_label,
        "action": "FLAT",
        "status": "flat",
        "blocked_reasons": [],
    }

    # Direction-sanity: LONG needs stop below / target above entry (SHORT mirror).
    def _valid_levels(action: str, stop: float, target: float) -> bool:
        if entry <= 0:
            return False
        if action == "LONG":
            return stop < entry < target
        return target < entry < stop

    if chosen is not None and _valid_levels(str(chosen.action).upper(), float(chosen.stop_price), float(chosen.target_price)):
        action = str(chosen.action).upper()
        setup = {"bar_time": snapshot_time.isoformat(), "agent": chosen.agent_name}
        row.update(
            {
                "action": action,
                "status": "actionable_paper",
                "confidence": round(float(chosen.confidence or 0.0), 4),
                "agent_name": chosen.agent_name,
                "long_setup": setup if action == "LONG" else None,
                "short_setup": setup if action == "SHORT" else None,
                "risk": {
                    "entry": round(entry, 4),
                    "stop": round(float(chosen.stop_price), 4),
                    "target1": round(float(chosen.target_price), 4),
                    "target2_long": None,
                    "target2_short": None,
                    "lot_size": int(lot_size or 1),
                    "risk_fraction": COMMODITY_AUCTION_RISK_FRACTION,
                },
            }
        )
    else:
        if not non_flat:
            row["status"] = "all_decisions_flat"
            row["blocked_reasons"] = ["all_decisions_flat"]
        elif not risk_allowed:
            row["status"] = "risk_blocked"
            row["blocked_reasons"] = list(getattr(bundle.risk, "reasons", []) or [])
        else:
            # non-FLAT + risk-allowed but no executable/valid-level candidate.
            row["status"] = "no_valid_contract"
            row["blocked_reasons"] = ["no_valid_futures_levels"]
    return row


# ── Cycle ───────────────────────────────────────────────────────────────────


def _mcx_open(now: datetime) -> bool:
    return bool(trading_calendar.is_exchange_open("MCX", now))


def _market_closed_payload(now: datetime) -> dict[str, Any]:
    return {
        "status": "market_closed",
        "market": "MCX",
        "next_run_at": trading_calendar.next_exchange_open("MCX", now).isoformat(),
        "result_count": 0,
        "actionable_count": 0,
        "failure_count": 0,
        "failures": {},
        "gate_breakdown": {"mcx_market_closed": 1},
        "results": [],
        "paper": commodity_auction_paper_book.summary(),
    }


async def run_commodity_market_hours_cycle(roots: list[str] | None = None) -> dict[str, Any]:
    """One commodity auction cycle: resolve the universe, run MP+OF analysis per
    root, and sync the direction-aware futures paper book. MCX-hours gated."""
    now = _now_ist()
    if not _mcx_open(now):
        return _market_closed_payload(now)

    universe = await resolve_commodity_auction_universe(now)
    contracts = dict(universe["contracts"])
    if roots:
        wanted = {canonicalize_commodity_root(r) for r in roots}
        contracts = {root: contract for root, contract in contracts.items() if root in wanted}

    futures_symbols = [str(contract.get("symbol")) for contract in contracts.values() if contract.get("symbol")]
    if futures_symbols:
        try:
            # Same shared WS router the IC-commodity lane uses — this is what
            # fills market_ticks for the order-flow tape.
            await market_data_router.add_subscriptions(futures_symbols)
        except Exception as exc:  # noqa: BLE001 — subscription is best-effort
            logger.warning(f"[AuctionCommodity] tick subscription reconciliation failed: {exc}")

    results: list[dict[str, Any]] = []
    failures: dict[str, str] = {}
    for root, contract in contracts.items():
        try:
            analysis = await build_commodity_analysis(root, contract=contract, now=now)
            # Re-check the MCX session immediately before any durable trade write
            # so a cycle that straddles 23:30 cannot open against a frozen close.
            if not _mcx_open(_now_ist()):
                logger.warning(f"[AuctionCommodity] MCX closed mid-cycle; skipping durable write for {root}")
                break
            row = bundle_to_result_row(
                root,
                bundle=analysis["bundle"],
                contract_symbol=analysis["contract_symbol"],
                spot=float(analysis["spot"]),
                snapshot_time=analysis["snapshot_time"],
                lot_size=int(analysis["lot_size"]),
            )
            row["order_flow_source"] = analysis.get("order_flow_source")
            results.append(row)
        except Exception as exc:  # noqa: BLE001 — one root must not kill the cycle
            failures[root] = str(exc)
            results.append(
                {
                    "kind": "commodity",
                    "symbol": root,
                    "market": "MCX",
                    "status": "error",
                    "action": "FLAT",
                    "spot": None,
                    "blocked_reasons": ["analysis_failed"],
                    "detail": str(exc),
                }
            )

    for root in universe["unresolved"]:
        results.append(
            {
                "kind": "commodity",
                "symbol": root,
                "market": "MCX",
                "status": "blocked",
                "action": "FLAT",
                "spot": None,
                "blocked_reasons": ["active_contract_unresolved"],
            }
        )

    paper = await asyncio.to_thread(commodity_auction_paper_book.sync, results, now)
    return {
        "status": "ok" if not failures else "degraded",
        "market": "MCX",
        "mode": "paper",
        "paper_execution_enabled": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session_date": now.date().isoformat(),
        "universe": universe,
        "results": results,
        "result_count": len(results),
        "actionable_count": sum(1 for row in results if row.get("status") == "actionable_paper"),
        "failure_count": len(failures),
        "failures": failures,
        "gate_breakdown": _gate_breakdown(results),
        "paper": paper,
    }


def _gate_breakdown(results: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in results:
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


# ── Read surfaces (endpoints) ───────────────────────────────────────────────


async def build_commodity_auction_snapshot(root: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Read-only live analysis snapshot for one commodity root (does NOT write to
    the paper book) — for the /commodity/snapshot endpoint."""
    now = now or _now_ist()
    analysis = await build_commodity_analysis(root, now=now)
    bundle = analysis["bundle"]
    row = bundle_to_result_row(
        canonicalize_commodity_root(root),
        bundle=bundle,
        contract_symbol=analysis["contract_symbol"],
        spot=float(analysis["spot"]),
        snapshot_time=analysis["snapshot_time"],
        lot_size=int(analysis["lot_size"]),
    )
    return jsonable_encoder(
        {
            "mode": "live",
            "market": "MCX",
            "symbol_code": canonicalize_commodity_root(root),
            "session_date": analysis["session_date"].isoformat(),
            "snapshot_mode": analysis["snapshot_mode"],
            "order_flow_source": analysis.get("order_flow_source"),
            "futures_contract": analysis["contract_symbol"],
            "data_status": analysis["data_status"],
            "result": row,
            "request": analysis["request"],
            "analysis": asdict(bundle),
        }
    )


async def commodity_auction_status() -> dict[str, Any]:
    """Status surface for the commodity auction sleeve (universe + paper book +
    supervisor runner)."""
    now = _now_ist()
    try:
        from core.market_hours_paper_supervisor import market_hours_paper_supervisor

        automation = market_hours_paper_supervisor.get_runner_status("auction_intelligence_commodity")
    except Exception:  # noqa: BLE001
        automation = {}
    universe = await resolve_commodity_auction_universe(now)
    return {
        "key": "auction_intelligence_commodity",
        "module": "auction_intelligence",
        "sleeve": "commodity",
        "enabled": bool(getattr(settings, "AUCTION_INTELLIGENCE_COMMODITY_ENABLED", True)),
        "mode": "paper",
        "market": "MCX",
        "paper_execution_enabled": True,
        "market_open": _mcx_open(now),
        "session_window": {"open": COMMODITY_SESSION_OPEN.isoformat(), "close": COMMODITY_SESSION_CLOSE.isoformat(), "squareoff": _SQUAREOFF.isoformat()},
        "universe": universe,
        "automation": automation,
        "paper": commodity_auction_paper_book.summary(),
        "paper_statistics": commodity_auction_paper_book.statistics(),
    }
