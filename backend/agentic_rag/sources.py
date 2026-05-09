from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from agentic_rag.schemas import TradeCaseRecord
from agentic_rag.text import normalized_upper

BACKEND_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = BACKEND_ROOT / "runtime"
MP_ROOT = BACKEND_ROOT / "mp_data"


def collect_runtime_trade_cases(limit: int = 500) -> list[TradeCaseRecord]:
    cases: list[TradeCaseRecord] = []
    cases.extend(_collect_mp_rows())
    cases.extend(_collect_paper_position_cases())
    cases.extend(_collect_paper_journal_cases())
    cases.sort(key=lambda item: item.entry_time or item.created_at, reverse=True)
    return cases[:limit]


def _collect_mp_rows() -> list[TradeCaseRecord]:
    cases: list[TradeCaseRecord] = []
    for path in sorted(MP_ROOT.glob("underlying=*/enriched_mp_with_failures.csv")):
        underlying = path.parent.name.split("=", 1)[-1].upper()
        cases.extend(_cases_from_mp_csv(path, underlying))
    runtime_mp = RUNTIME_ROOT / "index_analytics_data" / "market_profile"
    for path in sorted(runtime_mp.glob("underlying=*/enriched_mp_with_failures.csv")):
        underlying = path.parent.name.split("=", 1)[-1].upper()
        cases.extend(_cases_from_mp_csv(path, underlying))
    return cases


def _cases_from_mp_csv(path: Path, underlying: str) -> list[TradeCaseRecord]:
    rows: list[TradeCaseRecord] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                date_value = row.get("date") or row.get("session_date")
                if not date_value:
                    continue
                day_type = str(row.get("day_type") or row.get("dayType") or "unknown")
                direction = str(row.get("direction") or row.get("signal") or "NEUTRAL").upper()
                move = _float(row.get("daily_move") or row.get("move") or row.get("next_move"))
                pnl = _directional_outcome(direction, move)
                result = "win" if pnl and pnl > 0 else "loss" if pnl and pnl < 0 else "neutral"
                features = {
                    "day_type": day_type,
                    "close_location": _float(row.get("close_location")),
                    "range_factor": _float(row.get("range_factor")),
                    "buyer_fail": _float(row.get("buyer_fail")),
                    "seller_fail": _float(row.get("seller_fail")),
                    "net_failure": _float(row.get("net_failure")),
                    "inside_value": _bool(row.get("inside_value")),
                    "above_value": _bool(row.get("above_value")),
                    "below_value": _bool(row.get("below_value")),
                    "poor_high": _bool(row.get("poor_high")),
                    "poor_low": _bool(row.get("poor_low")),
                    "poc": _float(row.get("poc")),
                    "close": _float(row.get("close")),
                }
                tags = [
                    day_type,
                    direction,
                    "inside_value" if features["inside_value"] else "",
                    "above_value" if features["above_value"] else "",
                    "below_value" if features["below_value"] else "",
                    "poor_high" if features["poor_high"] else "",
                    "poor_low" if features["poor_low"] else "",
                ]
                rows.append(
                    TradeCaseRecord(
                        id=f"mp:{underlying}:{date_value}",
                        strategy_key="auction_intelligence",
                        underlying=underlying,
                        symbol=f"{underlying} FUT",
                        setup_name=day_type,
                        regime=day_type,
                        direction=direction,
                        entry_time=str(date_value),
                        exit_time=str(date_value),
                        pnl=pnl,
                        result=result,
                        tags=[tag for tag in tags if tag],
                        features={key: value for key, value in features.items() if value is not None},
                        lesson=f"Historical MP case: {day_type} {direction} with daily move {move}.",
                        source=str(path.relative_to(BACKEND_ROOT)),
                    )
                )
    except OSError:
        return []
    return rows


