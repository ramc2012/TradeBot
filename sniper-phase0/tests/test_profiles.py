"""Profile primitive + open-type + day-type tests."""
from __future__ import annotations
from datetime import datetime, time, timedelta
import numpy as np, pandas as pd
import pytest
from nomad_sniper.profiles.profile import build_profile
from nomad_sniper.profiles.open_type import classify_open_type
from nomad_sniper.utils.timeutil import IST


def _session(prices, start_min=0, penetrate=True):
    start = IST.localize(datetime(2025, 1, 8, 9, 15)) + timedelta(minutes=start_min)
    rows = []
    for p in prices:
        lo = p - 1 if penetrate else p
        hi = p + 1 if penetrate else p
        rows.append({"open": p, "high": hi, "low": lo, "close": p, "volume": 1000})
    idx = pd.date_range(start, periods=len(prices), freq="1min", tz=IST)
    return pd.DataFrame(rows, index=idx)


def test_hvn_detected_at_volume_peak():
    # build a session where most volume concentrates around 100
    start = IST.localize(datetime(2025, 1, 8, 9, 15))
    rows = []
    for i in range(120):
        px = 100 + (np.sin(i / 5))  # oscillate around 100
        vol = 5000 if abs(px - 100) < 0.3 else 800
        rows.append({"open": px, "high": px + 0.2, "low": px - 0.2, "close": px, "volume": vol})
    bars = pd.DataFrame(rows, index=pd.date_range(start, periods=120, freq="1min", tz=IST))
    p = build_profile(bars, tick_size=0.1)
    assert p.hvn_prices, "expected at least one HVN"
    assert abs(p.poc - 100) < 1.0


def test_open_drive_one_sided():
    # price opens and only goes up, never back through open
    ot = classify_open_type(_session([100, 102, 104, 106, 108, 110], penetrate=False))
    assert ot["open_drive"] == 1
    assert ot["open_type_confidence"] > 0.5


def test_open_auction_two_way():
    # rotate around the open with no conviction
    ot = classify_open_type(_session([100, 101, 99, 100, 101, 99, 100]))
    assert ot["open_auction"] == 1


def test_day_type_scores_sum_sensible(synthetic_bars):
    from nomad_sniper.profiles.day_type import day_type_scores
    dev = build_profile(synthetic_bars.loc["2025-01-08"], session_date=
                        synthetic_bars.loc["2025-01-08"].index[0].date())
    s = day_type_scores(dev)
    assert 0 <= s["trend_day_score"] <= 1
    assert 0 <= s["balanced_day_score"] <= 1
    assert 0 <= s["neutral_day_score"] <= 1
