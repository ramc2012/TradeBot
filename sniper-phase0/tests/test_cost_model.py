"""Cost model sanity tests. Specific rupee values are spot-checks against the published
Zerodha charge schedule. If you update fee constants, update these values too."""

from __future__ import annotations

import pytest

from nomad_sniper.labels.cost_model import ZerodhaFnoCostModel


def test_long_future_costs_are_positive():
    cm = ZerodhaFnoCostModel(slippage_inr_per_share=0.05)
    breakdown = cm.compute(
        instrument_type="future",
        direction="long",
        entry_price=22000.0,
        exit_price=22050.0,
        quantity=50,
    )
    assert breakdown.brokerage > 0
    assert breakdown.stt > 0
    assert breakdown.exchange_fee > 0
    assert breakdown.gst > 0
    assert breakdown.slippage == pytest.approx(0.05 * 50 * 2)
    assert breakdown.total > 0


def test_short_option_costs_are_positive():
    cm = ZerodhaFnoCostModel(slippage_inr_per_share=0.20)
    breakdown = cm.compute(
        instrument_type="option",
        direction="short",
        entry_price=100.0,
        exit_price=70.0,
        quantity=50,
    )
    assert breakdown.total > 0
    # Option STT (0.10% on sell) should be higher than future STT (0.02%)
    fut_breakdown = cm.compute(
        instrument_type="future",
        direction="short",
        entry_price=100.0,
        exit_price=70.0,
        quantity=50,
    )
    assert breakdown.stt > fut_breakdown.stt


def test_brokerage_cap_at_20_rupees():
    cm = ZerodhaFnoCostModel()
    # Large notional — 0.03% would be huge, brokerage should cap at ₹20/leg = ₹40 total
    breakdown = cm.compute(
        instrument_type="future",
        direction="long",
        entry_price=50000.0,
        exit_price=50100.0,
        quantity=100,
    )
    assert breakdown.brokerage == pytest.approx(40.0, abs=0.5)


def test_slippage_calibration():
    cm = ZerodhaFnoCostModel(slippage_inr_per_share=0.10)
    cm.calibrate_slippage(0.25)
    assert cm.slippage_inr_per_share == 0.25
    with pytest.raises(ValueError):
        cm.calibrate_slippage(-0.1)
