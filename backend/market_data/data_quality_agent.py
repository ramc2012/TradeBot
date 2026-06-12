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
from datetime import datetime, time, timedelta, timezone
from threading import RLock
from typing import Any, Optional

from loguru import logger

IST = timezone(timedelta(hours=5, minutes=30))
NSE_MARKET_OPEN = time(9, 15)
NSE_MARKET_CLOSE = time(15, 30)


def _is_nse_market_hours(now: datetime) -> bool:
    local = now.astimezone(IST)
    return local.weekday() < 5 and NSE_MARKET_OPEN <= local.time() <= NSE_MARKET_CLOSE


def _is_nse_realtime_symbol(symbol: str) -> bool:
    text = str(symbol or "").upper()
    return text.startswith("NSE:") or text.startswith("BSE:")


def _is_index_spot_symbol(symbol: str) -> bool:
    """Index spot feeds (NIFTY/BANKNIFTY/SENSEX) tick continuously during RTH,
    so a stuck value there means a wedged WS session. Single-name OPTION
    contracts routinely repeat the same LTP for minutes (no trades) — applying
    the frozen-value detector to them flips the whole verdict to critical and
    blocks S1 scans on normal market microstructure (2026-06-12: one illiquid
    TIINDIA PE froze the entire NSE lane for an hour)."""
    text = str(symbol or "").upper()
    return (text.startswith("NSE:") or text.startswith("BSE:")) and text.endswith("-INDEX")


