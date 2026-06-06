"""Regression tests for price-relative value-area tolerances (auction swing agent).

Bug: the *_tolerance_min/max_points config values are absolute NSE-index points
calibrated at NIFTY's price scale (~23000). On a low-priced instrument (e.g. SPY
~$670, value area ~0.6pt) the 8-70pt floors swamped the fractional term, pinning
the tolerance and silently disabling the value-area entry filter. The fix makes the
point floor/cap price-relative (a bps-of-price band) while leaving NIFTY unchanged.
See docs/STRATEGY_TESTING_RESULTS.md Run 2/3.
"""
from __future__ import annotations

import pytest

from auction_intelligence.agents.base import NIFTY_REFERENCE_PRICE
from auction_intelligence.agents.swing import SwingAgent
from auction_intelligence.config import clone_default_config


@pytest.fixture
def swing() -> SwingAgent:
    return SwingAgent(clone_default_config()["agents"]["swing"])


def test_nifty_scale_is_unchanged(swing):
    """At NIFTY's reference price the price-relative band must equal the legacy band."""
    for ref_range in (0.0, 5.0, 60.0, 500.0):
        legacy = swing._bounded_tolerance(reference_range=ref_range, fraction=0.35, minimum=10.0, maximum=70.0)
        scaled = swing._bounded_tolerance(
            reference_range=ref_range, fraction=0.35, minimum=10.0, maximum=70.0, price=NIFTY_REFERENCE_PRICE
        )
        assert scaled == pytest.approx(legacy, abs=1e-6)


def test_price_none_preserves_legacy_behaviour(swing):
    """No price -> legacy absolute-point behaviour (floor dominates a tiny value area)."""
    assert swing._bounded_tolerance(reference_range=0.5, fraction=0.35, minimum=10.0, maximum=70.0, price=None) == pytest.approx(10.0)


def test_low_price_instrument_no_longer_swamped(swing):
    """SPY-scale instrument: the legacy 10pt floor would swamp a 0.6pt value area; the
    scaled floor must collapse to the instrument's scale instead."""
    price, ref_range = 670.0, 0.6
    legacy = swing._bounded_tolerance(reference_range=ref_range, fraction=0.35, minimum=10.0, maximum=70.0)
    scaled = swing._bounded_tolerance(reference_range=ref_range, fraction=0.35, minimum=10.0, maximum=70.0, price=price)
    assert legacy == pytest.approx(10.0)            # degenerate: floor dominates
    assert scaled < 1.0                             # fixed: on the instrument's scale
    # floor scaled to 10 * 670/23000 ~= 0.2913 (result is rounded to 4dp)
    assert scaled == pytest.approx(10.0 * price / NIFTY_REFERENCE_PRICE, abs=1e-3)


def test_effective_tolerance_scales_with_price(swing):
    """When the floor binds, the resolved tolerance is proportional to instrument price."""
    kw = dict(reference_range=0.1, fraction=0.35, minimum=10.0, maximum=70.0)
    t1 = swing._bounded_tolerance(price=500.0, **kw)
    t2 = swing._bounded_tolerance(price=1000.0, **kw)
    t4 = swing._bounded_tolerance(price=2000.0, **kw)
    assert t1 > 0
    assert t2 == pytest.approx(2 * t1, rel=1e-6)
    assert t4 == pytest.approx(4 * t1, rel=1e-6)


def test_configured_swing_tolerances_scale(swing):
    """Using the ACTUAL configured swing tolerances, a low-price instrument must get a
    far smaller value-entry tolerance than NIFTY (the bug made them effectively equal)."""
    cfg = clone_default_config()["agents"]["swing"]
    mn = float(cfg["value_entry_tolerance_min_points"])
    mx = float(cfg["value_entry_tolerance_max_points"])
    fr = float(cfg["value_entry_tolerance_fraction"])
    nifty = swing._bounded_tolerance(reference_range=60.0, fraction=fr, minimum=mn, maximum=mx, price=23500.0)
    spy = swing._bounded_tolerance(reference_range=0.6, fraction=fr, minimum=mn, maximum=mx, price=670.0)
    assert spy < nifty / 20.0


def test_reference_price_override():
    """tolerance_reference_price lets a different instrument scale be the anchor."""
    cfg = clone_default_config()["agents"]["swing"]
    cfg["tolerance_reference_price"] = 670.0
    agent = SwingAgent(cfg)
    # With SPY as the reference, price=670 => scale 1.0 => floor unchanged at 10.
    assert agent._bounded_tolerance(reference_range=0.1, fraction=0.35, minimum=10.0, maximum=70.0, price=670.0) == pytest.approx(10.0)