def _collect_paper_position_cases() -> list[TradeCaseRecord]:
    cases: list[TradeCaseRecord] = []
    for path in sorted(RUNTIME_ROOT.rglob("paper_positions.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for row in payload.get("closed_positions", []) or []:
            underlying = normalized_upper(row.get("underlying") or row.get("underlying_symbol") or row.get("symbol"))
            if not underlying:
                continue
            strategy_key = _strategy_from_path(path)
            pnl = _float(row.get("realized_pnl") or row.get("pnl"))
            result = "win" if pnl and pnl > 0 else "loss" if pnl and pnl < 0 else "neutral"
            setup_name = str(row.get("setup_name") or row.get("signal_reason") or row.get("regime") or "paper_case")
            cases.append(
                TradeCaseRecord(
                    id=f"{strategy_key}:position:{row.get('position_id') or row.get('symbol') or len(cases)}",
                    strategy_key=strategy_key,
                    underlying=underlying,
                    symbol=str(row.get("trading_symbol") or row.get("symbol") or underlying),
                    setup_name=setup_name,
                    regime=str(row.get("regime") or row.get("daily_shape") or row.get("phase") or ""),
                    direction=str(row.get("action") or row.get("option_type") or row.get("direction") or ""),
                    entry_time=str(row.get("opened_at") or row.get("entered_at") or row.get("entry_time") or ""),
                    exit_time=str(row.get("closed_at") or row.get("exit_time") or ""),
                    entry_price=_float(row.get("entry_premium") or row.get("entry_price")),
                    exit_price=_float(row.get("exit_premium") or row.get("exit_price")),
                    pnl=pnl,
                    result=result,
                    tags=[setup_name, result, str(row.get("option_type") or "")],
                    features={key: row.get(key) for key in ("confidence", "strike", "expiry", "option_type", "latest_spot", "expected_pnl") if row.get(key) is not None},
                    lesson=str(row.get("close_reason") or row.get("selection_reason") or ""),
                    source=str(path.relative_to(BACKEND_ROOT)),
                )
            )
    return cases


def _collect_paper_journal_cases() -> list[TradeCaseRecord]:
    cases: list[TradeCaseRecord] = []
    for path in sorted(RUNTIME_ROOT.rglob("paper_journal.jsonl")):
        strategy_key = _strategy_from_path(path)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines[-250:]):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            underlying = normalized_upper(row.get("underlying") or row.get("underlying_symbol") or row.get("symbol"))
            if not underlying:
                continue
            setup_name = str(row.get("setup_name") or row.get("regime") or row.get("signal_reason") or "paper_journal")
            cases.append(
                TradeCaseRecord(
                    id=f"{strategy_key}:journal:{row.get('recorded_at') or index}",
                    strategy_key=strategy_key,
                    underlying=underlying,
                    symbol=str(row.get("trading_symbol") or row.get("symbol") or underlying),
                    setup_name=setup_name,
                    regime=str(row.get("regime") or row.get("daily_shape") or row.get("hourly_shape") or ""),
                    direction=str(row.get("action") or row.get("option_type") or ""),
                    entry_time=str(row.get("recorded_at") or ""),
                    result="open_or_logged",
                    tags=[setup_name, str(row.get("execution_style") or ""), str(row.get("option_type") or "")],
                    features={key: row.get(key) for key in ("confidence", "premium", "strike", "expiry", "order_flow_bias") if row.get(key) is not None},
                    lesson="Logged paper signal without final closed-position outcome yet.",
                    source=str(path.relative_to(BACKEND_ROOT)),
                )
            )
    return cases


def _strategy_from_path(path: Path) -> str:
    parts = {part.lower() for part in path.parts}
    if "directional_options" in parts:
        return "directional_long_options"
    if "fractal_market_profile" in parts:
        return "fractal_market_profile"
    if "auction_intelligence" in parts:
        return "auction_intelligence"
    return "strategy_agent"


def _float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _directional_outcome(direction: str, move: float | None) -> float | None:
    if move is None:
        return None
    normalized = direction.upper()
    if normalized in {"CE", "LONG", "BUY", "BULLISH"}:
        return move
    if normalized in {"PE", "SHORT", "SELL", "BEARISH"}:
        return -move
    return 0.0
