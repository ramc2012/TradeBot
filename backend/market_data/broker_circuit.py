"""Per-broker/data-type circuit breaker with Upstox↔Fyers failover ordering.

A broker that is throttling (sustained 429s) or erroring should be SKIPPED for a
cooldown rather than retried into the ground on every call — and requests should
prefer the healthy broker. This tracks failures per (broker, data-type) in a
rolling window and exposes:

  - `record_success(broker, datatype)` / `record_failure(broker, datatype)`
  - `allow(broker, datatype)`  → False while OPEN (cooling down); a single probe
    is allowed once the cooldown elapses (HALF_OPEN)
  - `preferred_order(candidates, datatype)` → reorder a broker preference list to
    put healthy brokers first and OPEN ones last (failover ordering)

FAIL-OPEN by design: it only trips after sustained failure and, even when OPEN,
callers may still choose to try (it informs, it does not hard-block) — so a bug
here degrades to "no circuit benefit", never "all brokers blocked". Enable/tune
via BROKER_CIRCUIT_* settings.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any, Optional

from loguru import logger

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class _Breaker:
    __slots__ = ("failures", "opened_at", "state", "probe_inflight", "trips")

    def __init__(self) -> None:
        self.failures: deque[float] = deque()  # monotonic ts of recent failures
        self.opened_at: Optional[float] = None
        self.state: str = CLOSED
        self.probe_inflight: bool = False
        self.trips: int = 0


class BrokerCircuit:
    def __init__(
        self,
        *,
        window_seconds: float = 60.0,
        failure_threshold: int = 5,
        cooldown_seconds: float = 30.0,
    ) -> None:
        self._window = window_seconds
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._breakers: dict[str, _Breaker] = {}

    @staticmethod
    def _key(broker: str, datatype: str) -> str:
        return f"{broker}:{datatype}" if datatype else broker

    def _enabled(self) -> bool:
        try:
            from core.config import settings
            return bool(getattr(settings, "BROKER_CIRCUIT_ENABLED", True))
        except Exception:  # noqa: BLE001
            return True

    def _get(self, key: str) -> _Breaker:
        b = self._breakers.get(key)
        if b is None:
            b = _Breaker()
            self._breakers[key] = b
        return b

    def _purge(self, b: _Breaker, now: float) -> None:
        while b.failures and now - b.failures[0] >= self._window:
            b.failures.popleft()

    def allow(self, broker: str, datatype: str = "") -> bool:
        if not self._enabled():
            return True
        now = time.monotonic()
        b = self._get(self._key(broker, datatype))
        if b.state == OPEN:
            if b.opened_at is not None and (now - b.opened_at) >= self._cooldown:
                # Cooldown elapsed → allow a single probe.
                b.state = HALF_OPEN
                b.probe_inflight = True
                return True
            return False
        if b.state == HALF_OPEN:
            # Only one probe in flight at a time; hold the rest back.
            if b.probe_inflight:
                return False
            b.probe_inflight = True
            return True
        return True

    def record_success(self, broker: str, datatype: str = "") -> None:
        b = self._get(self._key(broker, datatype))
        b.failures.clear()
        b.probe_inflight = False
        if b.state != CLOSED:
            logger.info(f"[broker-circuit] {self._key(broker, datatype)} → CLOSED (recovered)")
        b.state = CLOSED
        b.opened_at = None

    def record_failure(self, broker: str, datatype: str = "") -> None:
        now = time.monotonic()
        key = self._key(broker, datatype)
        b = self._get(key)
        b.probe_inflight = False
        if b.state == HALF_OPEN:
            # Probe failed → straight back to OPEN for another cooldown.
            b.state = OPEN
            b.opened_at = now
            b.trips += 1
            logger.warning(f"[broker-circuit] {key} probe failed → OPEN {self._cooldown:.0f}s")
            return
        b.failures.append(now)
        self._purge(b, now)
        if b.state == CLOSED and len(b.failures) >= self._threshold:
            b.state = OPEN
            b.opened_at = now
            b.trips += 1
            logger.warning(
                f"[broker-circuit] {key} OPEN — {len(b.failures)} failures/"
                f"{self._window:.0f}s, cooldown {self._cooldown:.0f}s"
            )

    def _still_cooling(self, broker: str, datatype: str) -> bool:
        """OPEN and the cooldown has NOT yet elapsed. Once it elapses we let the
        broker be tried again (so record_success/failure can flip it CLOSED or
        re-OPEN) — otherwise a broker that recovered would stay deprioritised for
        the whole session because the healthy broker keeps serving and this one is
        never called to record a success."""
        b = self._breakers.get(self._key(broker, datatype))
        if b is None or b.state != OPEN or b.opened_at is None:
            return False
        return (time.monotonic() - b.opened_at) < self._cooldown

    def preferred_order(self, candidates: list[str], datatype: str = "") -> list[str]:
        """Stable-reorder a broker preference list: healthy + cooldown-elapsed
        brokers first, still-cooling OPEN brokers last. Never drops a candidate —
        failover still has something to try even if all are unhealthy, and a
        cooled-down broker climbs back so it gets re-probed and can recover."""
        if not self._enabled():
            return list(candidates)
        healthy = [c for c in candidates if not self._still_cooling(c, datatype)]
        cooling = [c for c in candidates if self._still_cooling(c, datatype)]
        return healthy + cooling

    def allow_state(self, broker: str, datatype: str = "") -> str:
        b = self._breakers.get(self._key(broker, datatype))
        return b.state if b is not None else CLOSED

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        out: dict[str, Any] = {}
        for key, b in self._breakers.items():
            self._purge(b, now)
            out[key] = {
                "state": b.state,
                "recent_failures": len(b.failures),
                "trips": b.trips,
                "cooldown_remaining_s": (
                    round(max(0.0, self._cooldown - (now - b.opened_at)), 1)
                    if b.state == OPEN and b.opened_at is not None else 0.0
                ),
            }
        return out


def cadence_datatype(base: str) -> str:
    """Scope a circuit datatype by the active lane broker profile so a broker's
    degradation trips ONLY its cadence group's breaker (fast vs slow), never the
    other's — structural isolation with zero new breaker code.

    An Upstox chain degradation while a SLOW lane is fetching records under
    ``upstox:slow_chain`` and only reorders the slow group; a Fyers degradation
    under a FAST lane records ``fyers:fast_chain`` and only reorders the fast
    group. Flag-off OR the DEFAULT profile → the base datatype (byte-identical
    breaker keys to pre-routing behaviour). Must be applied identically at the
    record site (brokers/*.py) and the read site (source_policy) so the keys
    agree. Fail-safe: any error → base datatype (no isolation, never a crash).

    SCOPE — only ``chain`` is cadence-scoped. It is the sole datatype the read
    site (``source_policy.route_order`` → ``cadence_datatype("chain")``) and the
    ``/api/system/rate-budget`` telemetry actually consult, so scoping
    ``history``/``quote`` would only mint ``{fast,slow}_history`` /
    ``{fast,slow}_quote`` breaker keys nobody reads — fragmenting the circuit
    telemetry for zero routing benefit. Non-chain datatypes therefore keep their
    base key (identical to pre-routing) under every profile."""
    if base != "chain":
        return base
    try:
        from core.config import settings

        if not bool(getattr(settings, "LANE_BROKER_ROUTING_ENABLED", False)):
            return base
        from brokers.rate_limiter import current_lane_profile, LANE_PROFILE_DEFAULT

        prof = current_lane_profile()
        if prof and prof != LANE_PROFILE_DEFAULT:
            return f"{prof}_{base}"
    except Exception:  # noqa: BLE001
        pass
    return base


broker_circuit = BrokerCircuit()
