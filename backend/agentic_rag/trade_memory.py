from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from uuid import uuid5, NAMESPACE_URL

from loguru import logger

from agentic_rag.schemas import TradeCaseRecord
from agentic_rag.service import rag_service


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value or "").strip()
    return text or None


def _minutes_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)
    return max((end_dt - start_dt).total_seconds() / 60.0, 0.0)


def _result_from_pnl(pnl: float | None, *, partial: bool) -> str:
    if partial:
        return "partial_win" if (pnl or 0.0) > 0 else "partial_loss" if (pnl or 0.0) < 0 else "partial_flat"
    if (pnl or 0.0) > 0:
        return "win"
    if (pnl or 0.0) < 0:
        return "loss"
    return "flat"


def _clean_tags(*values: Any) -> list[str]:
    tags: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in tags:
            tags.append(text)
    return tags


def build_strategy_trade_case(
    *,
    runtime_key: str,
    runtime_label: str,
    position: Any,
    exit_price: float,
    reason: str,
    close_qty: int,
    pnl: float,
    partial: bool,
    source: str,
) -> TradeCaseRecord:
    entry_price = _safe_float(getattr(position, "entry_price", None))
    peak_price = _safe_float(getattr(position, "peak_price", None))
    entered_at = _safe_iso(getattr(position, "entered_at", None))
    exit_time = datetime.now(timezone.utc).isoformat()
    hold_minutes = _minutes_between(entered_at, exit_time)
    return_pct = (
        ((float(exit_price) - entry_price) / entry_price) * 100.0
        if entry_price and entry_price > 0
        else None
    )
    mfe_pct = (
        ((peak_price - entry_price) / entry_price) * 100.0
        if peak_price is not None and entry_price and entry_price > 0
        else None
    )
    signal_reason = str(getattr(position, "signal_reason", "") or "").strip()
    setup_name = signal_reason or str(getattr(position, "spot_setup", "") or "").strip() or reason
    strategy_key = str(runtime_key or getattr(position, "strategy_key", "") or "unknown_strategy")
    underlying = str(getattr(position, "underlying", "") or "").strip() or "UNKNOWN"
    direction = str(getattr(position, "option_type", "") or getattr(position, "action", "") or "").strip() or None
    case_id = uuid5(
        NAMESPACE_URL,
        "|".join(
            [
                source,
                strategy_key,
                str(getattr(position, "symbol", "") or getattr(position, "live_symbol", "") or ""),
                str(entered_at or ""),
                str(exit_time),
                str(reason),
                str(close_qty),
            ]
        ),
    ).hex

    features = {
        "runtime_label": runtime_label,
        "exit_reason": reason,
        "partial_exit": partial,
        "closed_qty": close_qty,
        "initial_qty": getattr(position, "initial_qty", None),
        "remaining_qty_before_exit": getattr(position, "qty", None),
        "return_pct": round(return_pct, 4) if return_pct is not None else None,
        "mfe_pct": round(mfe_pct, 4) if mfe_pct is not None else None,
        "hold_minutes": round(hold_minutes, 2) if hold_minutes is not None else None,
        "strike": _safe_float(getattr(position, "strike", None)),
        "expiry": getattr(position, "expiry", None),
        "phase": getattr(position, "phase", None),
        "entry_bar_time": getattr(position, "entry_bar_time", None),
        "signal_strength": _safe_float(getattr(position, "signal_strength", None)),
        "entry_iv_pct": _safe_float(getattr(position, "entry_iv_pct", None)),
        "latest_rsi": _safe_float(getattr(position, "latest_rsi", None)),
        "spot_setup": getattr(position, "spot_setup", None),
        "regime": getattr(position, "regime", None),
        "option_ma20": _safe_float(getattr(position, "option_ma20", None)),
        "option_ma50": _safe_float(getattr(position, "option_ma50", None)),
        "above_option_ma20": getattr(position, "above_option_ma20", None),
        "above_option_ma50": getattr(position, "above_option_ma50", None),
        "atr": _safe_float(getattr(position, "atr", None)),
        "macd_value": _safe_float(getattr(position, "macd_value", None)),
        "mp_poc": _safe_float(getattr(position, "mp_poc", None)),
        "mp_vah": _safe_float(getattr(position, "mp_vah", None)),
        "mp_val": _safe_float(getattr(position, "mp_val", None)),
        "entry_style": getattr(position, "entry_style", None),
        "contract_unit_label": getattr(position, "contract_unit_label", None),
    }
    tags = _clean_tags(
        strategy_key,
        underlying,
        direction,
        setup_name,
        getattr(position, "regime", None),
        getattr(position, "spot_setup", None),
        getattr(position, "phase", None),
        reason,
        "partial_exit" if partial else "full_exit",
    )
    lesson = (
        f"{strategy_key} {underlying} {direction or ''} {setup_name} closed via {reason}; "
        f"PnL={pnl:.2f}, return={return_pct:.2f}%."
        if return_pct is not None
        else f"{strategy_key} {underlying} closed via {reason}; PnL={pnl:.2f}."
    )
    return TradeCaseRecord(
        id=case_id,
        strategy_key=strategy_key,
        underlying=underlying,
        symbol=str(getattr(position, "symbol", "") or getattr(position, "live_symbol", "") or "") or None,
        setup_name=setup_name,
        regime=str(getattr(position, "regime", "") or "") or None,
        direction=direction,
        entry_time=entered_at,
        exit_time=exit_time,
        entry_price=entry_price,
        exit_price=float(exit_price),
        pnl=float(pnl),
        r_multiple=(return_pct / 100.0) if return_pct is not None else None,
        result=_result_from_pnl(pnl, partial=partial),
        tags=tags,
        features=features,
        lesson=lesson,
        source=source,
    )


async def record_trade_case(trade_case: TradeCaseRecord) -> None:
    try:
        await asyncio.to_thread(rag_service.add_trade_case, trade_case)
    except Exception as exc:
        logger.warning(f"[AgenticRAG] Failed to persist trade case {trade_case.id}: {exc}")
