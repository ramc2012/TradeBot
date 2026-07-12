"""Unit tests for the greeks-enrichment helpers (no DB required)."""
from __future__ import annotations

from datetime import datetime, timezone

from market_data.greeks_enrichment import (
    DEFAULT_INTERVALS,
    INDEX_SYMBOL_MAP,
    INTERVAL_SECONDS,
    _WINDOW_HOURS,
    _time_windows,
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


# 2026-07-08 refactor: _day_windows (UTC-midnight-aligned) became _time_windows
# (short fixed-hour windows, default _WINDOW_HOURS) so every enrichment UPDATE
# stays bounded and can't blow the compressed-chunk decompression limit. These
# tests assert the new contract.
def test_time_windows_tile_span_contiguously_and_are_bounded():
    since = datetime(2026, 6, 23, 9, 30, tzinfo=UTC)
    until = datetime(2026, 6, 26, 4, 0, tzinfo=UTC)
    windows = _time_windows(since, until)

    # First window keeps the fractional start; last keeps the fractional end.
    assert windows[0][0] == since
    assert windows[-1][1] == until
    # Windows tile [since, until) with no gaps or overlaps, and none exceeds
    # the configured bound (that bound is the whole point of the refactor).
    for (_, end_a), (start_b, _) in zip(windows, windows[1:]):
        assert end_a == start_b
    for start, end in windows:
        assert start < end
        assert (end - start).total_seconds() <= _WINDOW_HOURS * 3600


def test_time_windows_short_span_is_one_window():
    since = datetime(2026, 7, 6, 3, 45, tzinfo=UTC)
    until = datetime(2026, 7, 6, 4, 45, tzinfo=UTC)
    assert _time_windows(since, until) == [(since, until)]


def test_time_windows_empty_when_until_not_after_since():
    ts = datetime(2026, 7, 6, tzinfo=UTC)
    assert _time_windows(ts, ts) == []
