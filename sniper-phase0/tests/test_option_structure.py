from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pandas as pd

from nomad_sniper.data.option_bars import ATMOptionSeries
from nomad_sniper.features.option_structure import OPTION_FEATURE_NAMES, build_option_structure_features
from nomad_sniper.utils.timeutil import IST


def _bars(base: float, drift: float = 0.0) -> pd.DataFrame:
    rows = []
    start = IST.localize(datetime.combine(date(2025, 1, 8), time(9, 15)))
    price = base
    for i in range(80):
        ts = start + timedelta(minutes=i)
        o = price
        c = price * (1 + drift)
        rows.append({
            "ts": ts,
            "open": o,
            "high": max(o, c) + 0.5,
            "low": min(o, c) - 0.5,
            "close": c,
            "volume": 1000 + i,
            "oi": 10000 + i,
            "iv": 0.18,
        })
        price = c
    df = pd.DataFrame(rows).set_index("ts")
    df.index = df.index.tz_convert(IST)
    return df


def test_option_features_emit_null_schema_without_data(synthetic_bars, synthetic_decision_time):
    snap = build_option_structure_features(synthetic_decision_time, synthetic_bars, None)
    row = snap.to_row(strict=True)
    for name in OPTION_FEATURE_NAMES:
        assert name in row
        assert row[name] is None


def test_option_features_compute_ratios_when_data_present():
    dt = IST.localize(datetime(2025, 1, 8, 10, 15))
    ce = _bars(100, 0.002)
    pe = _bars(80, -0.001)
    st = ce.copy()
    for col in ("open", "high", "low", "close", "volume", "oi"):
        st[col] = ce[col] + pe[col]
    st["iv"] = 0.18
    atm = ATMOptionSeries("nifty", dt.date(), date(2025, 1, 30), 22000, ce, pe, st)
    u = _bars(22000, 0.00001)
    snap = build_option_structure_features(dt, u, atm)
    row = snap.to_row(strict=True)
    assert row["o_ce_pe_premium_ratio"] is not None
    assert row["o_ce_ret_minus_pe_ret"] > 0
    assert row["o_pcr_volume"] is not None
