"""Tests for the index volatility substrate.

Deliberately pure: no database, no network.  Everything here is arithmetic that
either round-trips or does not, which is the only kind of test worth having for
a pricing layer — a mocked chain would prove nothing about whether the solver
inverts correctly.

The database-facing behaviour of these modules is covered by the SQL-level
checks in `test_index_paper_lane.py::test_daily_series_sql_shape`, which skip
when Postgres is unreachable rather than passing vacuously.  This stack has
been bitten by a suite where "all 34 tests passed" while every one of them
stubbed the database out.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from directional_options.vol.blackscholes import (
    black76_greeks,
    black76_price,
    forward_from_parity,
    implied_vol,
)
from directional_options.vol.realized import (
    DailyBar,
    build_cone,
    close_to_close_rv,
    garman_klass_rv,
    parkinson_rv,
    variance_risk_premium,
)
from directional_options.vol.svi import SVIParams, calendar_arbitrage, fit_svi_slice, gatheral_g


# ── pricing and inversion ───────────────────────────────────────────────────


def test_put_call_parity_holds():
    F, K, T, sigma = 24_000.0, 24_100.0, 0.05, 0.12
    call = black76_price(F, K, T, sigma, "CE")
    put = black76_price(F, K, T, sigma, "PE")
    assert call - put == pytest.approx(F - K, abs=1e-6)


def test_forward_recovered_from_parity():
    F, K, T, sigma = 57_800.0, 57_500.0, 0.07, 0.11
    call = black76_price(F, K, T, sigma, "CE")
    put = black76_price(F, K, T, sigma, "PE")
    assert forward_from_parity(call, put, K, T) == pytest.approx(F, rel=1e-9)


@pytest.mark.parametrize("option_type", ["CE", "PE"])
@pytest.mark.parametrize("sigma", [0.08, 0.12, 0.25])
def test_implied_vol_round_trips(option_type, sigma):
    F, K, T = 24_000.0, 24_200.0, 0.05
    price = black76_price(F, K, T, sigma, option_type)
    result = implied_vol(price, F, K, T, option_type)
    assert result.ok
    assert result.sigma == pytest.approx(sigma, rel=1e-6)


def test_solver_refuses_the_specific_unidentified_case():
    """The 9.3%-for-8.0% failure this solver exists to prevent.

    3% out of the money with under three days left: the quote is worth 0.027,
    a whole vol point moves it by 0.037, and the exchange tick is 0.05.  The
    price simply does not contain sigma, and any number a root-finder returns
    here is noise wearing a decimal point.
    """
    F, K, T, sigma = 24_000.0, 24_720.0, 0.008, 0.10
    price = black76_price(F, K, T, sigma, "CE")
    result = implied_vol(price, F, K, T, "CE")

    assert result.status == "not_identified"
    assert result.sigma is None, "a refused inversion must not expose a sigma"
    assert result.sigma_raw is not None, "the raw solve stays available for diagnostics"
    assert result.vega_per_vol_point < 0.05


@pytest.mark.parametrize("T", [0.002, 0.005, 0.008])
@pytest.mark.parametrize("moneyness", [1.03, 1.05, 1.08, 1.12])
def test_solver_never_returns_a_sigma_for_a_dead_wing(T, moneyness):
    """The invariant, swept: no deep near-expiry wing yields a usable sigma.

    Which refusal fires (`no_time_value`, `not_identified` or `bad_input` when
    the price underflows entirely) depends on where in the wing the quote sits.
    What must never happen is `ok`.
    """
    F, sigma = 24_000.0, 0.10
    K = F * moneyness
    result = implied_vol(black76_price(F, K, T, sigma, "CE"), F, K, T, "CE")
    assert not result.ok
    assert result.sigma is None


def test_solver_refuses_prices_outside_the_no_arb_band():
    F, K, T = 24_000.0, 23_000.0, 0.05
    below_intrinsic = implied_vol(500.0, F, K, T, "CE")
    assert below_intrinsic.status == "arb_violation"
    above_ceiling = implied_vol(F * 1.5, F, K, T, "CE")
    assert above_ceiling.status == "arb_violation"


def test_greeks_have_expected_signs_and_magnitudes():
    F, K, T, sigma = 24_000.0, 24_000.0, 0.05, 0.12
    call = black76_greeks(F, K, T, sigma, "CE")
    put = black76_greeks(F, K, T, sigma, "PE")

    assert 0.4 < call.delta < 0.6
    assert -0.6 < put.delta < -0.4
    assert call.delta - put.delta == pytest.approx(1.0, abs=1e-6)
    assert call.vega > 0 and call.gamma > 0
    assert call.theta < 0 and put.theta < 0, "a long option decays"
    assert call.vega == pytest.approx(put.vega, rel=1e-6)


def test_vega_matches_a_numerical_derivative():
    F, K, T, sigma = 24_000.0, 24_500.0, 0.08, 0.14
    analytic = black76_greeks(F, K, T, sigma, "CE").vega
    h = 1e-5
    numeric = (
        black76_price(F, K, T, sigma + h, "CE") - black76_price(F, K, T, sigma - h, "CE")
    ) / (2 * h)
    assert analytic == pytest.approx(numeric, rel=1e-4)


# ── SVI ─────────────────────────────────────────────────────────────────────


def _synthetic_smile(params: SVIParams, T: float, n: int = 25):
    k = np.linspace(-0.12, 0.12, n)
    w = np.asarray(params.total_variance(k), dtype=float)
    return k, np.sqrt(w / T)


def test_svi_recovers_a_synthetic_smile():
    truth = SVIParams(a=0.0012, b=0.09, rho=-0.55, m=0.005, s=0.035)
    T = 0.06
    k, iv = _synthetic_smile(truth, T)
    fit = fit_svi_slice(k, iv, T)
    assert fit.ok, fit.reason
    assert fit.rmse_vol < 1e-3, "a noiseless smile should fit to well under a vol point"
    fitted = fit.params.implied_vol(k, T)
    assert np.allclose(fitted, iv, atol=5e-4)


def test_svi_refuses_a_slice_with_too_few_points():
    fit = fit_svi_slice([-0.02, 0.0, 0.02], [0.11, 0.10, 0.11], 0.05)
    assert not fit.ok
    assert fit.status == "insufficient"


def test_butterfly_check_flags_a_negative_density():
    """g(k) < 0 means the slice prices a butterfly negatively."""
    arb_free = SVIParams(a=0.0012, b=0.09, rho=-0.55, m=0.005, s=0.035)
    grid = np.linspace(-0.2, 0.2, 101)
    assert np.min(gatheral_g(arb_free, grid)) >= 0

    # A steep, sharply-curved wing with almost no smoothing violates it.
    pathological = SVIParams(a=0.0005, b=1.6, rho=-0.98, m=0.0, s=0.002)
    assert np.min(gatheral_g(pathological, grid)) < 0


def test_calendar_check_catches_inverted_total_variance():
    near = SVIParams(a=0.004, b=0.05, rho=-0.4, m=0.0, s=0.03)
    far = SVIParams(a=0.001, b=0.05, rho=-0.4, m=0.0, s=0.03)  # LOWER total variance
    report = calendar_arbitrage([(0.02, near), (0.10, far)])
    assert report["checked"] and not report["ok"]
    assert report["worst"]["deficit"] < 0

    ok = calendar_arbitrage([(0.02, far), (0.10, near)])
    assert ok["ok"]


# ── realised volatility ─────────────────────────────────────────────────────


def _flat_bars(n: int, level: float = 24_000.0) -> list[DailyBar]:
    return [
        DailyBar(day=None, open=level, high=level * 1.001, low=level * 0.999, close=level)
        for _ in range(n)
    ]


def test_estimators_agree_on_a_constructed_series():
    rng = np.random.default_rng(7)
    daily_sigma = 0.10 / math.sqrt(252)
    level, bars = 24_000.0, []
    for _ in range(400):
        ret = rng.normal(0.0, daily_sigma)
        close = level * math.exp(ret)
        high, low = max(level, close) * 1.0015, min(level, close) * 0.9985
        bars.append(DailyBar(day=None, open=level, high=high, low=low, close=close))
        level = close

    cc = close_to_close_rv(bars, 250)
    gk = garman_klass_rv(bars, 250)
    pk = parkinson_rv(bars, 250)
    assert cc == pytest.approx(0.10, abs=0.02)
    for value in (gk, pk):
        assert value == pytest.approx(cc, abs=0.05)


def test_close_to_close_skips_returns_that_span_a_dropped_day():
    """The cleaning step must not manufacture a one-day return out of a gap."""
    level = 24_000.0
    bars = [DailyBar(day=None, open=level, high=level, low=level, close=level, contiguous=True)]
    for i in range(1, 40):
        close = level * (1.10 if i == 20 else 1.0)  # a 10% jump across the gap
        bars.append(
            DailyBar(
                day=None, open=level, high=max(level, close), low=min(level, close),
                close=close, contiguous=(i != 20),
            )
        )
        level = close

    honest = close_to_close_rv(bars, 30)
    naive = close_to_close_rv(
        [DailyBar(b.day, b.open, b.high, b.low, b.close, True) for b in bars], 30
    )
    assert honest is not None and naive is not None
    assert honest < naive / 2, "the gap return must not be counted as one day"


def test_close_to_close_refuses_below_coverage():
    bars = [
        DailyBar(day=None, open=1.0, high=1.0, low=1.0, close=1.0, contiguous=(i % 2 == 0))
        for i in range(40)
    ]
    assert close_to_close_rv(bars, 30, min_coverage=0.8) is None


def test_variance_risk_premium_signs():
    bars = _flat_bars(200)
    rich = variance_risk_premium("NIFTY", _now(), 0.15, 30, bars, estimator="parkinson")
    assert rich.spread is not None and rich.spread > 0
    assert rich.ratio > 1.0

    cheap = variance_risk_premium("NIFTY", _now(), 0.001, 30, bars, estimator="parkinson")
    assert cheap.spread < 0


def test_cone_reports_insufficient_rather_than_guessing():
    cone = build_cone("NIFTY", _flat_bars(20))
    assert cone.status == "insufficient"


def _now():
    from datetime import datetime, timezone

    return datetime(2026, 9, 4, 8, 45, tzinfo=timezone.utc)
