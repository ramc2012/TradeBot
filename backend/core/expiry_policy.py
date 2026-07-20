"""Calendar-driven expiry policy — the single source of truth for
"which expiry does this instrument trade RIGHT NOW".

Owner spec (2026-07-20):

    "First pre-market fix the expiries to be traded, Indices till expiry
     and stocks till five days to expiry, After that shift watchlist for
     stocks to be moved to next expiry, it is to avoid compulsory
     settlements."

Design notes
------------
* Expiry dates are DERIVED from exchange board rules (per-symbol expiry
  weekday, last such weekday of the month) walked backward past market
  holidays using ``core.trading_calendar`` — the SAME holiday table the
  ops surface edits.  Before this module the expiry math used a private,
  hand-maintained ``analysis.instruments._KNOWN_HOLIDAYS`` set that had
  diverged from the calendar (it disagreed on the NSE monthly for
  2026-03 and 2026-11 and on the SENSEX monthly for 2026-03 and
  2026-05).  There is now exactly ONE holiday table.

* The broker is a VALIDATOR, not the hot path.  ``get_expiries`` used to
  probe the broker for nine hard-coded symbols on every cycle; on
  2026-07-20 that produced 405 ``TimeoutError``s, every one swallowed by
  an inner ``except`` that degraded silently to a stale ladder.  Here the
  calendar answers immediately and ``validate_against_exchange`` runs
  ONCE per session; a disagreement is LOUD (error log + durable runtime
  marker) and the exchange wins, never a silent fallback.

* ``core/`` placement is deliberate: this module imports only
  ``core.trading_calendar`` + stdlib, so both MACD stacks (and, later,
  ``directional_options``) can use it without dragging in the broker or
  watchlist graph.

Nothing here changes strategy math.  Entry gates that happen to be
expressed in days-to-expiry (``MIN_TTE_DAYS_INDEX``,
``MIN_TTE_DAYS_STOCK`` as an ENTRY gate, ``macd_refined``'s
``entry_window_days_before_expiry``) stay exactly where they are.  This
module only answers *which contract month the watchlist points at*.
"""
from __future__ import annotations

import logging
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# ── Owner-directed roll policy (2026-07-20) ───────────────────────────────
# Indian SINGLE-STOCK options are PHYSICALLY SETTLED.  Holding one into
# the final days risks compulsory delivery / assignment of the underlying
# shares plus the exchange's escalating delivery margin.  So the STOCK
# WATCHLIST rolls to the next monthly once <= 5 trading days remain, and
# no NEW position is ever opened inside the delivery-risk window.
#
# (Positions ALREADY open on the near expiry are NOT rolled by this
# module — they stay on their own contract until they close.  See the
# sticky-strike mechanism in market_data.macd_watchlist.)
STOCK_ROLL_TRADING_DAYS = 5

# Indices are CASH-settled: no delivery risk, so they trade until expiry.
INDEX_ROLL_TRADING_DAYS = 0


def _stock_roll_trading_days() -> int:
    """Owner-tunable roll horizon, defaulting to the module constant.

    Read through settings so the horizon can be changed without a code edit,
    but NEVER silently: a nonsense value falls back to the constant loudly.
    """
    try:
        from core.config import settings

        value = int(getattr(settings, "EXPIRY_POLICY_STOCK_ROLL_TRADING_DAYS", STOCK_ROLL_TRADING_DAYS))
    except Exception:  # noqa: BLE001 - settings unavailable in bare unit contexts
        return STOCK_ROLL_TRADING_DAYS
    if value < 0 or value > 20:
        logger.error(
            "[ExpiryPolicy] EXPIRY_POLICY_STOCK_ROLL_TRADING_DAYS=%s is out of the sane "
            "range [0, 20] — falling back to %s. Fix the setting.",
            value,
            STOCK_ROLL_TRADING_DAYS,
        )
        return STOCK_ROLL_TRADING_DAYS
    return value

_INDEX_KINDS = {"INDEX", "IDX", "INDICES"}


class ExpiryAnchor(str, Enum):
    """Where the returned expiry ultimately came from."""

    CALENDAR = "calendar"                      # derived from trading_calendar rules
    EXCHANGE_CONFIRMED = "exchange_confirmed"  # calendar agreed with the broker ladder
    EXCHANGE_OVERRIDE = "exchange_override"    # they disagreed; exchange won, LOUDLY


