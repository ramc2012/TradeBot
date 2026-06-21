"""Guards the net-GEX fix in directional_options.chain_analytics.

The option-chain builder stores `gamma_exposure` as a PER-STRIKE dict
`{strike: sign·gamma·OI·spot}`. The old code did `_safe_float(dict)` which is
always None, so `gex_total` was null for every underlying (NIFTY/BANKNIFTY/
SENSEX) on the analytics panel AND as policy feature 30. Net GEX is the signed
sum across strikes.
"""
from __future__ import annotations

from datetime import datetime, timezone

from directional_options.chain_analytics import (
    _expiry_state,
    _key_levels,
    _net_gex,
    _ntm_volx,
    _options_table_rows,
    _per_strike_exposure,
    _spectrum,
    _straddle_and_sigma,
    _unusual_activity,
    _writer_cash_proxy,
)


def test_net_gex_sums_per_strike_dict_signed():
    # CE positive, PE negative (repo sign convention already baked in).
    gex = {"23400": 1000.0, "23450": -600.0, "23500": 250.5}
    assert _net_gex(gex) == 650.5


def test_net_gex_none_and_empty_are_none():
    assert _net_gex(None) is None
    assert _net_gex({}) is None


def test_net_gex_tolerates_nulls_in_dict():
    assert _net_gex({"a": None, "b": 5.0, "c": "x"}) == 5.0


def test_net_gex_scalar_passthrough():
    # Backward-compatible if a future builder emits a scalar.
    assert _net_gex(42.0) == 42.0
    assert _net_gex("17.5") == 17.5


def test_net_gex_sensex_small_but_nonzero():
    # SENSEX has thin OI → small gammas, but must still produce a number
    # (the bug made it None). Mirrors prod: ~32k net GEX.
    sensex_like = {str(73000 + 100 * i): (0.12 if i % 2 == 0 else -0.08) * 1000 for i in range(20)}
    out = _net_gex(sensex_like)
    assert out is not None and out != 0.0


def test_advanced_key_levels_include_repriced_gamma_profile_and_flip():
    entries = [
        {"strike": 95.0, "option_type": "PE", "oi": 8000, "iv": 0.22, "delta": -0.35, "gamma": 0.018},
        {"strike": 100.0, "option_type": "CE", "oi": 5000, "iv": 0.20, "delta": 0.50, "gamma": 0.020},
        {"strike": 100.0, "option_type": "PE", "oi": 5000, "iv": 0.20, "delta": -0.50, "gamma": 0.020},
        {"strike": 105.0, "option_type": "CE", "oi": 9000, "iv": 0.21, "delta": 0.35, "gamma": 0.017},
    ]

    levels = _key_levels(
        entries,
        100.0,
        lot_size=75,
        tte_years=7 / 365,
        atm_iv=0.20,
        max_pain=100.0,
    )

    assert levels["call_wall"]["strike"] == 105.0
    assert levels["put_wall"]["strike"] == 95.0
    assert levels["gamma_profile"]
    assert levels["gamma_regime"] in {"positive_gamma_pinning", "negative_gamma_trend_amplifying"}
    assert "zero_gamma" in levels


def test_trace_exposure_rows_have_second_order_fields():
    rows = _per_strike_exposure(
        [
            {"strike": 100.0, "option_type": "CE", "oi": 1000, "iv": 0.20, "delta": 0.5, "gamma": 0.02},
            {"strike": 100.0, "option_type": "PE", "oi": 800, "iv": 0.22, "delta": -0.5, "gamma": 0.018},
        ],
        100.0,
        lot_size=75,
        tte_years=10 / 365,
        atm_iv=0.20,
    )

    assert rows and rows[0]["strike"] == 100.0
    assert "net_vanna_exposure" in rows[0]
    assert "net_charm_exposure" in rows[0]
    assert "net_volga_exposure" in rows[0]


def test_unusual_activity_flags_volume_oi_and_large_changes():
    flags = _unusual_activity(
        [
            {
                "strike": 101.0,
                "option_type": "CE",
                "ltp": 12.5,
                "volume": 900,
                "oi": 1000,
                "oi_change": 450,
                "oi_change_pct": 45.0,
                "ltp_change_pct": 13.0,
            }
        ],
        100.0,
    )

    assert flags
    assert flags[0]["flags"] == ["volume_vs_oi", "large_oi_change", "price_dislocation"]


def test_expiry_state_marks_zero_dte():
    state = _expiry_state("2026-06-20", now=datetime(2026, 6, 20, tzinfo=timezone.utc))
    assert state["is_expiry_day"] is True
    assert state["expiry_mode"] == "0dte"


def test_vtrender_style_pressure_layers_are_derived_from_chain_rows():
    entries = [
        {"strike": 100.0, "option_type": "CE", "ltp": 12.0, "prev_close": 10.0, "ltp_change": 2.0, "ltp_change_pct": 20.0, "oi": 1000, "prev_oi": 800, "oi_change": 200, "oi_change_pct": 25.0, "volume": 500, "iv": 0.2, "bid": 11.8, "ask": 12.2},
        {"strike": 100.0, "option_type": "PE", "ltp": 10.0, "prev_close": 11.0, "ltp_change": -1.0, "ltp_change_pct": -9.1, "oi": 900, "prev_oi": 950, "oi_change": -50, "oi_change_pct": -5.3, "volume": 250, "iv": 0.21, "bid": 9.8, "ask": 10.2},
        {"strike": 105.0, "option_type": "CE", "ltp": 6.0, "prev_close": 7.0, "ltp_change": -1.0, "ltp_change_pct": -14.3, "oi": 1500, "prev_oi": 1200, "oi_change": 300, "oi_change_pct": 25.0, "volume": 400, "iv": 0.22},
        {"strike": 95.0, "option_type": "PE", "ltp": 5.5, "prev_close": 5.0, "ltp_change": 0.5, "ltp_change_pct": 10.0, "oi": 1600, "prev_oi": 1200, "oi_change": 400, "oi_change_pct": 33.3, "volume": 450, "iv": 0.23},
    ]

    ntm = _ntm_volx(entries, 100.0, [95.0, 100.0, 105.0])
    spectrum = _spectrum(entries, 100.0)
    straddle, sigma = _straddle_and_sigma(entries, 100.0, 100.0, 0.2, 7 / 365)
    table_rows = _options_table_rows(entries, 100.0)
    cash = _writer_cash_proxy(entries, 75)

    assert ntm["control"] == "call_volume_control"
    assert spectrum["call_wall"]["strike"] == 105.0
    assert spectrum["put_wall"]["strike"] == 95.0
    assert straddle["atm_straddle"] == 22.0
    assert sigma["plus_one_sigma"] is not None
    assert table_rows[1]["ce"]["acceptance"] == "new_business"
    assert cash["dominant_side"] == "call_writers"
