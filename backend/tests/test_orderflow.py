"""Unit tests for analytics/orderflow.py — pure-function primitives."""

from __future__ import annotations

import math

import pytest

from analytics.orderflow import (
    anchored_cvd,
    anchored_vwap,
    bar_cvd,
    bar_signed_volume,
    cvd_agrees_with,
    cvd_divergence,
    hvn_lvn,
    l1_depth_pressure,
    l1_pressure_series,
    orderflow_snapshot,
    volume_node_density,
    vwap_bands,
)


def _candle(o: float, h: float, l: float, c: float, v: float, t: str = "") -> dict:
    return {"open": o, "high": h, "low": l, "close": c, "volume": v, "time": t}


# ─── bar_signed_volume / bar_cvd ────────────────────────────────────────────

def test_bar_signed_volume_uses_close_direction():
    candles = [
        _candle(100, 102, 99, 101, 50),
        _candle(101, 103, 100, 102, 60),  # close up -> +60
        _candle(102, 102, 100, 100, 80),  # close down -> -80
        _candle(100, 100, 100, 100, 40),  # unchanged, body=0 -> 0
    ]
    signed = bar_signed_volume(candles)
    assert signed[1] == 60.0
    assert signed[2] == -80.0
    assert signed[3] == 0.0


def test_bar_signed_volume_first_bar_uses_body():
    # First bar has no prior close, so body sign decides
    candles = [_candle(100, 105, 95, 103, 70)]  # green
    assert bar_signed_volume(candles)[0] == 70.0
    candles = [_candle(100, 105, 95, 98, 70)]  # red
    assert bar_signed_volume(candles)[0] == -70.0


def test_bar_cvd_is_running_sum():
    candles = [
        _candle(100, 100, 100, 100, 10),  # body=0 -> 0
        _candle(100, 102, 100, 101, 50),  # +50
        _candle(101, 101, 99, 100, 30),  # -30
        _candle(100, 105, 100, 104, 40),  # +40
    ]
    cvd = bar_cvd(candles)
    assert cvd == [0.0, 50.0, 20.0, 60.0]


def test_anchored_cvd_resets_at_anchor():
    candles = [_candle(100, 101, 99, 100, 100) for _ in range(5)]
    candles[1] = _candle(100, 102, 99, 102, 100)  # +100
    candles[2] = _candle(102, 103, 101, 103, 80)  # +80
    candles[3] = _candle(103, 103, 100, 100, 60)  # -60
    candles[4] = _candle(100, 101, 99, 100, 0)  # 0 vol
    cvd = anchored_cvd(candles, anchor_index=2)
    # Bars 0 and 1 should be 0; from bar 2 onwards, accumulate
    assert cvd[0] == 0.0
    assert cvd[1] == 0.0
    assert cvd[2] == 80.0  # bar 2's +80
    assert cvd[3] == 20.0  # +80 -60
    assert cvd[4] == 20.0  # +0


def test_anchored_cvd_invalid_anchor_returns_zeros():
    candles = [_candle(100, 101, 99, 100, 50) for _ in range(3)]
    assert anchored_cvd(candles, anchor_index=-1) == [0.0, 0.0, 0.0]
    assert anchored_cvd(candles, anchor_index=10) == [0.0, 0.0, 0.0]


# ─── anchored_vwap / vwap_bands ────────────────────────────────────────────

def test_anchored_vwap_equals_typical_price_for_single_bar():
    candles = [_candle(100, 110, 90, 105, 1000)]
    tp = (110 + 90 + 105) / 3
    assert anchored_vwap(candles, 0)[0] == pytest.approx(tp)


def test_anchored_vwap_weights_by_volume():
    # Bar A typical price 100 at volume 100; bar B typical price 200 at volume 100
    # → equal-weighted VWAP = 150
    a = _candle(100, 100, 100, 100, 100)
    b = _candle(200, 200, 200, 200, 100)
    vwap = anchored_vwap([a, b], 0)
    assert vwap[1] == pytest.approx(150.0)
    # Now skew B's volume up 9x → VWAP shifts toward 200
    b_heavy = _candle(200, 200, 200, 200, 900)
    vwap2 = anchored_vwap([a, b_heavy], 0)
    assert vwap2[1] == pytest.approx((100 * 100 + 200 * 900) / 1000)


def test_anchored_vwap_zero_volume_yields_none():
    candles = [_candle(100, 100, 100, 100, 0)]
    assert anchored_vwap(candles, 0) == [None]


def test_vwap_bands_returns_aligned_lists():
    candles = [_candle(100 + i, 101 + i, 99 + i, 100 + i, 50) for i in range(5)]
    bands = vwap_bands(candles, 0, n_std=1.0)
    assert len(bands["vwap"]) == len(candles)
    assert len(bands["upper"]) == len(candles)
    assert len(bands["lower"]) == len(candles)
    # Upper >= vwap >= lower
    for v, u, l in zip(bands["vwap"], bands["upper"], bands["lower"]):
        if v is None:
            continue
        assert u >= v >= l


# ─── cvd_divergence ────────────────────────────────────────────────────────