@dataclass(frozen=True)
class ExpiryDecision:
    symbol: str
    kind: str                       # INDEX | STOCK
    current_expiry: date            # the expiry this instrument trades RIGHT NOW
    next_expiry: Optional[date]
    rolled: bool                    # True when the stock delivery-risk roll fired
    roll_reason: Optional[str]      # "physical_settlement_roll_5td" | None
    trading_days_to_current: int
    anchor: ExpiryAnchor
    resolved_at: datetime
    exchange_ladder: Optional[tuple[date, ...]] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "kind": self.kind,
            "current_expiry": self.current_expiry.isoformat(),
            "next_expiry": self.next_expiry.isoformat() if self.next_expiry else None,
            "rolled": self.rolled,
            "roll_reason": self.roll_reason,
            "trading_days_to_current": self.trading_days_to_current,
            "anchor": self.anchor.value,
            "resolved_at": self.resolved_at.isoformat(),
            "exchange_ladder": (
                [d.isoformat() for d in self.exchange_ladder] if self.exchange_ladder else None
            ),
        }


@dataclass
class ValidationReport:
    checked: list[str] = field(default_factory=list)
    confirmed: list[str] = field(default_factory=list)
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    as_of: Optional[datetime] = None

    @property
    def ok(self) -> bool:
        return not self.mismatches

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked": list(self.checked),
            "confirmed": list(self.confirmed),
            "mismatches": list(self.mismatches),
            "unavailable": list(self.unavailable),
            "ok": self.ok,
            "as_of": self.as_of.isoformat() if self.as_of else None,
        }


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    """Last occurrence of `weekday` (0=Mon … 6=Sun) in the given month."""
    day = monthrange(year, month)[1]
    cursor = date(year, month, day)
    while cursor.weekday() != weekday:
        cursor -= timedelta(days=1)
    return cursor


def _advance_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


