"""Cross-strategy data-quality agent.

Strategy agents should never enter a trade on stale or malformed market data.
Today, the only freshness signal in the codebase is the `market_data` service
status surfaced by `/api/system/health` — and it only flags after a tick is
already overdue by minutes. There is no single owner for "is the data I'm
about to act on trustworthy."

This agent is that owner. Strategy agents call `assess_freshness(...)` or
`is_ready(...)` before deciding to scan/enter. The agent maintains an
in-memory ledger of last-seen timestamps per (broker_symbol, source) and
returns an aggregate health view for the dashboard.

Design rules:
  * Read-only. Never mutates strategy state.
  * Cheap. Avoid DB round-trips on the hot path.
  * Cooperative. Updates come from any producer that touches the data
    pipeline (DataRouter, history fetchers, paper agents on snapshot).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Optional

from loguru import logger


@dataclass
class _SymbolHealth:
    symbol: str
    source: str
    last_seen_at: datetime
    last_value: Optional[float] = None
    flagged: bool = False
    flag_reason: Optional[str] = None
    consecutive_stale_checks: int = 0


@dataclass
class FreshnessVerdict:
    symbol: str
    source: str
    age_seconds: float
    stale: bool
    reason: Optional[str] = None
    last_value: Optional[float] = None


class DataQualityAgent:
    """In-memory ledger + freshness rules. Process-local; single instance."""

    # Per-source freshness budgets in seconds. Override via update_budget().
    DEFAULT_BUDGETS: dict[str, int] = {
        "fyers_tick": 30,
        "fyers_quote": 30,
        "upstox_tick": 30,
        "upstox_quote": 30,
        "postgres_minute": 120,
        "broker_history_15m": 1200,
        "broker_history_30m": 2400,
        "mcx_tick": 60,
        "default": 60,
    }

    def __init__(self) -> None:
        self._lock = RLock()
        self._ledger: dict[tuple[str, str], _SymbolHealth] = {}
        self._budgets: dict[str, int] = dict(self.DEFAULT_BUDGETS)

    def update_budget(self, source: str, seconds: int) -> None:
        with self._lock:
            self._budgets[source] = max(1, int(seconds))

    def _budget_for(self, source: str) -> int:
        return int(self._budgets.get(source, self._budgets["default"]))

    def record_tick(
        self,
        *,
        symbol: str,
        source: str,
        observed_at: Optional[datetime] = None,
        last_value: Optional[float] = None,
    ) -> None:
        """Producer hook — called whenever fresh market data is observed."""
        when = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        key = (symbol, source)
        with self._lock:
            existing = self._ledger.get(key)
            if existing and existing.last_seen_at >= when:
                # Don't downgrade freshness if an older tick arrives late.
                return
            self._ledger[key] = _SymbolHealth(
                symbol=symbol,
                source=source,
                last_seen_at=when,
                last_value=last_value,
                flagged=False,
                flag_reason=None,
                consecutive_stale_checks=0,
            )

    def flag(self, *, symbol: str, source: str, reason: str) -> None:
        key = (symbol, source)
        with self._lock:
            existing = self._ledger.get(key)
            if existing is None:
                existing = _SymbolHealth(
                    symbol=symbol,
                    source=source,
                    last_seen_at=datetime.now(timezone.utc) - timedelta(days=1),
                )
                self._ledger[key] = existing
            existing.flagged = True
            existing.flag_reason = reason

    def assess_freshness(
        self,
        *,
        symbol: str,
        source: str,
        now: Optional[datetime] = None,
    ) -> FreshnessVerdict:
        when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        budget = self._budget_for(source)
        key = (symbol, source)
        with self._lock:
            health = self._ledger.get(key)
            if health is None:
                return FreshnessVerdict(
                    symbol=symbol,
                    source=source,
                    age_seconds=float("inf"),
                    stale=True,
                    reason=f"No observation recorded for {source}.",
                )
            age = (when - health.last_seen_at).total_seconds()
            stale = age > budget or health.flagged
            reason: Optional[str] = None
            if health.flagged:
                reason = health.flag_reason or "Flagged by upstream check."
            elif age > budget:
                reason = (
                    f"Last {source} observation is {int(age)}s old, "
                    f"beyond the {budget}s budget."
                )
            if stale:
                health.consecutive_stale_checks += 1
            else:
                health.consecutive_stale_checks = 0
            return FreshnessVerdict(
                symbol=symbol,
                source=source,
                age_seconds=round(age, 2),
                stale=stale,
                reason=reason,
                last_value=health.last_value,
            )

    def is_ready(self, *, symbol: str, source: str) -> bool:
        return not self.assess_freshness(symbol=symbol, source=source).stale

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = datetime.now(timezone.utc)
            entries = []
            stale_count = 0
            flagged_count = 0
            for (symbol, source), health in self._ledger.items():
                age = (now - health.last_seen_at).total_seconds()
                budget = self._budget_for(source)
                stale = age > budget or health.flagged
                if stale:
                    stale_count += 1
                if health.flagged:
                    flagged_count += 1
                entries.append(
                    {
                        "symbol": symbol,
                        "source": source,
                        "last_seen_at": health.last_seen_at.isoformat(),
                        "age_seconds": round(age, 2),
                        "budget_seconds": budget,
                        "stale": stale,
                        "flagged": health.flagged,
                        "flag_reason": health.flag_reason,
                        "consecutive_stale_checks": health.consecutive_stale_checks,
                        "last_value": health.last_value,
                    }
                )
            entries.sort(key=lambda item: (-item["stale"], item["symbol"]))
            overall = "healthy"
            if flagged_count:
                overall = "critical"
            elif stale_count:
                overall = "degraded"
            return {
                "overall": overall,
                "symbol_count": len(entries),
                "stale_count": stale_count,
                "flagged_count": flagged_count,
                "budgets": dict(self._budgets),
                "entries": entries,
            }


data_quality_agent = DataQualityAgent()


__all__ = ["data_quality_agent", "DataQualityAgent", "FreshnessVerdict"]
