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
    LaneSpec,
    build_lane_snapshot,
    build_lane_snapshots,
    get_registry,
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
    assert {"s1_atm_30m_macd", "s2_index_mp_macd", "commodity_mp_orderflow"} <= keys
    # Parked product lane + separate-container runner must be visible too.
    assert "us_macd_refined" in keys
    assert "research_sync" in keys


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
    # Today only S1 has a formal auditor — the visible gap is the point.
    assert covered == {"s1_atm_30m_macd"}
    payload = lane_registry._spec_payload(
        next(spec for spec in get_registry() if spec.key == "s1_atm_30m_macd"),
        audit_keys,
    )
    assert payload["audit_coverage"] is True
    other = lane_registry._spec_payload(
        next(spec for spec in get_registry() if spec.key == "macd_refined"),
        audit_keys,
    )
    assert other["audit_coverage"] is False


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
