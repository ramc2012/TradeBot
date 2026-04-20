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


_shadow_store = ShadowPersistenceService()


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
                    "history_symbol": metadata.get("history_symbol"),
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
    snapshot = await build_live_analysis(symbol_code=symbol)
    request = snapshot["request"]
    service = AuctionIntelligenceService()
    bundle, journal_paths, paper_positions = await service.analyze_and_record_option_paper(
        session=SessionContext(**request["session"]),
        bars=[MarketBar(**_parse_bar(item)) for item in request.get("bars", [])],
        quote=QuoteSnapshot(**_parse_quote(request["quote"])),
        trades=[TradePrint(**_parse_trade(item)) for item in request.get("trades", [])],
        prior_bars=[MarketBar(**_parse_bar(item)) for item in request.get("prior_bars", [])],
        depth=_depth_snapshot(request.get("depth")),
        portfolio=PortfolioSnapshot(**request.get("portfolio", {})),
        quote_history=[QuoteSnapshot(**_parse_quote(item)) for item in request.get("quote_history", [])],
    )
    records = build_shadow_records_from_snapshot(snapshot, shadow_options)
    storage = await _shadow_store.record_records(records)
    return {
        "symbol_code": str(snapshot.get("symbol_code") or symbol).upper(),
        "session_date": snapshot.get("session_date"),
        "snapshot_mode": request.get("metadata", {}).get("snapshot_mode"),
        "source": request.get("metadata", {}).get("history_source"),
        "decision_count": len(list(bundle.agent_decisions or [])),
        "execution_count": len(list(bundle.execution_plan or [])),
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
    requested = list(dict.fromkeys(symbols or auto_symbols()))
    results: list[dict[str, Any]] = []
    failures: dict[str, str] = {}

    for symbol in requested:
        try:
            results.append(await capture_live_paper_cycle(symbol, shadow_options=shadow_options))
        except Exception as exc:
            failures[symbol] = str(exc)

    if not results and failures:
        joined = "; ".join(f"{symbol}: {detail}" for symbol, detail in failures.items())
        raise RuntimeError(f"Auction Intelligence paper cycle failed: {joined}")

    return {
        "symbols_requested": requested,
        "symbols_completed": [item["symbol_code"] for item in results],
        "result_count": len(results),
        "failure_count": len(failures),
        "failures": failures,
        "shadow_record_count": sum(int(item.get("shadow_record_count") or 0) for item in results),
        "journal_path_count": sum(int(item.get("journal_path_count") or 0) for item in results),
        "results": results,
    }
