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

# ── Owner-directed HELD-POSITION rule (2026-07-21), verbatim ──────────────
#
#   "in the expiry rollover positions can be held upto 2 trading days till
#    expiry before compulsory closure, So except for held positions other
#    instruments to rollover to next expiry"
#
# So the 5TD roll SPLITS in two:
#
#   1. instrument with NO open position → the WATCHLIST rolls to the next
#      expiry at the 5TD mark, exactly as shipped.  Unchanged.
#   2. instrument WITH an open position → the position KEEPS its own expiry
#      past the 5TD roll (the rest of the universe moves without it) and may
#      be held until <= 2 TRADING DAYS remain, at which point it is
#      COMPULSORILY CLOSED.  It must never be allowed to reach expiry.
#
# BOUNDARY, stated exactly so it is testable rather than folklore:
#   closure is FORCED on the first exchange session for which
#       ``trading_days_until(expiry, today) <= FORCED_CLOSE_TRADING_DAYS``.
#   ``trading_days_until`` counts sessions AFTER ``today``, up to and
#   INCLUDING ``expiry``.  So "held up to 2 trading days till expiry" means
#   two sessions still remain AFTER the closing session.  For the 2026-07-28
#   (Tue) expiry that lands on Friday 2026-07-24, leaving 07-27 and 07-28.
#   Holiday-aware by construction — it reuses the same holiday-walked counter
#   the 5TD roll uses, so a holiday shifts the boundary automatically.
STOCK_FORCED_CLOSE_TRADING_DAYS = 2

# Indices: CASH settled.  The physical-settlement rationale that motivates the
# stock rule simply does not exist here, and INDEX_ROLL_TRADING_DAYS = 0
# deliberately keeps indices trading to expiry.  A SEPARATE constant, defaulted
# to 0 = DISABLED, so switching indices on (for expiry-day gamma / liquidity
# reasons, which would be a different argument entirely) is an explicit owner
# decision and can never be a side effect of tuning the stock number.
INDEX_FORCED_CLOSE_TRADING_DAYS = 0

# Attribution strings.  These land on the closing trade / watchlist row so the
# closures are separable in P&L review.
FORCED_CLOSE_REASON = "forced_expiry_roll_2td"
HELD_POSITION_ROLL_REASON = "held_position_retains_expiry"


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


def _forced_close_trading_days(kind: str) -> int:
    """Owner-tunable compulsory-closure horizon, per instrument class.

    Two independent settings on purpose (see the constants above): the stock
    number is a physical-settlement safety margin, the index number would be a
    gamma/liquidity preference.  They must never move together by accident.
    A nonsense value falls back to the constant LOUDLY.  ``0`` means DISABLED.
    """
    is_index = str(kind or "").upper().strip() in _INDEX_KINDS
    name = (
        "EXPIRY_POLICY_INDEX_FORCED_CLOSE_TRADING_DAYS"
        if is_index
        else "EXPIRY_POLICY_FORCED_CLOSE_TRADING_DAYS"
    )
    default = INDEX_FORCED_CLOSE_TRADING_DAYS if is_index else STOCK_FORCED_CLOSE_TRADING_DAYS
    try:
        from core.config import settings

        value = int(getattr(settings, name, default))
    except Exception:  # noqa: BLE001 - settings unavailable in bare unit contexts
        return default
    if value < 0 or value > 10:
        logger.error(
            "[ExpiryPolicy] %s=%s is out of the sane range [0, 10] — falling back to %s. "
            "Fix the setting.",
            name,
            value,
            default,
        )
        return default
    return value


