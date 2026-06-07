"""Shared async rate limiting + defensive parsing for broker REST clients.

Each broker exposes ONE shared REST budget per API key:
  - Fyers : 10 req/sec, 200 req/min, 100,000 req/day  (ALL REST endpoints share it)
  - Upstox: 50 req/sec, 2,000 req/30min, no daily cap  (analytics / historical)

Every scaled caller (the ~227-name option-chain poller, the history gap-fill, the
09:15 eager poll) MUST pull from a SINGLE process-global limiter so bursts get
spread under the per-minute / per-second governor instead of firing inside one
window and tripping a 429 against the sole live lane.

The limiter holds its lock across the back-off sleep on purpose: that serialises
admission so callers are spread (FIFO-ish) rather than all waking at once.
"""
from __future__ import annotations

import asyncio
import json as _json
import time
from collections import deque
from typing import Optional

from loguru import logger


class RateLimitDayExceeded(RuntimeError):
    """Raised when a per-day cap is hit — sleeping until tomorrow is pointless."""


class AsyncRateLimiter:
    """Multi-window token bucket. ``windows`` = list of (max_count, window_seconds)."""

    def __init__(self, *, windows: list[tuple[int, float]], per_day: Optional[int] = None, name: str = "limiter"):
        # Each window tracked by its own deque of monotonic admission timestamps.
        self._windows = [(int(c), float(s), deque()) for c, s in windows]
        self.per_day = int(per_day) if per_day else None
        self.name = name
        self._lock = asyncio.Lock()
        self._day_count = 0
        self._day_key: Optional[int] = None

    def _purge(self, now: float) -> None:
        for _count, span, dq in self._windows:
            while dq and now - dq[0] >= span:
                dq.popleft()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._purge(now)

                day_key = int(time.time() // 86400)
                if self._day_key != day_key:
                    self._day_key = day_key
                    self._day_count = 0
                if self.per_day is not None and self._day_count >= self.per_day:
                    raise RateLimitDayExceeded(f"{self.name}: per-day cap {self.per_day} reached")

                wait = 0.0
                for count, span, dq in self._windows:
                    if len(dq) >= count:
                        wait = max(wait, span - (now - dq[0]))

                if wait <= 0:
                    for _count, _span, dq in self._windows:
                        dq.append(now)
                    self._day_count += 1
                    return

                # Cap each sleep so the day-rollover / purge re-evaluates promptly.
                await asyncio.sleep(min(wait, 5.0) + 0.005)

    def snapshot(self) -> dict:
        now = time.monotonic()
        self._purge(now)
        return {
            "name": self.name,
            "windows": [{"max": c, "span_s": s, "used": len(dq)} for c, s, dq in self._windows],
            "day_count": self._day_count,
            "per_day": self.per_day,
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
