"""Task C (2026-07-18): the ONE lane registry.

Guards:
  * every supervisor runner key from the REAL _default_runners() is present in
    the registry (a future runner addition fails here until registered) and the
    registry claims no phantom runner keys;
  * the snapshot assembler NEVER raises — a failing status source degrades to
    status="unknown" with the error attached;
  * /api/system/lanes serves every registry entry plus an honest summary;
  * audit_coverage is True ONLY for lanes registered in audits.lanes (s1).
"""
from __future__ import annotations

import pytest

from core import lane_registry
from core.lane_registry import (
    EXPECTED_LANE_TOTAL,
    LaneSpec,
    build_lane_snapshot,
    build_lane_snapshots,
    get_registry,
    registry_counts,
    summarize,
    supervisor_runner_keys,
)


# ---------------------------------------------------------------------------
# Registry completeness against the real supervisor
# ---------------------------------------------------------------------------

def test_registry_covers_every_supervisor_runner_key() -> None:
    """Iterate the REAL supervisor _default_runners; each key must be claimed
    by exactly the registry. Adding a runner without registering it fails."""
    from core.market_hours_paper_supervisor import market_hours_paper_supervisor

    configured = {runner.key for runner in market_hours_paper_supervisor._default_runners()}
    registered = supervisor_runner_keys()

    missing = configured - registered
    assert not missing, (
        f"Supervisor runners missing from core/lane_registry.py: {sorted(missing)} "
        "— register the new runner as a LaneSpec (Task C contract)."
    )
    phantom = registered - configured
    assert not phantom, (
        f"Registry claims supervisor runners that do not exist: {sorted(phantom)}"
    )


def test_registry_keys_unique() -> None:
    keys = [spec.key for spec in get_registry()]
    assert len(keys) == len(set(keys)), "duplicate lane keys in registry"


def test_registry_supervisor_entries_have_runner_keys() -> None:
    for spec in get_registry():
        if spec.status_source == "supervisor":
            assert spec.runner_keys, f"{spec.key}: supervisor lane without runner_keys"


def test_registry_includes_own_loop_engines_and_parked_lanes() -> None:
    keys = {spec.key for spec in get_registry()}
    # Own-loop strategy engines started in main.py lifespan.
    assert {"s1_atm_30m_macd", "commodity_mp_orderflow"} <= keys
    # Separate-container runner must be visible too.
    assert "research_sync" in keys
    # RETIRED 2026-07-20 (owner: only MACD and MACD-refined survive). Both were
    # already exec=parked/enabled=False; historical data is kept.
    assert "s2_index_mp_macd" not in keys
    assert "us_macd_refined" not in keys
    # The two MACD lanes that SURVIVE, plus the machinery that is NOT a variant.
    assert {"s1_atm_30m_macd", "macd_refined"} <= keys


# ---------------------------------------------------------------------------
# Audit-coverage honesty
# ---------------------------------------------------------------------------

def test_audit_coverage_true_only_for_audited_lanes() -> None:
    from audits.lanes import REGISTRY as AUDIT_REGISTRY

    audit_keys = set(AUDIT_REGISTRY)
    covered = {
        spec.key
        for spec in get_registry()
        if spec.audit_lane_key and spec.audit_lane_key in audit_keys
    }
    # 2026-07-18: the execution-capable lanes now carry registered auditors
    # (backend/audits/lanes/). Parked lanes (chain_candle_builder; the two MACD
    # variants were RETIRED 2026-07-20) and pure daemons/monitors stay uncovered — that
    # visible gap is the point.
    assert covered == {
        "s1_atm_30m_macd",
        "directional_options",
        "macd_refined",
        "auction_intelligence",
        "institutional_convergence",
        "commodity_mp_orderflow",
    }
    payload = lane_registry._spec_payload(
        next(spec for spec in get_registry() if spec.key == "s1_atm_30m_macd"),
        audit_keys,
    )
    assert payload["audit_coverage"] is True
    # macd_refined now carries a registered auditor -> covered.
    covered_spec = lane_registry._spec_payload(
        next(spec for spec in get_registry() if spec.key == "macd_refined"),
        audit_keys,
    )
    assert covered_spec["audit_coverage"] is True
    # A parked lane with no auditor stays uncovered — the visible gap.
    uncovered = lane_registry._spec_payload(
        next(spec for spec in get_registry() if spec.key == "chain_candle_builder"),
        audit_keys,
    )
    assert uncovered["audit_coverage"] is False


