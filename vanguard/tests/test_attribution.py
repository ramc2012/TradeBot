"""Offline tests for M10's pure logic: hit rate/avg R, decile monotonicity,
per-component IC, and per-symbol attribution. load_closed_outcomes/persist_run
are thin SQL wrappers verified live instead (same pattern as M8's replay())."""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from journal.attribution import (  # noqa: E402
    MIN_DECILE_SAMPLE,
    MIN_IC_SAMPLE,
    compute_component_ic,
    compute_decile_report,
    compute_hit_rate_avg_r,
    per_symbol_sector_attribution,
)


def _row(symbol="TCS", conviction=90.0, r_multiple=0.5, pnl_rupees=1000.0,
         flow=70.0, sector_rs=75.0, timing=80.0, regime=75.0, leadlag=50.0):
    return {
        "symbol": symbol, "direction": "bullish", "conviction": conviction,
        "evidence": {"component_scores": {
            "flow": flow, "sector_rs": sector_rs, "timing": timing,
            "regime": regime, "leadlag": leadlag,
        }},
        "exit_reason": "target1", "pnl_rupees": pnl_rupees, "r_multiple": r_multiple,
        "exit_ts": datetime(2026, 8, 26, 10, 0),
    }


def test_hit_rate_avg_r_on_empty_input_is_none_not_a_fabricated_zero():
    assert compute_hit_rate_avg_r([]) == (None, None)


def test_hit_rate_avg_r_ignores_rows_with_no_r_multiple_yet():
    rows = [_row(r_multiple=1.0), {**_row(), "r_multiple": None}]
    hit_rate, avg_r = compute_hit_rate_avg_r(rows)
    assert hit_rate == 1.0
    assert avg_r == 1.0


def test_hit_rate_avg_r_hand_computed():
    rows = [_row(r_multiple=1.0), _row(r_multiple=-0.5), _row(r_multiple=0.5)]
    hit_rate, avg_r = compute_hit_rate_avg_r(rows)
    assert hit_rate == round(2 / 3, 4)
    assert avg_r == round((1.0 - 0.5 + 0.5) / 3, 4)


def test_decile_report_on_empty_input_reports_no_verdict_and_flags_sample_size():
    report, monotonic, adequate = compute_decile_report([])
    assert report == []
    assert monotonic is None
    assert adequate is False


def test_decile_report_flags_inadequate_sample_below_min_decile_sample():
    rows = [_row(conviction=50.0 + i, r_multiple=0.1) for i in range(MIN_DECILE_SAMPLE - 1)]
    _, _, adequate = compute_decile_report(rows)
    assert adequate is False


def test_decile_report_flags_adequate_sample_at_min_decile_sample():
    rows = [_row(conviction=50.0 + i, r_multiple=0.1) for i in range(MIN_DECILE_SAMPLE)]
    _, _, adequate = compute_decile_report(rows)
    assert adequate is True


def test_decile_report_detects_monotonic_and_non_monotonic_cases():
    monotonic_rows = [_row(conviction=10.0 * (i + 1), r_multiple=float(i)) for i in range(10)]
    _, monotonic, _ = compute_decile_report(monotonic_rows)
    assert monotonic is True

    non_monotonic_rows = [_row(conviction=10.0 * (i + 1), r_multiple=-float(i)) for i in range(10)]
    _, non_monotonic, _ = compute_decile_report(non_monotonic_rows)
    assert non_monotonic is False


def test_component_ic_is_none_below_min_ic_sample_not_a_noisy_coefficient():
    rows = [_row(flow=50.0 + i, r_multiple=float(i)) for i in range(MIN_IC_SAMPLE - 1)]
    report = compute_component_ic(rows)
    assert report["flow"]["ic"] is None
    assert report["flow"]["n"] == MIN_IC_SAMPLE - 1
    assert "MIN_IC_SAMPLE" in report["flow"]["reason"]


def test_component_ic_detects_a_perfect_positive_correlation():
    rows = [_row(flow=float(i), r_multiple=float(i)) for i in range(MIN_IC_SAMPLE)]
    report = compute_component_ic(rows)
    assert report["flow"]["ic"] == 1.0
    assert report["flow"]["n"] == MIN_IC_SAMPLE


def test_component_ic_detects_a_perfect_negative_correlation():
    rows = [_row(flow=float(i), r_multiple=-float(i)) for i in range(MIN_IC_SAMPLE)]
    report = compute_component_ic(rows)
    assert report["flow"]["ic"] == -1.0


def test_component_ic_handles_zero_variance_without_a_divide_by_zero_crash():
    rows = [_row(flow=50.0, r_multiple=float(i)) for i in range(MIN_IC_SAMPLE)]
    report = compute_component_ic(rows)
    assert report["flow"]["ic"] is None
    assert report["flow"]["reason"] == "zero variance in sample"


def test_component_ic_excludes_rows_missing_that_components_score():
    rows = [_row(flow=float(i), r_multiple=float(i)) for i in range(MIN_IC_SAMPLE)]
    for r in rows[:5]:
        del r["evidence"]["component_scores"]["flow"]
    report = compute_component_ic(rows)
    assert report["flow"]["n"] == MIN_IC_SAMPLE - 5


def test_component_ic_reports_all_five_components_independently():
    rows = [_row(flow=float(i), sector_rs=float(i), timing=50.0, regime=50.0, leadlag=50.0,
                 r_multiple=float(i)) for i in range(MIN_IC_SAMPLE)]
    report = compute_component_ic(rows)
    assert set(report.keys()) == {"flow", "sector_rs", "timing", "regime", "leadlag"}
    assert report["flow"]["ic"] == 1.0
    assert report["timing"]["reason"] == "zero variance in sample"


def test_per_symbol_attribution_sums_pnl_and_counts_correctly():
    rows = [
        _row(symbol="TCS", pnl_rupees=1000.0), _row(symbol="TCS", pnl_rupees=-200.0),
        _row(symbol="INFY", pnl_rupees=500.0),
    ]
    attribution = per_symbol_sector_attribution(rows)
    assert attribution["TCS"] == {"n": 2, "total_pnl_rupees": 800.0}
    assert attribution["INFY"] == {"n": 1, "total_pnl_rupees": 500.0}


def test_per_symbol_attribution_skips_rows_with_no_pnl_yet():
    rows = [_row(symbol="TCS", pnl_rupees=None)]
    assert per_symbol_sector_attribution(rows) == {}
