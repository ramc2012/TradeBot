"""Offline tests for the IV surface and the market sentiment composite."""
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from features.m_iv_surface import (  # noqa: E402
    SKEW_DELTA_TOLERANCE,
    _risk_reversal,
    build as build_surface,
    surface_for_session,
)
from features.m_sentiment import (  # noqa: E402
    MIN_FAMILIES,
    build as build_sentiment,
    positioning_for_session,
)


def _contract(strike, option_type, iv, delta, spot=1000.0, expiry=date(2026, 9, 25)):
    return {"dt": date(2026, 8, 26), "symbol": "TEST", "expiry": expiry,
            "strike": strike, "option_type": option_type, "iv": iv, "delta": delta,
            "spot": spot, "oi": 10_000, "volume": 500}


# ── the 25-delta refusal ───────────────────────────────────────────────────

def test_skew_is_refused_when_the_chain_does_not_reach_the_wings():
    """THE FINDING THIS ENFORCES. The collected chain has never carried enough
    strikes to hold a 25-delta contract — breadth peaked at 6.6 contracts per
    symbol per day and is now 1.2. m2_flow takes the NEAREST delta with no
    tolerance, so with 3-6 near-money strikes its "25-delta skew" was a
    near-ATM call-minus-put — the same quantity as its IVS ingredient, counted
    twice, for 55% of the composite between them."""
    chain = pd.DataFrame([
        _contract(990, "CE", 0.30, 0.55), _contract(990, "PE", 0.32, -0.45),
        _contract(1010, "CE", 0.29, 0.45), _contract(1010, "PE", 0.33, -0.55),
    ])
    out = _risk_reversal(chain)
    assert np.isnan(out["skew_25d"])
    assert "25-delta" in out["skew_reason"]


def test_skew_is_computed_when_both_wings_genuinely_exist():
    chain = pd.DataFrame([
        _contract(1150, "CE", 0.28, 0.25), _contract(850, "PE", 0.36, -0.25),
    ])
    out = _risk_reversal(chain)
    assert out["skew_25d"] == pytest.approx(0.36 - 0.28)
    assert out["skew_reason"] is None


def test_skew_accepts_a_contract_just_inside_the_tolerance_and_rejects_just_outside():
    inside = SKEW_DELTA_TOLERANCE - 0.01
    outside = SKEW_DELTA_TOLERANCE + 0.01
    ok = _risk_reversal(pd.DataFrame([
        _contract(1150, "CE", 0.28, 0.25 + inside), _contract(850, "PE", 0.36, -0.25)]))
    assert not np.isnan(ok["skew_25d"])
    bad = _risk_reversal(pd.DataFrame([
        _contract(1150, "CE", 0.28, 0.25 + outside), _contract(850, "PE", 0.36, -0.25)]))
    assert np.isnan(bad["skew_25d"])


def test_a_missing_wing_is_reported_differently_from_a_too_narrow_chain():
    """Different causes, different fixes: one needs the collector to widen, the
    other needs the contract to have traded at all."""
    out = _risk_reversal(pd.DataFrame([_contract(1150, "CE", 0.28, 0.25)]))
    assert "one wing" in out["skew_reason"]


# ── IVS ────────────────────────────────────────────────────────────────────

def test_ivs_is_the_near_atm_call_minus_put_spread():
    chain = pd.DataFrame([
        _contract(1000, "CE", 0.30, 0.52), _contract(1000, "PE", 0.26, -0.48),
    ])
    out = surface_for_session(chain)
    assert out["ivs"] == pytest.approx(0.04)
    assert out["atm_strike"] == 1000.0


def test_ivs_needs_both_sides_and_is_null_with_only_calls():
    """A one-sided 'spread' is a level. Reporting it would put an IV level into
    a column every downstream reader treats as a difference."""
    out = surface_for_session(pd.DataFrame([_contract(1000, "CE", 0.30, 0.52)]))
    assert np.isnan(out["ivs"])
    assert not np.isnan(out["atm_iv"])