# ---------------------------------------------------------------------------
# Snapshot assembler resilience
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_survives_raising_source(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(spec: LaneSpec) -> dict:
        raise RuntimeError("status provider exploded")

    monkeypatch.setitem(lane_registry._SOURCES, "supervisor", _boom)
    spec = next(s for s in get_registry() if s.status_source == "supervisor")
    snap = await build_lane_snapshot(spec)
    assert snap["status"] == "unknown"
    assert "status provider exploded" in snap["error"]
    assert snap["key"] == spec.key  # declarative payload still intact


@pytest.mark.asyncio
async def test_snapshot_unknown_source_never_raises() -> None:
    spec = LaneSpec(
        key="bogus",
        label="Bogus",
        kind="monitor",
        execution_mode="none",
        status_source="not_a_source",
    )
    snap = await build_lane_snapshot(spec)
    assert snap["status"] == "unknown"
    assert "unknown status source" in snap["error"]


@pytest.mark.asyncio
async def test_build_lane_snapshots_full_pass_offline() -> None:
    """With PG/Redis/broker unreachable every lane still yields a snapshot
    (worst case status=unknown) — the endpoint can never 500 from a lane."""
    snapshots = await build_lane_snapshots()
    assert len(snapshots) == len(get_registry())
    for snap in snapshots:
        assert "status" in snap and snap["status"], snap.get("key")
        assert "audit_coverage" in snap


def test_derive_status_precedence() -> None:
    derive = lane_registry._derive_status
    assert derive(enabled=True, running=True, stale=False, last_error=None, execution_mode="parked") == "parked"
    assert derive(enabled=True, running=True, stale=False, last_error="x", execution_mode="paper") == "error"
    assert derive(enabled=True, running=False, stale=True, last_error=None, execution_mode="paper") == "stale"
    assert derive(enabled=True, running=True, stale=False, last_error=None, execution_mode="paper") == "running"
    assert derive(enabled=False, running=False, stale=None, last_error=None, execution_mode="paper") == "disabled"
    assert derive(enabled=None, running=None, stale=None, last_error=None, execution_mode="none") == "configured"
    assert derive(enabled=True, running=False, stale=False, last_error=None, execution_mode="paper") == "ready"


# ---------------------------------------------------------------------------
# Endpoint shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lanes_endpoint_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.routers import system

    async def _fake_snapshots() -> list[dict]:
        return [
            {"key": "a", "kind": "monitor", "status": "running", "execution_mode": "none", "audit_coverage": False},
            {"key": "b", "kind": "strategy-engine", "status": "unknown", "execution_mode": "paper", "audit_coverage": True},
        ]

    monkeypatch.setattr(lane_registry, "build_lane_snapshots", _fake_snapshots)
    payload = await system.lane_snapshots()
    assert set(payload) == {"lanes", "summary"}
    assert [lane["key"] for lane in payload["lanes"]] == ["a", "b"]
    summary = payload["summary"]
    assert summary["total"] == 2
    assert summary["by_kind"] == {"monitor": 1, "strategy-engine": 1}
    assert summary["by_status"] == {"running": 1, "unknown": 1}
    assert summary["audit_covered"] == 1
    assert summary["audit_uncovered"] == 1


def test_summarize_counts() -> None:
    snaps = [
        {"kind": "monitor", "status": "running", "execution_mode": "none", "audit_coverage": False},
        {"kind": "monitor", "status": "disabled", "execution_mode": "none", "audit_coverage": False},
        {"kind": "scheduler-runner", "status": "running", "execution_mode": "paper", "audit_coverage": True},
    ]
    summary = summarize(snaps)
    assert summary["total"] == 3
    assert summary["by_kind"] == {"monitor": 2, "scheduler-runner": 1}
    assert summary["by_status"] == {"running": 2, "disabled": 1}
    assert summary["by_execution_mode"] == {"none": 2, "paper": 1}
    assert summary["audit_covered"] == 1


# ---------------------------------------------------------------------------
# Count reconciliation (assembler 32 vs stale "31" note) — the ONE total
# ---------------------------------------------------------------------------

def test_registry_count_is_internally_consistent() -> None:
    counts = registry_counts()
    specs = get_registry()
    # Buckets partition the registry → they sum to the total exactly (proves no
    # entry counted twice).
    assert counts["supervisor"] + counts["own_loop"] + counts["product_daemon"] == counts["total"]
    assert counts["total"] == len(specs) == EXPECTED_LANE_TOTAL == 33
    # No lane key is ALSO another lane's runner_key (no two-bucket duplicate).
    assert counts["key_runnerkey_collisions"] == 0


@pytest.mark.asyncio
async def test_registry_summary_total_matches_expected() -> None:
    snaps = await build_lane_snapshots()
    assert summarize(snaps)["total"] == EXPECTED_LANE_TOTAL


# ---------------------------------------------------------------------------
# Metadata correctness (Task 2)
# ---------------------------------------------------------------------------

def test_s1_broker_profile_matches_slow_seam() -> None:
    s1 = next(s for s in get_registry() if s.key == "s1_atm_30m_macd")
    # Own-loop seam wraps run_once in lane_broker_profile(LANE_PROFILE_SLOW).
    assert s1.broker_profile == "slow"


def test_no_advertised_status_endpoint_is_a_known_404() -> None:
    # The three paths the second review flagged as 404 must never appear.
    dead = {"/api/strategy/status", "/api/cbe/summary", "/api/institutional-convergence/summary"}
    advertised = {s.status_endpoint for s in get_registry() if s.status_endpoint}
    assert advertised.isdisjoint(dead), advertised & dead


def test_corrected_status_endpoints_point_at_served_paths() -> None:
    by_key = {s.key: s for s in get_registry()}
    assert by_key["s1_atm_30m_macd"].status_endpoint == "/api/trading/strategy-agent/status"
    # s2_index_mp_macd RETIRED 2026-07-20 — the LANE is gone, but the endpoint it
    # shared with S1 is NOT removed (S1 still serves it, asserted above).
    assert by_key["cbe_scanner"].status_endpoint == "/api/cbe/paper-summary"
    assert by_key["cbe_marks"].status_endpoint == "/api/cbe/paper-summary"
    assert by_key["institutional_convergence"].status_endpoint == "/api/institutional-convergence/status"
    assert (
        by_key["institutional_convergence_commodity"].status_endpoint
        == "/api/institutional-convergence/commodity/status"
    )


# ---------------------------------------------------------------------------
# Honest status: config-on-but-unprobed = "enabled", not "ready"
# ---------------------------------------------------------------------------

def test_derive_status_unprobed_reports_enabled_not_ready() -> None:
    derive = lane_registry._derive_status
    assert derive(enabled=True, running=None, stale=None, last_error=None, execution_mode="none", probed=False) == "enabled"
    assert derive(enabled=True, running=False, stale=False, last_error=None, execution_mode="paper", probed=True) == "ready"
    assert derive(enabled=False, running=None, stale=None, last_error=None, execution_mode="none", probed=False) == "disabled"


@pytest.mark.asyncio
async def test_flag_only_and_always_on_never_claim_ready() -> None:
    snapshots = await build_lane_snapshots()
    by_key = {s["key"]: s for s in snapshots}
    # greeks_enrichment is flag_only; held_position_marks_refresh is always_on.
    for key in ("greeks_enrichment", "held_position_marks_refresh"):
        assert by_key[key]["status"] in {"enabled", "disabled"}, (key, by_key[key]["status"])
        assert by_key[key]["status"] != "ready"


# ---------------------------------------------------------------------------
# Foreign-plane staleness propagation (Task 1)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_foreign_plane_snapshot_stale_downgrades_status(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.market_hours_paper_supervisor import market_hours_paper_supervisor

    # A dead strategy container still publishes running=True from a frozen
    # snapshot; snapshot_stale=True must fold into effective staleness.
    def _fake_status(key: str) -> dict:
        return {
            "key": key,
            "enabled": True,
            "running": True,
            "stale": False,  # the frozen snapshot's own field lies
            "last_error": None,
            "loop_active": True,
            "plane": "strategies",
            "foreign": True,
            "snapshot_stale": True,
            "snapshot_age_seconds": 900.0,
        }

    monkeypatch.setattr(market_hours_paper_supervisor, "get_runner_status", _fake_status)
    spec = next(s for s in get_registry() if s.status_source == "supervisor")
    snap = await build_lane_snapshot(spec)
    assert snap["status"] == "stale", snap
    assert snap["snapshot_stale"] is True
    assert snap["snapshot_age_seconds"] == 900.0
    assert snap["plane"] == "strategies"
    assert snap["foreign_plane"] is True


@pytest.mark.asyncio
async def test_local_runner_fresh_snapshot_stays_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.market_hours_paper_supervisor import market_hours_paper_supervisor

    def _fake_status(key: str) -> dict:
        return {
            "key": key,
            "enabled": True,
            "running": False,
            "stale": False,
            "last_error": None,
            "loop_active": True,
            # local runner: no snapshot_stale key
        }

    monkeypatch.setattr(market_hours_paper_supervisor, "get_runner_status", _fake_status)
    spec = next(s for s in get_registry() if s.status_source == "supervisor" and s.execution_mode != "parked")
    snap = await build_lane_snapshot(spec)
    assert snap["status"] == "ready"
    assert snap["snapshot_stale"] is None


# ---------------------------------------------------------------------------
# Own-loop liveness (Task 1) + parked-lane state borrowing (Task 2c)
# ---------------------------------------------------------------------------

class _FakeAgent:
    def __init__(self, status: dict) -> None:
        self._status = status

    def get_status(self, *, refresh: bool = True) -> dict:
        return self._status


def test_agent_dead_loop_reads_stale() -> None:
    agent = _FakeAgent(
        {
            "enabled": True,
            "running": True,  # persisted state says running
            "loop_active": False,  # but the own-loop is dead
            "last_error": None,
            "last_run_at": "2026-07-18T10:00:00+05:30",
            "strategies": [{"key": "macd_strategy", "summary": {"open_positions": 0}}],
        }
    )
    state = lane_registry._agent_state(agent, "macd_strategy")
    assert state["stale"] is True
    assert state["loop_active"] is False


def test_agent_live_loop_not_stale() -> None:
    agent = _FakeAgent(
        {
            "enabled": True,
            "running": False,
            "loop_active": True,
            "last_error": None,
            "strategies": [{"key": "macd_strategy", "summary": {"open_positions": 1}}],
        }
    )
    state = lane_registry._agent_state(agent, "macd_strategy")
    assert state["stale"] is None


def test_parked_strategy_never_borrows_live_agent_state() -> None:
    # The live agent is enabled/running with a fresh last_run_at and a message,
    # but the requested (deleted) strategy is ABSENT from strategies[].
    agent = _FakeAgent(
        {
            "enabled": True,
            "running": True,
            "loop_active": True,
            "last_error": None,
            "last_run_at": "2026-07-18T10:00:00+05:30",
            "last_message": "S1 scanned 217 names",
            "strategies": [{"key": "macd_strategy", "summary": {"open_positions": 3}}],
        }
    )
    state = lane_registry._agent_state(agent, "index_mp_strategy")  # deleted s2
    assert state["running"] is False
    assert state["enabled"] is False
    assert state["last_success_at"] is None
    assert state["book"] is None
    assert "borrowed" in state["last_message"].lower() or "parked" in state["last_message"].lower()


# ---------------------------------------------------------------------------
# Risk-breach VISIBILITY (Task 3) — flag only, never enforcement, never raises
# ---------------------------------------------------------------------------

def test_risk_breach_from_book_flags_negative_capital_and_drawdown() -> None:
    breach, reason = lane_registry._risk_breach_from_book(
        {"initial_capital": 1_000_000, "available_capital": -1_363_960, "max_drawdown": 0.5}
    )
    assert breach is True
    assert "available_capital negative" in reason
    assert "drawdown" in reason


def test_risk_breach_healthy_book_is_false_not_none() -> None:
    breach, reason = lane_registry._risk_breach_from_book(
        {"initial_capital": 5_000_000, "available_capital": 4_600_000, "max_drawdown": 0.05}
    )
    assert breach is False
    assert reason is None


def test_risk_breach_unknown_when_no_book() -> None:
    assert lane_registry._risk_breach_from_book(None) == (None, None)
    assert lane_registry._risk_breach_from_book({}) == (None, None)
    # Book dict with only unrelated keys → still unknown.
    assert lane_registry._risk_breach_from_book({"foo": 1}) == (None, None)


def test_risk_breach_drawdown_threshold_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    book = {"available_capital": 100.0, "max_drawdown": 0.2}
    # Default 0.30 → 20% drawdown does not breach.
    assert lane_registry._risk_breach_from_book(book)[0] is False
    # Tighten the surfacing threshold (config-driven; default patched here since
    # the Settings model is frozen) → the same book now breaches.
    monkeypatch.setattr(lane_registry, "_DEFAULT_DRAWDOWN_SURFACING_PCT", 0.15)
    assert lane_registry._risk_breach_from_book(book)[0] is True


@pytest.mark.asyncio
async def test_s1_snapshot_surfaces_breach_visibility_only(monkeypatch: pytest.MonkeyPatch) -> None:
    # S1's served book is deep-negative; the snapshot must SURFACE risk_breach
    # without any status becoming a block (visibility only).
    async def _fake_s1_state(spec: LaneSpec) -> dict:
        return {
            "enabled": True,
            "running": True,
            "stale": None,
            "last_error": None,
            "loop_active": True,
            "book": {"initial_capital": 1_000_000, "available_capital": -1_363_960, "max_drawdown": 0.5},
            "probed": True,
        }

    monkeypatch.setitem(lane_registry._SOURCES, "nse_agent", _fake_s1_state)
    spec = next(s for s in get_registry() if s.key == "s1_atm_30m_macd")
    snap = await build_lane_snapshot(spec)
    assert snap["risk_breach"] is True
    assert snap["risk_breach_reason"]
    # Visibility only: status is the ordinary running state, NOT a block.
    assert snap["status"] == "running"


@pytest.mark.asyncio
async def test_risk_book_probe_used_for_supervisor_paper_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    # macd_refined is supervisor-scheduled (no book in runner status); its
    # declared risk_book_source probe supplies the book best-effort.
    async def _fake_probe() -> dict:
        return {"initial_capital": 5_000_000, "available_capital": -2_714_266, "max_drawdown": 0.375}

    monkeypatch.setitem(lane_registry._BOOK_PROBES, "macd_refined_paper", _fake_probe)

    async def _fake_supervisor(spec: LaneSpec) -> dict:
        return {"enabled": True, "running": True, "stale": False, "last_error": None, "probed": True}

    monkeypatch.setitem(lane_registry._SOURCES, "supervisor", _fake_supervisor)
    spec = next(s for s in get_registry() if s.key == "macd_refined")
    snap = await build_lane_snapshot(spec)
    assert snap["risk_breach"] is True
    assert "available_capital negative" in snap["risk_breach_reason"]


@pytest.mark.asyncio
async def test_risk_probe_failure_leaves_breach_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom() -> dict:
        raise RuntimeError("service down")

    monkeypatch.setitem(lane_registry._BOOK_PROBES, "macd_refined_paper", _boom)

    async def _fake_supervisor(spec: LaneSpec) -> dict:
        return {"enabled": True, "running": True, "stale": False, "last_error": None, "probed": True}

    monkeypatch.setitem(lane_registry._SOURCES, "supervisor", _fake_supervisor)
    spec = next(s for s in get_registry() if s.key == "macd_refined")
    snap = await build_lane_snapshot(spec)
    # Best-effort: a dead probe never raises and never fabricates → unknown.
    assert snap["risk_breach"] is None
    assert snap["status"] == "running"


@pytest.mark.asyncio
async def test_full_snapshot_pass_includes_risk_fields() -> None:
    snapshots = await build_lane_snapshots()
    for snap in snapshots:
        assert "risk_breach" in snap  # True / False / None on every lane
        assert "risk_breach_reason" in snap