def test_cvd_divergence_detects_bullish():
    # Price makes lower low while CVD makes higher low — classic bullish
    # divergence. Construct 25 candles: first 12 trend down, then 12 sideways
    # but with positive volume sign accumulation.
    candles = []
    cvd = []
    # 25 bars total
    for i in range(25):
        # Price path: 100 -> 90 over 12 bars, then back to 95
        if i < 12:
            close = 100 - i
        else:
            close = 88 - (i - 12) * 0.3  # makes a lower low later
        candles.append(_candle(close, close + 0.5, close - 0.5, close, 100))
    # Build CVD that makes a HIGHER low when price made its lower low:
    # easiest: monotonic up-trend in CVD
    for i in range(25):
        cvd.append(float(i * 10))
    div = cvd_divergence(candles, cvd, lookback=24)
    assert div is not None
    assert div.kind == "bullish"


def test_cvd_divergence_detects_bearish():
    candles = []
    cvd = []
    for i in range(25):
        if i < 12:
            close = 100 + i
        else:
            close = 112 + (i - 12) * 0.3  # higher high later
        candles.append(_candle(close, close + 0.5, close - 0.5, close, 100))
    # CVD trending DOWN -> bearish divergence
    for i in range(25):
        cvd.append(float(-i * 10))
    div = cvd_divergence(candles, cvd, lookback=24)
    assert div is not None
    assert div.kind == "bearish"


def test_cvd_divergence_returns_none_when_insufficient_data():
    assert cvd_divergence([], [], lookback=20) is None
    assert cvd_divergence([_candle(1, 1, 1, 1, 1)], [0.0], lookback=20) is None


def test_cvd_agrees_with():
    assert cvd_agrees_with("BUY", [0, 10, 20]) is True
    assert cvd_agrees_with("BUY", [10, 5, 0]) is False
    assert cvd_agrees_with("SELL", [10, 0, -5]) is True
    assert cvd_agrees_with("SELL", [0, 5, 10]) is False
    assert cvd_agrees_with("FLAT", [1, 2, 3]) is False  # invalid signal
    assert cvd_agrees_with("BUY", [5]) is False  # insufficient


# ─── volume_node_density ───────────────────────────────────────────────────

def test_volume_node_density_distributes_across_bins():
    candles = [
        _candle(100, 110, 100, 105, 100),
        _candle(105, 115, 100, 110, 100),
    ]
    hist = volume_node_density(candles, bins=15)
    assert len(hist) == 15
    total_vol = sum(b["volume"] for b in hist)
    # Roughly the original total volume (200), allowing for binning
    assert total_vol > 150
    assert total_vol <= 200 + 1e-6


def test_hvn_lvn_classification():
    histogram = [{"price_low": i, "price_high": i + 1, "volume": float(i)} for i in range(10)]
    nodes = hvn_lvn(histogram)
    # Top 25% have volumes >= 7 (since vols are 0..9, 75th percentile = vol 7)
    assert all(b["volume"] >= 7 for b in nodes["hvn"])
    assert all(b["volume"] <= 2 for b in nodes["lvn"])


def test_volume_node_density_empty():
    assert volume_node_density([], bins=10) == []
    # All bars at same price → no range, returns []
    flat = [_candle(100, 100, 100, 100, 50) for _ in range(3)]
    assert volume_node_density(flat, bins=10) == []


# ─── L1 depth pressure ─────────────────────────────────────────────────────

def test_l1_depth_pressure_bounds():
    assert l1_depth_pressure(100, 0) == pytest.approx(1.0)
    assert l1_depth_pressure(0, 100) == pytest.approx(-1.0)
    assert l1_depth_pressure(50, 50) == pytest.approx(0.0)
    assert l1_depth_pressure(0, 0) == 0.0
    assert -1 <= l1_depth_pressure(70, 30) <= 1


def test_l1_pressure_series():
    ticks = [
        {"bid_qty": 100, "ask_qty": 100},
        {"bid_qty": 200, "ask_qty": 50},
        {"bid_qty": 0, "ask_qty": 100},
    ]
    series = l1_pressure_series(ticks)
    assert series[0] == pytest.approx(0.0)
    assert series[1] > 0.5
    assert series[2] == pytest.approx(-1.0)


# ─── snapshot ──────────────────────────────────────────────────────────────

def test_orderflow_snapshot_returns_expected_keys():
    candles = [_candle(100 + i, 101 + i, 99 + i, 100 + i, 50, t=f"2026-05-26T09:{i:02d}:00+05:30") for i in range(30)]
    snap = orderflow_snapshot(candles, anchor_index=0)
    assert set(snap.keys()) >= {
        "cvd_latest",
        "cvd_anchored_latest",
        "vwap_latest",
        "vwap_upper_latest",
        "vwap_lower_latest",
        "divergence",
        "volume_profile_bins",
        "hvn_count",
        "lvn_count",
        "anchor_index",
    }
    assert snap["vwap_latest"] is not None
    assert snap["volume_profile_bins"] > 0


def test_orderflow_snapshot_handles_empty():
    snap = orderflow_snapshot([])
    assert snap["cvd_latest"] is None
    assert snap["vwap_latest"] is None
    assert snap["hvn_count"] == 0
