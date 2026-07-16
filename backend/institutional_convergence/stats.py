"""Pure trade-surface math for the institutional-convergence paper books.

Everything in this module is a pure function over position dicts (the JSON
shapes persisted by ``ConvergencePaperBook``) so the statistics endpoints and
the tests share one implementation.  No I/O, no globals, no imports from the
paper module (paper imports from here).

Backward compatibility: positions written before the trade-surface work lack
``initial_stop`` (the stop is mutated to break-even at target1) and may lack
``initial_lots``.  Every function degrades gracefully — R-multiples become
``None`` instead of lying, durations become ``None`` when timestamps are
missing or unparseable.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any


DEFAULT_INITIAL_CAPITAL = 1_000_000.0


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def initial_risk_amount(position: dict[str, Any]) -> float:
    """Rupee risk at entry: |entry − initial stop| × lot_size × initial lots.

    Positions persisted before ``initial_stop`` existed fall back to the
    current ``stop`` — after a break-even move that collapses to 0, which the
    R-multiple helpers treat as "unknown" rather than dividing by it.
    """
    entry = _number(position.get("entry_price"))
    stop = _number(position.get("initial_stop"), _number(position.get("stop")))
    lot_size = _number(position.get("lot_size"))
    lots = _number(position.get("initial_lots"), _number(position.get("lots")))
    return abs(entry - stop) * lot_size * lots


def closed_r_multiple(position: dict[str, Any]) -> float | None:
    risk = initial_risk_amount(position)
    if risk <= 0:
        return None
    return round(_number(position.get("realized_pnl")) / risk, 3)


def trade_records(closed_positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flat (CSV-able) rows for the closed-trade book."""
    records: list[dict[str, Any]] = []
    for position in closed_positions:
        opened = _parse_time(position.get("opened_at"))
        closed = _parse_time(position.get("closed_at"))
        duration = (
            round((closed - opened).total_seconds() / 60.0, 2)
            if opened is not None and closed is not None
            else None
        )
        records.append(
            {
                "position_id": position.get("position_id"),
                "symbol": position.get("symbol"),
                "direction": position.get("direction"),
                "futures_contract": position.get("futures_contract"),
                "session_date": position.get("session_date"),
                "opened_at": position.get("opened_at"),
                "closed_at": position.get("closed_at"),
                "entry_price": _number(position.get("entry_price")),
                "exit_price": _number(position.get("exit_price")),
                "exit_reason": position.get("exit_reason"),
                "lots": int(_number(position.get("initial_lots"), _number(position.get("lots")))),
                "lot_size": int(_number(position.get("lot_size"), 1)),
                "pnl": round(_number(position.get("realized_pnl")), 2),
                "r_multiple": closed_r_multiple(position),
                "duration_minutes": duration,
            }
        )
    return records