@dataclass(frozen=True)
class HoldDecision:
    """Answer to: may this OPEN position keep riding its own expiry today?"""

    symbol: str
    kind: str                        # INDEX | STOCK
    expiry: date
    trading_days_to_expiry: int
    boundary_trading_days: int       # 0 ⇒ the rule is disabled for this class
    must_close: bool
    reason: Optional[str]            # FORCED_CLOSE_REASON when must_close
    forced_close_date: Optional[date]
    evaluated_on: date

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "kind": self.kind,
            "expiry": self.expiry.isoformat(),
            "trading_days_to_expiry": self.trading_days_to_expiry,
            "boundary_trading_days": self.boundary_trading_days,
            "must_close": self.must_close,
            "reason": self.reason,
            "forced_close_date": (
                self.forced_close_date.isoformat() if self.forced_close_date else None
            ),
            "evaluated_on": self.evaluated_on.isoformat(),
        }


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

    # ── held-position rule (compulsory closure at the 2TD boundary) ──────
    def forced_close_trading_days(self, kind: str) -> int:
        """The compulsory-closure boundary for this instrument class (0 = off)."""
        return _forced_close_trading_days(kind)

    def forced_close_date(self, symbol: str, kind: str, expiry: date) -> Optional[date]:
        """The EARLIEST session on which a position on `expiry` must be closed.

        Walks BACKWARD from expiry over real sessions and keeps the earliest one
        still inside the boundary — so a holiday inside the final week moves the
        answer automatically instead of needing a second table.
        Returns None when the rule is disabled for this class.
        """
        boundary = _forced_close_trading_days(kind)
        if boundary <= 0:
            return None
        best: Optional[date] = None
        cursor = expiry
        guard = 0
        while guard < 60:
            if self._is_trading_day(cursor):
                if self.trading_days_until(expiry, today=cursor) <= boundary:
                    best = cursor
                else:
                    break
            cursor -= timedelta(days=1)
            guard += 1
        return best

    def must_force_close(
        self,
        symbol: str,
        kind: str,
        expiry: date,
        *,
        today: Optional[date] = None,
    ) -> HoldDecision:
        """May this OPEN position keep riding `expiry` today, or is it forced out?

        `must_close` is True once ``trading_days_until(expiry) <= boundary`` —
        including an expiry that is already in the past, which is the degenerate
        case the rule exists to make impossible.
        """
        symbol_u = str(symbol or "").upper().strip()
        kind_u = "INDEX" if str(kind or "").upper().strip() in _INDEX_KINDS else "STOCK"
        today = today or datetime.now(IST).date()
        boundary = _forced_close_trading_days(kind_u)
        ttd = self.trading_days_until(expiry, today=today)
        must = bool(boundary > 0 and ttd <= boundary)
        return HoldDecision(
            symbol=symbol_u,
            kind=kind_u,
            expiry=expiry,
            trading_days_to_expiry=ttd,
            boundary_trading_days=boundary,
            must_close=must,
            reason=FORCED_CLOSE_REASON if must else None,
            forced_close_date=self.forced_close_date(symbol_u, kind_u, expiry),
            evaluated_on=today,
        )

    # ── the policy ───────────────────────────────────────────────────────
    def decide(
        self,
        symbol: str,
        kind: str,
        *,
        today: Optional[date] = None,
        held_expiry: Optional[date] = None,
    ) -> ExpiryDecision:
        """Which expiry does `symbol` trade today?

        ``held_expiry`` is the ROLL SPLIT: pass the expiry of an already-open
        position and the instrument KEEPS that contract (the 5TD stock roll is
        suppressed for it) while every un-held instrument rolls normally.  The
        held contract is not held forever — ``must_force_close`` is what ends
        it, and that is enforced in the EXIT cascade, not here.
        """
        symbol = str(symbol or "").upper().strip()
        kind_u = str(kind or "").upper().strip()
        kind_u = "INDEX" if kind_u in _INDEX_KINDS else "STOCK"
        today = today or datetime.now(IST).date()

        ladder = self.expiry_ladder(symbol, today=today, count=3)
        if not ladder:  # pragma: no cover - guarded by expiry_ladder's loop
            raise RuntimeError(f"[ExpiryPolicy] could not build an expiry ladder for {symbol}")

        if held_expiry is not None:
            if held_expiry >= today:
                return ExpiryDecision(
                    symbol=symbol,
                    kind=kind_u,
                    current_expiry=held_expiry,
                    next_expiry=next((d for d in ladder if d > held_expiry), None),
                    rolled=False,
                    roll_reason=HELD_POSITION_ROLL_REASON,
                    trading_days_to_current=self.trading_days_until(held_expiry, today=today),
                    anchor=ExpiryAnchor.CALENDAR,
                    resolved_at=datetime.now(IST),
                )
            logger.error(
                "[ExpiryPolicy] %s carries an OPEN position on an EXPIRED contract (%s < %s). "
                "The held-expiry override is refused and the normal ladder answers; the "
                "position must be force-closed by the exit cascade.",
                symbol,
                held_expiry.isoformat(),
                today.isoformat(),
            )

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
        held_expiry: Optional[date] = None,
    ) -> ExpiryDecision:
        """Session-cached decision when validated, otherwise a fresh calendar one.

        A ``held_expiry`` always bypasses the session cache: the cache holds the
        UN-held (rolled) answer for the symbol, and returning it for a held
        instrument is exactly the orphaning bug this override exists to prevent.
        """
        today = today or datetime.now(IST).date()
        if held_expiry is None and self._session_date == today:
            cached = self.validated_decision(symbol)
            if cached is not None:
                return cached
        return self.decide(symbol, kind, today=today, held_expiry=held_expiry)

    def session_cache_state(self) -> dict[str, Any]:
        return {
            "session_date": self._session_date.isoformat() if self._session_date else None,
            "validated_symbols": sorted(self._validated),
            "stock_roll_trading_days": _stock_roll_trading_days(),
            "stock_forced_close_trading_days": _forced_close_trading_days("STOCK"),
            "index_forced_close_trading_days": _forced_close_trading_days("INDEX"),
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


def instrument_kind(symbol: str) -> str:
    """INDEX | STOCK for an underlying symbol, from the F&O index list."""
    try:
        from analysis.instruments import ALL_FO_INDICES

        return "INDEX" if str(symbol or "").upper().strip() in set(ALL_FO_INDICES) else "STOCK"
    except Exception:  # noqa: BLE001
        return "STOCK"


def forced_close_check(
    symbol: str,
    expiry: Optional[date],
    *,
    kind: Optional[str] = None,
    today: Optional[date] = None,
) -> Optional[HoldDecision]:
    """The ONE gate both MACD exit cascades call.

    Returns None when the feature is OFF (either flag) or the expiry is
    unusable, so a caller's ``if decision and decision.must_close`` is
    byte-identically inert with the flags down.  Both flags are required:
    ``EXPIRY_POLICY_ENABLED`` (the whole calendar policy) AND
    ``EXPIRY_POLICY_FORCED_CLOSE_ENABLED`` (this rule specifically), so the
    compulsory closure can be reverted on its own without giving up the
    calendar expiry fix.
    """
    if expiry is None:
        return None
    try:
        from core.config import settings

        if not bool(getattr(settings, "EXPIRY_POLICY_ENABLED", False)):
            return None
        if not bool(getattr(settings, "EXPIRY_POLICY_FORCED_CLOSE_ENABLED", False)):
            return None
    except Exception:  # noqa: BLE001 - settings unavailable ⇒ stay inert
        return None
    return expiry_policy.must_force_close(
        symbol, kind or instrument_kind(symbol), expiry, today=today
    )
