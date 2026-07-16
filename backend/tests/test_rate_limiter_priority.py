"""Weighted fair-share priority admission for the shared broker rate limiter.

Verifies: (1) the governor still caps admissions per window; (2) higher priority
is admitted first when the governor is contended; (3) aging prevents starvation
of low-priority waiters; (4) the default (no priority) path is FIFO and behaves
exactly like before; (5) quota classes reserve capacity orthogonally to
priority (CRITICAL 40% reservation, BULK 25% hard cap + instant yield).
"""
from __future__ import annotations

import asyncio

import pytest

from brokers.rate_limiter import (
    CLASS_BULK,
    CLASS_CRITICAL,
    CLASS_STANDARD,
    PRIORITY_BULK,
    PRIORITY_HIGH,
    PRIORITY_NORMAL,
    AsyncRateLimiter,
    broker_class,
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


# ── Cancellation safety (2026-07-15 event-per-waiter rewrite) ─────────────────
# The Condition-based monitor used `wait_for(cond.wait(), timeout)` — the
# documented cancellation-unsafe pattern. A waiter cancelled mid-wait could
# escape with "RuntimeError: Lock is not acquired" (observed 4x on 2026-07-14)
# and wedge admission for EVERY subsequent Upstox REST call. These tests pin
# the regression: cancelled waiters must depart cleanly and never block others.


@pytest.mark.asyncio
async def test_cancelled_waiter_departs_cleanly_and_admission_continues() -> None:
    lim = AsyncRateLimiter(windows=[(1, 0.4)], name="t")
    await lim.acquire()  # occupy the only slot for ~0.4s

    waiter = asyncio.ensure_future(lim.acquire())
    await asyncio.sleep(0.05)  # let it enqueue and start waiting
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    # Departure must be complete — no orphaned ticket/event may linger to
    # distort ranking or leak.
    assert lim._pending == {}
    assert lim._waiters == {}

    # Admission must still work: a fresh acquire gets the next slot instead of
    # wedging behind the cancelled ghost (the 2026-07-14 failure mode).
    await asyncio.wait_for(lim.acquire(), timeout=2.0)


@pytest.mark.asyncio
async def test_cancelling_best_ranked_waiter_unblocks_the_next() -> None:
    # HIGH waiter ranks best; cancelling it must let the BULK waiter through
    # (a lost wakeup here is exactly how the old monitor wedged).
    lim = AsyncRateLimiter(windows=[(1, 0.3)], name="t", aging_step_seconds=1000.0)
    await lim.acquire()

    async def acquire_with(prio: float) -> None:
        with broker_priority(prio):
            await lim.acquire()

    high = asyncio.ensure_future(acquire_with(PRIORITY_HIGH))
    await asyncio.sleep(0.02)
    bulk = asyncio.ensure_future(acquire_with(PRIORITY_BULK))
    await asyncio.sleep(0.02)

    high.cancel()
    with pytest.raises(asyncio.CancelledError):
        await high

    await asyncio.wait_for(bulk, timeout=2.0)  # would hang before the rewrite
    assert lim._pending == {}
    assert lim._waiters == {}


@pytest.mark.asyncio
async def test_cancellation_storm_leaves_state_clean_and_others_complete() -> None:
    # Half the waiters get cancelled at staggered moments while the governor is
    # saturated; every survivor must still be admitted and no state may leak.
    lim = AsyncRateLimiter(windows=[(4, 0.1)], name="t", aging_step_seconds=0.05)

    async def one(idx: int) -> str:
        await lim.acquire()
        return f"ok{idx}"

    tasks = [asyncio.ensure_future(one(i)) for i in range(16)]
    for i, task in enumerate(tasks):
        if i % 2 == 0:
            await asyncio.sleep(0.01)
            task.cancel()

    results = await asyncio.gather(*tasks, return_exceptions=True)
    survivors = [r for r in results if isinstance(r, str)]
    cancelled = [r for r in results if isinstance(r, asyncio.CancelledError)]
    assert len(survivors) + len(cancelled) == 16
    assert survivors  # the storm must not take the lane down with it
    assert lim._pending == {}
    assert lim._waiters == {}


# ── Quota classes (2026-07-15 reserved broker budget) ─────────────────────────
# Classes reserve CAPACITY orthogonally to priority: CRITICAL owns a 40%
# reservation lower classes can never consume, BULK is hard-capped at 25% and
# must yield instantly while any CRITICAL waiter is queued. Priority/aging
# still order waiters WITHIN the admissible set but never bypass class limits.


@pytest.mark.asyncio
async def test_class_contextvar_defaults_to_standard() -> None:
    from brokers.rate_limiter import _request_class

    assert _request_class.get() == CLASS_STANDARD
    with broker_class(CLASS_BULK):
        assert _request_class.get() == CLASS_BULK
        with broker_class(CLASS_CRITICAL):
            assert _request_class.get() == CLASS_CRITICAL
        assert _request_class.get() == CLASS_BULK
    assert _request_class.get() == CLASS_STANDARD
    with pytest.raises(ValueError):
        with broker_class("no-such-class"):
            pass
    lim = AsyncRateLimiter(windows=[(2, 0.2)], name="t")
    with pytest.raises(ValueError):
        await lim.acquire(request_class="no-such-class")


@pytest.mark.asyncio
async def test_critical_reservation_never_consumed_by_lower_classes() -> None:
    # (10, 0.6) window → critical_reserved=4, non_critical_cap=6, bulk_cap=2.
    lim = AsyncRateLimiter(windows=[(10, 0.6)], name="t")
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    for _ in range(6):
        await lim.acquire()  # default class STANDARD
    assert loop.time() - t0 < 0.2  # up to the 60% non-critical cap: instant

    # The reserved 40% is intact — a CRITICAL acquire is admitted immediately
    # even though STANDARD has saturated its share.
    with broker_class(CLASS_CRITICAL):
        await lim.acquire()
    assert loop.time() - t0 < 0.3

    # A 7th STANDARD is blocked on the reservation (NOT on total capacity —
    # only 7/10 slots are used) until a standard admission ages out at ~0.6s.
    await lim.acquire()
    assert loop.time() - t0 >= 0.55

    snap = lim.snapshot()
    assert snap["admitted_by_class"][CLASS_CRITICAL] == 1
    assert snap["admitted_by_class"][CLASS_STANDARD] == 7


@pytest.mark.asyncio
async def test_bulk_hard_cap_binds_even_when_window_is_idle() -> None:
    # (10, 0.6) window → bulk_cap=2. The 3rd BULK admit must wait for a bulk
    # slot to age out even though 8/10 governor slots are completely free.
    lim = AsyncRateLimiter(windows=[(10, 0.6)], name="t")
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    with broker_class(CLASS_BULK):
        await lim.acquire()
        await lim.acquire()
        assert loop.time() - t0 < 0.2
        await lim.acquire()
    assert loop.time() - t0 >= 0.55


@pytest.mark.asyncio
async def test_bulk_yields_instantly_when_critical_queues() -> None:
    # Class beats priority AND arrival order: the BULK waiter arrives first
    # and carries PRIORITY_HIGH, the CRITICAL waiter arrives later with
    # PRIORITY_BULK — the CRITICAL waiter must still be admitted first.
    lim = AsyncRateLimiter(windows=[(1, 0.4)], name="t", aging_step_seconds=1000.0)
    await lim.acquire()  # occupy the only slot for ~0.4s
    order: list[str] = []

    async def worker(tag: str, cls: str, prio: float, delay: float) -> None:
        await asyncio.sleep(delay)
        with broker_class(cls), broker_priority(prio):
            await lim.acquire()
        order.append(tag)

    await asyncio.gather(
        worker("bulk", CLASS_BULK, PRIORITY_HIGH, 0.02),
        worker("critical", CLASS_CRITICAL, PRIORITY_BULK, 0.06),
    )
    assert order == ["critical", "bulk"]


@pytest.mark.asyncio
async def test_class_blocked_bulk_does_not_shadow_standard() -> None:
    # A cap-blocked BULK waiter with the best priority must NOT block an
    # admissible STANDARD waiter behind it (priority inversion via the class
    # dimension). (10, 0.6) window → bulk_cap=2, both taken.
    lim = AsyncRateLimiter(windows=[(10, 0.6)], name="t", aging_step_seconds=1000.0)
    with broker_class(CLASS_BULK):
        await lim.acquire()
        await lim.acquire()
    order: list[str] = []
    loop = asyncio.get_event_loop()
    t0 = loop.time()

    async def bulk_waiter() -> None:
        with broker_class(CLASS_BULK), broker_priority(PRIORITY_HIGH):
            await lim.acquire()
        order.append("bulk")

    async def standard_waiter() -> None:
        await asyncio.sleep(0.05)  # arrives AFTER bulk, with worse priority
        with broker_priority(PRIORITY_BULK):
            await lim.acquire()
        order.append("standard")
        assert loop.time() - t0 < 0.3  # admitted instantly, not behind bulk

    await asyncio.gather(bulk_waiter(), standard_waiter())
    assert order == ["standard", "bulk"]


@pytest.mark.asyncio
async def test_critical_may_use_the_full_window() -> None:
    # (4, 0.5) window → critical_reserved=1, but CRITICAL is never capped
    # below total capacity: all 4 slots admit back-to-back.
    lim = AsyncRateLimiter(windows=[(4, 0.5)], name="t")
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    with broker_class(CLASS_CRITICAL):
        for _ in range(4):
            await lim.acquire()
        assert loop.time() - t0 < 0.2
        await lim.acquire()  # 5th: total cap → gated by the 0.5s window
    assert loop.time() - t0 >= 0.45


@pytest.mark.asyncio
async def test_cancelled_critical_waiter_releases_the_bulk_yield() -> None:
    # A queued CRITICAL waiter holds BULK out; cancelling it must clear the
    # yield flag and leave no class bookkeeping behind.
    lim = AsyncRateLimiter(windows=[(1, 0.3)], name="t", aging_step_seconds=1000.0)
    await lim.acquire()

    async def acquire_as(cls: str) -> None:
        with broker_class(cls):
            await lim.acquire()

    bulk = asyncio.ensure_future(acquire_as(CLASS_BULK))
    await asyncio.sleep(0.02)
    critical = asyncio.ensure_future(acquire_as(CLASS_CRITICAL))
    await asyncio.sleep(0.02)
    assert lim._critical_waiting == 1

    critical.cancel()
    with pytest.raises(asyncio.CancelledError):
        await critical
    assert lim._critical_waiting == 0

    await asyncio.wait_for(bulk, timeout=2.0)
    assert lim._pending == {}
    assert lim._pending_class == {}
    assert lim._waiters == {}


@pytest.mark.asyncio
async def test_bulk_flood_cannot_starve_critical_arrivals() -> None:
    # ADVERSARIAL contention proof: 100 BULK acquires flood the queue FIRST,
    # then 5 CRITICAL requests arrive. Every CRITICAL must clear the governor
    # almost immediately — the 40% reservation plus the instant-yield rule
    # must hold under a saturated queue, not just a polite one.
    # (10, 0.25) window → critical_reserved=4, non_critical_cap=6, bulk_cap=2.
    lim = AsyncRateLimiter(windows=[(10, 0.25)], name="t", aging_step_seconds=0.01)
    loop = asyncio.get_event_loop()
    admitted: list[tuple[str, float]] = []
    t0 = loop.time()

    async def bulk(i: int) -> None:
        with broker_class(CLASS_BULK):
            await lim.acquire()
        admitted.append((f"bulk{i}", loop.time() - t0))

    async def critical(i: int) -> None:
        await asyncio.sleep(0.05)  # arrive AFTER the whole flood is queued
        with broker_class(CLASS_CRITICAL):
            await lim.acquire()
        admitted.append((f"crit{i}", loop.time() - t0))

    bulk_tasks = [asyncio.ensure_future(bulk(i)) for i in range(100)]
    crit_tasks = [asyncio.ensure_future(critical(i)) for i in range(5)]
    try:
        # All 5 CRITICALs must be admitted promptly despite 100 queued BULKs.
        await asyncio.wait_for(asyncio.gather(*crit_tasks), timeout=1.0)
        crit_times = [t for tag, t in admitted if tag.startswith("crit")]
        assert len(crit_times) == 5
        # Reservation headroom (4 reserved + idle share) admits them ~at once.
        assert max(crit_times) < 0.3, f"critical starved: {admitted}"
        # While the CRITICALs were queued/admitted, BULK stayed at its cap:
        # only the 2 pre-arrival admits (bulk_cap=2) may precede the last crit.
        last_crit = max(crit_times)
        early_bulk = [tag for tag, t in admitted if tag.startswith("bulk") and t <= last_crit]
        assert len(early_bulk) <= 2, f"bulk broke the cap/yield: {admitted}"
        # No CRITICAL ever waited behind the flood in queue-order terms.
        snap = lim.snapshot()
        assert snap["admitted_by_class"][CLASS_CRITICAL] == 5
        assert snap["critical_waiting"] == 0
    finally:
        for task in bulk_tasks:
            task.cancel()
        await asyncio.gather(*bulk_tasks, return_exceptions=True)
    assert lim._pending == {}
    assert lim._waiters == {}


@pytest.mark.asyncio
async def test_sustained_bulk_flood_critical_stream_never_waits_a_window() -> None:
    # Continuous variant: CRITICALs arrive one at a time WHILE the bulk flood
    # keeps re-queuing. Each must be admitted well inside one window span —
    # i.e. the reservation is available on ARRIVAL, not merely eventually.
    lim = AsyncRateLimiter(windows=[(8, 0.2)], name="t", aging_step_seconds=0.01)
    loop = asyncio.get_event_loop()
    stop = False

    async def bulk_pump() -> None:
        while not stop:
            with broker_class(CLASS_BULK):
                await lim.acquire()

    pumps = [asyncio.ensure_future(bulk_pump()) for _ in range(20)]
    try:
        await asyncio.sleep(0.05)  # let the flood saturate the bulk cap
        for _ in range(4):
            t0 = loop.time()
            with broker_class(CLASS_CRITICAL):
                await asyncio.wait_for(lim.acquire(), timeout=1.0)
            assert loop.time() - t0 < 0.15, "critical waited ≥ a window behind bulk"
            await asyncio.sleep(0.06)
    finally:
        stop = True
        for task in pumps:
            task.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
    assert lim._pending == {}
    assert lim._waiters == {}
