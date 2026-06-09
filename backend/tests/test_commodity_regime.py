"""Tests for the commodity 30-min regime gate (MP+OF redesign, 2026-06-09)."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from paper_engine.commodity_mp_signal import (
    _DIRECTIONAL_TRIGGERS,
    _MEAN_REVERT_TRIGGERS,
    _resample_30m,
    classify_htf_regime,
)


def _bars(fn, n=300):
    base = datetime(2026, 6, 9, 9, 0)
    return [
        {"time": (base + timedelta(minutes=i)).isoformat(),
         "open": fn(i), "high": fn(i) + 0.5, "low": fn(i) - 0.5, "close": fn(i)}
        for i in range(n)
    ]


def test_resample_30m_buckets():
    bars = _bars(lambda i: 100.0, n=300)
    assert len(_resample_30m(bars)) == 10  # 300 1-min → 10 30-min buckets


def test_regime_trend_up():
    regime, detail = classify_htf_regime(_bars(lambda i: 100 + i * 0.1), cvd_session=5000)
    assert regime == "TREND_UP"
    assert detail["efficiency"] >= 0.9


def test_regime_trend_down():
    assert classify_htf_regime(_bars(lambda i: 200 - i * 0.1), cvd_session=-5000)[0] == "TREND_DOWN"


def test_regime_balance_when_oscillating():
    # Price wanders (low efficiency) → BALANCE even if the endpoints drift.
    assert classify_htf_regime(_bars(lambda i: 100 + math.sin(i / 20) * 3), cvd_session=50)[0] == "BALANCE"


def test_regime_unknown_when_too_short():
    # < 3 30-min bars (early session) → UNKNOWN (gate stays permissive).
    assert classify_htf_regime(_bars(lambda i: 100 + i * 0.1, n=40))[0] == "UNKNOWN"


def test_trend_requires_cvd_agreement():
    # Price trends up but order flow disagrees (CVD < 0) → not a trend.
    assert classify_htf_regime(_bars(lambda i: 100 + i * 0.1), cvd_session=-5000)[0] == "BALANCE"


def test_trigger_categories_partition_the_five_triggers():
    assert _DIRECTIONAL_TRIGGERS.isdisjoint(_MEAN_REVERT_TRIGGERS)
    assert (_DIRECTIONAL_TRIGGERS | _MEAN_REVERT_TRIGGERS) == {
        "open_drive", "ib_break", "va_migration", "failed_auction", "lvn_fade",
    }
