"""Shared async rate limiting + defensive parsing for broker REST clients.

Each broker exposes ONE shared REST budget per API key:
  - Fyers : 10 req/sec, 200 req/min, 100,000 req/day  (ALL REST endpoints share it)
  - Upstox: 50 req/sec, 2,000 req/30min, no daily cap  (analytics / historical)

Every scaled caller (the ~227-name option-chain poller, the history gap-fill, the
09:15 eager poll) MUST pull from a SINGLE process-global limiter so bursts get
spread under the per-minute / per-second governor instead of firing inside one
window and tripping a 429 against the sole live lane.

Admission is a WEIGHTED-FAIR-SHARE PRIORITY MONITOR, not a plain FIFO lock.
Every waiter owns a private asyncio.Event; the best-ranked waiter (lowest AGED
priority, FIFO tie-break on arrival seq) is admitted when a governor slot frees.
Priority comes from the `broker_priority` contextvar (LOWER = higher); aging
lets a long-waiting low-priority ticket climb to parity so nothing STARVES (the
tail-starvation that froze 2026-07-07 is exactly what strict priority would
recreate). A per-wakeup timeout backstop means a missed wake degrades to a
little latency, never a deadlock.

CANCELLATION SAFETY (2026-07-15): admission previously used asyncio.Condition
with `await asyncio.wait_for(self._cond.wait(), timeout)` — the documented
cancellation-unsafe pattern: when wait_for cancels cond.wait() the waiter must
RE-ACQUIRE the condition lock inside its cancellation path, and a second
cancellation landing there (watchdog kill, caller wait_for) escaped as
"RuntimeError: Lock is not acquired" and could leave the monitor wedged so ALL
Upstox REST stalled (observed 4x on 2026-07-14). The event-per-waiter design
has NO lock: all bookkeeping between awaits is synchronous (single-threaded
event loop => atomic), the only await is `Event.wait()` (trivially cancellable,
nothing to re-acquire), and departure cleanup in `finally` is synchronous so a
cancelled waiter can never corrupt admission state. Do NOT reintroduce a lock
held across waits/back-off sleeps — that would re-serialise admission FIFO and
silently defeat the priority scheme.

QUOTA CLASSES (2026-07-15): priority orders WAITERS; classes reserve CAPACITY.
The two are orthogonal dimensions. Every request carries a class from the
`broker_class` contextvar (default STANDARD):

  CLASS_CRITICAL — ATM watchlist build rows, index chain refresh, held-position
                   marks. 40% of every window is HARD-RESERVED for this class:
                   lower classes can never occupy it, so a token-restore burst
                   of bulk work can no longer wedge the watchlist build.
  CLASS_STANDARD — premium top-up, stock chains, commodity quote poll. Shares
                   the remaining 60% with BULK; because BULK is capped at 25%,
                   STANDARD is guaranteed ≥35%.
  CLASS_BULK     — macd_refined universe sweep, gap-fill/backfills, chain
                   builder. HARD-CAPPED at 25% of every window, never borrows
                   from the CRITICAL reservation, and yields INSTANTLY while
                   any CRITICAL waiter is queued (admission-level: in-flight
                   requests are never revoked). It may otherwise freely use
                   idle capacity up to its cap.

Aging still prevents starvation WITHIN the admissible set, but it deliberately
does NOT let a class climb past its reservation/cap — the class limits are
structural, not fair-share (that is the whole point: BULK must never be able
to age its way into the CRITICAL share).
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json as _json
import time
from collections import deque
from typing import Optional

from loguru import logger


# ── Weighted fair-share priority (contextvar, no signature threading) ─────────
# LOWER number = HIGHER priority. Callers wrap a block in `broker_priority(...)`
# and every acquire() underneath inherits it (contextvars copy into child tasks
# at asyncio.gather/create_task time). Aging (see acquire) lets a long-waiting
# low-priority call climb toward parity, so NOTHING starves — this is fair-share,
# not strict priority (the tail-starvation that froze 2026-07-07 is exactly what
# strict priority would re-create).
PRIORITY_HIGH = -10.0        # token/session health, live position marks
PRIORITY_NORMAL = 0.0        # default — interactive reads, watchlist build
PRIORITY_CHAIN_BUILDER = 5.0  # broad-universe chain sweep (important but bulk)
PRIORITY_BULK = 10.0         # premium top-up / backfill / research

_request_priority: contextvars.ContextVar[float] = contextvars.ContextVar(
    "broker_request_priority", default=PRIORITY_NORMAL
)


@contextlib.contextmanager
def broker_priority(priority: float):
    """Set the broker-request priority for the current context (and any child
    tasks spawned under it). Restores the previous value on exit."""
    token = _request_priority.set(float(priority))
    try:
        yield
    finally:
        _request_priority.reset(token)


# ── Broker quota classes (orthogonal to priority — see module docstring) ──────
# Priority ranks waiters; the class reserves capacity. Callers wrap a block in
# `broker_class(...)` exactly like `broker_priority(...)`; the two compose.
CLASS_CRITICAL = "critical"   # ATM watchlist rows, index chain refresh, held-position marks
CLASS_STANDARD = "standard"   # premium top-up, stock chains, commodity quote poll (default)
CLASS_BULK = "bulk"           # macd_refined sweep, gap-fill/backfills, chain builder

_KNOWN_CLASSES = (CLASS_CRITICAL, CLASS_STANDARD, CLASS_BULK)

# Reservation geometry. CRITICAL's share is floor()ed so tiny test windows
# ((1, s), (2, s)) reserve nothing and legacy behavior is preserved; BULK's cap
# keeps a floor of one slot so the class is capped, never bricked entirely.
CRITICAL_RESERVED_FRACTION = 0.40
BULK_CAP_FRACTION = 0.25

_request_class: contextvars.ContextVar[str] = contextvars.ContextVar(
    "broker_request_class", default=CLASS_STANDARD
)


@contextlib.contextmanager
def broker_class(request_class: str):
    """Set the broker quota class for the current context (and any child tasks
    spawned under it). Restores the previous value on exit."""
    value = str(request_class).strip().lower()
    if value not in _KNOWN_CLASSES:
        raise ValueError(
            f"unknown broker class {request_class!r}; expected one of {_KNOWN_CLASSES}"
        )
    token = _request_class.set(value)
    try:
        yield
    finally:
        _request_class.reset(token)


# ── Per-lane broker profile (contextvar, cadence axis) ────────────────────────
# Orthogonal to priority and class: those rank/ration waiters WITHIN a broker;
# this steers WHICH broker a lane prefers. Set once at runner dispatch
# (core/market_hours_paper_supervisor._run_runner) and read inside
# market_data.source_policy.route_order to reorder the failover list:
#   SLOW (30m: S1/MACD Refined/CBE/Gann) → upstox-first
#   FAST (3m + tick: directional/auction/convergence/MP+OF) → fyers-first
# Reorder ONLY (never drops a source). Propagates into child tasks / to_thread
# via contextvars copy semantics — the exact guarantee broker_priority/
# broker_class already rely on. Gated by settings.LANE_BROKER_ROUTING_ENABLED at
# the READ seam, so setting this contextvar with the flag off is a no-op.
LANE_PROFILE_DEFAULT = "default"  # global order, unchanged
LANE_PROFILE_SLOW = "slow"        # upstox-preferred
LANE_PROFILE_FAST = "fast"        # fyers-preferred

_KNOWN_LANE_PROFILES = (LANE_PROFILE_DEFAULT, LANE_PROFILE_SLOW, LANE_PROFILE_FAST)

_lane_broker_profile: contextvars.ContextVar[str] = contextvars.ContextVar(
    "lane_broker_profile", default=LANE_PROFILE_DEFAULT
)


@contextlib.contextmanager
def lane_broker_profile(profile: str):
    """Set the per-lane broker profile for the current context (and any child
    tasks/threads spawned under it). Restores the previous value on exit.

    An unknown/empty profile is coerced to DEFAULT rather than raising, so a
    mistyped RunnerConfig.broker_profile degrades to the global order (safe) —
    the routing layer must never crash a runner dispatch."""
    value = str(profile or "").strip().lower()
    if value not in _KNOWN_LANE_PROFILES:
        value = LANE_PROFILE_DEFAULT
    token = _lane_broker_profile.set(value)
    try:
        yield
    finally:
        _lane_broker_profile.reset(token)


def current_lane_profile() -> str:
    """Read the active lane broker profile (DEFAULT when unset)."""
    return _lane_broker_profile.get()


class RateLimitDayExceeded(RuntimeError):
    """Raised when a per-day cap is hit — sleeping until tomorrow is pointless."""


class _Window:
    """One governor window with per-class usage accounting.

    ``dq`` holds ``(monotonic admission ts, class)`` tuples; the class counters
    are maintained incrementally on append/purge so admission checks are O(1).
    """

    __slots__ = (
        "count", "span", "dq", "critical_reserved", "bulk_cap",
        "non_critical_used", "bulk_used",
    )

    def __init__(
        self,
        count: int,
        span: float,
        *,
        critical_reserved_fraction: float,
        bulk_cap_fraction: float,
    ) -> None:
        self.count = int(count)
        self.span = float(span)
        self.dq: deque = deque()
        self.critical_reserved = int(self.count * float(critical_reserved_fraction))
        self.bulk_cap = max(1, int(self.count * float(bulk_cap_fraction)))
        self.non_critical_used = 0
        self.bulk_used = 0

    @property
    def non_critical_cap(self) -> int:
        return self.count - self.critical_reserved

    def admit(self, now: float, request_class: str) -> None:
        self.dq.append((now, request_class))
        if request_class != CLASS_CRITICAL:
            self.non_critical_used += 1
            if request_class == CLASS_BULK:
                self.bulk_used += 1

    def purge(self, now: float) -> None:
        dq = self.dq
        while dq and now - dq[0][0] >= self.span:
            _ts, request_class = dq.popleft()
            if request_class != CLASS_CRITICAL:
                self.non_critical_used -= 1
                if request_class == CLASS_BULK:
                    self.bulk_used -= 1


class AsyncRateLimiter:
    """Multi-window token bucket. ``windows`` = list of (max_count, window_seconds)."""

    def __init__(
        self,
        *,
        windows: list[tuple[int, float]],
        per_day: Optional[int] = None,
        name: str = "limiter",
        aging_step_seconds: float = 2.0,
        critical_reserved_fraction: float = CRITICAL_RESERVED_FRACTION,
        bulk_cap_fraction: float = BULK_CAP_FRACTION,
    ):
        # Each window tracked by its own deque of (monotonic ts, class) tuples
        # with incrementally-maintained per-class counters (see _Window).
        self._windows = [
            _Window(
                c, s,
                critical_reserved_fraction=critical_reserved_fraction,
                bulk_cap_fraction=bulk_cap_fraction,
            )
            for c, s in windows
        ]
        self.per_day = int(per_day) if per_day else None
        self.name = name
        # Priority-aware admission monitor. One private Event per waiter — no
        # shared lock, so cancellation can never strand a lock re-acquire
        # (the Condition-based predecessor wedged ALL Upstox REST when a
        # cancelled cond.wait() lost its lock — see module docstring).
        self._seq = 0                                   # monotone ticket id (FIFO tie-break)
        self._pending: dict[int, float] = {}            # seq -> base priority
        self._pending_class: dict[int, str] = {}        # seq -> quota class
        self._enqueued_at: dict[int, float] = {}        # seq -> monotonic enqueue time
        self._waiters: dict[int, asyncio.Event] = {}    # seq -> private wake event
        self._critical_waiting = 0                      # pending CRITICAL waiters (BULK yields while > 0)
        # Seconds of waiting that buys one level of priority (aging → no starve).
        self._aging_step = max(float(aging_step_seconds), 0.0)
        self._day_count = 0
        self._day_key: Optional[int] = None
        # Telemetry counters (observability — surfaced via snapshot()).
        self._admitted = 0          # total acquire() grants
        self._admitted_by_class = {cls: 0 for cls in _KNOWN_CLASSES}
        self._wait_events = 0       # acquires that had to sleep on the governor
        self._wait_total = 0.0      # cumulative seconds spent waiting
        self._throttle_429 = 0      # broker 429s reported by callers

    def _purge(self, now: float) -> None:
        for window in self._windows:
            window.purge(now)

    def _slot_wait(self, now: float, request_class: str) -> float:
        """Seconds until `request_class` could clear every window's class
        limits (0 if it could be admitted now, capacity-wise). Covers the total
        cap for every class, plus the CRITICAL reservation and the BULK hard
        cap for the lower classes."""
        wait = 0.0
        for w in self._windows:
            if len(w.dq) >= w.count:
                wait = max(wait, w.span - (now - w.dq[0][0]))
            if request_class == CLASS_CRITICAL:
                continue
            if w.non_critical_used >= w.non_critical_cap:
                # Blocked on the CRITICAL reservation: wait for the earliest
                # NON-critical admission to age out of the window.
                ts = next((t for t, cls in w.dq if cls != CLASS_CRITICAL), None)
                if ts is not None:
                    wait = max(wait, w.span - (now - ts))
            if request_class == CLASS_BULK and w.bulk_used >= w.bulk_cap:
                ts = next((t for t, cls in w.dq if cls == CLASS_BULK), None)
                if ts is not None:
                    wait = max(wait, w.span - (now - ts))
        return wait

    def _effective(self, seq: int, now: float) -> tuple[float, int]:
        """Aged priority key for `seq`. Lower = admitted sooner. Waiting reduces
        the effective priority (aging) so a low-priority ticket eventually
        out-ranks fresh high-priority ones — fair-share, never starvation."""
        base = self._pending[seq]
        if self._aging_step > 0:
            base -= (now - self._enqueued_at[seq]) / self._aging_step
        return (base, seq)

    def _admissible(self, seq: int, now: float) -> bool:
        """Whether `seq` could be admitted RIGHT NOW under its class limits.
        Ranking only ever runs over the admissible set — a class-blocked waiter
        (e.g. BULK at its hard cap) must never shadow an admissible one behind
        it (that would be priority inversion via the class dimension)."""
        request_class = self._pending_class.get(seq, CLASS_STANDARD)
        if request_class == CLASS_BULK and self._critical_waiting:
            # Instant yield: while ANY critical waiter is queued, bulk is
            # ineligible regardless of aging or free bulk slots.
            return False
        return self._slot_wait(now, request_class) <= 0.0

    def _best_admissible_seq(self, now: float) -> Optional[int]:
        best: Optional[int] = None
        best_key: Optional[tuple[float, int]] = None
        for seq in self._pending:
            if not self._admissible(seq, now):
                continue
            key = self._effective(seq, now)
            if best_key is None or key < best_key:
                best, best_key = seq, key
        return best

    def _wake_all(self) -> None:
        """Synchronously wake every registered waiter so it re-evaluates its
        rank. Pure-sync (no awaits) so it is safe to call from `finally` even
        while the caller is being cancelled."""
        for event in self._waiters.values():
            event.set()

    async def acquire(
        self,
        priority: Optional[float] = None,
        request_class: Optional[str] = None,
    ) -> None:
        """Admit one request under the multi-window governor, honouring
        weighted-fair-share priority (explicit arg, else the broker_priority
        contextvar, else NORMAL) and the quota class (explicit arg, else the
        broker_class contextvar, else STANDARD). A per-wakeup timeout backstop
        guarantees every waiter re-evaluates within ≤5s even if a wake is
        missed — so a missed wakeup degrades to a little latency, never a
        deadlock.

        Cancellation-safe by construction: every code path between awaits is
        synchronous, and the single await (`Event.wait()`) holds no lock, so a
        caller cancelled mid-wait (watchdog, wait_for) departs cleanly via the
        synchronous `finally` without corrupting admission state."""
        prio = float(priority) if priority is not None else float(_request_priority.get())
        if request_class is not None:
            cls = str(request_class).strip().lower()
            if cls not in _KNOWN_CLASSES:
                raise ValueError(
                    f"unknown broker class {request_class!r}; expected one of {_KNOWN_CLASSES}"
                )
        else:
            cls = _request_class.get()
        self._seq += 1
        seq = self._seq
        started = time.monotonic()
        self._pending[seq] = prio
        self._pending_class[seq] = cls
        self._enqueued_at[seq] = started
        if cls == CLASS_CRITICAL:
            self._critical_waiting += 1
        event = asyncio.Event()
        self._waiters[seq] = event
        waited_any = False
        try:
            while True:
                now = time.monotonic()
                self._purge(now)

                day_key = int(time.time() // 86400)
                if self._day_key != day_key:
                    self._day_key = day_key
                    self._day_count = 0
                if self.per_day is not None and self._day_count >= self.per_day:
                    raise RateLimitDayExceeded(f"{self.name}: per-day cap {self.per_day} reached")

                slot_wait = self._slot_wait(now, cls)
                admissible = slot_wait <= 0 and not (
                    cls == CLASS_BULK and self._critical_waiting
                )

                if admissible and self._best_admissible_seq(now) == seq:
                    for window in self._windows:
                        window.admit(now, cls)
                    self._day_count += 1
                    self._admitted += 1
                    self._admitted_by_class[cls] = self._admitted_by_class.get(cls, 0) + 1
                    if waited_any:
                        self._wait_events += 1
                        self._wait_total += now - started
                    # Wake the rest so the next-best re-ranks and proceeds
                    # (the departing `finally` also wakes, but be explicit).
                    return

                waited_any = True
                # Backstop timeout: wake to re-evaluate even without an explicit
                # wake. Bounded by this waiter's class-aware slot wait (window
                # capacity, CRITICAL reservation, BULK cap) and by the aging
                # step (so ranking refreshes promptly). A BULK waiter blocked
                # only by a queued CRITICAL has no computable wait — the
                # departing critical's `_wake_all` (or the ≤5s backstop) covers
                # that transition.
                timeout = 5.0
                if slot_wait > 0:
                    timeout = min(timeout, slot_wait)
                if self._aging_step > 0:
                    timeout = min(timeout, self._aging_step)
                timeout += 0.005
                # No state is touched between this clear() and the wait, so a
                # wake landing in between simply completes the wait immediately.
                event.clear()
                try:
                    await asyncio.wait_for(event.wait(), timeout)
                except asyncio.TimeoutError:
                    pass
        finally:
            # Synchronous departure — safe under cancellation (no awaits).
            self._pending.pop(seq, None)
            self._enqueued_at.pop(seq, None)
            self._waiters.pop(seq, None)
            if self._pending_class.pop(seq, None) == CLASS_CRITICAL:
                self._critical_waiting -= 1
            # A departing waiter (admitted, cancelled, or day-capped) may have
            # been blocking the ranking — let everyone re-evaluate.
            self._wake_all()

    def record_429(self) -> None:
        """Callers report a broker 429 so throttling is observable in snapshot()."""
        self._throttle_429 += 1

    def snapshot(self) -> dict:
        now = time.monotonic()
        self._purge(now)
        return {
            "name": self.name,
            "windows": [
                {
                    "max": w.count,
                    "span_s": w.span,
                    "used": len(w.dq),
                    "critical_reserved": w.critical_reserved,
                    "non_critical_used": w.non_critical_used,
                    "non_critical_cap": w.non_critical_cap,
                    "bulk_used": w.bulk_used,
                    "bulk_cap": w.bulk_cap,
                }
                for w in self._windows
            ],
            "day_count": self._day_count,
            "per_day": self.per_day,
            "admitted": self._admitted,
            "admitted_by_class": dict(self._admitted_by_class),
            "critical_waiting": self._critical_waiting,
            "wait_events": self._wait_events,
            "avg_wait_ms": round(1000.0 * self._wait_total / self._wait_events, 1) if self._wait_events else 0.0,
            "throttle_429": self._throttle_429,
        }


def parse_first_json(text_body: str):
    """Parse the FIRST JSON object from a possibly concatenated response body.

    Fyers occasionally returns multiple JSON objects back-to-back on a burst, so a
    plain ``response.json()`` raises. Decode just the leading object defensively.
    """
    return _json.JSONDecoder().raw_decode(text_body.lstrip())[0]


# ── Process-global shared limiters ────────────────────────────────────────────
# Set a hair under the hard caps to leave headroom for clock skew + the trading
# (order/position) endpoints that share the same Fyers REST budget.
FYERS_DATA_LIMITER = AsyncRateLimiter(
    windows=[(9, 1.0), (190, 60.0)], per_day=95_000, name="fyers-rest",
)
# Upstox: 50/s, 2000/30min. Keep well under both (8/s, 1800/30min) for off-hours backfill.
UPSTOX_DATA_LIMITER = AsyncRateLimiter(
    windows=[(8, 1.0), (1800, 1800.0)], per_day=None, name="upstox-rest",
)