def test_the_front_series_is_the_nearest_expiry_present():
    chain = pd.DataFrame([
        _contract(1000, "CE", 0.30, 0.52, expiry=date(2026, 10, 30)),
        _contract(1000, "CE", 0.34, 0.52, expiry=date(2026, 9, 25)),
        _contract(1000, "PE", 0.32, -0.48, expiry=date(2026, 9, 25)),
    ])
    out = surface_for_session(chain)
    assert out["expiry"] == date(2026, 9, 25)
    assert out["atm_iv"] == pytest.approx(0.33)     # mean of the two Sept legs


def test_breadth_is_recorded_alongside_every_row():
    """n_strikes and delta_span travel with the number so nobody has to guess
    how much chain is behind it."""
    chain = pd.DataFrame([
        _contract(950, "CE", 0.31, 0.70), _contract(1000, "CE", 0.30, 0.52),
        _contract(1050, "PE", 0.33, -0.30),
    ])
    out = surface_for_session(chain)
    assert out["n_strikes"] == 3
    assert out["delta_span"] == pytest.approx(1.0)


def test_iv_percentile_is_null_when_the_current_value_is_missing():
    """The trap m2_flow's rolling percentile fell into: comparing against NaN
    is False everywhere, so the mean of an all-False array returns 0.0 — the
    most extreme possible reading, fabricated from no data at all."""
    rows = []
    for i in range(20):
        iv = np.nan if i == 19 else 0.2 + i * 0.001
        rows.append({"dt": date(2026, 8, 1) + pd.Timedelta(days=i), "symbol": "TEST",
                     "expiry": date(2026, 9, 25), "strike": 1000.0,
                     "option_type": "CE", "iv": iv, "delta": 0.5, "spot": 1000.0,
                     "oi": 1, "volume": 1})
    frame = build_surface(pd.DataFrame(rows)).sort_values("dt")
    assert np.isnan(frame["iv_percentile"].iloc[-1])
    assert not np.isnan(frame["iv_percentile"].iloc[-2])


# ── participant positioning ────────────────────────────────────────────────

def _participant(participant, bucket, long_c, short_c, dt=date(2026, 8, 26)):
    return {"dt": dt, "participant": participant, "bucket": bucket,
            "long_contracts": long_c, "short_contracts": short_c}


def test_index_option_net_treats_short_puts_as_bullish():
    """Long calls and SHORT puts are both bullish. Summing the legs blindly
    nets one bullish position against the other and reports neutrality."""
    frame = pd.DataFrame([
        _participant("FII", "opt_index_call", 100, 0),
        _participant("FII", "opt_index_put", 0, 100),
    ])
    out = positioning_for_session(frame)
    assert out["fii_opt_index_net"] == 200      # +100 calls, -(-100) puts


def test_futures_net_is_long_minus_short():
    frame = pd.DataFrame([_participant("FII", "fut_index", 25_651, 211_711)])
    out = positioning_for_session(frame)
    assert out["fii_fut_index_net"] == 25_651 - 211_711
    assert out["fii_index_long_ratio"] == pytest.approx(25_651 / (25_651 + 211_711))


def test_an_absent_participant_yields_none_not_zero():
    out = positioning_for_session(pd.DataFrame(columns=[
        "dt", "participant", "bucket", "long_contracts", "short_contracts"]))
    assert out["fii_fut_index_net"] is None
    assert out["fii_index_long_ratio"] is None


# ── the composite ──────────────────────────────────────────────────────────

def _sentiment_inputs(sessions, families=5):
    participants, oi, surface = [], [], []
    for i, dt in enumerate(sessions):
        participants.append(_participant("FII", "fut_index", 100_000 + i * 20_000, 200_000, dt=dt))
        for symbol in ("A", "B", "C"):
            oi.append({"dt": dt, "symbol": symbol, "ce_oi": 1000, "pe_oi": 800 + i * 50,
                       "oi_state": "long_buildup", "d_price_pct": 1.0, "close": 100.0,
                       "ret_20d": 5.0})
        if families >= 4:
            surface.append({"dt": dt, "symbol": "NIFTY", "atm_iv": 0.12 - i * 0.002,
                            "iv_percentile": 0.5})
    surface_frame = (pd.DataFrame(surface) if surface
                     else pd.DataFrame(columns=["dt", "symbol", "atm_iv", "iv_percentile"]))
    return (pd.DataFrame(participants), pd.DataFrame(oi), surface_frame,
            pd.DataFrame(columns=["dt", "ce_volume", "pe_volume"]))


