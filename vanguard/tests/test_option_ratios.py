from datetime import date

import numpy as np
import pandas as pd
import pytest

from features.m_option_ratios import ratios_for_snapshot


def _chain(wings=True):
    rows = []
    specs = [
        (90, "CE", 12, 0.75, 0.21), (90, "PE", 2, -0.25, 0.27),
        (100, "CE", 6, 0.50, 0.22), (100, "PE", 5, -0.50, 0.24),
        (110, "CE", 2, 0.25 if wings else 0.44, 0.25),
        (110, "PE", 12, -0.75, 0.23),
    ]
    for strike, side, premium, delta, iv in specs:
        rows.append({
            "ts": pd.Timestamp("2026-08-28T09:45:00Z"), "dt": date(2026, 8, 28),
            "symbol": "TEST", "expiry": date(2026, 9, 29), "strike": float(strike),
            "option_type": side, "premium": float(premium), "spot": 100.0,
            "volume": 100, "delta": delta, "iv": iv, "quality": "good",
        })
    return pd.DataFrame(rows)


def test_ratios_use_same_expiry_extrinsic_values_and_true_25d_wings():
    row = ratios_for_snapshot(_chain())
    assert row["atm_strike"] == 100
    assert row["straddle_to_spot"] == pytest.approx(0.11)
    assert row["normalized_straddle"] > row["straddle_to_spot"]
    assert row["atm_put_call_premium_ratio"] == pytest.approx(5 / 6)
    assert row["call_otm_atm_extrinsic_ratio"] == pytest.approx(2 / 6)
    assert row["put_otm_atm_extrinsic_ratio"] == pytest.approx(2 / 5)
    assert row["call_wing_iv_ratio"] == pytest.approx(0.25 / 0.23)
    assert row["put_wing_iv_ratio"] == pytest.approx(0.27 / 0.23)
    assert row["strangle_straddle_ratio"] == pytest.approx(4 / 11)
    assert row["wing_valid"] is True
    assert np.isfinite(row["premium_pcr"])


def test_wing_ratios_are_missing_when_nearest_call_is_not_25_delta():
    row = ratios_for_snapshot(_chain(wings=False))
    assert row["wing_valid"] is False
    assert row["call_wing_iv_ratio"] is None
    assert row["put_wing_iv_ratio"] == pytest.approx(0.27 / 0.23)
    assert row["strangle_straddle_ratio"] is None
