"""WS-1.3 — adaptive WS reconnect backoff + shortened stale budget."""
from market_data.data_router import DataRouter


def test_backoff_exponential_with_jitter_and_cap():
    dr = DataRouter()
    # (failure_streak, lower_bound, upper_bound) — raw = min(5*2^n, 120), +0..50% jitter
    for failures, lo, hi in [(0, 5, 7.5), (1, 10, 15), (2, 20, 30), (3, 40, 60), (8, 120, 180)]:
        dr._reconnect_failures = failures
        for _ in range(100):
            secs = dr._current_reconnect_backoff().total_seconds()
            assert lo <= secs <= hi, f"failures={failures}: {secs} not in [{lo},{hi}]"


def test_first_retry_is_fast_after_success_reset():
    dr = DataRouter()
    dr._reconnect_failures = 6              # was failing
    dr._reconnect_failures = 0              # success resets it (mirrors _reconnect_if_stale)
    secs = dr._current_reconnect_backoff().total_seconds()
    assert secs <= 7.5                      # fast first retry, not the old fixed 60s


def test_stale_budget_shortened():
    assert DataRouter()._required_tick_stale_seconds == 45.0
