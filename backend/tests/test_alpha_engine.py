"""Unit tests for the CBE alpha engine — pure-compute helpers + scorer."""
from __future__ import annotations

import math

import pytest

from cbe_scanner.alpha_engine import (
    AlphaEngineConfig,
    LayerWeights,
    composite_score,
    _atr_expansion,
    _bias_from_signals,
    _bucket_score,
    _ema,
    _normalize_rs_pct,
    _trend_score,
    _volume_score,
)


# ---------------------------------------------------------------------------
# Composite scorer
# ---------------------------------------------------------------------------
class TestCompositeScore:
    def test_all_components_neutral_yields_50(self):
        result = composite_score(
            asset_score=50.0,
            sector_rs_pct=0.0,
            stock_rs_pct=0.0,
            trend_score=0.5,
            atr_expansion=1.0,
            volume_score=0.5,
            oi_score=0.0,
            iv_score=0.0,
            weights=LayerWeights(),
        )
        # Every component clamps to 50 in the neutral case.
        assert result["score"] == 50.0

    def test_all_components_max_yields_100(self):
        # Saturate every component.
        result = composite_score(
            asset_score=100.0,
            sector_rs_pct=50.0,   # → normalized 100
            stock_rs_pct=50.0,    # → normalized 100
            trend_score=1.0,
            atr_expansion=2.0,    # → MP proxy 150 → clamped 100
            volume_score=1.0,     # → OF proxy 100
            oi_score=1.0,
            iv_score=1.0,
            weights=LayerWeights(),
        )
        assert result["score"] == 100.0

    def test_asset_weighted_zero_drags_score(self):
        result = composite_score(
            asset_score=0.0,
            sector_rs_pct=50.0,
            stock_rs_pct=50.0,
            trend_score=1.0,
            atr_expansion=2.0,
            volume_score=1.0,
            oi_score=1.0,
            iv_score=1.0,
            weights=LayerWeights(),
        )
        # 4 components at 100, 1 at 0 with equal weights → 80
        assert result["score"] == 80.0

    def test_gate_at_80_admits_balanced_strong_signal(self):
        # All 5 components at 80 → composite 80 → exactly on the gate.
        result = composite_score(
            asset_score=80.0,
            sector_rs_pct=12.0,
            stock_rs_pct=12.0,
            trend_score=0.8,
            atr_expansion=1.3,
            volume_score=0.8,
            oi_score=0.6,
            iv_score=0.5,
            weights=LayerWeights(),
        )
        # All non-trivial; should hover near the threshold without exceeding 100.
        assert 70.0 <= result["score"] <= 90.0

    def test_components_dict_includes_breakdown(self):
        result = composite_score(
            asset_score=100.0,
            sector_rs_pct=10.0,
            stock_rs_pct=10.0,
            trend_score=0.6,
            atr_expansion=1.1,
            volume_score=0.55,
            oi_score=0.3,
            iv_score=0.2,
            weights=LayerWeights(),
        )
        c = result["components"]
        assert "asset" in c and "sector" in c and "stock" in c
        assert "market_profile_proxy" in c and "order_flow_proxy" in c
        assert "trend_score" in c and "atr_expansion" in c

    def test_weights_sum_validation(self):
        # User can re-weight; total drives normalization.
        custom = LayerWeights(asset=30, sector=30, stock=20, market_profile=10, order_flow=10)
        result = composite_score(
            asset_score=100.0,
            sector_rs_pct=20.0,
            stock_rs_pct=20.0,
            trend_score=0.5,
            atr_expansion=1.0,
            volume_score=0.5,
            oi_score=0.0,
            iv_score=0.0,
            weights=custom,
        )
        # Asset+Sector are big (100+100); Stock big (100); MP/OF neutral (50)
        # weighted: (30*100 + 30*100 + 20*100 + 10*50 + 10*50) / 100 = 90
        assert result["score"] == 90.0

    def test_zero_weights_safe(self):
        custom = LayerWeights(asset=0, sector=0, stock=0, market_profile=0, order_flow=0)
        result = composite_score(
            asset_score=100.0, sector_rs_pct=50.0, stock_rs_pct=50.0,
            trend_score=1.0, atr_expansion=2.0, volume_score=1.0,
            oi_score=1.0, iv_score=1.0,
            weights=custom,
        )
        assert result["score"] == 0.0


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------
class TestNormalizers:
    def test_normalize_rs_pct_zero_is_50(self):
        assert _normalize_rs_pct(0.0) == 50.0

    def test_normalize_rs_pct_positive(self):
        assert _normalize_rs_pct(10.0) == 75.0  # 50 + 10*2.5

    def test_normalize_rs_pct_negative_clamps(self):
        assert _normalize_rs_pct(-50.0) == 0.0  # clamped

    def test_normalize_rs_pct_positive_clamps(self):
        assert _normalize_rs_pct(100.0) == 100.0


