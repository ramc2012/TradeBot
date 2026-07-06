"""Unit tests for the greeks-enrichment helpers (no DB required)."""
from __future__ import annotations

from datetime import datetime, timezone

from market_data.greeks_enrichment import (
    DEFAULT_INTERVALS,
    INDEX_SYMBOL_MAP,
    INTERVAL_SECONDS,
    _day_windows,
)

UTC = timezone.utc


def test_every_default_interval_has_bar_seconds():
    # The daemon iterates DEFAULT_INTERVALS and indexes INTERVAL_SECONDS[interval];
    # a missing entry would KeyError at runtime.
    for interval in DEFAULT_INTERVALS:
        assert interval in INTERVAL_SECONDS
        assert INTERVAL_SECONDS[interval] > 0


def test_index_symbol_map_is_the_five_indices():
    assert set(INDEX_SYMBOL_MAP) == {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"}
    # Values are the Fyers index form stored in option_chain_snapshots.symbol.
    assert INDEX_SYMBOL_MAP["SENSEX"] == "BSE:SENSEX-INDEX"
    assert all("-INDEX" in v for v in INDEX_SYMBOL_MAP.values())


def test_day_windows_align_to_utc_midnight_and_are_contiguous():
    since = datetime(2026, 6, 23, 9, 30, tzinfo=UTC)
    until = datetime(2026, 6, 26, 4, 0, tzinfo=UTC)
    windows = _day_windows(since, until)

    # First window keeps the fractional start; last keeps the fractional end.
    assert windows[0][0] == since
    assert windows[-1][1] == until
    # Interior boundaries are UTC midnight and the windows tile [since, until)
    # with no gaps or overlaps.
    for (_, end_a), (start_b, _) in zip(windows, windows[1:]):
        assert end_a == start_b
        assert end_a.hour == 0 and end_a.minute == 0 and end_a.second == 0
    assert len(windows) == 4  # 06-23 (partial), 06-24, 06-25, 06-26 (partial)


def test_day_windows_single_intraday_span_is_one_window():
    since = datetime(2026, 7, 6, 3, 45, tzinfo=UTC)
    until = datetime(2026, 7, 6, 10, 0, tzinfo=UTC)
    assert _day_windows(since, until) == [(since, until)]


def test_day_windows_empty_when_until_not_after_since():
    ts = datetime(2026, 7, 6, tzinfo=UTC)
    assert _day_windows(ts, ts) == []
