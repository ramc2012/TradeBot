from __future__ import annotations

from datetime import datetime
from typing import Any

from auction_intelligence.config import clone_default_config
from auction_intelligence.live import (
    _parse_bar,
    _parse_quote,
    _parse_trade,
    build_live_analysis,
)
from auction_intelligence.schemas import (
    DepthLevel,
    DepthSnapshot,
    MarketBar,
    PortfolioSnapshot,
    QuoteSnapshot,
    SessionContext,
    TradePrint,
)
from auction_intelligence.service import AuctionIntelligenceService
from auction_intelligence.shadow import ShadowPersistenceService
from core.config import auction_front_month_book_symbols
from core.trading_calendar import trading_calendar
from loguru import logger
from market_data import data_router as market_data_router
from paper_engine.base_strategy_agent import _now_ist


_shadow_store = ShadowPersistenceService()
_active_order_flow_book_symbols: set[str] = set()


def _nse_market_open() -> bool:
    return trading_calendar.is_exchange_open("NSE", _now_ist())


def _market_closed_result(symbol: str, *, stage: str) -> dict[str, Any]:
    return {
        "status": "market_closed",
        "symbol_code": str(symbol or "").strip().upper(),
        "decision_count": 0,
        "non_flat_decision_count": 0,
        "risk_allowed": False,
        "risk_reasons": ["nse_market_closed"],
        "flat_reasons": {},
        "no_trade_gate": "nse_market_closed",
        "execution_count": 0,
        "journal_paths": [],
        "journal_path_count": 0,
        "paper_positions_summary": None,
        "shadow_record_count": 0,
        "shadow_storage": None,
        "guard_stage": stage,
        "next_market_open": trading_calendar.next_exchange_open("NSE", _now_ist()).isoformat(),
    }


async def _sync_order_flow_book_subscriptions() -> dict[str, Any]:
    """Reconcile calendar-rolled book contracts without risking Upstox WS."""
    global _active_order_flow_book_symbols

    mapping = auction_front_month_book_symbols()
    desired = {symbol for symbol in mapping.values() if symbol}
    broker_name = str(
        getattr(getattr(market_data_router, "_broker", None), "broker_name", "") or ""
    ).lower()
    if broker_name != "fyers":
        return {
            "status": "broker_not_supported",
            "broker": broker_name or None,
            "mapping": mapping,
            "active_symbols": sorted(_active_order_flow_book_symbols),
        }

    removed = sorted(_active_order_flow_book_symbols - desired)
    added = sorted(desired - _active_order_flow_book_symbols)
    if removed:
        await market_data_router.remove_subscriptions(removed)
    if added:
        await market_data_router.add_subscriptions(added)
    _active_order_flow_book_symbols = desired
    return {
        "status": "active",
        "broker": broker_name,
        "mapping": mapping,
        "added": added,
        "removed": removed,
        "active_symbols": sorted(desired),
    }


