"""Market Profile feature tests — normalized auction geometry."""
from __future__ import annotations
from datetime import datetime
import pytest
from nomad_sniper.features.market_profile import build_mp_features
from nomad_sniper.profiles.profile import build_profile


def test_profile_poc_in_range(synthetic_bars):
    one_day = synthetic_bars.loc["2025-01-08"]
    p = build_profile(one_day)
    assert p.low <= p.poc <= p.high
    assert p.val <= p.poc <= p.vah
    assert p.profile_width >= 0
    assert p.total_volume > 0


def test_mp_features_are_normalized(synthetic_bars, synthetic_decision_time):
    snap = build_mp_features(synthetic_decision_time, synthetic_bars)
    row = {f.name: f.value for f in snap.features}
    # core normalized distances present
    for k in ("u_dist_prev_poc_atr", "u_dist_dev_poc_atr", "u_location_vs_prev_value"):
        assert k in row
    # no raw price levels leak through
    assert "prev_poc" not in row and "current_price" not in row
    # numeric features must be small-magnitude (ATR/ratio units), never price-scale
    for k, v in row.items():
        if isinstance(v, (int, float)) and v is not None:
            assert abs(v) < 500, f"{k}={v} looks price-scale, not normalized"


def test_prev_levels_use_only_yesterday(synthetic_bars, synthetic_decision_time):
    snap = build_mp_features(synthetic_decision_time, synthetic_bars)
    feat = next(f for f in snap.features if f.name == "u_prev_value_width_atr")
    # available at yesterday's close (2025-01-07 15:30) — strictly before decision
    assert feat.data_available_at.date().isoformat() == "2025-01-07"
    assert feat.data_available_at <= synthetic_decision_time
