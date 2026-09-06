"""Offline tests for the Black-Scholes solver.

An IV solver is worth testing hard because it fails QUIETLY: a wrong answer is
still a plausible-looking number in a plausible-looking range, and everything
downstream keeps working while measuring nothing. So these check against
closed-form identities and round trips rather than against remembered outputs.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from features.m_implied_vol import (  # noqa: E402
    IV_CEILING,
    IV_FLOOR,
    MAX_IV_UNCERTAINTY,
    MIN_OI,
    RISK_FREE_RATE,
    TICK_SIZE,
    assess_quality,
    bs_greeks,
    bs_price,
    bs_vega,
    implied_vol,
    solve_frame,
)


# ── the pricer ─────────────────────────────────────────────────────────────

def test_put_call_parity_holds():
    """C - P = S - K*exp(-rT). If the pricer breaks parity, every put IV is
    wrong in a way no round-trip test on calls alone would ever reveal."""
    spot, strike, t = 1000.0, 1050.0, 0.25
    call = bs_price(spot, strike, t, 0.28, True)
    put = bs_price(spot, strike, t, 0.28, False)
    assert float(call - put) == pytest.approx(spot - strike * np.exp(-RISK_FREE_RATE * t), abs=1e-8)


def test_a_call_is_worth_at_least_its_intrinsic_value():
    deep_itm = bs_price(1000.0, 500.0, 0.5, 0.2, True)
    assert float(deep_itm) >= 1000.0 - 500.0 * np.exp(-RISK_FREE_RATE * 0.5) - 1e-9


def test_price_rises_monotonically_with_volatility():
    prices = [float(bs_price(1000.0, 1000.0, 0.25, s, True)) for s in (0.1, 0.2, 0.4, 0.8)]
    assert prices == sorted(prices)


def test_vega_matches_a_numerical_derivative():
    spot, strike, t, sigma = 1000.0, 1020.0, 0.3, 0.25
    bump = 1e-5
    numeric = (float(bs_price(spot, strike, t, sigma + bump, True))
               - float(bs_price(spot, strike, t, sigma - bump, True))) / (2 * bump)
    assert float(bs_vega(spot, strike, t, sigma)) == pytest.approx(numeric, rel=1e-4)


def test_expired_or_zero_vol_contracts_price_at_intrinsic_not_nan():
    assert float(bs_price(1000.0, 900.0, 0.0, 0.3, True)) == pytest.approx(100.0)
    assert float(bs_price(1000.0, 1100.0, 0.0, 0.3, True)) == 0.0


# ── the solver ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("sigma", [0.15, 0.25, 0.45, 0.90, 1.60])
@pytest.mark.parametrize("moneyness", [0.85, 0.95, 1.0, 1.05, 1.20])
@pytest.mark.parametrize("is_call", [True, False])
def test_round_trip_recovers_the_volatility_it_priced_with(sigma, moneyness, is_call):
    """The one test that matters: price with a known sigma, solve, get it back.
    Swept across the moneyness and vol range the NSE chain actually spans."""
    spot, t = 1000.0, 0.15
    strike = spot * moneyness
    price = bs_price(spot, strike, t, sigma, is_call)
    solved, _ = implied_vol(price, spot, strike, t, is_call)
    assert float(solved) == pytest.approx(sigma, abs=1e-4)


@pytest.mark.parametrize("moneyness", [0.85, 1.20])
@pytest.mark.parametrize("is_call", [True, False])
def test_an_unidentifiable_volatility_returns_nan_rather_than_a_plausible_wrong_number(
    moneyness, is_call
):
    """THE QUIET FAILURE THIS GUARDS.

    At 8% vol, 0.15y and 15-20% out of the money, the contract's entire time
    value is ~6e-08 rupees and vega is ~3e-05. One 5-paise tick is worth about
    1,800 vol points of sigma there, so the price carries no information about
    volatility at all. Measured before the conditioning gate existed: a true
    8% vol came back as 9.3% — a number in a completely plausible range, with
    nothing about it to suggest it was meaningless.

    The solver must decline. A wrong IV propagates silently into IVS, skew and
    every trailing z-score built on them; a NaN stops at the quality gate."""
    spot, t, sigma = 1000.0, 0.15, 0.08
    strike = spot * moneyness
    price = bs_price(spot, strike, t, sigma, is_call)
    solved, uncertainty = implied_vol(price, spot, strike, t, is_call)
    assert np.isnan(float(solved))
    assert float(uncertainty) > MAX_IV_UNCERTAINTY


def test_the_uncertainty_is_reported_even_where_the_iv_is_refused():
    """A NaN with no explanation is indistinguishable from a bug. The tick-
    implied uncertainty is always returned so a rejection can be inspected."""
    _, uncertainty = implied_vol(bs_price(1000.0, 1200.0, 0.15, 0.08, True),
                                 1000.0, 1200.0, 0.15, True)
    assert np.isfinite(float(uncertainty))


def test_uncertainty_is_one_tick_divided_by_vega():
    spot, strike, t, sigma = 1000.0, 1000.0, 0.25, 0.30
    price = bs_price(spot, strike, t, sigma, True)
    _, uncertainty = implied_vol(price, spot, strike, t, True)
    assert float(uncertainty) == pytest.approx(TICK_SIZE / float(bs_vega(spot, strike, t, sigma)), rel=1e-3)


def test_a_well_conditioned_atm_contract_is_far_inside_the_uncertainty_bound():
    _, uncertainty = implied_vol(bs_price(1000.0, 1000.0, 0.25, 0.30, True),
                                 1000.0, 1000.0, 0.25, True)
    assert float(uncertainty) < MAX_IV_UNCERTAINTY / 10


def test_the_solver_is_vectorised_and_order_preserving():
    spot = np.full(4, 1000.0)
    strike = np.array([900.0, 1000.0, 1100.0, 1200.0])
    sigmas = np.array([0.2, 0.3, 0.4, 0.5])
    t = np.full(4, 0.25)
    is_call = np.array([True, True, False, False])
    prices = bs_price(spot, strike, t, sigmas, is_call)
    solved, _ = implied_vol(prices, spot, strike, t, is_call)
    assert np.allclose(solved, sigmas, atol=1e-4)


def test_a_price_below_intrinsic_yields_no_solution_rather_than_a_floor():
    """A crossed or stale print has no implied vol. Returning IV_FLOOR would
    put a fabricated 1% vol into the surface and it would look real."""
    solved, _ = implied_vol(np.array([1.0]), np.array([1000.0]), np.array([500.0]),
                            np.array([0.25]), np.array([True]))
    assert np.isnan(solved[0])


def test_a_price_above_the_no_arbitrage_ceiling_yields_no_solution():
    solved, _ = implied_vol(np.array([1200.0]), np.array([1000.0]), np.array([1000.0]),
                            np.array([0.25]), np.array([True]))
    assert np.isnan(solved[0])


def test_an_expired_contract_yields_no_solution():
    solved, _ = implied_vol(np.array([10.0]), np.array([1000.0]), np.array([1000.0]),
                            np.array([0.0]), np.array([True]))
    assert np.isnan(solved[0])


def test_deep_out_of_the_money_still_solves_where_newton_alone_would_diverge():
    """Vega falls away steeply out of the money, so a Newton step divides by
    something small and can leave the bracket entirely. The bisection fallback
    is what makes these solvable — 20% OTM at 45% vol is a contract that both
    trades and carries real vol information."""
    spot, strike, t, sigma = 1000.0, 1200.0, 0.25, 0.45
    price = bs_price(spot, strike, t, sigma, True)
    assert float(price) > 1.0
    solved, _ = implied_vol(price, spot, strike, t, True)
    assert float(solved) == pytest.approx(sigma, abs=1e-3)


def test_an_option_worth_less_than_one_tick_is_refused_however_far_it_solves():
    """A 60%-OTM call with 18 days left at 55% vol is worth 0.27 PAISE — less
    than a single 5-paise tick, so it cannot trade at its theoretical value and
    its printed price cannot identify a volatility. The mathematics solves; the
    measurement does not exist."""
    price = bs_price(1000.0, 1600.0, 0.05, 0.55, True)
    assert float(price) < TICK_SIZE
    solved, uncertainty = implied_vol(price, 1000.0, 1600.0, 0.05, True)
    assert np.isnan(float(solved))
    assert float(uncertainty) > MAX_IV_UNCERTAINTY


# ── greeks ─────────────────────────────────────────────────────────────────

def test_call_and_put_delta_differ_by_one():
    call = bs_greeks(1000.0, 1000.0, 0.25, 0.3, True)["delta"]
    put = bs_greeks(1000.0, 1000.0, 0.25, 0.3, False)["delta"]
    assert float(call - put) == pytest.approx(1.0, abs=1e-9)


def test_an_atm_call_delta_sits_a_little_above_a_half():
    delta = float(bs_greeks(1000.0, 1000.0, 0.25, 0.3, True)["delta"])
    assert 0.5 < delta < 0.65


def test_delta_matches_a_numerical_derivative_of_price():
    spot, strike, t, sigma = 1000.0, 1050.0, 0.3, 0.28
    bump = 0.01
    numeric = (float(bs_price(spot + bump, strike, t, sigma, True))
               - float(bs_price(spot - bump, strike, t, sigma, True))) / (2 * bump)
    assert float(bs_greeks(spot, strike, t, sigma, True)["delta"]) == pytest.approx(numeric, rel=1e-5)


def test_theta_is_negative_for_a_long_option():
    assert float(bs_greeks(1000.0, 1000.0, 0.25, 0.3, True)["theta"]) < 0
    assert float(bs_greeks(1000.0, 1000.0, 0.25, 0.3, False)["theta"]) < 0


def test_gamma_is_identical_for_calls_and_puts():
    call = bs_greeks(1000.0, 1000.0, 0.25, 0.3, True)["gamma"]
    put = bs_greeks(1000.0, 1000.0, 0.25, 0.3, False)["gamma"]
    assert float(call) == pytest.approx(float(put), rel=1e-12)


# ── quality gating ─────────────────────────────────────────────────────────

def _row(**kwargs):
    base = {"premium": 50.0, "oi": 10_000, "volume": 500, "log_moneyness": 0.0,
            "iv": 0.3, "days_to_expiry": 20.0, "iv_uncertainty": 0.001}
    base.update(kwargs)
    return base


def test_a_clean_liquid_contract_is_good():
    out = assess_quality(pd.DataFrame([_row()]))
    assert out.loc[0, "quality"] == "good"
    assert out.loc[0, "quality_flags"] == ""


def test_an_unsolved_row_is_unusable_and_says_why():
    out = assess_quality(pd.DataFrame([_row(iv=np.nan, iv_uncertainty=np.nan)]))
    assert out.loc[0, "quality"] == "unusable"
    assert "no_solution" in out.loc[0, "quality_flags"]


def test_an_unidentified_row_is_flagged_apart_from_an_unsolvable_one():
    """They need different responses — one is a bad print, the other is a
    contract too far out of the money to carry vol information — so they must
    not both land under the same flag."""
    out = assess_quality(pd.DataFrame([_row(iv=np.nan, iv_uncertainty=900.0)]))
    assert out.loc[0, "quality"] == "unusable"
    assert "vol_not_identified" in out.loc[0, "quality_flags"]
    assert "no_solution" not in out.loc[0, "quality_flags"]


def test_a_thin_but_solvable_contract_is_weak_not_unusable():
    """Thin is real, just not aggregable. Discarding it entirely would hide
    that the chain exists at all."""
    out = assess_quality(pd.DataFrame([_row(oi=MIN_OI - 1)]))
    assert out.loc[0, "quality"] == "weak"
    assert "thin_oi" in out.loc[0, "quality_flags"]


def test_a_sub_tick_premium_is_unusable_because_one_tick_dominates_it():
    out = assess_quality(pd.DataFrame([_row(premium=0.05)]))
    assert out.loc[0, "quality"] == "unusable"
    assert "below_tick" in out.loc[0, "quality_flags"]


def test_an_untraded_or_far_otm_contract_is_flagged_weak():
    assert assess_quality(pd.DataFrame([_row(volume=0)])).loc[0, "quality"] == "weak"
    assert assess_quality(pd.DataFrame([_row(log_moneyness=0.9)])).loc[0, "quality"] == "weak"


def test_a_boundary_iv_is_unusable_because_it_is_the_clamp_not_a_solution():
    assert assess_quality(pd.DataFrame([_row(iv=IV_CEILING)])).loc[0, "quality"] == "unusable"
    assert assess_quality(pd.DataFrame([_row(iv=IV_FLOOR)])).loc[0, "quality"] == "unusable"


def test_expiry_day_contracts_are_unusable():
    out = assess_quality(pd.DataFrame([_row(days_to_expiry=0.0)]))
    assert "near_expiry" in out.loc[0, "quality_flags"]
    assert out.loc[0, "quality"] == "unusable"


# ── end-to-end on a synthetic chain ────────────────────────────────────────

def test_solve_frame_round_trips_a_whole_synthetic_chain():
    import datetime as dt
    spot, sigma = 1000.0, 0.32
    rows = []
    for strike in (900.0, 950.0, 1000.0, 1050.0, 1100.0):
        for option_type, is_call in (("CE", True), ("PE", False)):
            t = 30.0 / 365.0
            rows.append({
                "dt": dt.date(2026, 8, 26), "symbol": "TEST",
                "expiry": dt.date(2026, 9, 25), "strike": strike,
                "option_type": option_type,
                "premium": float(bs_price(spot, strike, t, sigma, is_call)),
                "spot": spot, "oi": 50_000, "volume": 1_000,
            })
    out = solve_frame(pd.DataFrame(rows))
    assert (out["quality"] == "good").all()
    assert np.allclose(out["iv"], sigma, atol=1e-3)
    # Calls have positive delta, puts negative — the sign convention the
    # 25-delta skew depends on.
    assert (out.loc[out["option_type"] == "CE", "delta"] > 0).all()
    assert (out.loc[out["option_type"] == "PE", "delta"] < 0).all()
