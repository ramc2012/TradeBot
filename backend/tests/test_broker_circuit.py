"""Broker circuit breaker — trip on sustained failure, cool down, half-open probe,
and failover ordering. Uses tiny thresholds so no real time/sleep is needed."""
from __future__ import annotations

import time

from market_data.broker_circuit import CLOSED, HALF_OPEN, OPEN, BrokerCircuit


def _circuit() -> BrokerCircuit:
    return BrokerCircuit(window_seconds=60.0, failure_threshold=3, cooldown_seconds=30.0)


def test_stays_closed_and_allows_under_threshold() -> None:
    c = _circuit()
    assert c.allow("upstox", "chain") is True
    c.record_failure("upstox", "chain")
    c.record_failure("upstox", "chain")            # 2 < threshold 3
    assert c.allow_state("upstox", "chain") == CLOSED
    assert c.allow("upstox", "chain") is True


def test_opens_after_threshold_and_blocks() -> None:
    c = _circuit()
    for _ in range(3):
        c.record_failure("upstox", "chain")
    assert c.allow_state("upstox", "chain") == OPEN
    assert c.allow("upstox", "chain") is False     # cooling down → blocked


def test_success_resets_to_closed() -> None:
    c = _circuit()
    c.record_failure("fyers", "chain")
    c.record_failure("fyers", "chain")
    c.record_success("fyers", "chain")
    assert c.allow_state("fyers", "chain") == CLOSED
    # Prior failures cleared → threshold not reached by two more.
    c.record_failure("fyers", "chain")
    c.record_failure("fyers", "chain")
    assert c.allow_state("fyers", "chain") == CLOSED


def test_half_open_probe_then_recovery() -> None:
    c = BrokerCircuit(window_seconds=60.0, failure_threshold=2, cooldown_seconds=0.0)
    c.record_failure("upstox", "chain")
    c.record_failure("upstox", "chain")
    assert c.allow_state("upstox", "chain") == OPEN
    # cooldown 0 → next allow() transitions to HALF_OPEN and permits one probe.
    assert c.allow("upstox", "chain") is True
    assert c.allow_state("upstox", "chain") == HALF_OPEN
    # Second concurrent probe held back while one is in flight.
    assert c.allow("upstox", "chain") is False
    # Probe succeeds → CLOSED.
    c.record_success("upstox", "chain")
    assert c.allow_state("upstox", "chain") == CLOSED


def test_half_open_probe_failure_reopens() -> None:
    c = BrokerCircuit(window_seconds=60.0, failure_threshold=2, cooldown_seconds=0.0)
    c.record_failure("upstox", "chain")
    c.record_failure("upstox", "chain")
    assert c.allow("upstox", "chain") is True       # → HALF_OPEN probe
    c.record_failure("upstox", "chain")             # probe fails
    assert c.allow_state("upstox", "chain") == OPEN


def test_preferred_order_puts_open_broker_last() -> None:
    c = _circuit()
    for _ in range(3):
        c.record_failure("upstox", "chain")         # upstox OPEN
    order = c.preferred_order(["upstox", "fyers", "catalog"], "chain")
    assert order.index("fyers") < order.index("upstox")
    assert set(order) == {"upstox", "fyers", "catalog"}  # nothing dropped


def test_preferred_order_repromotes_after_cooldown() -> None:
    # With cooldown 0, a tripped broker's cooldown elapses immediately, so it is
    # NOT stuck last — it climbs back so routing re-tries it and it can recover.
    c = BrokerCircuit(window_seconds=60.0, failure_threshold=2, cooldown_seconds=0.0)
    c.record_failure("upstox", "chain")
    c.record_failure("upstox", "chain")            # OPEN, but cooldown 0
    order = c.preferred_order(["upstox", "fyers"], "chain")
    assert order == ["upstox", "fyers"]            # original order — not deprioritised
    # A subsequent success (from being re-tried) closes it.
    c.record_success("upstox", "chain")
    assert c.allow_state("upstox", "chain") == CLOSED


def test_disabled_circuit_always_allows(monkeypatch) -> None:
    c = _circuit()
    for _ in range(5):
        c.record_failure("upstox", "chain")
    import core.config as config
    monkeypatch.setattr(config.settings, "BROKER_CIRCUIT_ENABLED", False)
    assert c.allow("upstox", "chain") is True
    assert c.preferred_order(["upstox", "fyers"], "chain") == ["upstox", "fyers"]
