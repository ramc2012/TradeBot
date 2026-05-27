"""Unit tests for analytics/market_profile_ext.py."""

from __future__ import annotations

import pytest

from analytics.market_profile_ext import (
    DayTypeAssessment,
    IBExtension,
    POCMigration,
    assess_day_type,
    ib_extension,
    market_profile_ext_snapshot,
    naked_poc,
    poc_migration,
    single_prints,
    value_area_overlap,
)


# ─── poc_migration ─────────────────────────────────────────────────────────

def test_poc_migration_up_aligned():
    today = {"poc": 102.0, "close": 103.0}
    prior = {"poc": 100.0, "close": 99.0}
    m = poc_migration(today, prior)
    assert m is not None
    assert m.direction == "up"
    assert m.delta == pytest.approx(2.0)
    assert m.pct == pytest.approx(0.02)
    assert m.aligned_with_close is True


def test_poc_migration_flat_when_change_is_tiny():
    today = {"poc": 100.02, "close": 100.0}
    prior = {"poc": 100.0, "close": 100.0}
    m = poc_migration(today, prior)
    assert m is not None
    assert m.direction == "flat"


def test_poc_migration_handles_missing_prior():
    assert poc_migration({"poc": 100}, None) is None


def test_poc_migration_handles_missing_poc():
    assert poc_migration({"close": 100}, {"poc": 100}) is None
    assert poc_migration({"poc": 100}, {"close": 100}) is None


# ─── single_prints / naked_poc ─────────────────────────────────────────────

def test_single_prints_returns_levels_with_one_tpo():
    tpo = {100.0: 1, 100.5: 3, 101.0: 1, 101.5: 5}
    assert single_prints(tpo) == [100.0, 101.0]


def test_single_prints_empty():
    assert single_prints({}) == []


def test_naked_poc_returns_unfilled_within_range():
    naked = naked_poc(
        prior_pocs=[99.0, 95.0, 105.0, 200.0],
        session_high=105.0,
        session_low=95.0,
        today_high=98.0,
        today_low=92.0,
    )
    # Touched by today's range [92, 98]: 95.0 (touched) — should be excluded
    # 99.0 not touched, within 5% of mid (95) → included
    # 105.0 not touched but >5% away → excluded
    # 200.0 way out → excluded
    assert 95.0 not in naked
    assert 99.0 in naked
    assert 200.0 not in naked


# ─── ib_extension ──────────────────────────────────────────────────────────

def test_ib_extension_above():
    profile = {"ibh": 100.0, "ibl": 95.0}
    ext = ib_extension(profile, current_price=107.5)
    assert ext is not None
    assert ext.extended_above is True
    assert ext.extended_below is False
    assert ext.ib_range == 5.0
    assert ext.extension_above_pct == pytest.approx(1.5)  # 7.5 / 5.0


def test_ib_extension_below():
    profile = {"ibh": 100.0, "ibl": 95.0}
    ext = ib_extension(profile, current_price=92.5)
    assert ext.extended_above is False
    assert ext.extended_below is True
    assert ext.extension_below_pct == pytest.approx(0.5)  # 2.5 / 5.0


def test_ib_extension_inside_returns_zeros():
    profile = {"ibh": 100.0, "ibl": 95.0}
    ext = ib_extension(profile, current_price=97.5)
    assert ext.extended_above is False
    assert ext.extended_below is False
    assert ext.extension_above_pct == 0.0
    assert ext.extension_below_pct == 0.0


def test_ib_extension_missing_data_returns_none():
    assert ib_extension({}, 100) is None
    assert ib_extension({"ibh": 100}, 100) is None  # missing ibl


# ─── value_area_overlap ────────────────────────────────────────────────────

def test_value_area_overlap_full():
    today = {"vah": 105.0, "val": 95.0}
    prior = {"vah": 110.0, "val": 90.0}
    # Today VA is fully inside prior VA → overlap = full today range
    assert value_area_overlap(today, prior) == pytest.approx(1.0)


def test_value_area_overlap_none():
    today = {"vah": 120.0, "val": 115.0}
    prior = {"vah": 110.0, "val": 100.0}
    # Today entirely above prior → 0 overlap
    assert value_area_overlap(today, prior) == 0.0


def test_value_area_overlap_partial():
    today = {"vah": 105.0, "val": 95.0}  # range 10
    prior = {"vah": 100.0, "val": 90.0}
    # Overlap: max(95,90)..min(105,100) = 95..100 → 5
    assert value_area_overlap(today, prior) == pytest.approx(0.5)


def test_value_area_overlap_handles_missing():
    assert value_area_overlap({"vah": 100}, {"vah": 100, "val": 95}) is None


# ─── assess_day_type ───────────────────────────────────────────────────────

def test_assess_day_type_trend_up():
    profile = {"vah": 100.0, "val": 95.0}
    ext = IBExtension(extended_above=True, extended_below=False, extension_above_pct=0.7, extension_below_pct=0.0, ib_range=5)
    migration = POCMigration(direction="up", delta=1.5, pct=0.015, aligned_with_close=True)
    result = assess_day_type(profile, current_price=105.0, ib_extension_info=ext, poc_migration_info=migration, cvd_session=10000)
    assert result.classification == "trend_up"
    assert result.confidence > 0.5
    assert any("VAH" in r for r in result.reasons)


def test_assess_day_type_balance_when_inside_va():
    profile = {"vah": 105.0, "val": 95.0}
    result = assess_day_type(profile, current_price=100.0)
    assert result.classification == "balance"


def test_assess_day_type_unknown_when_no_inputs():
    result = assess_day_type({}, current_price=0.0)
    assert result.classification == "unknown"
    assert result.confidence == 0.0


def test_assess_day_type_with_only_va_signal():
    profile = {"vah": 100.0, "val": 95.0}
    result = assess_day_type(profile, current_price=105.0)
    assert result.classification == "trend_up"
    assert result.confidence == 1.0  # Only one input, fully agrees


# ─── market_profile_ext_snapshot ───────────────────────────────────────────

def test_snapshot_includes_all_keys():
    today = {"poc": 102.0, "close": 103.0, "vah": 105.0, "val": 100.0, "ibh": 104.0, "ibl": 101.0, "session_high": 106.0, "session_low": 99.0}
    prior = {"poc": 100.0, "close": 99.0, "vah": 102.0, "val": 97.0}
    snap = market_profile_ext_snapshot(today, prior, current_price=103.5, cvd_session=5000, prior_session_pocs=[100.0, 98.0])
    assert "ib_extension" in snap
    assert "poc_migration" in snap
    assert "value_area_overlap" in snap
    assert "naked_pocs" in snap
    assert "day_type_assessment" in snap
    assert snap["poc_migration"]["direction"] == "up"
    assert snap["day_type_assessment"]["classification"] in {"trend_up", "trend_down", "balance"}


def test_snapshot_with_minimal_inputs():
    snap = market_profile_ext_snapshot({"poc": 100}, None)
    assert snap["poc_migration"] is None
    assert snap["value_area_overlap"] is None
    # ib_extension requires ibh/ibl; should be None
    assert snap["ib_extension"] is None
