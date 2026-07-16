"""Shared strategy-agent helpers used across paper trading runtimes."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from paper_engine.portfolio import PaperPortfolio, TradeRecord


IST = timezone(timedelta(hours=5, minutes=30))


# ── Persist-payload bounds (F-18, 2026-07-15) ─────────────────────────────────
# The app_runtime_state JSONB blobs grow DAILY (equity curve points every scan,
# a full trade row per closed trade, events) and the blob is json-decoded on
# the event loop on every restore/refresh — py-spy caught MainThread seized
# decoding a giant state blob, the prime "stales later each day" mechanism.
# Persisted payloads are therefore TRIMMED at these bounds; anything older is
# folded into a compact aggregate (trades) or dropped (curve/events). The
# durable per-trade record lives in the append-only paper_trade_book table,
# so nothing is lost — only the hot blob stays bounded.
PERSIST_EQUITY_CURVE_MAX_POINTS = 2000
PERSIST_RECENT_EVENTS_MAX = 200
PERSIST_TRADE_HISTORY_MAX_ROWS = 500


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


def _sort_trades_recent_first(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return trades sorted by exit_time (fallback: entry_time) descending."""
    def _key(row: dict[str, Any]) -> str:
        return str(row.get("exit_time") or row.get("entry_time") or "")

    return sorted(rows, key=_key, reverse=True)


def _split_today_history(
    rows: Sequence[dict[str, Any]],
    *,
    session_date: Optional[date] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split serialized trades into (today, history) using IST session date.

    A trade lands in 'today' if its exit_time falls on today's IST date.
    Each bucket is sorted recent-first.
    """
    if session_date is None:
        session_date = datetime.now(IST).date()
    today: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    for row in rows:
        ts_text = str(row.get("exit_time") or row.get("entry_time") or "")
        ts = _parse_iso_timestamp(ts_text)
        bucket = today if ts is not None and ts.astimezone(IST).date() == session_date else history
        bucket.append(row)
    return _sort_trades_recent_first(today), _sort_trades_recent_first(history)


def _summarize_trade_rows(
    rows: Sequence[dict[str, Any]],
    prior: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Fold serialized trade rows into a compact aggregate, optionally on top
    of a prior summary (the one restored from the last persisted payload)."""
    summary: dict[str, Any] = {
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "pnl": 0.0,
        "first_entry_time": None,
        "last_exit_time": None,
    }
    if isinstance(prior, dict):
        for key in ("trades", "wins", "losses"):
            try:
                summary[key] = int(prior.get(key) or 0)
            except (TypeError, ValueError):
                pass
        try:
            summary["pnl"] = float(prior.get("pnl") or 0.0)
        except (TypeError, ValueError):
            pass
        summary["first_entry_time"] = prior.get("first_entry_time")
        summary["last_exit_time"] = prior.get("last_exit_time")
    for row in rows:
        try:
            pnl = float(row.get("pnl") or 0.0)
        except (TypeError, ValueError):
            pnl = 0.0
        summary["trades"] += 1
        if pnl > 0:
            summary["wins"] += 1
        elif pnl < 0:
            summary["losses"] += 1
        summary["pnl"] = round(float(summary["pnl"]) + pnl, 2)
        entry_time = str(row.get("entry_time") or "") or None
        exit_time = str(row.get("exit_time") or "") or None
        if entry_time and (summary["first_entry_time"] is None or entry_time < summary["first_entry_time"]):
            summary["first_entry_time"] = entry_time
        if exit_time and (summary["last_exit_time"] is None or exit_time > summary["last_exit_time"]):
            summary["last_exit_time"] = exit_time
    return summary


def _trade_history_persist_payload(
    portfolio: PaperPortfolio,
    *,
    max_rows: int = PERSIST_TRADE_HISTORY_MAX_ROWS,
) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    """(rows, summary) for the persisted payload: the last `max_rows` trades
    verbatim, everything older folded into a summary on top of the archive
    summary restored at load time (``portfolio._archived_trade_summary``).

    Pure/idempotent per persist: the summary is recomputed from the immutable
    restored base + the CURRENT in-memory overflow, so repeated persists never
    double-count a trade. Backward-compatible both ways — old payloads simply
    have no ``trade_history_summary`` (base None) and old readers ignore the
    extra key."""
    rows = _serialize_trade_history(portfolio)
    base = getattr(portfolio, "_archived_trade_summary", None)
    if len(rows) <= max_rows:
        return rows, (dict(base) if isinstance(base, dict) else None)
    overflow, kept = rows[:-max_rows], rows[-max_rows:]
    return kept, _summarize_trade_rows(overflow, prior=base)


def _restore_archived_trade_summary(
    portfolio: PaperPortfolio, portfolio_payload: dict[str, Any]
) -> None:
    """Stash the persisted trade-history summary on the portfolio so the next
    persist folds new overflow on top of it instead of losing the aggregate."""
    summary = portfolio_payload.get("trade_history_summary")
    portfolio._archived_trade_summary = dict(summary) if isinstance(summary, dict) else None


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


def _serialize_equity_curve(
    portfolio: PaperPortfolio,
    *,
    max_points: int = PERSIST_EQUITY_CURVE_MAX_POINTS,
) -> list[dict[str, Any]]:
    # Persist only the most recent `max_points` — the curve grows per scan and
    # is the biggest single contributor to the F-18 state-blob bloat (matches
    # the portfolio's own Redis snapshot bound of 2000 points).
    curve = getattr(portfolio, "_equity_curve", [])
    if max_points and max_points > 0:
        curve = curve[-max_points:]
    return [
        {"time": timestamp.isoformat(), "equity": float(equity)}
        for timestamp, equity in curve
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
    _trade_history_persist_payload = staticmethod(_trade_history_persist_payload)
    _summarize_trade_rows = staticmethod(_summarize_trade_rows)
    _restore_archived_trade_summary = staticmethod(_restore_archived_trade_summary)
