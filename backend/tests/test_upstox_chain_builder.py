"""Tests for the Upstox chain builder — the live equity-iv feed.

Concentrated on the two mistakes that would NOT crash anything and would
therefore ship silently:

  1. writing Upstox's percent iv straight into a column that stores fractions,
     inflating every downstream z-score and percentile 100x;
  2. accumulating 30-minute bars on the :00/:30 wall-clock grid instead of the
     NSE :15/:45 session grid, interleaving two 15-minute-offset grids inside
     the same interval='30minute' partition.

Both are pure functions of the module's own constants, so both are cheap to
pin and expensive to leave unpinned.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from market_data.chain_candle_builder import BUCKET_PHASE_30M, BUCKET_SECONDS_30M
from market_data.upstox_chain_builder import (
    INTERVAL,
    POLL_INTERVAL_SECONDS,
    SOURCE,
    UpstoxChainBuilder,
    _iv_fraction,
)


# ── the IV unit trap ────────────────────────────────────────────────────────

def test_percent_iv_is_converted_to_the_fraction_the_column_stores():
    """Upstox reports 27.34 for 27.34%. option_premium_candles.iv is a
    fraction, matching every existing consumer and greeks_enrichment's own
    documented convention."""
    assert _iv_fraction(27.34) == pytest.approx(0.2734)
    assert _iv_fraction("13.98") == pytest.approx(0.1398)


def test_missing_or_nonpositive_iv_becomes_none_not_zero():
    """A strike with no iv is unpriced. 0.0 would read as 'volatility is zero',
    which is a claim rather than an absence."""
    for raw in (None, "", "abc", 0, 0.0, -1.0):
        assert _iv_fraction(raw) is None


def test_a_realistic_chain_row_round_trips_into_a_plausible_fraction():
    for percent in (8.5, 27.34, 55.54, 120.0):
        got = _iv_fraction(percent)
        assert 0.0 < got < 2.0, f"{percent}% -> {got} is not a plausible vol fraction"


# ── the 30-minute grid trap ─────────────────────────────────────────────────

def test_builder_uses_the_nse_session_grid_not_the_wall_clock_grid():
    """The accumulator must carry the :15/:45 phase. Without it the builder
    writes a second, 15-minute-offset grid into the same partition as every
    other 30-minute writer."""
    builder = UpstoxChainBuilder()
    assert builder._acc.bucket_seconds == BUCKET_SECONDS_30M
    assert builder._acc.phase_offset_seconds == BUCKET_PHASE_30M


def test_the_grid_phase_actually_lands_bars_on_ist_quarter_past_and_to():
    """09:15 IST == 03:45 UTC. A snapshot anywhere inside 09:15-09:45 IST must
    floor to the 09:15 bar, not to 09:00 or 09:30."""
    from market_data.chain_candle_builder import _bucket_start

    inside = datetime(2026, 8, 27, 3, 50, tzinfo=timezone.utc)   # 09:20 IST
    bucket = _bucket_start(inside, BUCKET_SECONDS_30M, BUCKET_PHASE_30M)
    assert (bucket.hour, bucket.minute) == (3, 45)                # 09:15 IST


# ── ingest behaviour ────────────────────────────────────────────────────────

def _entry(strike, option_type, ltp, iv=27.34, key="NSE_FO|X"):
    return SimpleNamespace(
        strike=strike, option_type=option_type, ltp=ltp, iv=iv,
        delta=0.5, gamma=0.01, theta=-1.0, vega=2.0, volume=100, oi=1000,
        instrument_key=key,
    )


def _chain(entries, spot=1300.0):
    return SimpleNamespace(entries=entries, spot_price=spot)


def test_ingest_stores_the_converted_fraction_not_the_raw_percent():
    builder = UpstoxChainBuilder()
    ts = datetime(2026, 8, 27, 4, 0, tzinfo=timezone.utc)
    builder._ingest_chain("RELIANCE", date(2026, 9, 29), _chain([_entry(1300, "CE", 45.0)]), ts)
    bar = next(iter(builder._acc._cur.values()))
    assert bar.iv == pytest.approx(0.2734)


def test_unquoted_strikes_are_skipped_rather_than_opening_a_fabricated_bar():
    """ltp<=0 means the strike has not traded. Accumulating it would invent an
    OHLC bar at a price that never existed."""
    builder = UpstoxChainBuilder()
    ts = datetime(2026, 8, 27, 4, 0, tzinfo=timezone.utc)
    builder._ingest_chain(
        "RELIANCE", date(2026, 9, 29),
        _chain([_entry(1300, "CE", 0.0), _entry(1320, "PE", 0)]), ts,
    )
    assert builder._acc._cur == {}


def test_non_option_rows_are_ignored():
    builder = UpstoxChainBuilder()
    ts = datetime(2026, 8, 27, 4, 0, tzinfo=timezone.utc)
    builder._ingest_chain("RELIANCE", date(2026, 9, 29), _chain([_entry(1300, "XX", 45.0)]), ts)
    assert builder._acc._cur == {}


def test_the_expiry_in_the_key_is_the_one_we_REQUESTED():
    """Keying off the response would let an empty or reformatted echo open a
    fresh bar on every poll, so no bar would ever close."""
    builder = UpstoxChainBuilder()
    ts = datetime(2026, 8, 27, 4, 0, tzinfo=timezone.utc)
    chain = _chain([_entry(1300, "CE", 45.0)])
    chain.expiry = ""  # deliberately unhelpful echo
    builder._ingest_chain("RELIANCE", date(2026, 9, 29), chain, ts)
    (_, expiry_iso, _, _) = next(iter(builder._acc._cur.keys()))
    assert expiry_iso == "2026-09-29"


def test_a_second_snapshot_in_the_same_bucket_updates_rather_than_closes():
    builder = UpstoxChainBuilder()
    first = datetime(2026, 8, 27, 3, 50, tzinfo=timezone.utc)
    second = datetime(2026, 8, 27, 4, 0, tzinfo=timezone.utc)   # same 09:15 IST bar
    chain_a = _chain([_entry(1300, "CE", 45.0)])
    chain_b = _chain([_entry(1300, "CE", 52.0)])
    assert builder._ingest_chain("RELIANCE", date(2026, 9, 29), chain_a, first) == []
    assert builder._ingest_chain("RELIANCE", date(2026, 9, 29), chain_b, second) == []
    bar = next(iter(builder._acc._cur.values()))
    assert (bar.open, bar.high, bar.close) == (45.0, 52.0, 52.0)


# ── contract / governance ───────────────────────────────────────────────────

def test_source_tag_is_distinct_from_every_other_iv_writer():
    """It must not collide with fyers_chain, upstox, or upstox_expired — the
    read-path dedup and every provenance query key off this string."""
    assert SOURCE == "upstox_chain"
    assert SOURCE not in {"fyers_chain", "upstox", "upstox_expired", "fyers"}


def test_it_writes_the_thirty_minute_partition_consumers_actually_read():
    assert INTERVAL == "30minute"


def test_poll_cadence_matches_the_bar_width():
    """A tighter cadence buys intra-bar detail no consumer of this feed reads,
    at a directly proportional cost in broker calls."""
    assert POLL_INTERVAL_SECONDS == BUCKET_SECONDS_30M


def test_the_builder_is_disabled_by_default():
    """~213 chain calls per bar against a shared broker budget is not something
    that should switch itself on."""
    from core.config import Settings

    assert Settings().UPSTOX_CHAIN_BUILDER_ENABLED is False