def test_a_composite_below_the_family_minimum_is_suppressed():
    """MEASURED FAILURE. On the first run 2026-08-27 had only the options
    family present — the NSE spot feed is an overnight batch so the session had
    no price or IV — and renormalising the weights over that one family
    reported +100.0. One extreme reading is not a composite."""
    sessions = [date(2026, 8, 25), date(2026, 8, 26)]
    participants, oi, surface, volume = _sentiment_inputs(sessions, families=2)
    oi = oi.drop(columns=["d_price_pct", "ret_20d", "oi_state"])
    oi["d_price_pct"] = np.nan
    oi["ret_20d"] = np.nan
    oi["oi_state"] = None
    frame = build_sentiment(participants, oi, surface, volume)
    last = frame.iloc[-1]
    assert last["sentiment_components"]["n_families"] < MIN_FAMILIES
    assert last["sentiment_score"] is None
    assert last["sentiment_components"]["suppressed"] is True


def test_the_readings_are_kept_even_when_the_composite_is_suppressed():
    """The parts are measurements in their own right; discarding them because
    the blend could not be formed throws away data that is perfectly fine."""
    sessions = [date(2026, 8, 25), date(2026, 8, 26)]
    participants, oi, surface, volume = _sentiment_inputs(sessions, families=2)
    oi["d_price_pct"] = np.nan
    oi["ret_20d"] = np.nan
    oi["oi_state"] = None
    frame = build_sentiment(participants, oi, surface, volume)
    assert frame.iloc[-1]["sentiment_components"]["parts"]


def test_a_full_session_scores_and_records_every_component():
    sessions = [date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26)]
    frame = build_sentiment(*_sentiment_inputs(sessions))
    last = frame.iloc[-1]
    assert last["sentiment_score"] is not None
    assert last["sentiment_components"]["n_families"] >= MIN_FAMILIES
    assert -100.0 <= last["sentiment_score"] <= 100.0
    assert set(last["sentiment_components"]["weights_used"]) == set(
        last["sentiment_components"]["parts"])


def test_session_changes_skip_non_trading_gaps():
    """MEASURED FAILURE. A weekend enters the frame as an empty row, and a
    plain .diff() differences the next real session against it. 2026-08-24 had
    a real FII net, PCR and NIFTY IV and still reported two families because
    all three of its CHANGE columns had been differenced against a Saturday."""
    sessions = [date(2026, 8, 21), date(2026, 8, 22), date(2026, 8, 24)]
    participants, oi, surface, volume = _sentiment_inputs(sessions)
    # strip the Saturday to an empty publication, as the real feeds do
    participants = participants[participants["dt"] != date(2026, 8, 22)]
    oi = oi[oi["dt"] != date(2026, 8, 22)]
    surface = surface[surface["dt"] != date(2026, 8, 22)]
    frame = build_sentiment(participants, oi, surface, volume).set_index("dt")
    assert not pd.isna(frame.loc[date(2026, 8, 24), "d_fii_fut_index_net"])
    assert frame.loc[date(2026, 8, 24), "sentiment_score"] is not None


def test_a_rising_put_call_ratio_reads_bearish():
    """Sign discipline: positive always means bullish everywhere in this table,
    so a defensive rise in PCR must enter negative."""
    sessions = [date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26)]
    frame = build_sentiment(*_sentiment_inputs(sessions))
    # pe_oi climbs each session in the fixture, so PCR rises
    assert frame["d_market_oi_pcr"].iloc[-1] > 0
    assert frame.iloc[-1]["sentiment_components"]["parts"]["options"] < 0
