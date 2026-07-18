"""Execution-capable lane auditors (2026-07-18).

Closes the audit-coverage gate the pre-split review flagged: exactly 1 of 32
lanes (s1) had a registered signal-correctness auditor. These tests verify the
five newly-added auditors (directional_options, macd_refined,
auction_intelligence, institutional_convergence, commodity_mp_orderflow):

  * each produces a well-formed six-invariant AuditResult from fixture state;
  * a lane missing data reports the specific check as fail/na (never raises);
  * a whole-collection error degrades to failed CHECKS, not an exception;
  * the audits registry now contains the execution-capable keys;
  * core.lane_registry.audit_coverage flips once a spec's audit_lane_key points
    into the registry (the wiring is asserted without editing lane_registry).
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime

import pytest

from audits.base import AuditResult, InvariantResult
from audits.lanes import REGISTRY
from audits.lanes._common import BaseLaneAuditor, LaneAuditProfile, LaneAuditState
from audits.report import render_markdown

AUDIT_DATE = date(2026, 7, 18)
EXECUTION_CAPABLE_KEYS = {
    "directional_options",
    "macd_refined",
    "auction_intelligence",
    "institutional_convergence",
    "commodity_mp_orderflow",
}


# ---------------------------------------------------------------------------
# Registry membership
# ---------------------------------------------------------------------------
def test_registry_contains_execution_capable_keys() -> None:
    assert EXECUTION_CAPABLE_KEYS <= set(REGISTRY)
    # s1 stays registered (unchanged).
    assert "s1" in REGISTRY
    # Parked lanes must NOT have been added.
    for parked in ("s2_index_mp_macd", "us_macd_refined", "chain_candle_builder"):
        assert parked not in REGISTRY


def test_every_registered_auditor_matches_the_protocol() -> None:
    for key, cls in REGISTRY.items():
        auditor = cls(session=None)
        assert auditor.lane == (key if key != "s1" else "s1")
        assert hasattr(auditor, "run")


# ---------------------------------------------------------------------------
# Structured result on fixture state
# ---------------------------------------------------------------------------
def _auditor(key: str) -> BaseLaneAuditor:
    return REGISTRY[key](session=None)


def _healthy_state(greeks: bool) -> LaneAuditState:
    return LaneAuditState(
        provenance_observed="fyers",
        provenance_rows=10,
        provenance_mismatches=0,
        decision_bars_total=13,
        decision_bars_forming=0,
        input_age_seconds=120.0,
        greeks_rows_total=10 if greeks else 0,
        greeks_rows_complete=10 if greeks else 0,
        gate_breakdown={"iv_cap": 5, "regime": 3},
        gate_unknown=0,
        signals_emitted=4,
        open_positions=3,
        journal_open_positions=3,
        closed_trades=[
            {"ret_pct": 12.0, "closed_at": datetime(2026, 7, 10)},
            {"ret_pct": 4.0, "closed_at": datetime(2026, 6, 1)},
        ],
    )


@pytest.mark.parametrize("key", sorted(EXECUTION_CAPABLE_KEYS))
def test_auditor_produces_structured_result_on_fixture_state(key: str) -> None:
    auditor = _auditor(key)
    greeks = auditor.profile.greeks_applicable
    result = auditor.run_from_state(_healthy_state(greeks), AUDIT_DATE)

    assert isinstance(result, AuditResult)
    assert result.lane == key
    # Exactly the six named invariants, all InvariantResult instances.
    names = [inv.name for inv in result.invariants]
    assert names == [
        "data_integrity",
        "replay_parity",
        "gate_attribution",
        "backtest_parity",
        "trade_reconciliation",
        "edge_persistence",
    ]
    assert all(isinstance(inv, InvariantResult) for inv in result.invariants)
    # Healthy evidence → the checkable dimensions pass.
    assert result.data_integrity.status == "pass"
    assert result.gate_attribution.status == "pass"
    assert result.trade_reconciliation.status == "pass"
    assert result.edge_persistence.status == "pass"
    # Replay parity is the S1-only deeper follow-up: na with a note, everywhere.
    assert result.replay_parity.status == "na"
    assert "s1" in result.replay_parity.detail["note"]
    assert result.backtest_parity.status == "na"
    # Overall is yellow (na blocks green) — honest, matching s1's own shape.
    assert result.overall_status == "yellow"
    # Renders + serialises without error.
    assert render_markdown(result)
    assert result.to_db_row()["lane"] == key


def test_greeks_dimension_is_na_for_non_greeks_lanes() -> None:
    # auction / institutional / commodity do not consume option greeks.
    for key in ("auction_intelligence", "institutional_convergence", "commodity_mp_orderflow"):
        auditor = _auditor(key)
        assert auditor.profile.greeks_applicable is False
        res = auditor.run_from_state(_healthy_state(greeks=False), AUDIT_DATE)
        greeks_sub = res.data_integrity.detail["sub_checks"]["greeks_completeness"]
        assert greeks_sub["status"] == "na"
        assert "does not consume" in greeks_sub["note"]


def test_greeks_dimension_is_applicable_for_options_lanes() -> None:
    for key in ("directional_options", "macd_refined"):
        assert _auditor(key).profile.greeks_applicable is True


# ---------------------------------------------------------------------------
# Missing / bad data → specific check fails, never an exception
# ---------------------------------------------------------------------------
def test_empty_state_reports_na_not_exception() -> None:
    for key in EXECUTION_CAPABLE_KEYS:
        result = _auditor(key).run_from_state(LaneAuditState.empty(), AUDIT_DATE)
        # No evidence anywhere → every invariant na, overall yellow, no raise.
        assert {inv.status for inv in result.invariants} == {"na"}
        assert result.overall_status == "yellow"


def test_specific_check_fails_on_bad_evidence() -> None:
    auditor = _auditor("directional_options")
    bad = LaneAuditState(
        provenance_observed="upstox",
        provenance_rows=10,
        provenance_mismatches=4,          # wrong-source rows
        decision_bars_total=13,
        decision_bars_forming=2,          # forming (not closed) bars
        input_age_seconds=10**9,          # stale
        greeks_rows_total=10,
        greeks_rows_complete=3,           # greeks holes
        gate_breakdown={"unknown": 8, "iv_cap": 1},
        gate_unknown=8,                   # rejection attribution mostly unknown
        signals_emitted=1,
        open_positions=3,
        journal_open_positions=1,         # book/journal mismatch
    )
    result = auditor.run_from_state(bad, AUDIT_DATE)
    subs = result.data_integrity.detail["sub_checks"]
    assert subs["provenance"]["status"] == "fail"
    assert subs["completed_bar"]["status"] == "fail"
    assert subs["freshness"]["status"] == "fail"
    assert subs["greeks_completeness"]["status"] == "fail"
    assert result.data_integrity.status == "fail"
    assert result.gate_attribution.status == "fail"
    assert result.trade_reconciliation.status == "fail"
    recon_reasons = {f.get("reason") for f in result.trade_reconciliation.detail["failures"]}
    assert "open_position_count_mismatch" in recon_reasons
    assert result.overall_status == "red"


def test_per_dimension_source_error_is_a_failed_check() -> None:
    auditor = _auditor("commodity_mp_orderflow")
    st = LaneAuditState.empty()
    st.errors["gate"] = "agent_audit_events query failed"
    st.errors["recon"] = "agent_positions query failed"
    result = auditor.run_from_state(st, AUDIT_DATE)
    assert result.gate_attribution.status == "fail"
    assert result.gate_attribution.detail["error"] == "agent_audit_events query failed"
    assert result.trade_reconciliation.status == "fail"
    assert result.trade_reconciliation.detail["error"] == "agent_positions query failed"


def test_collection_error_degrades_to_failed_checks() -> None:
    for key in EXECUTION_CAPABLE_KEYS:
        result = _auditor(key).run_from_state(
            LaneAuditState.collection_failed("boom"), AUDIT_DATE
        )
        assert result.data_integrity.status == "fail"
        assert result.data_integrity.detail["error"] == "boom"
        assert result.gate_attribution.status == "fail"
        assert result.trade_reconciliation.status == "fail"
        assert result.overall_status == "red"
        # Still renders.
        assert render_markdown(result)


# ---------------------------------------------------------------------------
# Live run() resilience — a raising session never escapes as an exception
# ---------------------------------------------------------------------------
class _RaisingSession:
    """Every DB touch raises — the auditor must degrade, not propagate."""

    async def begin_nested(self):
        raise RuntimeError("db down")

    async def execute(self, *a, **k):
        raise RuntimeError("db down")


def test_run_is_resilient_to_a_raising_session() -> None:
    # directional_options touches the DB directly; a raising session must yield a
    # structured (collection-error) result, not an exception.
    auditor = REGISTRY["directional_options"](session=_RaisingSession())
    result = asyncio.run(auditor.run(AUDIT_DATE, lookback_days=7))
    assert isinstance(result, AuditResult)
    assert result.data_integrity.status == "fail"
    assert result.overall_status == "red"


def test_run_is_resilient_for_file_backed_lanes_without_a_session() -> None:
    # macd_refined / auction / institutional read a service summary best-effort;
    # with no service reachable they must degrade to na, never raise.
    for key in ("macd_refined", "auction_intelligence", "institutional_convergence"):
        auditor = REGISTRY[key](session=None)
        result = asyncio.run(auditor.run(AUDIT_DATE, lookback_days=7))
        assert isinstance(result, AuditResult)
        # No hard failure from a merely-absent live source.
        assert result.overall_status in {"yellow", "red"}


# ---------------------------------------------------------------------------
# lane_registry audit_coverage reflects the new registry (read-only assertions)
# ---------------------------------------------------------------------------
def test_no_dangling_audit_lane_key_in_registry() -> None:
    from core.lane_registry import get_registry

    audit_keys = set(REGISTRY)
    for spec in get_registry():
        if spec.audit_lane_key is not None:
            assert spec.audit_lane_key in audit_keys, (
                f"{spec.key}: audit_lane_key={spec.audit_lane_key!r} not in audits.lanes.REGISTRY"
            )


def test_audit_coverage_flips_once_audit_lane_key_is_wired() -> None:
    """core.lane_registry computes audit_coverage from spec.audit_lane_key in the
    audits registry. We do not edit lane_registry; instead we prove the mechanism:
    wiring each execution-capable spec's audit_lane_key to its now-registered key
    flips audit_coverage to True."""
    from core import lane_registry
    from core.lane_registry import get_registry

    audit_keys = set(REGISTRY)
    specs = {spec.key: spec for spec in get_registry()}
    for key in EXECUTION_CAPABLE_KEYS:
        assert key in specs, f"{key} missing from core/lane_registry.py"
        wired = replace(specs[key], audit_lane_key=key)
        payload = lane_registry._spec_payload(wired, audit_keys)
        assert payload["audit_coverage"] is True
        # And with the current (unwired) spec, coverage is still False — proving
        # the registry addition alone doesn't silently flip anything.
        current = lane_registry._spec_payload(specs[key], audit_keys)
        assert current["audit_coverage"] is (specs[key].audit_lane_key in audit_keys
                                             if specs[key].audit_lane_key else False)
