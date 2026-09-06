"""Tests for the index paper lane: fills, sizing, attribution.

Pure arithmetic only — the book and the journals are exercised end to end by
`directional_options.index_paper.replay`, which runs against real bars.

The emphasis here is on the failure modes this lane was built to avoid, not on
happy paths: a fill that flatters the trader, a size derived from full premium,
a gate pincer that empties the feasible set silently, and an attribution that
hides its residual.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import pytest

from directional_options.index_paper.attribution import attribute, attribute_pathwise
from directional_options.index_paper.costs import (
    CostModel,
    FeeSchedule,
    estimate_fill,
    round_trip_cost_premium_fraction,
)
from directional_options.index_paper.schemas import PaperPosition
from directional_options.index_paper.sizing import (
    adverse_premium_move,
    size_position,
    vrp_risk_multiplier,
)

TS = datetime(2026, 9, 4, 8, 45, tzinfo=timezone.utc)


# ── fills ───────────────────────────────────────────────────────────────────


def test_fills_are_always_adverse():
    """The failure mode: a paper book that transacts at prices nobody offered."""
    buy = estimate_fill(200.0, 65, "buy", "entry", oi=1_000_000)
    sell = estimate_fill(200.0, 65, "sell", "exit", oi=1_000_000)
    assert buy.fill_price > 200.0
    assert sell.fill_price < 200.0
    assert buy.total_cost > 0 and sell.total_cost > 0


def test_no_configuration_produces_a_favourable_fill():
    generous = CostModel(
        half_spread_ticks_liquid=0.0,
        lag_slippage_ticks=0.0,
        impact_eta=0.0,
        spread_multiplier=0.0,
    )
    buy = estimate_fill(200.0, 65, "buy", "entry", model=generous, oi=10_000_000)
    assert buy.fill_price >= 200.0


def test_statutory_charges_land_on_the_correct_leg():
    fees = FeeSchedule()
    buy = fees.charges(200.0, 65, "buy")
    sell = fees.charges(200.0, 65, "sell")
    assert buy["stt"] == 0.0 and buy["stamp_duty"] > 0
    assert sell["stt"] > 0 and sell["stamp_duty"] == 0.0
    # GST never applies to STT or stamp duty.
    assert sell["gst"] == pytest.approx(
        (sell["brokerage"] + sell["exchange"] + sell["sebi"]) * fees.gst_rate
    )


def test_liquidity_falls_back_to_open_interest():
    """Volume is absent from this feed; a volume-only bucket vetoes everything."""
    model = CostModel()
    assert model.liquidity_bucket(volume=0, oi=2_000_000) == "liquid"
    assert model.liquidity_bucket(volume=0, oi=100_000) == "normal"
    assert model.liquidity_bucket(volume=0, oi=100) == "thin"
    assert model.liquidity_bucket(volume=None, oi=None) == "thin"


def test_spread_multiplier_scales_the_sensitivity_run():
    base = estimate_fill(200.0, 65, "buy", "entry", oi=1_000_000)
    doubled = estimate_fill(
        200.0, 65, "buy", "entry", model=CostModel(spread_multiplier=2.0), oi=1_000_000
    )
    assert doubled.half_spread == pytest.approx(base.half_spread * 2.0)
    assert doubled.total_cost > base.total_cost


def test_round_trip_cost_rises_into_the_wings():
    near = round_trip_cost_premium_fraction(200.0, 65, oi=1_000_000, log_moneyness=0.0)
    far = round_trip_cost_premium_fraction(20.0, 65, oi=1_000_000, log_moneyness=0.15)
    assert far > near, "a cheap far wing must cost more as a fraction of its premium"


# ── sizing ──────────────────────────────────────────────────────────────────


def _sizing(**overrides):
    kwargs = dict(
        capital=3_000_000.0,
        premium=200.0,
        lot_size=65,
        spot=24_000.0,
        atm_iv=0.12,
        delta=0.40,
        vega=1_200.0,
        theta_per_day=-12.0,
        horizon_bars=3,
        bar_minutes=30.0,
    )
    kwargs.update(overrides)
    return size_position(**kwargs)


def test_risk_is_the_modelled_adverse_move_not_the_premium():
    """Sizing on full premium made every limit in this stack ~6.7x too loose."""
    decision = _sizing()
    assert decision.approved
    assert decision.adverse_move_per_unit < 200.0
    assert decision.risk_per_lot == pytest.approx(decision.adverse_move_per_unit * 65)


def test_adverse_move_scales_with_vol_and_horizon():
    base, _ = adverse_premium_move(
        spot=24_000.0, atm_iv=0.12, delta=0.4, vega=1_200.0,
        theta_per_day=-12.0, sessions=0.24,
    )
    higher_vol, _ = adverse_premium_move(
        spot=24_000.0, atm_iv=0.24, delta=0.4, vega=1_200.0,
        theta_per_day=-12.0, sessions=0.24,
    )
    longer, _ = adverse_premium_move(
        spot=24_000.0, atm_iv=0.12, delta=0.4, vega=1_200.0,
        theta_per_day=-12.0, sessions=1.0,
    )
    assert higher_vol > base
    assert longer > base


def test_theta_is_added_not_combined_in_quadrature():
    """Theta is a certainty the position pays, not a shock."""
    total, parts = adverse_premium_move(
        spot=24_000.0, atm_iv=0.12, delta=0.4, vega=1_200.0,
        theta_per_day=-12.0, sessions=1.0,
    )
    assert total == pytest.approx(parts["shock"] + parts["theta_term"])
    assert parts["shock"] == pytest.approx(math.hypot(parts["delta_term"], parts["vega_term"]))


def test_empty_feasible_set_is_a_refusal_with_the_shortfall():
    """The COPPER pincer: 34 setups fired, zero executed, silently."""
    decision = _sizing(capital=50_000.0)
    assert not decision.approved
    assert decision.reason_code == "below_one_lot"
    assert "feasible set is empty" in decision.reason
    assert decision.risk_per_lot > (decision.risk_budget or 0.0)


def test_binding_cap_is_named():
    tiny_outlay = _sizing(max_premium_fraction=0.0001)
    assert not tiny_outlay.approved or tiny_outlay.caps["binding"] == "outlay"
    capped = _sizing(risk_fraction=1.0, max_lots=2)
    assert capped.approved and capped.lots == 2
    assert capped.caps["binding"] == "max_lots"


def test_rich_variance_premium_shrinks_size_but_never_vetoes():
    neutral, _ = vrp_risk_multiplier(0.0)
    rich, note = vrp_risk_multiplier(0.04, rich_threshold=0.02, floor=0.35)
    missing, _ = vrp_risk_multiplier(None)
    assert neutral == 1.0 and missing == 1.0
    assert rich == pytest.approx(0.35)
    assert rich > 0.0, "an expensive vol regime must size down, not veto"
    assert "vol points" in note

    smaller = _sizing(risk_multiplier=0.35)
    bigger = _sizing(risk_multiplier=1.0)
    assert smaller.lots <= bigger.lots


def test_sizing_refuses_degenerate_input():
    assert not size_position(
        capital=0.0, premium=200.0, lot_size=65, spot=24_000.0, atm_iv=0.12,
        delta=0.4, vega=1_200.0, theta_per_day=-12.0, horizon_bars=3,
    ).approved


# ── attribution ─────────────────────────────────────────────────────────────


def _position(**overrides) -> PaperPosition:
    kwargs = dict(
        position_id="test-1",
        status="closed",
        session_date=date(2026, 9, 4),
        underlying="NIFTY",
        expiry=date(2026, 9, 8),
        strike=24_000.0,
        option_type="CE",
        lots=1,
        lot_size=65,
        quantity=65,
        entry_ts=TS,
        entry_premium=200.0,
        entry_cost=50.0,
        entry_iv=0.12,
        entry_spot=24_000.0,
        entry_delta=0.50,
        entry_gamma=0.0004,
        entry_vega=1_200.0,
        entry_theta=-12.0,
        exit_ts=TS + timedelta(hours=1),
        exit_premium=260.0,
        exit_cost=50.0,
    )
    kwargs.update(overrides)
    return PaperPosition(**kwargs)


def test_terms_sum_to_gross_by_construction():
    position = _position()
    result = attribute(position, exit_spot=24_100.0, exit_iv=0.125)
    total = (
        result.delta_pnl + result.gamma_pnl + result.vega_pnl
        + result.theta_pnl + result.residual_pnl
    )
    assert total == pytest.approx(result.gross_pnl)
    assert result.net_pnl == pytest.approx(result.gross_pnl - result.costs)
    assert result.costs == 100.0


def test_delta_and_vega_terms_carry_the_right_sign():
    up = attribute(_position(), exit_spot=24_100.0, exit_iv=0.12)
    assert up.delta_pnl > 0, "a call gains on a spot rally"
    vol_up = attribute(_position(), exit_spot=24_000.0, exit_iv=0.13)
    assert vol_up.vega_pnl > 0, "a long option gains on a vol expansion"
    assert vol_up.theta_pnl < 0, "and pays for the time it waited"


def test_missing_inputs_are_reported_not_silently_zeroed():
    result = attribute(_position(), exit_spot=None, exit_iv=None)
    assert result.delta_pnl == 0.0
    assert "exit_spot" in result.detail["incomplete_inputs"]
    assert "exit_iv" in result.detail["incomplete_inputs"]


def test_pathwise_explains_more_than_a_single_step():
    """Entry greeks stop describing the position once it has moved.

    The path is generated from actual Black-76 prices rather than made-up
    numbers, so premium, spot, vol and greeks stay mutually consistent — the
    comparison is meaningless otherwise, because an arbitrary path can make
    either method look better.
    """
    from directional_options.vol.blackscholes import black76_greeks, black76_price

    strike, quantity = 24_000.0, 65
    steps = [
        (24_000.0, 0.120, 4.0 / 365.0),
        (24_080.0, 0.125, 3.9 / 365.0),
        (24_140.0, 0.127, 3.8 / 365.0),
        (24_200.0, 0.130, 3.7 / 365.0),
    ]
    points = []
    for spot, iv, tenor in steps:
        greeks = black76_greeks(spot, strike, tenor, iv, "CE", spot=spot)
        points.append(
            {
                "spot": spot,
                "iv": iv,
                "premium": black76_price(spot, strike, tenor, iv, "CE"),
                "delta": greeks.delta,
                "gamma": greeks.gamma,
                "vega": greeks.vega,
                "theta": greeks.theta,
            }
        )

    entry, exit_ = points[0], points[-1]
    position = _position(
        entry_premium=entry["premium"],
        entry_iv=entry["iv"],
        entry_spot=entry["spot"],
        entry_delta=entry["delta"],
        entry_gamma=entry["gamma"],
        entry_vega=entry["vega"],
        entry_theta=entry["theta"],
        exit_premium=exit_["premium"],
        exit_ts=TS + timedelta(hours=3),
        quantity=quantity,
    )
    marks = [
        {**point, "ts": TS + timedelta(hours=idx)}
        for idx, point in enumerate(points[1:-1], start=1)
    ]

    single = attribute(position, exit_spot=exit_["spot"], exit_iv=exit_["iv"])
    path = attribute_pathwise(position, marks, exit_spot=exit_["spot"], exit_iv=exit_["iv"])

    assert path.detail["method"] == "pathwise"
    assert path.gross_pnl == pytest.approx(single.gross_pnl)
    assert abs(path.residual_pnl) < abs(single.residual_pnl)
    assert path.explained_fraction > single.explained_fraction
    assert path.explained_fraction > 0.9


def test_pathwise_falls_back_when_there_is_no_trail():
    position = _position()
    result = attribute_pathwise(position, [], exit_spot=24_100.0, exit_iv=0.125)
    total = (
        result.delta_pnl + result.gamma_pnl + result.vega_pnl
        + result.theta_pnl + result.residual_pnl
    )
    assert total == pytest.approx(result.gross_pnl)


def test_dominant_driver_names_the_largest_term():
    position = _position(entry_delta=0.0, entry_gamma=0.0, entry_vega=50_000.0)
    result = attribute(position, exit_spot=24_000.0, exit_iv=0.13)
    assert result.dominant_driver() == "vega"
