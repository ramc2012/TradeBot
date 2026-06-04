"""Prometheus instrumentation — WS-0.2 of the platform remediation plan.

Pure observation. Two hard rules this module must never break:
  1. Nothing here may change trading behaviour.
  2. Nothing here may raise into a hot path. Every public helper swallows its own
     errors, so a metrics bug can never take down tick ingest, order placement, or
     a strategy scan.

If ``prometheus_client`` is not installed the entire module degrades to no-ops so
the app still boots — observability must never be a single point of failure. (The
dependency is added to requirements.txt; until the image is rebuilt the no-op path
keeps prod healthy.)

Exposed at ``GET /metrics`` (Prometheus text format). Scrape it for p50/p95/p99 on:
  - nomad_tick_age_seconds          tick.timestamp → ingest        (data plane freshness)
  - nomad_ticks_ingested_total      accepted ticks                 (throughput)
  - nomad_ingest_rejected_total     dropped at ingest              (wired by WS-0.1)
  - nomad_scan_duration_seconds     per-lane scan wall-time        (compute cost)
  - nomad_order_rtt_seconds         broker send → ack              (execution latency)
  - nomad_fill_confirm_seconds      decision → fill confirmed      (wired by WS-1.2)
  - nomad_event_loop_lag_seconds    scheduler delay                (WS-1.1 probe — the
                                                                     direct measure of
                                                                     compute blocking the
                                                                     data plane)
"""
from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from typing import Iterator, Optional, Tuple

from loguru import logger

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    METRICS_ENABLED = True
except Exception as exc:  # pragma: no cover - only when dependency is absent
    METRICS_ENABLED = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
    logger.warning(f"[metrics] prometheus_client unavailable — metrics disabled ({exc})")

    class _Noop:  # minimal stand-in matching the prometheus_client surface we use
        def __init__(self, *_a, **_k) -> None: ...
        def labels(self, *_a, **_k) -> "_Noop":
            return self
        def observe(self, *_a, **_k) -> None: ...
        def inc(self, *_a, **_k) -> None: ...
        def set(self, *_a, **_k) -> None: ...

    def generate_latest(*_a, **_k) -> bytes:  # type: ignore[misc]
        return b"# metrics disabled: prometheus_client not installed\n"

    Counter = Gauge = Histogram = _Noop  # type: ignore[assignment,misc]


# ── Bucket families ───────────────────────────────────────────────────────────
# This is a positional/swing F&O system on retail broker APIs, not HFT — buckets
# span milliseconds (loop lag) through minutes (slow scans).
_FAST = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 15.0)
_SLOW = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 180.0)
_AGE = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0)
_LAG = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)


# ── Metric definitions (created once at import) ──────────────────────────────
TICKS_INGESTED = Counter("nomad_ticks_ingested_total", "Ticks accepted at ingest", ["source"])
INGEST_REJECTED = Counter("nomad_ingest_rejected_total", "Ticks/candles rejected at ingest", ["reason"])
TICK_AGE = Histogram(
    "nomad_tick_age_seconds", "Age of a tick at ingest (tick.timestamp → ingest)", ["source"], buckets=_AGE
)
SCAN_DURATION = Histogram(
    "nomad_scan_duration_seconds", "Strategy/runner scan wall-time", ["lane"], buckets=_SLOW
)
ORDER_RTT = Histogram(
    "nomad_order_rtt_seconds", "Broker order round-trip (send → ack)", ["broker", "result"], buckets=_FAST
)
FILL_CONFIRM = Histogram(
    "nomad_fill_confirm_seconds", "Decision → fill confirmed (wired by WS-1.2)", ["lane"], buckets=_SLOW
)
LOOP_LAG = Histogram("nomad_event_loop_lag_seconds", "Event-loop scheduling delay", buckets=_LAG)
LOOP_LAG_CURRENT = Gauge("nomad_event_loop_lag_seconds_current", "Most recent event-loop lag sample (seconds)")


# ── Helpers (never raise) ─────────────────────────────────────────────────────
def observe_tick(source: str, age_seconds: Optional[float]) -> None:
    """Record one accepted tick and, if known, its age at ingest."""
    try:
        src = source or "unknown"
        TICKS_INGESTED.labels(source=src).inc()
        if age_seconds is not None and age_seconds >= 0:
            TICK_AGE.labels(source=src).observe(age_seconds)
    except Exception:
        pass


def record_reject(reason: str) -> None:
    """Increment the ingest-reject counter (called by the WS-0.1 validation gate)."""
    try:
        INGEST_REJECTED.labels(reason=reason or "unknown").inc()
    except Exception:
        pass


def observe_order_rtt(broker: str, result: str, seconds: float) -> None:
    try:
        ORDER_RTT.labels(broker=broker or "unknown", result=result or "unknown").observe(max(seconds, 0.0))
    except Exception:
        pass


def observe_scan(lane: str, seconds: float) -> None:
    try:
        SCAN_DURATION.labels(lane=lane or "unknown").observe(max(seconds, 0.0))
    except Exception:
        pass


def observe_fill_confirm(lane: str, seconds: float) -> None:
    try:
        FILL_CONFIRM.labels(lane=lane or "unknown").observe(max(seconds, 0.0))
    except Exception:
        pass


@contextmanager
def scan_timer(lane: str) -> Iterator[None]:
    """Time a block and record it under nomad_scan_duration_seconds{lane=...}."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        observe_scan(lane, time.perf_counter() - t0)


def render() -> Tuple[bytes, str]:
    """Return (body, content_type) for the /metrics endpoint."""
    try:
        return generate_latest(), CONTENT_TYPE_LATEST
    except Exception as exc:  # pragma: no cover
        return (f"# metrics render error: {exc}\n".encode(), CONTENT_TYPE_LATEST)


async def run_loop_lag_monitor(interval: float = 0.1) -> None:
    """Sample event-loop scheduling delay forever (cancellable).

    Sleeps ``interval`` and records how much *longer* than ``interval`` the wake-up
    actually took. That overshoot is time the loop spent blocked on something else
    — a CPU-bound scan, a sync DB call — i.e. head-of-line latency for every
    coroutine, including tick ingest and WS push. This is the direct probe for the
    data-plane/compute-plane contention identified as WS-1.1; watch its p99.
    """
    loop = asyncio.get_running_loop()
    while True:
        start = loop.time()
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
        lag = max(0.0, (loop.time() - start) - interval)
        try:
            LOOP_LAG.observe(lag)
            LOOP_LAG_CURRENT.set(lag)
        except Exception:
            pass
