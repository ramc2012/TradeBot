"""Shared async rate limiting + defensive parsing for broker REST clients.

Each broker exposes ONE shared REST budget per API key:
  - Fyers : 10 req/sec, 200 req/min, 100,000 req/day  (ALL REST endpoints share it)
  - Upstox: 50 req/sec, 2,000 req/30min, no daily cap  (analytics / historical)

Every scaled caller (the ~227-name option-chain poller, the history gap-fill, the
09:15 eager poll) MUST pull from a SINGLE process-global limiter so bursts get
spread under the per-minute / per-second governor instead of firing inside one
window and tripping a 429 against the sole live lane.

Admission is a WEIGHTED-FAIR-SHARE PRIORITY MONITOR (asyncio.Condition), not a
plain FIFO lock. `acquire()` RELEASES the condition lock while waiting
(`cond.wait()`) so many waiters coexist; the best-ranked waiter (lowest AGED
priority, FIFO tie-break on arrival seq) is admitted when a governor slot frees.
Priority comes from the `broker_priority` contextvar (LOWER = higher); aging
lets a long-waiting low-priority ticket climb to parity so nothing STARVES (the
tail-starvation that froze 2026-07-07 is exactly what strict priority would
recreate). A per-wakeup timeout backstop means a missed notify degrades to a
little latency, never a deadlock. Do NOT reintroduce holding a lock across the
back-off sleep — that would re-serialise admission FIFO and silently defeat the
priority scheme.
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


class RateLimitDayExceeded(RuntimeError):
    """Raised when a per-day cap is hit — sleeping until tomorrow is pointless."""


class AsyncRateLimiter:
    """Multi-window token bucket. ``windows`` = list of (max_count, window_seconds)."""

    def __init__(
        self,
        *,
        windows: list[tuple[int, float]],
        per_day: Optional[int] = None,
        name: str = "limiter",
        aging_step_seconds: float = 2.0,
    ):
        # Each window tracked by its own deque of monotonic admission timestamps.
        self._windows = [(int(c), float(s), deque()) for c, s in windows]
        self.per_day = int(per_day) if per_day else None
        self.name = name
        # Priority-aware admission monitor (replaces the plain FIFO lock).
        self._cond = asyncio.Condition()
        self._seq = 0                                   # monotone ticket id (FIFO tie-break)
        self._pending: dict[int, float] = {}            # seq -> base priority
        self._enqueued_at: dict[int, float] = {}        # seq -> monotonic enqueue time
        # Seconds of waiting that buys one level of priority (aging → no starve).
        self._aging_step = max(float(aging_step_seconds), 0.0)
        self._day_count = 0
        self._day_key: Optional[int] = None
        # Telemetry counters (observability — surfaced via snapshot()).
        self._admitted = 0          # total acquire() grants
        self._wait_events = 0       # acquires that had to sleep on the governor
        self._wait_total = 0.0      # cumulative seconds spent waiting
        self._throttle_429 = 0      # broker 429s reported by callers

    def _purge(self, now: float) -> None:
        for _count, span, dq in self._windows:
            while dq and now - dq[0] >= span:
                dq.popleft()

    def _slot_wait(self, now: float) -> float:
        """Seconds until a governor slot frees (0 if one is free now)."""
        wait = 0.0
        for count, span, dq in self._windows:
            if len(dq) >= count:
                wait = max(wait, span - (now - dq[0]))
        return wait

    def _effective(self, seq: int, now: float) -> tuple[float, int]:
        """Aged priority key for `seq`. Lower = admitted sooner. Waiting reduces
        the effective priority (aging) so a low-priority ticket eventually
        out-ranks fresh high-priority ones — fair-share, never starvation."""
        base = self._pending[seq]
        if self._aging_step > 0:
            base -= (now - self._enqueued_at[seq]) / self._aging_step
        return (base, seq)

    def _best_seq(self, now: float) -> Optional[int]:
        if not self._pending:
            return None
        return min(self._pending, key=lambda s: self._effective(s, now))

    async def acquire(self, priority: Optional[float] = None) -> None:
        """Admit one request under the multi-window governor, honouring
        weighted-fair-share priority (explicit arg, else the broker_priority
        contextvar, else NORMAL). A per-wakeup timeout backstop guarantees every
        waiter re-evaluates within ≤5s even if a notify is missed — so a missed
        wakeup degrades to a little latency, never a deadlock."""
        prio = float(priority) if priority is not None else float(_request_priority.get())
        async with self._cond:
            self._seq += 1
            seq = self._seq
            started = time.monotonic()
            self._pending[seq] = prio
            self._enqueued_at[seq] = started
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

                    slot_wait = self._slot_wait(now)
                    is_best = self._best_seq(now) == seq

                    if is_best and slot_wait <= 0:
                        for _count, _span, dq in self._windows:
                            dq.append(now)
                        self._day_count += 1
                        self._admitted += 1
                        if waited_any:
                            self._wait_events += 1
                            self._wait_total += now - started
                        # Wake the rest so the next-best re-ranks and proceeds.
                        self._cond.notify_all()
                        return

                    waited_any = True
                    # Backstop timeout: wake to re-evaluate even without a notify.
                    # Bounded by the slot wait (if we're best but the governor is
                    # full) and by the aging step (so ranking refreshes promptly).
                    timeout = 5.0
                    if is_best and slot_wait > 0:
                        timeout = min(timeout, slot_wait)
                    if self._aging_step > 0:
                        timeout = min(timeout, self._aging_step)
                    timeout += 0.005
                    try:
                        await asyncio.wait_for(self._cond.wait(), timeout)
                    except asyncio.TimeoutError:
                        pass
            finally:
                self._pending.pop(seq, None)
                self._enqueued_at.pop(seq, None)
                # A departing waiter (admitted, cancelled, or day-capped) may have
                # been blocking the ranking — let everyone re-evaluate.
                self._cond.notify_all()

    def record_429(self) -> None:
        """Callers report a broker 429 so throttling is observable in snapshot()."""
        self._throttle_429 += 1

    def snapshot(self) -> dict:
        now = time.monotonic()
        self._purge(now)
        return {
            "name": self.name,
            "windows": [{"max": c, "span_s": s, "used": len(dq)} for c, s, dq in self._windows],
            "day_count": self._day_count,
            "per_day": self.per_day,
            "admitted": self._admitted,
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
