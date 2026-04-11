"""Shared strategy-agent helpers used across paper trading runtimes."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from paper_engine.portfolio import PaperPortfolio, TradeRecord


IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist() -> datetime:
    return datetime.now(IST)


def _round_or_none(value: Optional[float], digits: int = 2) -> Optional[float]:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric != numeric or numeric in (float("inf"), float("-inf")):
        return None
    return round(numeric, digits)


def _parse_iso_timestamp(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def _latest_session_rows(
    rows: Sequence[dict[str, Any]],
    *,
    timestamp_keys: Iterable[str] = ("time",),
) -> tuple[list[dict[str, Any]], Optional[date]]:
    parsed_rows: list[tuple[datetime, dict[str, Any]]] = []
    keys = tuple(timestamp_keys)
    for row in rows:
        parsed: Optional[datetime] = None
        for key in keys:
            parsed = _parse_iso_timestamp(row.get(key))
            if parsed is not None:
                break
        if parsed is not None:
            parsed_rows.append((parsed, row))

    if not parsed_rows:
        return [], None

    parsed_rows.sort(key=lambda item: item[0])
    session_date = max(parsed.date() for parsed, _ in parsed_rows)
    session_rows = [row for parsed, row in parsed_rows if parsed.date() == session_date]
    return session_rows, session_date


def _latest_runtime_day(values: Iterable[Any]) -> Optional[date]:
    latest: Optional[date] = None
    for value in values:
        raw = str(value or "").strip()
        if not raw:
            continue
        try:
            candidate = date.fromisoformat(raw)
        except ValueError:
            parsed = _parse_iso_timestamp(raw)
            candidate = parsed.date() if parsed is not None else None
        if candidate is None:
            continue
        latest = max(latest, candidate) if latest else candidate
    return latest


def _ensure_ist_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=IST)
    return value.astimezone(IST)


def _serialize_trade_history(portfolio: PaperPortfolio) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for trade in getattr(portfolio, "_trade_history", []):
        rows.append(
            {
                "symbol": trade.symbol,
                "action": trade.action,
                "qty": int(trade.qty),
                "entry_price": float(trade.entry_price),
                "exit_price": float(trade.exit_price),
                "pnl": float(trade.pnl),
                "entry_time": trade.entry_time.isoformat(),
                "exit_time": trade.exit_time.isoformat(),
                "instrument_type": trade.instrument_type,
                "expiry": trade.expiry,
                "strike": trade.strike,
                "option_type": trade.option_type,
                "signal_id": trade.signal_id,
                "setup_type": trade.setup_type,
                "entry_iv_pct": trade.entry_iv_pct,
                "regime": trade.regime,
            }
        )
    return rows


def _deserialize_trade_history(rows: Sequence[dict[str, Any]]) -> list[TradeRecord]:
    trades: list[TradeRecord] = []
    for row in rows:
        entry_time = _parse_iso_timestamp(row.get("entry_time"))
        exit_time = _parse_iso_timestamp(row.get("exit_time"))
        if entry_time is None or exit_time is None:
            continue
        try:
            trades.append(
                TradeRecord(
                    symbol=str(row.get("symbol") or ""),
                    action=str(row.get("action") or ""),
                    qty=int(row.get("qty") or 0),
                    entry_price=float(row.get("entry_price") or 0.0),
                    exit_price=float(row.get("exit_price") or 0.0),
                    pnl=float(row.get("pnl") or 0.0),
                    entry_time=entry_time,
                    exit_time=exit_time,
                    instrument_type=str(row.get("instrument_type") or "CE"),
                    expiry=row.get("expiry"),
                    strike=float(row["strike"]) if row.get("strike") is not None else None,
                    option_type=row.get("option_type"),
                    signal_id=row.get("signal_id"),
                    setup_type=row.get("setup_type"),
                    entry_iv_pct=_round_or_none(row.get("entry_iv_pct"), 1),
                    regime=row.get("regime"),
                )
            )
        except (TypeError, ValueError):
            continue
    return trades


def _serialize_equity_curve(portfolio: PaperPortfolio) -> list[dict[str, Any]]:
    return [
        {"time": timestamp.isoformat(), "equity": float(equity)}
        for timestamp, equity in getattr(portfolio, "_equity_curve", [])
    ]


def _deserialize_equity_curve(rows: Sequence[dict[str, Any]]) -> list[tuple[datetime, float]]:
    curve: list[tuple[datetime, float]] = []
    for row in rows:
        timestamp = _parse_iso_timestamp(row.get("time"))
        if timestamp is None:
            continue
        try:
            curve.append((timestamp, float(row.get("equity") or 0.0)))
        except (TypeError, ValueError):
            continue
    return curve


class BaseStrategyAgent:
    """Common helper surface shared by strategy runtimes."""

    IST = IST
    _now_ist = staticmethod(_now_ist)
    _round_or_none = staticmethod(_round_or_none)
    _parse_iso_timestamp = staticmethod(_parse_iso_timestamp)
    _latest_session_rows = staticmethod(_latest_session_rows)
    _latest_runtime_day = staticmethod(_latest_runtime_day)
    _ensure_ist_datetime = staticmethod(_ensure_ist_datetime)
    _serialize_trade_history = staticmethod(_serialize_trade_history)
    _deserialize_trade_history = staticmethod(_deserialize_trade_history)
    _serialize_equity_curve = staticmethod(_serialize_equity_curve)
    _deserialize_equity_curve = staticmethod(_deserialize_equity_curve)