class TestBucketScore:
    def test_below_all_thresholds_is_zero(self):
        assert _bucket_score(100, [500, 2000, 10000, 50000]) == 0.0

    def test_above_top_threshold_is_one(self):
        assert _bucket_score(100_000, [500, 2000, 10000, 50000]) == 1.0

    def test_progressive_bucketing(self):
        # 5000 → above 500 and 2000, below 10000/50000 → 2/4 = 0.5
        assert _bucket_score(5000, [500, 2000, 10000, 50000]) == 0.5


# ---------------------------------------------------------------------------
# Technical helpers
# ---------------------------------------------------------------------------
class TestTechnicals:
    def test_ema_returns_same_length(self):
        series = [100.0 + i for i in range(50)]
        ema = _ema(series, 8)
        assert len(ema) == 50

    def test_ema_smooths_upward_trend(self):
        series = [100.0 + i for i in range(50)]
        ema = _ema(series, 8)
        # EMA lags the linear trend
        assert ema[-1] < series[-1]
        assert ema[-1] > series[-10]

    def test_trend_score_uptrend(self):
        closes = [100.0 + i * 0.5 for i in range(60)]
        score = _trend_score(closes)
        assert score > 0.55

    def test_trend_score_downtrend(self):
        closes = [200.0 - i * 0.5 for i in range(60)]
        score = _trend_score(closes)
        assert score < 0.45

    def test_trend_score_flat_is_neutral(self):
        closes = [100.0] * 60
        score = _trend_score(closes)
        assert 0.45 <= score <= 0.55

    def test_atr_expansion_expanding_range(self):
        # First 40 bars stable, last 20 widening — ATR expansion > 1
        closes = [100.0 + math.sin(i / 5) * 0.5 for i in range(40)]
        closes += [100.0 + math.sin(i / 5) * 3.0 for i in range(20)]
        ratio = _atr_expansion(closes)
        assert ratio > 1.0

    def test_atr_expansion_contracting_range(self):
        closes = [100.0 + math.sin(i / 5) * 3.0 for i in range(40)]
        closes += [100.0 + math.sin(i / 5) * 0.5 for i in range(20)]
        ratio = _atr_expansion(closes)
        assert ratio < 1.0

    def test_volume_score_rising(self):
        volumes = [1000.0] * 40 + [3000.0] * 20
        score = _volume_score(volumes)
        assert score > 0.7

    def test_volume_score_falling(self):
        volumes = [3000.0] * 40 + [1000.0] * 20
        score = _volume_score(volumes)
        assert score < 0.3


# ---------------------------------------------------------------------------
# Bias derivation
# ---------------------------------------------------------------------------
class TestBiasDerivation:
    def test_leading_sector_leading_stock_bullish_trend(self):
        row = {"stock_quadrant": "leading", "sector_quadrant": "leading"}
        assert _bias_from_signals(row, trend_score=0.7) == "bullish"

    def test_improving_sector_improving_stock_bullish(self):
        row = {"stock_quadrant": "improving", "sector_quadrant": "improving"}
        assert _bias_from_signals(row, trend_score=0.6) == "bullish"

    def test_lagging_sector_weakening_stock_bearish(self):
        row = {"stock_quadrant": "weakening", "sector_quadrant": "lagging"}
        assert _bias_from_signals(row, trend_score=0.3) == "bearish"

    def test_leading_sector_lagging_stock_neutral(self):
        row = {"stock_quadrant": "lagging", "sector_quadrant": "leading"}
        assert _bias_from_signals(row, trend_score=0.6) == "neutral"

    def test_leading_quadrants_but_flat_trend_neutral(self):
        row = {"stock_quadrant": "leading", "sector_quadrant": "leading"}
        assert _bias_from_signals(row, trend_score=0.5) == "neutral"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def test_default_config_weights_sum_to_100():
    w = LayerWeights()
    assert w.total() == 100.0


def test_alpha_engine_config_defaults():
    cfg = AlphaEngineConfig()
    assert cfg.timeframe == "weekly"
    assert cfg.sectors_to_keep == 4
    assert cfg.stocks_per_sector == 5
    assert cfg.composite_gate == 80.0
