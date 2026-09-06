"""Offline tests for the futures OI ingest + baselines -- no network, no DB.

Exercises the pure functions: candle normalization (the whole reason this
module exists is keeping index 6), the intraday collapse, the rollover guard,
the rolling z / percentile math, and the surge flag edges. The buildup
classifier itself lives in features/m2_flow.classify_oi_state and has its own
truth-table here only as an import-contract check.
"""
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parents[1]))

from features.m2_flow import classify_oi_state  # noqa: E402
from features.m_futures_oi import (  # noqa: E402
    MIN_WINDOW,
    SURGE_Z,
    compute_baselines,
)
from ingest.futures_oi import (  # noqa: E402
    Contract,
    active_fut_contracts,
    normalize_candles,
)


# ---------------------------------------------------------------------------
# normalize_candles -- OI must survive
# ---------------------------------------------------------------------------

def test_normalize_candles_keeps_oi_index_6():
    rows = normalize_candles([
        ["2026-08-28T00:00:00+05:30", 100.0, 105.0, 99.0, 104.0, 5000, 123456],
    ])
    assert rows[0]["oi"] == 123456
    assert rows[0]["ts"] == date(2026, 8, 28)
    assert rows[0]["volume"] == 5000


def test_normalize_candles_tolerates_six_element_rows_and_reverses():
    rows = normalize_candles([
        ["2026-08-29T00:00:00+05:30", 2, 2, 2, 2, 20, 999],
        ["2026-08-28T00:00:00+05:30", 1, 1, 1, 1, 10],  # no OI element
    ])
    # Upstox returns newest-first; normalize reverses to ascending.
    assert [r["ts"].day for r in rows] == [28, 29]
    assert rows[0]["oi"] == 0
    assert rows[1]["oi"] == 999


# ---------------------------------------------------------------------------
# active_fut_contracts -- master parsing (epoch-millis expiry, name matching)
# ---------------------------------------------------------------------------

def test_active_fut_contracts_fronts_sorted_and_expired_dropped():
    # 2026-09-29 ~= 1790640000000 ms; 2026-08-25 (past) ~= 1787616000000 ms
    master = [
        {"instrument_type": "FUT", "underlying_symbol": "ACME",
         "instrument_key": "NSE_FO|1", "expiry": 1790640000000},
        {"instrument_type": "FUT", "underlying_symbol": "ACME",
         "instrument_key": "NSE_FO|2", "expiry": 1787616000000},
        {"instrument_type": "CE", "underlying_symbol": "ACME",
         "instrument_key": "NSE_FO|3", "expiry": 1790640000000},
        {"instrument_type": "FUT", "underlying_symbol": "OTHER",
         "instrument_key": "NSE_FO|4", "expiry": 1790640000000},
    ]
    out = active_fut_contracts(master, ["ACME"], date(2026, 9, 1))
    assert [c.instrument_key for c in out["ACME"]] == ["NSE_FO|1"]


# ---------------------------------------------------------------------------
# compute_baselines -- rollover guard
# ---------------------------------------------------------------------------

def _frame(rows):
    return pd.DataFrame(rows, columns=["symbol", "ts", "expiry", "close", "volume", "oi"])


def test_rollover_suppresses_deltas_and_state():
    aug, sep = date(2026, 8, 27), date(2026, 9, 29)
    frame = _frame([
        ("ACME", date(2026, 8, 25), aug, 100.0, 1000, 50_000),
        ("ACME", date(2026, 8, 26), aug, 102.0, 1100, 55_000),
        ("ACME", date(2026, 8, 27), sep, 103.0, 1200, 20_000),  # roll: OI level jumps
        ("ACME", date(2026, 8, 28), sep, 104.0, 1300, 24_000),
    ])
    out = compute_baselines(frame)
    roll_row = out[out["ts"] == date(2026, 8, 27)].iloc[0]
    assert bool(roll_row["is_rollover"]) is True
    assert pd.isna(roll_row["d_oi"]) and pd.isna(roll_row["d_oi_pct"])
    assert roll_row["oi_state"] is None
    after = out[out["ts"] == date(2026, 8, 28)].iloc[0]
    assert bool(after["is_rollover"]) is False
    assert after["d_oi"] == 4000
    assert after["oi_state"] == "long_buildup"  # price up, OI up


def test_first_row_is_not_a_rollover():
    out = compute_baselines(_frame([
        ("ACME", date(2026, 8, 25), date(2026, 8, 27), 100.0, 1000, 50_000),
    ]))
    assert bool(out.iloc[0]["is_rollover"]) is False


# ---------------------------------------------------------------------------
# z-scores, percentile, surge flag
# ---------------------------------------------------------------------------

def _long_frame(n=80, last_oi_jump=0, last_vol_jump=0):
    expiry = date(2027, 1, 28)
    rows = []
    oi = 100_000
    for i in range(n):
        # Varied but stationary increments: a constant step would make d_oi_pct
        # a monotone-declining series (fixed step on a growing base), whose
        # latest value is always its own outlier.
        oi += 80 + (i % 7) * 40
        vol = 1000 + (i % 5) * 50
        if i == n - 1:
            oi += last_oi_jump
            vol += last_vol_jump
        rows.append(("ACME", date(2026, 1, 1) + pd.Timedelta(days=i).to_pytimedelta(),
                     expiry, 100.0 + i * 0.1, vol, oi))
    return _frame(rows)


def test_zscores_null_until_min_window():
    out = compute_baselines(_long_frame(n=MIN_WINDOW))
    # Row index MIN_WINDOW-1 has only MIN_WINDOW-1 trailing observations.
    assert out["d_oi_pct_z"].notna().sum() == 0


def test_steady_series_scores_near_zero_and_spike_scores_high():
    steady = compute_baselines(_long_frame(n=80))
    assert abs(float(steady.iloc[-1]["d_oi_pct_z"])) < 1.0

    spiked = compute_baselines(_long_frame(n=80, last_oi_jump=50_000, last_vol_jump=9_000))
    last = spiked.iloc[-1]
    assert float(last["d_oi_pct_z"]) > SURGE_Z
    assert float(last["volume_z"]) > SURGE_Z
    assert bool(last["activity_surge"]) is True
    assert float(last["oi_pctile"]) == 1.0  # monotone series: latest OI is the max


def test_surge_needs_both_legs():
    oi_only = compute_baselines(_long_frame(n=80, last_oi_jump=50_000, last_vol_jump=0))
    assert bool(oi_only.iloc[-1]["activity_surge"]) is False


# ---------------------------------------------------------------------------
# classify_oi_state import contract
# ---------------------------------------------------------------------------

def test_classifier_truth_table():
    assert classify_oi_state(10, 1) == "long_buildup"
    assert classify_oi_state(10, -1) == "short_buildup"
    assert classify_oi_state(-10, 1) == "short_covering"
    assert classify_oi_state(-10, -1) == "long_unwind"
    assert classify_oi_state(0, 1) is None
    assert classify_oi_state(None, 1) is None