class ExpiryPolicy:
    """Calendar-first expiry resolution with a once-per-session exchange check."""

    def __init__(self, *, exchange: str = "NSE") -> None:
        self._exchange = exchange
        # session cache: (session_date) -> {symbol: ExpiryDecision}
        self._session_date: Optional[date] = None
        self._validated: dict[str, ExpiryDecision] = {}
        self._last_report: Optional[ValidationReport] = None

    # ── calendar plumbing ────────────────────────────────────────────────
    def _is_trading_day(self, day: date) -> bool:
        # Imported lazily: trading_calendar reads a runtime JSON at import
        # time, and atm_watchlist already uses this function-local pattern.
        from core.trading_calendar import trading_calendar

        return bool(trading_calendar.has_exchange_session(self._exchange, day))

    def _expiry_weekday(self, symbol: str) -> int:
        from analysis.instruments import INDEX_EXPIRY_WEEKDAY

        # Stocks share the NSE index board (Tuesday) — default 1.
        return int(INDEX_EXPIRY_WEEKDAY.get(str(symbol or "").upper().strip(), 1))

    def monthly_expiry(self, symbol: str, year: int, month: int) -> date:
        """The listed monthly expiry for `symbol` in (year, month).

        Last board-weekday of the month, walked BACKWARD past holidays and
        weekends.  A holiday can only shift an expiry earlier, never later.
        """
        candidate = _last_weekday_of_month(year, month, self._expiry_weekday(symbol))
        guard = 0
        while not self._is_trading_day(candidate) and guard < 14:
            candidate -= timedelta(days=1)
            guard += 1
        return candidate

    def expiry_ladder(self, symbol: str, *, today: date, count: int = 3) -> list[date]:
        """`count` monthly expiries at/after `today`, ascending."""
        out: list[date] = []
        year, month = today.year, today.month
        guard = 0
        while len(out) < max(count, 1) and guard < 36:
            candidate = self.monthly_expiry(symbol, year, month)
            if candidate >= today and candidate not in out:
                out.append(candidate)
            year, month = _advance_month(year, month)
            guard += 1
        return out

    def trading_days_until(self, target: date, *, today: Optional[date] = None) -> int:
        """Exchange trading days from `today` (exclusive) to `target` (inclusive).

        Holiday-aware.  The predecessor (``atm_watchlist._trading_days_until``)
        counted bare Mon–Fri and so OVER-counted in a holiday week, firing the
        stock roll a day late.
        """
        today = today or datetime.now(IST).date()
        if target <= today:
            return 0
        count = 0
        cursor = today
        guard = 0
        while cursor < target and guard < 400:
            cursor += timedelta(days=1)
            if self._is_trading_day(cursor):
                count += 1
            guard += 1
        return count

    # ── the policy ───────────────────────────────────────────────────────
    def decide(
        self,
        symbol: str,
        kind: str,
        *,
        today: Optional[date] = None,
    ) -> ExpiryDecision:
        symbol = str(symbol or "").upper().strip()
        kind_u = str(kind or "").upper().strip()
        kind_u = "INDEX" if kind_u in _INDEX_KINDS else "STOCK"
        today = today or datetime.now(IST).date()

        ladder = self.expiry_ladder(symbol, today=today, count=3)
        if not ladder:  # pragma: no cover - guarded by expiry_ladder's loop
            raise RuntimeError(f"[ExpiryPolicy] could not build an expiry ladder for {symbol}")

        nearest = ladder[0]
        ttd = self.trading_days_until(nearest, today=today)
        now = datetime.now(IST)

        if kind_u == "INDEX":
            # Cash-settled: no delivery risk, so the watchlist tracks the
            # nearest listed expiry right up to expiry day.  Whether the
            # STRATEGY enters on T-0 is a strategy gate (MIN_TTE_DAYS_INDEX),
            # not our business.
            return ExpiryDecision(
                symbol=symbol,
                kind=kind_u,
                current_expiry=nearest,
                next_expiry=ladder[1] if len(ladder) > 1 else None,
                rolled=False,
                roll_reason=None,
                trading_days_to_current=ttd,
                anchor=ExpiryAnchor.CALENDAR,
                resolved_at=now,
            )

        # STOCKS — physically settled.  Roll the WATCHLIST to the next
        # monthly once <= STOCK_ROLL_TRADING_DAYS trading days remain, so no
        # NEW position is opened in the compulsory-delivery window.
        roll_td = _stock_roll_trading_days()
        if roll_td > 0 and ttd <= roll_td and len(ladder) > 1:
            rolled_to = ladder[1]
            return ExpiryDecision(
                symbol=symbol,
                kind=kind_u,
                current_expiry=rolled_to,
                next_expiry=ladder[2] if len(ladder) > 2 else None,
                rolled=True,
                roll_reason=f"physical_settlement_roll_{roll_td}td",
                trading_days_to_current=self.trading_days_until(rolled_to, today=today),
                anchor=ExpiryAnchor.CALENDAR,
                resolved_at=now,
            )

        return ExpiryDecision(
            symbol=symbol,
            kind=kind_u,
            current_expiry=nearest,
            next_expiry=ladder[1] if len(ladder) > 1 else None,
            rolled=False,
            roll_reason=None,
            trading_days_to_current=ttd,
            anchor=ExpiryAnchor.CALENDAR,
            resolved_at=now,
        )

    def decide_many(
        self,
        symbols: Iterable[tuple[str, str]],
        *,
        today: Optional[date] = None,
    ) -> dict[str, ExpiryDecision]:
        out: dict[str, ExpiryDecision] = {}
        for symbol, kind in symbols:
            key = str(symbol or "").upper().strip()
            if not key:
                continue
            out[key] = self.decide(key, kind, today=today)
        return out

    # ── exchange validation (once per session, LOUD on disagreement) ─────
    async def validate_against_exchange(
        self,
        *,
        probe: Any,
        symbols: Sequence[tuple[str, str]],
        today: Optional[date] = None,
    ) -> ValidationReport:
        """Compare the calendar's answer with the broker's listed ladder.

        `probe` is an awaitable ``probe(symbol, kind) -> list[date] | None``.
        Passing a callable (rather than an adapter) keeps this module free of
        the broker import graph; ``market_data.macd_watchlist`` supplies one
        backed by ``ATMWatchlistService._get_broker_expiry_snapshot_for_symbol``.

        Contract:
          * agreement            → anchor EXCHANGE_CONFIRMED, INFO log
          * disagreement         → anchor EXCHANGE_OVERRIDE, ERROR log naming
                                   BOTH values + a durable runtime marker.
                                   The EXCHANGE wins: it is ground truth for
                                   what is listed; the calendar is our model.
          * broker unavailable   → anchor CALENDAR, WARNING, and we PROCEED.
                                   A usable expiry is still returned — that is
                                   the entire point of inverting the dependency.
        """
        today = today or datetime.now(IST).date()
        report = ValidationReport(as_of=datetime.now(IST))
        self._session_date = today
        self._validated = {}

        for symbol, kind in symbols:
            sym = str(symbol or "").upper().strip()
            if not sym:
                continue
            report.checked.append(sym)
            decision = self.decide(sym, kind, today=today)
            ladder: Optional[list[date]] = None
            try:
                ladder = await probe(sym, kind)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[ExpiryPolicy] exchange probe failed for %s: %r — proceeding on the "
                    "CALENDAR ladder (unvalidated).", sym, exc
                )
                ladder = None

            future = sorted({d for d in (ladder or []) if d >= today})
            if not future:
                report.unavailable.append(sym)
                logger.warning(
                    "[ExpiryPolicy] no exchange ladder for %s — using UNVALIDATED calendar "
                    "expiry %s.", sym, decision.current_expiry.isoformat()
                )
                self._validated[sym] = decision
                continue

            if decision.current_expiry in future:
                report.confirmed.append(sym)
                confirmed = ExpiryDecision(
                    **{
                        **decision.__dict__,
                        "anchor": ExpiryAnchor.EXCHANGE_CONFIRMED,
                        "exchange_ladder": tuple(future),
                    }
                )
                self._validated[sym] = confirmed
                continue

            # ── MISMATCH — the LOUD path ──────────────────────────────────
            exchange_pick = self._closest_exchange_match(decision, future)
            logger.error(
                "[ExpiryPolicy] MISMATCH %s: calendar=%s exchange=%s ladder=%s — using "
                "EXCHANGE, calendar holiday data is stale. Fix "
                "backend/core/trading_calendar.py exceptions or "
                "runtime/trading_calendar.json.",
                sym,
                decision.current_expiry.isoformat(),
                exchange_pick.isoformat(),
                [d.isoformat() for d in future[:6]],
            )
            report.mismatches.append(
                {
                    "symbol": sym,
                    "kind": decision.kind,
                    "calendar": decision.current_expiry.isoformat(),
                    "exchange": exchange_pick.isoformat(),
                    "ladder": [d.isoformat() for d in future[:6]],
                }
            )
            nxt = next((d for d in future if d > exchange_pick), None)
            self._validated[sym] = ExpiryDecision(
                **{
                    **decision.__dict__,
                    "current_expiry": exchange_pick,
                    "next_expiry": nxt,
                    "trading_days_to_current": self.trading_days_until(exchange_pick, today=today),
                    "anchor": ExpiryAnchor.EXCHANGE_OVERRIDE,
                    "exchange_ladder": tuple(future),
                }
            )

        self._last_report = report
        if report.mismatches:
            await self._persist_mismatch_marker(report)
        else:
            logger.info(
                "[ExpiryPolicy] exchange validation OK: confirmed=%d unavailable=%d (%s)",
                len(report.confirmed),
                len(report.unavailable),
                today.isoformat(),
            )
        return report

    def _closest_exchange_match(self, decision: ExpiryDecision, future: list[date]) -> date:
        """Pick the listed expiry the calendar was *trying* to name.

        Same contract month if the exchange lists one (a holiday shift moves an
        expiry by days, never across months); otherwise the nearest listed date.
        """
        same_month = [
            d
            for d in future
            if (d.year, d.month) == (decision.current_expiry.year, decision.current_expiry.month)
        ]
        if same_month:
            return same_month[-1]
        return min(future, key=lambda d: abs((d - decision.current_expiry).days))

    async def _persist_mismatch_marker(self, report: ValidationReport) -> None:
        """Durable marker so a mismatch survives log rotation and shows on the
        lanes/system surface as a red board rather than needing a grep."""
        try:
            import asyncio

            from core.runtime_state import save_runtime_state

            await asyncio.to_thread(
                save_runtime_state, "expiry_policy_mismatch", report.as_dict()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[ExpiryPolicy] could not persist the mismatch marker (%r); the ERROR log "
                "above remains the record.", exc
            )

    # ── session cache access ─────────────────────────────────────────────
    def validated_decision(self, symbol: str) -> Optional[ExpiryDecision]:
        """The session-cached, exchange-validated decision for `symbol`, if any."""
        return self._validated.get(str(symbol or "").upper().strip())

    def resolve(
        self,
        symbol: str,
        kind: str,
        *,
        today: Optional[date] = None,
    ) -> ExpiryDecision:
        """Session-cached decision when validated, otherwise a fresh calendar one."""
        today = today or datetime.now(IST).date()
        if self._session_date == today:
            cached = self.validated_decision(symbol)
            if cached is not None:
                return cached
        return self.decide(symbol, kind, today=today)

    def session_cache_state(self) -> dict[str, Any]:
        return {
            "session_date": self._session_date.isoformat() if self._session_date else None,
            "validated_symbols": sorted(self._validated),
            "stock_roll_trading_days": _stock_roll_trading_days(),
            "last_report": self._last_report.as_dict() if self._last_report else None,
        }

    def reset_session(self) -> None:
        self._session_date = None
        self._validated = {}
        self._last_report = None


expiry_policy = ExpiryPolicy()


# ── Convenience wrappers used by analysis.instruments shims ───────────────
def monthly_expiry(symbol: str, year: int, month: int) -> date:
    return expiry_policy.monthly_expiry(symbol, year, month)


def is_trading_day(day: date) -> bool:
    return expiry_policy._is_trading_day(day)