@dataclass
class _SymbolHealth:
    symbol: str
    source: str
    last_seen_at: datetime
    last_value: Optional[float] = None
    flagged: bool = False
    flag_reason: Optional[str] = None
    consecutive_stale_checks: int = 0
    # When last_value last actually CHANGED. A feed that keeps delivering ticks
    # but repeats the same value (a frozen/stuck WS session) holds an old
    # value_changed_at while last_seen_at advances — that gap is the freeze a
    # plain timestamp-age check can't see.
    value_changed_at: Optional[datetime] = None


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
        "broker_quote": 30,
        # MCX futures quotes refresh on the commodity scan cadence (every
        # 30s) so a 30s budget falsely flags them stale on every cycle.
        # 90s gives ~3 scan windows of slack before flagging.
        "broker_futures_quote": 90,
        # Option contract quotes refresh only when the option watchlist
        # rebuilds (~every 3 min) — not via the live WS feed. A 30s budget
        # would mark every option contract stale immediately after refresh.
        # 300s gives ~5 watchlist cycles of slack before flagging.
        "broker_option_quote": 300,
        "upstox_tick": 30,
        "upstox_quote": 30,
        "postgres_minute": 120,
        "broker_history_15m": 1200,
        "broker_history_30m": 2400,
        "option_history_5m": 900,
        "option_history_30m": 2400,
        "mcx_tick": 60,
        "default": 60,
    }

    # An NSE/BSE index recomputes continuously from its constituents during
    # market hours — it essentially never holds the exact same LTP for more
    # than a few seconds. If the feed keeps ticking (age within budget) but the
    # value is byte-identical for longer than this, the WS session is frozen,
    # not the market. Conservative (90s) so a genuine lull never false-flags;
    # the indices still move sub-second in practice. A frozen required index
    # rolls the aggregate to "critical" → the NSE lane skips entries (fail-safe).
    FROZEN_VALUE_BUDGET_SECONDS: int = 90

    def __init__(self) -> None:
        self._lock = RLock()
        self._ledger: dict[tuple[str, str], _SymbolHealth] = {}
        self._budgets: dict[str, int] = dict(self.DEFAULT_BUDGETS)
        self._frozen_budget_seconds: int = int(self.FROZEN_VALUE_BUDGET_SECONDS)

    def update_budget(self, source: str, seconds: int) -> None:
        with self._lock:
            self._budgets[source] = max(1, int(seconds))

    def _budget_for(self, source: str) -> int:
        return int(self._budgets.get(source, self._budgets["default"]))

    def _frozen_seconds(self, health: "_SymbolHealth", when: datetime) -> Optional[float]:
        """Seconds the LTP has been stuck while the feed keeps ticking.

        Returns None when frozen detection does not apply: a non-NSE/BSE symbol,
        market closed, no value history yet, or the feed is already DEAD (age
        beyond budget — caught by the ordinary staleness check, not this one).
        A positive return means ticks are still arriving but the value has not
        moved for that many seconds.
        """
        if health.value_changed_at is None or health.last_value is None:
            return None
        if not _is_index_spot_symbol(health.symbol) or not _is_nse_market_hours(when):
            return None
        age = (when - health.last_seen_at).total_seconds()
        if age > self._budget_for(health.source):
            return None  # dead feed, not a frozen one
        return (when - health.value_changed_at).total_seconds()

    def record_tick(
        self,
        *,
        symbol: str,
        source: str,
        observed_at: Optional[datetime] = None,
        last_value: Optional[float] = None,
    ) -> None:
        """Producer hook — called whenever fresh market data is observed."""
        symbol = str(symbol or "").strip()
        source = str(source or "").strip() or "default"
        if not symbol:
            return
        when = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        key = (symbol, source)
        with self._lock:
            existing = self._ledger.get(key)
            if existing and existing.last_seen_at >= when:
                # Don't downgrade freshness if an older tick arrives late.
                return
            # Track when the value last actually MOVED so a frozen feed (ticks
            # arriving, value stuck) is detectable. Preserve the prior change
            # time only when the new value is byte-identical to the last one;
            # any change — or a first/None value — stamps `when`.
            if (
                existing is not None
                and existing.value_changed_at is not None
                and existing.last_value is not None
                and last_value is not None
                and last_value == existing.last_value
            ):
                value_changed_at = existing.value_changed_at
            else:
                value_changed_at = when
            self._ledger[key] = _SymbolHealth(
                symbol=symbol,
                source=source,
                last_seen_at=when,
                last_value=last_value,
                flagged=False,
                flag_reason=None,
                consecutive_stale_checks=0,
                value_changed_at=value_changed_at,
            )

    def flag(self, *, symbol: str, source: str, reason: str) -> None:
        symbol = str(symbol or "").strip()
        source = str(source or "").strip() or "default"
        if not symbol:
            return
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
        symbol = str(symbol or "").strip()
        source = str(source or "").strip() or "default"
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
            else:
                # Feed is delivering ticks on time — but is the value moving?
                frozen_for = self._frozen_seconds(health, when)
                if frozen_for is not None and frozen_for > self._frozen_budget_seconds:
                    stale = True
                    reason = (
                        f"{source} LTP stuck at {health.last_value} for "
                        f"{int(frozen_for)}s (>{self._frozen_budget_seconds}s) while "
                        f"ticks keep arriving — frozen feed."
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

    def assess_observation(
        self,
        *,
        symbol: str,
        source: str,
        observed_at: Optional[datetime],
        now: Optional[datetime] = None,
        last_value: Optional[float] = None,
    ) -> FreshnessVerdict:
        symbol = str(symbol or "").strip()
        source = str(source or "").strip() or "default"
        if observed_at is None:
            return FreshnessVerdict(
                symbol=symbol,
                source=source,
                age_seconds=float("inf"),
                stale=True,
                reason=f"No observation timestamp available for {source}.",
                last_value=last_value,
            )
        when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        observed = observed_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        observed = observed.astimezone(timezone.utc)
        budget = self._budget_for(source)
        age = max((when - observed).total_seconds(), 0.0)
        stale = age > budget
        return FreshnessVerdict(
            symbol=symbol,
            source=source,
            age_seconds=round(age, 2),
            stale=stale,
            reason=(
                f"Last {source} observation is {int(age)}s old, beyond the {budget}s budget."
                if stale
                else None
            ),
            last_value=last_value,
        )

    def is_ready(self, *, symbol: str, source: str) -> bool:
        return not self.assess_freshness(symbol=symbol, source=source).stale

    def snapshot(self, *, now: Optional[datetime] = None) -> dict[str, Any]:
        with self._lock:
            now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
            entries = []
            stale_entry_count = 0
            flagged_count = 0
            frozen_entry_count = 0
            by_symbol: dict[str, list[dict[str, Any]]] = {}
            for (symbol, source), health in self._ledger.items():
                age = (now - health.last_seen_at).total_seconds()
                budget = self._budget_for(source)
                stale = age > budget or health.flagged
                frozen_for = self._frozen_seconds(health, now)
                frozen = frozen_for is not None and frozen_for > self._frozen_budget_seconds
                if frozen:
                    stale = True
                if stale:
                    stale_entry_count += 1
                if frozen:
                    frozen_entry_count += 1
                if health.flagged:
                    flagged_count += 1
                entry = {
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
                    "frozen": frozen,
                    "frozen_seconds": round(frozen_for, 1) if frozen_for is not None else None,
                }
                entries.append(entry)
                by_symbol.setdefault(symbol, []).append(entry)
            entries.sort(key=lambda item: (-item["stale"], item["symbol"]))
            symbol_health: list[dict[str, Any]] = []
            stale_symbol_count = 0
            frozen_symbol_count = 0
            for symbol, source_entries in sorted(by_symbol.items()):
                symbol_stale = all(bool(item["stale"]) for item in source_entries)
                symbol_flagged = any(bool(item["flagged"]) for item in source_entries)
                symbol_frozen = any(bool(item.get("frozen")) for item in source_entries)
                if symbol_stale:
                    stale_symbol_count += 1
                if symbol_frozen:
                    frozen_symbol_count += 1
                freshest = min(source_entries, key=lambda item: float(item["age_seconds"]))
                symbol_health.append(
                    {
                        "symbol": symbol,
                        "stale": symbol_stale,
                        "flagged": symbol_flagged,
                        "frozen": symbol_frozen,
                        "freshest_source": freshest["source"],
                        "freshest_age_seconds": freshest["age_seconds"],
                        "sources": len(source_entries),
                    }
                )
            overall = "healthy"
            # A frozen required index feed (ticks arriving, value stuck) is as
            # dangerous as a flagged/dead one — both put a stale price in front
            # of the agent. Roll it into "critical" so the NSE lane's
            # critical-gate skips entries until the value moves again.
            if flagged_count or frozen_symbol_count:
                overall = "critical"
            elif stale_symbol_count:
                stale_symbols = [
                    row["symbol"]
                    for row in symbol_health
                    if bool(row.get("stale"))
                ]
                if stale_symbols and all(_is_nse_realtime_symbol(symbol) for symbol in stale_symbols) and not _is_nse_market_hours(now):
                    overall = "idle"
                else:
                    overall = "degraded"
            return {
                "overall": overall,
                "market_state": "nse_open" if _is_nse_market_hours(now) else "nse_closed",
                "symbol_count": len(symbol_health),
                "entry_count": len(entries),
                "stale_count": stale_symbol_count,
                "stale_entry_count": stale_entry_count,
                "frozen_count": frozen_symbol_count,
                "frozen_entry_count": frozen_entry_count,
                "flagged_count": flagged_count,
                "budgets": dict(self._budgets),
                "symbol_health": symbol_health,
                "entries": entries,
            }


data_quality_agent = DataQualityAgent()


__all__ = ["data_quality_agent", "DataQualityAgent", "FreshnessVerdict"]
