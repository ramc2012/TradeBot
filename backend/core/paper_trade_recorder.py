"""Unified paper-trade recorder.

A thin facade every strategy can opt into to record open / mark / close events
with a uniform schema and to participate in a single end-of-session portfolio
reconciliation. Existing per-strategy paper stores keep writing their own
journals — this layer is additive and non-breaking.

Events are appended to `backend/runtime/portfolio/events.jsonl` and a per-day
snapshot is written to `backend/runtime/portfolio/daily_<YYYY-MM-DD>.json` by
`PaperTradeRecorder.snapshot_daily()`.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PORTFOLIO_ROOT = Path(__file__).resolve().parents[1] / "runtime" / "portfolio"
EVENTS_PATH = PORTFOLIO_ROOT / "events.jsonl"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def realized_pnl(entry_premium: float, exit_premium: float, quantity: int, fees: float = 0.0) -> float:
    """Single source of truth for paper realized P&L. Long-only options."""
    return round((float(exit_premium) - float(entry_premium)) * int(quantity) - float(fees), 2)


def unrealized_pnl(entry_premium: float, latest_premium: float, quantity: int) -> float:
    return round((float(latest_premium) - float(entry_premium)) * int(quantity), 2)


class PaperTradeRecorder:
    """Append-only event log + portfolio reconciliation snapshots."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        PORTFOLIO_ROOT.mkdir(parents=True, exist_ok=True)

    async def record_event(
        self,
        *,
        strategy: str,
        event: str,  # one of: open, mark, close
        underlying: str,
        instrument_key: str | None,
        option_type: str | None,
        strike: float | None,
        expiry: str | None,
        quantity: int,
        entry_premium: float | None = None,
        latest_premium: float | None = None,
        exit_premium: float | None = None,
        realized: float | None = None,
        unrealized: float | None = None,
        position_id: str | None = None,
        reason: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        record = {
            "ts": _utc_now_iso(),
            "strategy": strategy,
            "event": event,
            "underlying": str(underlying or "").upper(),
            "instrument_key": instrument_key,
            "option_type": option_type,
            "strike": _safe_float(strike) if strike is not None else None,
            "expiry": expiry,
            "quantity": _safe_int(quantity),
            "entry_premium": _safe_float(entry_premium) if entry_premium is not None else None,
            "latest_premium": _safe_float(latest_premium) if latest_premium is not None else None,
            "exit_premium": _safe_float(exit_premium) if exit_premium is not None else None,
            "realized": _safe_float(realized) if realized is not None else None,
            "unrealized": _safe_float(unrealized) if unrealized is not None else None,
            "position_id": position_id,
            "reason": reason,
            "extra": extra or {},
        }
        async with self._lock:
            PORTFOLIO_ROOT.mkdir(parents=True, exist_ok=True)
            with EVENTS_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, default=str) + "\n")

    async def snapshot_daily(self, session_date: str | None = None) -> dict[str, Any]:
        """Aggregate today's events into a per-strategy summary; persist + return."""
        target_date = session_date or datetime.now(timezone.utc).date().isoformat()
        per_strategy: dict[str, dict[str, Any]] = {}
        if EVENTS_PATH.exists():
            with EVENTS_PATH.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = str(rec.get("ts") or "")
                    if not ts.startswith(target_date):
                        continue
                    strategy = str(rec.get("strategy") or "unknown")
                    bucket = per_strategy.setdefault(
                        strategy,
                        {
                            "open_events": 0,
                            "mark_events": 0,
                            "close_events": 0,
                            "realized_total": 0.0,
                            "unrealized_open": 0.0,
                            "wins": 0,
                            "losses": 0,
                            "breakeven": 0,
                            "last_event_ts": None,
                        },
                    )
                    event = str(rec.get("event") or "")
                    if event == "open":
                        bucket["open_events"] += 1
                    elif event == "mark":
                        bucket["mark_events"] += 1
                        bucket["unrealized_open"] = _safe_float(rec.get("unrealized"))
                    elif event == "close":
                        bucket["close_events"] += 1
                        pnl = _safe_float(rec.get("realized"))
                        bucket["realized_total"] = round(bucket["realized_total"] + pnl, 2)
                        if pnl > 0:
                            bucket["wins"] += 1
                        elif pnl < 0:
                            bucket["losses"] += 1
                        else:
                            bucket["breakeven"] += 1
                    bucket["last_event_ts"] = ts

        snapshot = {
            "session_date": target_date,
            "generated_at": _utc_now_iso(),
            "per_strategy": per_strategy,
            "totals": {
                "realized": round(sum(v["realized_total"] for v in per_strategy.values()), 2),
                "unrealized_open": round(sum(v["unrealized_open"] for v in per_strategy.values()), 2),
                "open_events": sum(v["open_events"] for v in per_strategy.values()),
                "close_events": sum(v["close_events"] for v in per_strategy.values()),
            },
        }
        path = PORTFOLIO_ROOT / f"daily_{target_date}.json"
        async with self._lock:
            PORTFOLIO_ROOT.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
        return snapshot


paper_trade_recorder = PaperTradeRecorder()


__all__ = ["paper_trade_recorder", "PaperTradeRecorder", "realized_pnl", "unrealized_pnl"]
