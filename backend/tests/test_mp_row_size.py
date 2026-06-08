"""Regression tests for the Market-Profile TPO row-height (bracket) sizing.

Bug: the TPO ladder used the raw exchange tick as the row height, so high-priced
instruments (SILVERM ~Rs 2.6L @ tick Rs 1) produced ~13.5k one-tick rows and the
profile (POC / value-area / single-prints) was statistical noise. The fix sizes
the bracket to the day's range via ``target_rows`` while leaving the legacy
behaviour intact when ``target_rows`` is not configured.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from auction_intelligence.market_profile.engine import MarketProfileEngine
from auction_intelligence.schemas import MarketBar


def _session_bars(center: float, amplitude: float, *, periods: int = 22, spread: float = 50.0) -> list[MarketBar]:
    """Deterministic 1-min session that oscillates across a band of width ~2*amplitude."""
    base = datetime(2026, 6, 5, 9, 0, 0)
    bars: list[MarketBar] = []
    minutes = periods * 15
    for m in range(minutes):
        price = center + amplitude * math.sin(m / 17.0)
        high = price + spread
        low = price - spread
        bars.append(
            MarketBar(
                timestamp=base + timedelta(minutes=m),
                open=price,
                high=high,
                low=low,
                close=price,
                volume=100.0,
            )
        )
    return bars


def test_high_priced_commodity_profile_is_not_degenerate():
    bars = _session_bars(center=260000.0, amplitude=6000.0)  # ~248k-272k, like SILVERM

    legacy = MarketProfileEngine({"period_minutes": 15, "tick_size": 1.0}).build_profile("SILVERM", bars)
    fixed = MarketProfileEngine(
        {"period_minutes": 15, "tick_size": 1.0, "target_rows": 50, "max_rows": 90}
    ).build_profile("SILVERM", bars)

    # Legacy reproduces the bug: thousands of one-tick rows.
    assert len(legacy.tpo_counts) > 5000
    assert legacy.tick_size == 1.0

    # Fixed: bounded, meaningful number of rows and a coarser bracket.
    assert 20 <= len(fixed.tpo_counts) <= 90
    assert fixed.tick_size > 1.0
    # Bracket stays on the exchange-tick grid (whole multiple of tick_size=1.0).
    assert abs(fixed.tick_size - round(fixed.tick_size)) < 1e-9
    # Single-prints should collapse from "everything" to a small fraction.
    assert len(fixed.single_prints) < len(fixed.tpo_counts)


def test_value_area_and_poc_are_well_formed():
    bars = _session_bars(center=153000.0, amplitude=900.0)  # GOLD-like
    snap = MarketProfileEngine(
        {"period_minutes": 15, "tick_size": 1.0, "target_rows": 50, "max_rows": 90}
    ).build_profile("GOLD", bars)

    assert snap.low_price <= snap.val <= snap.poc <= snap.vah <= snap.high_price
    assert 20 <= len(snap.tpo_counts) <= 90


def test_low_priced_commodity_stays_healthy():
    # NATURALGAS-like: tick 0.1, range ~7. Already produced a good ~70-row profile;
    # the fix must not wreck it.
    bars = _session_bars(center=303.0, amplitude=3.4, spread=0.3)
    snap = MarketProfileEngine(
        {"period_minutes": 15, "tick_size": 0.1, "target_rows": 50, "max_rows": 90}
    ).build_profile("NATURALGAS", bars)

    assert 20 <= len(snap.tpo_counts) <= 90
    assert snap.low_price <= snap.poc <= snap.high_price


def test_legacy_path_unchanged_when_not_opted_in():
    """Index/auction callers don't set target_rows -> identical to old behaviour."""
    bars = _session_bars(center=23000.0, amplitude=150.0, spread=2.0)  # NIFTY-like
    snap = MarketProfileEngine({"period_minutes": 30, "tick_size": 0.5}).build_profile("NIFTY", bars)

    # Row height equals the exchange tick exactly (no bracket resizing).
    assert snap.tick_size == 0.5
    prices = sorted(snap.tpo_counts)
    # Grid step is exactly tick_size.
    steps = {round(b - a, 6) for a, b in zip(prices, prices[1:])}
    assert steps == {0.5}


def test_explicit_row_size_override():
    bars = _session_bars(center=9000.0, amplitude=170.0)  # CRUDEOIL-like
    snap = MarketProfileEngine(
        {"period_minutes": 15, "tick_size": 1.0, "row_size": 5.0}
    ).build_profile("CRUDEOIL", bars)
    assert snap.tick_size == 5.0
    prices = sorted(snap.tpo_counts)
    steps = {round(b - a, 6) for a, b in zip(prices, prices[1:])}
    assert steps == {5.0}
