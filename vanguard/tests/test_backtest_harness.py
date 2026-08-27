"""Offline tests for M8's pure logic: walk-forward windowing, decile
bucketing, drawdown, and metric aggregation. replay() itself drives M6's
own real DB-backed functions and is covered by a live smoke test instead
(scratchpad, not part of the offline suite -- matches the pattern used to
verify M2/M3/M5/M9 against real data during this build)."""
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from backtest.harness import (  # noqa: E402
    MIN_CANDIDATES_FOR_DECILE_CHECK,
    TRANSACTION_COST_PER_LOT,
    bucketize_by_conviction,
    _max_drawdown,
    compute_metrics,
    walk_forward_windows,
)


def test_walk_forward_windows_covers_the_range_with_no_gap_between_train_and_test():
    windows = walk_forward_windows(date(2026, 1, 1), date(2026, 3, 1), train_days=30, test_days=10, step_days=10)
    assert windows
    for train_start, train_end, test_start, test_end in windows:
        assert test_start == train_end   # no gap, no overlap
        assert (test_end - test_start).days == 10
        assert (train_end - train_start).days == 30


def test_walk_forward_windows_steps_forward_each_iteration():
    windows = walk_forward_windows(date(2026, 1, 1), date(2026, 4, 1), train_days=30, test_days=10, step_days=10)
    starts = [w[0] for w in windows]
    assert starts == sorted(starts)
    assert len(set(starts)) == len(starts)


def test_walk_forward_windows_returns_empty_when_the_range_is_too_short():
    windows = walk_forward_windows(date(2026, 1, 1), date(2026, 1, 10), train_days=30, test_days=10, step_days=10)
    assert windows == []


def test_bucketize_by_conviction_splits_as_evenly_as_possible_with_early_buckets_absorbing_remainder():
    items = [{"conviction": float(i)} for i in range(23)]
    buckets = bucketize_by_conviction(items, 10)
    sizes = [len(b[1]) for b in buckets]
    assert sum(sizes) == 23
    assert max(sizes) - min(sizes) <= 1


def test_bucketize_by_conviction_never_returns_more_buckets_than_data_points():
    items = [{"conviction": 1.0}, {"conviction": 2.0}, {"conviction": 3.0}]
    buckets = bucketize_by_conviction(items, 10)
    assert len(buckets) == 3
    assert all(len(b[1]) == 1 for b in buckets)


def test_bucketize_by_conviction_orders_ascending_by_conviction():
    items = [{"conviction": 90.0}, {"conviction": 10.0}, {"conviction": 50.0}]
    buckets = bucketize_by_conviction(items, 3)
    convictions_in_order = [b[1][0]["conviction"] for b in buckets]
    assert convictions_in_order == [10.0, 50.0, 90.0]


def test_max_drawdown_is_zero_for_an_all_winning_sequence():
    assert _max_drawdown([100.0, 200.0, 50.0]) == 0.0


def test_max_drawdown_measures_the_worst_peak_to_trough_decline():
    # equity path: 100 -> 300 (peak) -> 100 -> 150 -> trough at 100, dd = 100-300 = -200
    assert _max_drawdown([100.0, 200.0, -200.0, 50.0]) == -200.0


def test_max_drawdown_of_empty_sequence_is_none_not_a_fabricated_zero():
    assert _max_drawdown([]) is None


def _candidate(conviction, r_multiple, symbol="A", ts=None, would_emit=False, resolved=True):
    return {
        "ts": ts or datetime(2026, 8, 26, 10, 0), "symbol": symbol, "conviction": conviction,
        "would_emit": would_emit, "resolved": resolved,
        "exit": {"entry": 50.0, "exit_price": 50.0 * (1 + r_multiple * 0.15), "exit_reason": "stop",
                 "holding_bars": 1, "r_multiple": r_multiple, "lot_size": 150} if resolved else None,
    }


def test_compute_metrics_reports_unresolved_candidates_separately_from_resolved_ones():
    result = {
        "all_candidates": [_candidate(70.0, 0.5), _candidate(60.0, None, resolved=False)],
        "emitted_trades": [],
    }
    metrics = compute_metrics(result)
    assert metrics["candidates_evaluated"] == 2
    assert metrics["candidates_unresolved_no_contract_or_no_exit"] == 1


def test_compute_metrics_flags_inadequate_sample_size_below_the_threshold():
    result = {"all_candidates": [_candidate(70.0, 0.5)], "emitted_trades": []}
    metrics = compute_metrics(result)
    assert metrics["decile_check_sample_size_adequate"] is False


def test_compute_metrics_flags_adequate_sample_size_at_or_above_the_threshold():
    candidates = [_candidate(50.0 + i, 0.1) for i in range(MIN_CANDIDATES_FOR_DECILE_CHECK)]
    result = {"all_candidates": candidates, "emitted_trades": []}
    metrics = compute_metrics(result)
    assert metrics["decile_check_sample_size_adequate"] is True


def test_compute_metrics_detects_monotonic_deciles():
    # 10 candidates, one per decile, avg_r strictly increasing with conviction
    candidates = [_candidate(conviction=10.0 * (i + 1), r_multiple=float(i)) for i in range(10)]
    result = {"all_candidates": candidates, "emitted_trades": []}
    metrics = compute_metrics(result)
    assert metrics["conviction_decile_monotonic"] is True


def test_compute_metrics_detects_non_monotonic_deciles():
    candidates = [_candidate(conviction=10.0 * (i + 1), r_multiple=-float(i)) for i in range(10)]
    result = {"all_candidates": candidates, "emitted_trades": []}
    metrics = compute_metrics(result)
    assert metrics["conviction_decile_monotonic"] is False


def test_compute_metrics_expectancy_net_is_gross_minus_transaction_cost_per_lot():
    ts = datetime(2026, 8, 26, 10, 0)
    candidate = _candidate(90.0, r_multiple=1.0, symbol="TCS", ts=ts, would_emit=True)
    # entry=50, r=1.0 -> exit_price = 50*(1+1.0*0.15) = 57.5; gross = (57.5-50)*lots*lot_size
    emitted = {"ts": ts, "symbol": "TCS", "sizing_lots": 2}
    result = {"all_candidates": [candidate], "emitted_trades": [emitted]}
    metrics = compute_metrics(result)
    gross_expected = (57.5 - 50.0) * 2 * 150
    net_expected = gross_expected - 2 * TRANSACTION_COST_PER_LOT
    assert metrics["expectancy_gross_rupees"] == round(gross_expected, 2)
    assert metrics["expectancy_net_rupees"] == round(net_expected, 2)


def test_compute_metrics_hit_rate_counts_only_positive_r_multiples():
    ts = datetime(2026, 8, 26, 10, 0)
    candidates = [
        _candidate(90.0, r_multiple=1.0, symbol="A", ts=ts, would_emit=True),
        _candidate(88.0, r_multiple=-0.5, symbol="B", ts=ts, would_emit=True),
    ]
    emitted = [
        {"ts": ts, "symbol": "A", "sizing_lots": 1},
        {"ts": ts, "symbol": "B", "sizing_lots": 1},
    ]
    result = {"all_candidates": candidates, "emitted_trades": emitted}
    metrics = compute_metrics(result)
    assert metrics["hit_rate"] == 0.5
    assert metrics["emitted_trades_closed"] == 2