def auto_symbols() -> list[str]:
    config = clone_default_config()
    scope = config.get("mvp_scope", {})
    seen: set[str] = set()
    symbols: list[str] = []
    for raw in [
        *(scope.get("primary_underlyings") or []),
        *(scope.get("secondary_underlyings") or []),
    ]:
        symbol = str(raw or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return symbols


def default_shadow_capture_options() -> dict[str, Any]:
    return {
        "reconciliation_status": "matched",
        "mismatch_duration_seconds": 0.0,
        "kill_switch_tested": False,
        "kill_switch_passed": False,
        "dashboard_checked": False,
        "alerts_checked": False,
        "manual_override_tested": False,
        "record_flat_decisions": True,
    }


def build_shadow_records_from_snapshot(
    snapshot: dict[str, Any],
    options: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    capture_options = {
        **default_shadow_capture_options(),
        **dict(options or {}),
    }
    request = snapshot.get("request", {})
    analysis = snapshot.get("analysis", {})
    session = request.get("session", {})
    quote = request.get("quote", {})
    metadata = request.get("metadata", {})
    tick_size = float(analysis.get("market_profile", {}).get("tick_size") or 0.5)
    risk = analysis.get("risk", {})
    execution_by_agent = {
        item.get("agent_name"): item
        for item in analysis.get("execution_plan", [])
        if item.get("agent_name")
    }
    snapshot_time = str(metadata.get("snapshot_time") or session.get("session_date") or "na")
    snapshot_key = (
        snapshot_time.replace(":", "")
        .replace("-", "")
        .replace("+", "")
        .replace(".", "")
        .replace("T", "_")
    )
    stale_signal = float(session.get("stale_data_seconds") or 0.0) > float(
        clone_default_config()["risk"].get("stale_data_seconds", 10)
    )

    records: list[dict[str, Any]] = []
    for index, decision in enumerate(analysis.get("agent_decisions", [])):
        action = str(decision.get("action") or "FLAT")
        if action == "FLAT" and not bool(capture_options.get("record_flat_decisions", True)):
            continue
        execution = execution_by_agent.get(decision.get("agent_name"))
        simulated_fill_price = (
            execution.get("limit_price")
            if execution and execution.get("limit_price") is not None
            else decision.get("entry_price")
        )
        observed_touch_price = None
        if action == "LONG":
            observed_touch_price = quote.get("ask")
        elif action == "SHORT":
            observed_touch_price = quote.get("bid")
        observed_fill_price = observed_touch_price
        fill_drift_ticks = None
        if simulated_fill_price is not None and observed_touch_price is not None and tick_size > 0:
            fill_drift_ticks = round(abs(float(simulated_fill_price) - float(observed_touch_price)) / tick_size, 4)
        records.append(
            {
                "signal_id": f"{session.get('session_date')}:{snapshot_key}:{session.get('symbol')}:{decision.get('agent_name')}:{index}",
                "session_date": session.get("session_date"),
                "symbol": session.get("symbol"),
                "source": metadata.get("history_source", snapshot.get("mode", "shadow")),
                "snapshot_mode": metadata.get("snapshot_mode"),
                "agent_name": decision.get("agent_name"),
                "action": action,
                "regime_label": analysis.get("regime", {}).get("label"),
                "setup_name": decision.get("metadata", {}).get(
                    "setup_name",
                    decision.get("metadata", {}).get("flat_reason"),
                ),
                "confidence": float(decision.get("confidence") or 0.0),
                "quantity": int(decision.get("quantity") or 0),
                "entry_price": decision.get("entry_price"),
                "stop_price": decision.get("stop_price"),
                "target_price": decision.get("target_price"),
                "tick_size": tick_size,
                "risk_allowed": bool(risk.get("allowed", False)),
                "kill_switch_active": bool(risk.get("kill_switch", False)),
                "simulated_fill_price": simulated_fill_price,
                "observed_touch_price": observed_touch_price,
                "observed_fill_price": observed_fill_price,
                "fill_drift_ticks": fill_drift_ticks,
                "stale_signal": stale_signal,
                "reconciliation_status": capture_options.get("reconciliation_status", "matched"),
                "mismatch_duration_seconds": float(capture_options.get("mismatch_duration_seconds", 0.0) or 0.0),
                "kill_switch_tested": bool(capture_options.get("kill_switch_tested", False)),
                "kill_switch_passed": bool(capture_options.get("kill_switch_passed", False)),
                "dashboard_checked": bool(capture_options.get("dashboard_checked", False)),
                "alerts_checked": bool(capture_options.get("alerts_checked", False)),
                "manual_override_tested": bool(capture_options.get("manual_override_tested", False)),
                "metadata": {
                    "symbol_code": snapshot.get("symbol_code"),
                    "request_symbol": session.get("symbol"),
                    "quote_source": metadata.get("quote_source"),
                    "order_flow_source": metadata.get("order_flow_source"),
                    "history_symbol": metadata.get("history_symbol"),
                    "data_status": snapshot.get("data_status"),
                    "risk_reasons": risk.get("reasons", []),
                    "rationale": decision.get("rationale", []),
                    "decision_metadata": decision.get("metadata", {}),
                },
            }
        )
    return records


def _depth_snapshot(payload: dict[str, Any] | None) -> DepthSnapshot | None:
    if not payload:
        return None
    return DepthSnapshot(
        timestamp=datetime.fromisoformat(str(payload["timestamp"])),
        bids=[DepthLevel(**item) for item in list(payload.get("bids") or [])],
        asks=[DepthLevel(**item) for item in list(payload.get("asks") or [])],
    )


async def capture_live_paper_cycle(
    symbol: str,
    *,
    shadow_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not _nse_market_open():
        return _market_closed_result(symbol, stage="before_analysis")

    try:
        order_flow_subscription = await _sync_order_flow_book_subscriptions()
    except Exception as exc:
        logger.warning(f"auction order-flow subscription reconciliation failed: {exc}")
        order_flow_subscription = {"status": "error", "detail": str(exc)}

    snapshot = await build_live_analysis(symbol_code=symbol)
    request = snapshot["request"]
    # Paper mode bypasses live-execution data-quality gates. The live-snapshot
    # builder sets session.broker_connected = data_status.execution_ready and
    # inflates stale_data_seconds past the budget whenever the order-flow path
    # is bar_inference (which is the only path in live_session mode without a
    # tick subscription). For real-money trading those gates are correct, but
    # they make paper trades impossible on bar-inferred sessions even though
    # the regime + agent logic is perfectly valid on bar data. Override only
    # for the paper cycle so the risk governor judges the trade on regime,
    # exposure, and confidence — not on tick freshness.
    session_payload = dict(request["session"])
    session_payload["broker_connected"] = True
    session_payload["stale_data_seconds"] = 0.0
    # Paper-mode portfolio sizing fix (2026-06-03): _load_portfolio_snapshot
    # populates net_liquidation from the LIVE broker funds (adapter.get_funds).
    # This data/paper broker account is near-empty, so net_liquidation was a
    # few thousand rupees → every agent's
    #   quantity = floor(net_liq * sleeve_fraction / margin_per_lot) * lot_size
    # floored to 0 → all three agents returned `insufficient_notional` every
    # cycle → AI made 0 paper trades for its entire lifetime. Paper trading
    # must size against the PAPER account's notional capital (the same
    # shadow_net_liquidation the shadow path uses), exactly like every other
    # paper desk uses its ₹1,000,000 PaperPortfolio — not the real broker
    # balance. Override net_liquidation for the paper cycle only; live-money
    # trading still sizes against real funds.
    portfolio_payload = dict(request.get("portfolio", {}))
    paper_capital = float(
        clone_default_config().get("paper_trading", {}).get("shadow_net_liquidation", 1_000_000.0)
    )
    if not portfolio_payload.get("net_liquidation") or float(portfolio_payload["net_liquidation"]) < paper_capital:
        portfolio_payload["net_liquidation"] = paper_capital
    service = AuctionIntelligenceService(paper_mode=True)
    bundle = await service.analyze_with_options(
        session=SessionContext(**session_payload),
        bars=[MarketBar(**_parse_bar(item)) for item in request.get("bars", [])],
        quote=QuoteSnapshot(**_parse_quote(request["quote"])),
        trades=[TradePrint(**_parse_trade(item)) for item in request.get("trades", [])],
        prior_bars=[MarketBar(**_parse_bar(item)) for item in request.get("prior_bars", [])],
        depth=_depth_snapshot(request.get("depth")),
        portfolio=PortfolioSnapshot(**portfolio_payload),
        quote_history=[QuoteSnapshot(**_parse_quote(item)) for item in request.get("quote_history", [])],
    )

    # Analysis can take several minutes on a cold chain. Re-check immediately
    # before every durable trade write so a cycle that straddles 15:30 cannot
    # journal or open a position against a frozen closing snapshot.
    if not _nse_market_open():
        result = _market_closed_result(symbol, stage="before_persist")
        result.update(
            {
                "session_date": snapshot.get("session_date"),
                "snapshot_mode": request.get("metadata", {}).get("snapshot_mode"),
                "source": request.get("metadata", {}).get("history_source"),
                "decision_count": len(list(bundle.agent_decisions or [])),
                "non_flat_decision_count": sum(
                    1 for decision in (bundle.agent_decisions or [])
                    if getattr(decision, "action", "FLAT") != "FLAT"
                ),
            }
        )
        logger.warning(
            "auction.cycle blocked before persist: NSE session closed during analysis "
            f"(symbol={result['symbol_code']})"
        )
        return result

    journal_paths = service.paper.record_analysis(bundle)
    paper_positions = await service.paper.sync_positions(bundle)
    records = build_shadow_records_from_snapshot(snapshot, shadow_options)
    storage = await _shadow_store.record_records(records)

    # ── Why-no-trade diagnostics (2026-06-02) ──────────────────────────────
    # AI has 0 paper trades lifetime, but the journal only records EXECUTED
    # trades — so the desk was a black box: we couldn't tell whether the
    # empty execution plan came from (a) every agent returning FLAT,
    # (b) the risk governor blocking, or (c) options-mapping dropping every
    # contract (no chain/expiry/strike). Surface all three so a single
    # market-hours cycle in `last_result_meta` / docker logs names the gate.
    decisions = list(bundle.agent_decisions or [])
    non_flat = [d for d in decisions if getattr(d, "action", "FLAT") != "FLAT"]
    # When every agent is FLAT, capture WHY per agent (each agent stamps a
    # `flat_reason` into decision.metadata — e.g. no_scalp_alignment,
    # confidence_below_threshold, a setup-mismatch tag). This is the layer
    # below `all_decisions_flat`: it names which condition each agent failed,
    # so the fix (loosen a threshold vs add a missing setup) is obvious.
    flat_reasons: dict[str, str] = {}
    for d in decisions:
        if getattr(d, "action", "FLAT") == "FLAT":
            meta = getattr(d, "metadata", {}) or {}
            flat_reasons[str(getattr(d, "agent_name", "?"))] = str(
                meta.get("flat_reason") or "unspecified"
            )
    risk_allowed = bool(getattr(bundle.risk, "allowed", False))
    risk_reasons = list(getattr(bundle.risk, "reasons", []) or [])
    exec_count = len(list(bundle.execution_plan or []))
    if exec_count > 0:
        gate = "executed"
    elif not non_flat:
        gate = "all_decisions_flat"
    elif not risk_allowed:
        gate = "risk_blocked"
    else:
        gate = "options_mapping_empty"  # risk allowed + non-FLAT, yet 0 plan
    sym_code = str(snapshot.get("symbol_code") or symbol).upper()
    logger.info(
        "auction.cycle symbol={sym} gate={gate} decisions={dc} non_flat={nf} "
        "risk_allowed={ra} risk_reasons={rr} flat_reasons={fr} executions={ec}",
        sym=sym_code, gate=gate, dc=len(decisions), nf=len(non_flat),
        ra=risk_allowed, rr=risk_reasons, fr=flat_reasons, ec=exec_count,
    )

    return {
        "symbol_code": sym_code,
        "session_date": snapshot.get("session_date"),
        "snapshot_mode": request.get("metadata", {}).get("snapshot_mode"),
        "source": request.get("metadata", {}).get("history_source"),
        "order_flow_source": request.get("metadata", {}).get("order_flow_source"),
        "order_flow_book_symbols": auction_front_month_book_symbols(),
        "order_flow_subscription": order_flow_subscription,
        "decision_count": len(decisions),
        "non_flat_decision_count": len(non_flat),
        "risk_allowed": risk_allowed,
        "risk_reasons": risk_reasons,
        "flat_reasons": flat_reasons,
        "no_trade_gate": gate,
        "execution_count": exec_count,
        "journal_paths": journal_paths,
        "journal_path_count": len(journal_paths),
        "paper_positions_summary": paper_positions.get("summary") if isinstance(paper_positions, dict) else None,
        "shadow_record_count": len(records),
        "shadow_storage": storage,
    }


async def run_market_hours_cycle(
    symbols: list[str] | None = None,
    *,
    shadow_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from time import monotonic

    requested = list(dict.fromkeys(symbols or auto_symbols()))
    if not _nse_market_open():
        results = [_market_closed_result(symbol, stage="cycle_start") for symbol in requested]
        return {
            "status": "market_closed",
            "symbols_requested": requested,
            "symbols_completed": [],
            "result_count": len(results),
            "failure_count": 0,
            "failures": {},
            "gate_breakdown": {"nse_market_closed": len(results)},
            "execution_total": 0,
            "shadow_record_count": 0,
            "journal_path_count": 0,
            "results": results,
        }
    results: list[dict[str, Any]] = []
    failures: dict[str, str] = {}
    _sym_timings: dict[str, float] = {}

    for symbol in requested:
        try:
            _s = monotonic()
            results.append(await capture_live_paper_cycle(symbol, shadow_options=shadow_options))
            _sym_timings[symbol] = round(monotonic() - _s, 2)
        except Exception as exc:
            failures[symbol] = str(exc)
    logger.info(
        f"[AuctionProfile] per-symbol cycle(s): {_sym_timings} "
        f"total={round(sum(_sym_timings.values()), 1)}"
    )

    if not results and failures:
        joined = "; ".join(f"{symbol}: {detail}" for symbol, detail in failures.items())
        raise RuntimeError(f"Auction Intelligence paper cycle failed: {joined}")

    # Roll up the per-symbol no-trade gate so last_result_meta shows, at a
    # glance, WHY the cycle produced (or didn't produce) trades.
    gate_breakdown: dict[str, int] = {}
    for item in results:
        gate = str(item.get("no_trade_gate") or "unknown")
        gate_breakdown[gate] = gate_breakdown.get(gate, 0) + 1

    return {
        "symbols_requested": requested,
        "symbols_completed": [item["symbol_code"] for item in results],
        "result_count": len(results),
        "failure_count": len(failures),
        "failures": failures,
        "gate_breakdown": gate_breakdown,
        "execution_total": sum(int(item.get("execution_count") or 0) for item in results),
        "shadow_record_count": sum(int(item.get("shadow_record_count") or 0) for item in results),
        "journal_path_count": sum(int(item.get("journal_path_count") or 0) for item in results),
        "results": results,
    }
