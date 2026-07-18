"""Lane→broker cadence routing (owner directive 2026-07-17).

Covers the four guarantees the routing layer must hold:
  1. FLAG-OFF no-op: with LANE_BROKER_ROUTING_ENABLED False the failover order is
     byte-identical to the pre-routing global order, under BOTH a slow and a fast
     lane profile (provable no-op).
  2. FLAG-ON reorder: a slow lane prefers Upstox-first, a fast lane Fyers-first —
     but only for DECISION purposes (option_chain / historical).
  3. REAL-TIME plane is never rerouted: live_ticks / market_profile / order_flow
     stay on the global order for every profile (marks/quote-tape stay Fyers-WS).
  4. Per-cadence circuit isolation: an Upstox degradation trips only the SLOW
     group's breaker (slow_chain) and a Fyers degradation only the FAST group's
     (fast_chain); degradation reorders (never drops) — graceful, not fail-open.

Plus the option-flow watchdog decision core and the commodity MCX exception guard.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import core.config as config
from brokers.rate_limiter import (
    LANE_PROFILE_DEFAULT,
    LANE_PROFILE_FAST,
    LANE_PROFILE_SLOW,
    current_lane_profile,
    lane_broker_profile,
)
from market_data import source_policy
from market_data.broker_circuit import broker_circuit, cadence_datatype
from market_data.source_policy import ordered_live_adapters, route_order


@pytest.fixture(autouse=True)
def _clean_circuit_and_profile():
    """Isolate every test from circuit-breaker singleton state and lane context."""
    broker_circuit._breakers.clear()
    assert current_lane_profile() == LANE_PROFILE_DEFAULT
    yield
    broker_circuit._breakers.clear()


def _enable_routing(monkeypatch, *, on: bool) -> None:
    monkeypatch.setattr(config.settings, "LANE_BROKER_ROUTING_ENABLED", on)


# ── 1. FLAG-OFF no-op (both profiles) ─────────────────────────────────────────
def test_flag_off_is_noop_for_slow_and_fast(monkeypatch) -> None:
    _enable_routing(monkeypatch, on=False)
    monkeypatch.setattr(config.settings, "BROKER_CIRCUIT_ENABLED", False)
    baseline_chain = ["upstox", "fyers", "catalog"]
    baseline_hist = ["postgres", "upstox_analytics", "upstox", "fyers"]

    # No context → global order.
    assert route_order("option_chain") == baseline_chain
    assert route_order("historical") == baseline_hist

    for profile in (LANE_PROFILE_SLOW, LANE_PROFILE_FAST):
        with lane_broker_profile(profile):
            assert route_order("option_chain") == baseline_chain
            assert route_order("historical") == baseline_hist


def test_flag_on_default_profile_is_noop(monkeypatch) -> None:
    _enable_routing(monkeypatch, on=True)
    monkeypatch.setattr(config.settings, "BROKER_CIRCUIT_ENABLED", False)
    # Flag on but DEFAULT profile → global order unchanged.
    assert route_order("option_chain") == ["upstox", "fyers", "catalog"]
    with lane_broker_profile(LANE_PROFILE_DEFAULT):
        assert route_order("option_chain") == ["upstox", "fyers", "catalog"]


# ── 2. FLAG-ON reorder (decision purposes) ────────────────────────────────────
def test_flag_on_fast_prefers_fyers(monkeypatch) -> None:
    _enable_routing(monkeypatch, on=True)
    monkeypatch.setattr(config.settings, "BROKER_CIRCUIT_ENABLED", False)
    with lane_broker_profile(LANE_PROFILE_FAST):
        assert route_order("option_chain") == ["fyers", "upstox", "catalog"]
        # historical: fyers hoisted to front, rest order preserved.
        assert route_order("historical")[0] == "fyers"
        assert set(route_order("historical")) == {
            "postgres", "upstox_analytics", "upstox", "fyers",
        }


def test_flag_on_slow_prefers_upstox(monkeypatch) -> None:
    _enable_routing(monkeypatch, on=True)
    monkeypatch.setattr(config.settings, "BROKER_CIRCUIT_ENABLED", False)
    with lane_broker_profile(LANE_PROFILE_SLOW):
        assert route_order("option_chain") == ["upstox", "fyers", "catalog"]
        # historical default is postgres-first; slow hoists upstox above fyers.
        hist = route_order("historical")
        assert hist[0] == "upstox"
        assert hist.index("upstox") < hist.index("fyers")


# ── 3. REAL-TIME plane never rerouted (R3 structural guarantee) ───────────────
def test_realtime_plane_not_rerouted_under_slow(monkeypatch) -> None:
    _enable_routing(monkeypatch, on=True)
    monkeypatch.setattr(config.settings, "BROKER_CIRCUIT_ENABLED", False)
    with lane_broker_profile(LANE_PROFILE_SLOW):
        # A slow lane must NOT drag its marks / quote-tape / tick MP-OF onto
        # Upstox — these stay Fyers-first (global order), seconds cadence.
        assert route_order("live_ticks") == ["fyers", "upstox"]
        assert route_order("market_profile") == ["fyers", "postgres", "upstox"]
        assert route_order("order_flow") == ["fyers", "upstox"]


def test_realtime_plane_not_rerouted_under_fast(monkeypatch) -> None:
    _enable_routing(monkeypatch, on=True)
    monkeypatch.setattr(config.settings, "BROKER_CIRCUIT_ENABLED", False)
    with lane_broker_profile(LANE_PROFILE_FAST):
        assert route_order("live_ticks") == ["fyers", "upstox"]
        assert route_order("order_flow") == ["fyers", "upstox"]


# ── cadence_datatype scoping (R1 key agreement source) ────────────────────────
def test_cadence_datatype_scopes_by_profile(monkeypatch) -> None:
    _enable_routing(monkeypatch, on=True)
    assert cadence_datatype("chain") == "chain"  # DEFAULT profile
    with lane_broker_profile(LANE_PROFILE_SLOW):
        assert cadence_datatype("chain") == "slow_chain"
    with lane_broker_profile(LANE_PROFILE_FAST):
        assert cadence_datatype("chain") == "fast_chain"


def test_cadence_datatype_flag_off_is_base(monkeypatch) -> None:
    _enable_routing(monkeypatch, on=False)
    with lane_broker_profile(LANE_PROFILE_SLOW):
        assert cadence_datatype("chain") == "chain"
    with lane_broker_profile(LANE_PROFILE_FAST):
        assert cadence_datatype("chain") == "chain"


def test_cadence_datatype_only_scopes_chain(monkeypatch) -> None:
    """Cosmetic (2026-07-17): only ``chain`` is cadence-scoped — it is the sole
    datatype the reorder read-site and rate-budget telemetry consult. Scoping
    history/quote would mint {fast,slow}_history/quote breaker keys nobody reads
    and fragment the circuit telemetry, so those stay on their base key under
    every profile even with the flag ON."""
    _enable_routing(monkeypatch, on=True)
    for base in ("history", "quote"):
        assert cadence_datatype(base) == base  # DEFAULT profile
        with lane_broker_profile(LANE_PROFILE_SLOW):
            assert cadence_datatype(base) == base
        with lane_broker_profile(LANE_PROFILE_FAST):
            assert cadence_datatype(base) == base
    # chain is still scoped (unchanged behaviour).
    with lane_broker_profile(LANE_PROFILE_FAST):
        assert cadence_datatype("chain") == "fast_chain"


# ── 4. Per-cadence circuit isolation + graceful degradation ───────────────────
def test_upstox_open_degrades_slow_only(monkeypatch) -> None:
    _enable_routing(monkeypatch, on=True)
    monkeypatch.setattr(config.settings, "BROKER_CIRCUIT_ENABLED", True)
    # Record an Upstox chain degradation as it would be recorded by the adapter
    # UNDER a slow lane (cadence_datatype → slow_chain). Threshold is 5.
    with lane_broker_profile(LANE_PROFILE_SLOW):
        key = cadence_datatype("chain")
        assert key == "slow_chain"
        for _ in range(5):
            broker_circuit.record_failure("upstox", key)
        slow_order = route_order("option_chain")
    # SLOW lane degrades: Upstox demoted last, nothing dropped (not fail-open).
    assert slow_order[-1] == "upstox"
    assert slow_order.index("fyers") < slow_order.index("upstox")
    assert set(slow_order) == {"upstox", "fyers", "catalog"}

    # FAST lane is UNAFFECTED: its breaker key (fast_chain) is clean.
    with lane_broker_profile(LANE_PROFILE_FAST):
        fast_order = route_order("option_chain")
    assert fast_order[0] == "fyers"
    assert fast_order[1] == "upstox"  # upstox NOT demoted for the fast group


def test_fyers_open_degrades_fast_only(monkeypatch) -> None:
    _enable_routing(monkeypatch, on=True)
    monkeypatch.setattr(config.settings, "BROKER_CIRCUIT_ENABLED", True)
    with lane_broker_profile(LANE_PROFILE_FAST):
        key = cadence_datatype("chain")
        assert key == "fast_chain"
        for _ in range(5):
            broker_circuit.record_failure("fyers", key)
        fast_order = route_order("option_chain")
    # FAST lane degrades: Fyers demoted last, nothing dropped.
    assert fast_order[-1] == "fyers"
    assert set(fast_order) == {"upstox", "fyers", "catalog"}

    # SLOW lane unaffected: upstox-first, fyers not demoted for the slow group.
    with lane_broker_profile(LANE_PROFILE_SLOW):
        slow_order = route_order("option_chain")
    assert slow_order[0] == "upstox"
    assert slow_order[-1] != "fyers"  # fyers not demoted by the fast degradation


# ── ordered_live_adapters helper (the R2 wiring seam) ─────────────────────────
def test_ordered_live_adapters_follows_profile(monkeypatch) -> None:
    _enable_routing(monkeypatch, on=True)
    monkeypatch.setattr(config.settings, "BROKER_CIRCUIT_ENABLED", False)
    ups, fyr = object(), object()
    adapters = {"upstox": ups, "fyers": fyr}
    with lane_broker_profile(LANE_PROFILE_SLOW):
        got = ordered_live_adapters("option_chain", adapters)
    assert [s for s, _ in got] == ["upstox", "fyers"]
    with lane_broker_profile(LANE_PROFILE_FAST):
        got = ordered_live_adapters("option_chain", adapters)
    assert [s for s, _ in got] == ["fyers", "upstox"]


def test_ordered_live_adapters_flag_off_matches_literal(monkeypatch) -> None:
    # Flag off ⇒ byte-identical to the former hardcoded (upstox, fyers) literal
    # in _refresh_cached_index_option_chain, regardless of profile.
    _enable_routing(monkeypatch, on=False)
    monkeypatch.setattr(config.settings, "BROKER_CIRCUIT_ENABLED", False)
    ups, fyr = object(), object()
    adapters = {"upstox": ups, "fyers": fyr}
    for profile in (LANE_PROFILE_SLOW, LANE_PROFILE_FAST, LANE_PROFILE_DEFAULT):
        with lane_broker_profile(profile):
            got = ordered_live_adapters("option_chain", adapters)
        assert got == [("upstox", ups), ("fyers", fyr)]


def test_ordered_live_adapters_drops_missing_and_nonlive(monkeypatch) -> None:
    _enable_routing(monkeypatch, on=False)
    monkeypatch.setattr(config.settings, "BROKER_CIRCUIT_ENABLED", False)
    fyr = object()
    # upstox adapter not connected (None) → dropped; catalog is non-live → dropped.
    got = ordered_live_adapters("option_chain", {"upstox": None, "fyers": fyr})
    assert got == [("fyers", fyr)]


# ── Option-flow watchdog decision core ────────────────────────────────────────
def test_watchdog_alerts_when_stale() -> None:
    from market_data.option_flow_watchdog import STATUS_STALE, evaluate_option_flow

    v = evaluate_option_flow(
        options_subscribed=True, newest_persist_age_s=600.0, stale_seconds=300.0
    )
    assert v["status"] == STATUS_STALE and v["alert"] is True


def test_watchdog_ok_when_fresh() -> None:
    from market_data.option_flow_watchdog import STATUS_OK, evaluate_option_flow

    v = evaluate_option_flow(
        options_subscribed=True, newest_persist_age_s=30.0, stale_seconds=300.0
    )
    assert v["status"] == STATUS_OK and v["alert"] is False


def test_watchdog_idle_when_unsubscribed() -> None:
    from market_data.option_flow_watchdog import STATUS_IDLE, evaluate_option_flow

    v = evaluate_option_flow(
        options_subscribed=False, newest_persist_age_s=None, stale_seconds=300.0
    )
    assert v["status"] == STATUS_IDLE and v["alert"] is False


def test_watchdog_unknown_when_age_missing() -> None:
    # A DB read failure (age None) must NOT masquerade as a stall → no alert.
    from market_data.option_flow_watchdog import STATUS_UNKNOWN, evaluate_option_flow

    v = evaluate_option_flow(
        options_subscribed=True, newest_persist_age_s=None, stale_seconds=300.0
    )
    assert v["status"] == STATUS_UNKNOWN and v["alert"] is False


@pytest.mark.asyncio
async def test_watchdog_disabled_is_noop(monkeypatch) -> None:
    from market_data.option_flow_watchdog import STATUS_DISABLED, run_option_flow_watchdog

    monkeypatch.setattr(config.settings, "OPTION_FLOW_WATCHDOG_ENABLED", False)
    v = await run_option_flow_watchdog()
    assert v == {"status": STATUS_DISABLED, "alert": False}


# ── Commodity MCX exception (exc-a) — must never be rerouted to Fyers ──────────
def test_mcx_resolution_never_consults_route_order() -> None:
    """MCX contract/quote resolution is Upstox-only and must not go through the
    cadence router, so a FAST profile can never reroute it to Fyers. Guard at the
    source level: the module must not import/call the routing seam."""
    src = Path(source_policy.__file__).with_name("upstox_commodity.py").read_text()
    # Ignore comment lines (the exc-a rationale legitimately NAMES route_order);
    # what matters is that the module never CALLS or IMPORTS the routing seam.
    code = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )
    assert "route_order(" not in code
    assert "ordered_live_adapters(" not in code
    assert "_profile_reorder(" not in code
    assert "import source_policy" not in code
    assert "from market_data.source_policy" not in code
