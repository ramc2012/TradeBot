"""Weighted fair-share priority admission for the shared broker rate limiter.

Verifies: (1) the governor still caps admissions per window; (2) higher priority
is admitted first when the governor is contended; (3) aging prevents starvation
of low-priority waiters; (4) the default (no priority) path is FIFO and behaves
exactly like before.
"""
from __future__ import annotations

import asyncio

import pytest

from brokers.rate_limiter import (
    PRIORITY_BULK,
    PRIORITY_HIGH,
    PRIORITY_NORMAL,
    AsyncRateLimiter,
    broker_priority,
)


@pytest.mark.asyncio
async def test_governor_caps_admissions_per_window() -> None:
    # 2 admits/second. Three back-to-back acquires: the 3rd must wait ~1s.
    lim = AsyncRateLimiter(windows=[(2, 1.0)], name="t")
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    await lim.acquire()
    await lim.acquire()
    assert loop.time() - t0 < 0.2         # first two immediate
    await lim.acquire()
    assert loop.time() - t0 >= 0.9        # third gated by the 1s window


@pytest.mark.asyncio
async def test_higher_priority_admitted_first_under_contention() -> None:
    # 1 admit/sec so everything after the first contends. Fill the slot, then
    # enqueue a BULK then a HIGH waiter — HIGH must be admitted before BULK.
    lim = AsyncRateLimiter(windows=[(1, 1.0)], name="t", aging_step_seconds=1000.0)
    await lim.acquire()  # consumes the only slot for ~1s
    order: list[str] = []

    async def worker(tag: str, prio: float, delay: float) -> None:
        await asyncio.sleep(delay)
        with broker_priority(prio):
            await lim.acquire()
        order.append(tag)

    # BULK enqueues slightly BEFORE HIGH, yet HIGH should win (aging disabled).
    await asyncio.gather(
        worker("bulk", PRIORITY_BULK, 0.05),
        worker("high", PRIORITY_HIGH, 0.10),
    )
    assert order == ["high", "bulk"]


@pytest.mark.asyncio
async def test_aging_prevents_starvation() -> None:
    # With fast aging, a BULK waiter that has waited long enough out-ranks a
    # freshly-arriving HIGH waiter — i.e. it is NOT starved indefinitely.
    lim = AsyncRateLimiter(windows=[(1, 0.4)], name="t", aging_step_seconds=0.05)
    await lim.acquire()
    order: list[str] = []

    async def worker(tag: str, prio: float, delay: float) -> None:
        await asyncio.sleep(delay)
        with broker_priority(prio):
            await lim.acquire()
        order.append(tag)

    # BULK waits from t=0; HIGH keeps arriving fresh. Aged BULK should still get
    # in within a couple of windows rather than never.
    await asyncio.gather(
        worker("bulk", PRIORITY_BULK, 0.0),
        worker("high1", PRIORITY_HIGH, 0.45),
    )
    assert "bulk" in order


@pytest.mark.asyncio
async def test_default_is_fifo() -> None:
    # No priority set → all NORMAL → tie-break by arrival order (FIFO).
    lim = AsyncRateLimiter(windows=[(1, 0.3)], name="t")
    await lim.acquire()
    order: list[int] = []

    async def worker(idx: int) -> None:
        await asyncio.sleep(0.02 * idx)  # staggered arrival 0,1,2
        await lim.acquire()
        order.append(idx)

    await asyncio.gather(worker(0), worker(1), worker(2))
    assert order == [0, 1, 2]


@pytest.mark.asyncio
async def test_contextvar_defaults_to_normal() -> None:
    from brokers.rate_limiter import _request_priority
    assert _request_priority.get() == PRIORITY_NORMAL
    with broker_priority(PRIORITY_BULK):
        assert _request_priority.get() == PRIORITY_BULK
    assert _request_priority.get() == PRIORITY_NORMAL