def open_position_detail(position: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Derived read-time fields for one open position (never persisted)."""
    entry = _number(position.get("entry_price"))
    current = _number(position.get("current_price"), entry)
    direction = 1 if position.get("direction") == "LONG" else -1
    lots = _number(position.get("lots"))
    lot_size = _number(position.get("lot_size"))
    unrealized = (current - entry) * lot_size * lots * direction
    realized = _number(position.get("realized_pnl"))
    risk = initial_risk_amount(position)

    def _adverse_buffer(level: Any) -> float | None:
        # Positive = buffer remaining before the level is hit adversely.
        return round((current - _number(level)) * direction, 4) if level is not None else None

    def _favourable_remaining(level: Any) -> float | None:
        # Positive = distance still to travel to reach the target.
        return round((_number(level) - current) * direction, 4) if level is not None else None

    def _pct(distance: float | None) -> float | None:
        return round(distance / current * 100.0, 4) if distance is not None and current > 0 else None

    opened = _parse_time(position.get("opened_at"))
    age_minutes = (
        round((now - opened).total_seconds() / 60.0, 2) if opened is not None else None
    )
    stop_distance = _adverse_buffer(position.get("stop"))
    target1_distance = _favourable_remaining(position.get("target1"))
    target2_distance = _favourable_remaining(position.get("target2"))
    total_pnl = realized + unrealized
    return {
        "unrealized_pnl": round(unrealized, 2),
        "total_pnl": round(total_pnl, 2),
        "r_multiple": round(total_pnl / risk, 3) if risk > 0 else None,
        "initial_risk_amount": round(risk, 2),
        "age_minutes": age_minutes,
        "stop_distance": stop_distance,
        "stop_distance_pct": _pct(stop_distance),
        "target1_distance": target1_distance,
        "target1_distance_pct": _pct(target1_distance),
        "target2_distance": target2_distance,
        "target2_distance_pct": _pct(target2_distance),
    }


def _breakdown(trades: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        groups.setdefault(str(trade.get(key) or "unknown"), []).append(trade)
    result: dict[str, dict[str, Any]] = {}
    for name in sorted(groups):
        rows = groups[name]
        pnls = [row["pnl"] for row in rows]
        wins = sum(1 for value in pnls if value > 0)
        rs = [row["r_multiple"] for row in rows if row["r_multiple"] is not None]
        result[name] = {
            "trades": len(rows),
            "wins": wins,
            "losses": sum(1 for value in pnls if value < 0),
            "win_rate": round(wins / len(rows), 4) if rows else None,
            "pnl": round(sum(pnls), 2),
            "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
        }
    return result


def compute_statistics(
    closed_positions: list[dict[str, Any]],
    initial_capital: float = DEFAULT_INITIAL_CAPITAL,
) -> dict[str, Any]:
    """Win rate, avg R, profit factor, expectancy, max drawdown, breakdowns,
    and the daily-pnl series — all from the closed-trade book only."""
    trades = trade_records(closed_positions)
    ordered = sorted(trades, key=lambda row: str(row.get("closed_at") or ""))
    pnls = [row["pnl"] for row in ordered]
    total = len(ordered)
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    rs = [row["r_multiple"] for row in ordered if row["r_multiple"] is not None]

    equity = peak = float(initial_capital)
    max_drawdown = max_drawdown_pct = 0.0
    for value in pnls:
        equity += value
        peak = max(peak, equity)
        drawdown = peak - equity
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            max_drawdown_pct = drawdown / peak if peak > 0 else 0.0

    daily: dict[str, dict[str, Any]] = {}
    for row in ordered:
        day = str(row.get("session_date") or str(row.get("closed_at") or "")[:10] or "unknown")
        bucket = daily.setdefault(day, {"date": day, "pnl": 0.0, "trades": 0, "wins": 0})
        bucket["pnl"] += row["pnl"]
        bucket["trades"] += 1
        bucket["wins"] += 1 if row["pnl"] > 0 else 0
    daily_series: list[dict[str, Any]] = []
    cumulative = 0.0
    for day in sorted(daily):
        bucket = daily[day]
        bucket["pnl"] = round(bucket["pnl"], 2)
        cumulative += bucket["pnl"]
        bucket["cumulative_pnl"] = round(cumulative, 2)
        daily_series.append(bucket)

    durations = [row["duration_minutes"] for row in ordered if row["duration_minutes"] is not None]
    return {
        "trade_count": total,
        "wins": len(wins),
        "losses": len(losses),
        "scratches": total - len(wins) - len(losses),
        "win_rate": round(len(wins) / total, 4) if total else None,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "net_pnl": round(sum(pnls), 2),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else None,
        "expectancy": round(sum(pnls) / total, 2) if total else None,
        "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
        "r_sample_size": len(rs),
        "avg_win": round(gross_profit / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "max_drawdown": round(max_drawdown, 2),
        "max_drawdown_pct": round(max_drawdown_pct * 100.0, 4),
        "avg_duration_minutes": round(sum(durations) / len(durations), 2) if durations else None,
        "initial_capital": float(initial_capital),
        "per_symbol": _breakdown(ordered, "symbol"),
        "per_exit_reason": _breakdown(ordered, "exit_reason"),
        "daily_pnl": daily_series,
    }
