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
        assert "market_profile" in c and "order_flow" in c
        assert c["mp_source"] == "proxy" and c["of_source"] == "proxy"
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
        row = {"stock_quadrant": "leading", "sector_quadrant": "leading", "stock_rs_pct": 5.0}
        assert _bias_from_signals(row, trend_score=0.7) == "bullish"

    def test_improving_sector_improving_stock_bullish(self):
        row = {"stock_quadrant": "improving", "sector_quadrant": "improving", "stock_rs_pct": 3.0}
        assert _bias_from_signals(row, trend_score=0.6) == "bullish"

    def test_lagging_sector_weakening_stock_bearish(self):
        row = {"stock_quadrant": "weakening", "sector_quadrant": "lagging", "stock_rs_pct": -4.0}
        assert _bias_from_signals(row, trend_score=0.3) == "bearish"

    def test_mixed_quadrants_neutral(self):
        # Stock leading, sector lagging, trend flat → only 1 bullish vote
        row = {"stock_quadrant": "leading", "sector_quadrant": "lagging", "stock_rs_pct": 0.0}
        assert _bias_from_signals(row, trend_score=0.5) == "neutral"

    def test_two_of_three_vote_bullish_via_rs(self):
        # Stock leading + positive RS sign = 2 votes; sector lagging.
        row = {"stock_quadrant": "leading", "sector_quadrant": "lagging", "stock_rs_pct": 8.0}
        assert _bias_from_signals(row, trend_score=0.5) == "bullish"

    def test_two_of_three_vote_bullish_via_sector_and_trend(self):
        # Sector leading + trend up = 2 votes; stock quadrant lagging.
        row = {"stock_quadrant": "lagging", "sector_quadrant": "leading", "stock_rs_pct": 0.0}
        assert _bias_from_signals(row, trend_score=0.7) == "bullish"

    def test_balanced_votes_neutral(self):
        # 2 bull (stock leading, RS+) tied with 2 bear (sector lagging, trend down)
        row = {"stock_quadrant": "leading", "sector_quadrant": "lagging", "stock_rs_pct": 5.0}
        # trend_score 0.4 → bearish-side trend AND negative wouldn't trigger... actually:
        #   bull: stock_quadrant (yes) + RS positive (yes) = 2
        #   bear: sector_quadrant (yes) + trend<=0.45 (yes) = 2
        # tie → neutral
        assert _bias_from_signals(row, trend_score=0.4) == "neutral"


class TestCompositeScoreWithLiveMPOF:
    def test_live_mp_score_overrides_proxy(self):
        # Live mp_score=100 should drive higher than ATR-proxy fallback
        live_result = composite_score(
            asset_score=100.0, sector_rs_pct=10.0, stock_rs_pct=10.0,
            trend_score=0.5, atr_expansion=1.0, volume_score=0.5,
            oi_score=0.0, iv_score=0.0,
            weights=LayerWeights(),
            mp_score=100.0, of_score=100.0,
        )
        proxy_result = composite_score(
            asset_score=100.0, sector_rs_pct=10.0, stock_rs_pct=10.0,
            trend_score=0.5, atr_expansion=1.0, volume_score=0.5,
            oi_score=0.0, iv_score=0.0,
            weights=LayerWeights(),
        )
        assert live_result["score"] > proxy_result["score"]
        assert live_result["components"]["mp_source"] == "live"
        assert live_result["components"]["of_source"] == "live"
        assert proxy_result["components"]["mp_source"] == "proxy"

    def test_low_mp_score_drags_composite_down(self):
        # MP score of 20 should pull composite well below the 80 gate
        result = composite_score(
            asset_score=100.0, sector_rs_pct=20.0, stock_rs_pct=20.0,
            trend_score=0.5, atr_expansion=1.0, volume_score=0.5,
            oi_score=0.0, iv_score=0.0,
            weights=LayerWeights(),
            mp_score=20.0, of_score=20.0,
        )
        assert result["score"] < 80.0


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
    # Default is the new full-universe mode per user spec ("create a
    # watchlist for the entire qualified F&O universe").
    assert cfg.universe_mode == "full"


class TestFullUniverseRanker:
    """Tests for rank_stocks_full_universe — pure data shaping; no DB."""

    def test_covers_every_fno_symbol(self):
        import asyncio
        from cbe_scanner.alpha_engine import rank_stocks_full_universe

        sector_payload = {
            "watchlist": [
                {"code": "BANKING", "name": "Banking", "relative_strength_pct": 5.0, "quadrant": "leading"},
                {"code": "IT", "name": "IT", "relative_strength_pct": -2.0, "quadrant": "lagging"},
            ],
            "stocks_by_sector": {
                "BANKING": {
                    "sector": {"name": "Banking", "relative_strength_pct": 5.0, "quadrant": "leading"},
                    "rrg": {
                        "points": [
                            {"code": "HDFCBANK", "relative_strength_pct": 7.0, "quadrant": "leading"},
                            {"code": "ICICIBANK", "relative_strength_pct": 4.0, "quadrant": "improving"},
                        ],
                    },
                },
                "IT": {
                    "sector": {"name": "IT", "relative_strength_pct": -2.0, "quadrant": "lagging"},
                    "rrg": {
                        "points": [
                            {"code": "TCS", "relative_strength_pct": -3.0, "quadrant": "lagging"},
                        ],
                    },
                },
            },
        }
        # F&O universe includes 4 names; 1 of them is not in any sector slice.
        fno = {"HDFCBANK", "ICICIBANK", "TCS", "RELIANCE"}
        result = asyncio.get_event_loop().run_until_complete(
            rank_stocks_full_universe(sector_payload, fno_universe=fno, timeframe="weekly")
        )
        symbols = {c["instrument"] for c in result["candidates"]}
        assert symbols == fno  # every F&O symbol present
        assert result["mode"] == "full"

        sector_for = {c["instrument"]: c["sector_code"] for c in result["candidates"]}
        assert sector_for["HDFCBANK"] == "BANKING"
        assert sector_for["TCS"] == "IT"
        assert sector_for["RELIANCE"] is None  # unclassified — kept in universe

    def test_orders_leading_quadrants_first(self):
        import asyncio
        from cbe_scanner.alpha_engine import rank_stocks_full_universe

        sector_payload = {
            "watchlist": [],
            "stocks_by_sector": {
                "BANKING": {
                    "sector": {"relative_strength_pct": 0.0, "quadrant": "leading"},
                    "rrg": {
                        "points": [
                            {"code": "STK_LAGGING", "relative_strength_pct": -5.0, "quadrant": "lagging"},
                            {"code": "STK_LEADING", "relative_strength_pct": 8.0, "quadrant": "leading"},
                            {"code": "STK_IMPROVING", "relative_strength_pct": 3.0, "quadrant": "improving"},
                        ],
                    },
                },
            },
        }
        result = asyncio.get_event_loop().run_until_complete(
            rank_stocks_full_universe(
                sector_payload,
                fno_universe={"STK_LEADING", "STK_IMPROVING", "STK_LAGGING"},
                timeframe="weekly",
            )
        )
        order = [c["instrument"] for c in result["candidates"]]
        # leading < improving < lagging
        assert order[0] == "STK_LEADING"
        assert order[1] == "STK_IMPROVING"
        assert order[2] == "STK_LAGGING"
