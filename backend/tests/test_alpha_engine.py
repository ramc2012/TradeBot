"""Unit tests for the CBE alpha engine v3 — MACD + RSI + RRG indicator set."""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from cbe_scanner.alpha_engine import (
    AlphaEngineConfig,
    LayerWeights,
    composite_score,
    compute_daily_indicators,
    compute_weekly_context,
    score_bearish_macd,
    score_bearish_rsi,
    score_macd,
    score_rsi,
    _bias_from_signals,
    _derive_equity_exposure,
    _ema,
    _rsi,
    _normalize_rs_pct,
    _completed_session_cutoff,
    _select_balanced_watchlist,
)


# ───────────────────────── Composite scorer ─────────────────────────
class TestCompositeScore:
    def test_all_neutral_yields_50(self):
        result = composite_score(
            asset_score=50.0, sector_rs_pct=0.0, stock_rs_pct=0.0,
            macd_score=50.0, rsi_score=50.0, weights=LayerWeights(),
        )
        assert result["score"] == 50.0

    def test_all_max_yields_100(self):
        result = composite_score(
            asset_score=100.0, sector_rs_pct=50.0, stock_rs_pct=50.0,
            macd_score=100.0, rsi_score=100.0, weights=LayerWeights(),
        )
        assert result["score"] == 100.0

    def test_one_component_zero_drags_score(self):
        # MACD = 0, everything else max → 80 (4 × 100 + 0) / 5
        result = composite_score(
            asset_score=100.0, sector_rs_pct=50.0, stock_rs_pct=50.0,
            macd_score=0.0, rsi_score=100.0, weights=LayerWeights(),
        )
        assert result["score"] == 80.0

    def test_components_include_macd_and_rsi(self):
        result = composite_score(
            asset_score=80.0, sector_rs_pct=10.0, stock_rs_pct=10.0,
            macd_score=90.0, rsi_score=80.0, weights=LayerWeights(),
        )
        comps = result["components"]
        for key in ("asset", "sector", "stock", "macd", "rsi"):
            assert key in comps

    def test_weights_sum_drives_normalization(self):
        # 30/30/20/10/10 — heavy weights on asset/sector
        custom = LayerWeights(asset=30, sector=30, stock=20, macd=10, rsi=10)
        result = composite_score(
            asset_score=100.0, sector_rs_pct=20.0, stock_rs_pct=20.0,
            macd_score=50.0, rsi_score=50.0, weights=custom,
        )
        # 30*100 + 30*100 + 20*100 + 10*50 + 10*50 = 7000; /100 = 70 — actually
        # sector at +20 RS → 100 component, stock at +20 RS → 100 component
        # Score = (30·100 + 30·100 + 20·100 + 10·50 + 10·50)/100 = 90
        assert result["score"] == 90.0


# ───────────────────────── MACD scoring ─────────────────────────
class TestMacdScoring:
    def test_above_zero_bullish_fresh_cross_max(self):
        indicators = {
            "macd_line": 1.5, "macd_signal": 1.0, "macd_histogram": 0.5,
            "macd_bullish": True, "macd_above_zero": True, "macd_cross_today": True,
        }
        score, meta = score_macd(indicators)
        assert score == 95.0
        assert meta["label"] == "above_zero_bullish_fresh"

    def test_above_zero_bullish_no_fresh_cross(self):
        indicators = {
            "macd_line": 1.5, "macd_signal": 1.0,
            "macd_bullish": True, "macd_above_zero": True, "macd_cross_today": False,
        }
        score, _ = score_macd(indicators)
        assert score == 75.0

    def test_below_zero_bearish_fresh_cross_lowest(self):
        indicators = {
            "macd_line": -1.5, "macd_signal": -1.0,
            "macd_bullish": False, "macd_above_zero": False, "macd_cross_today": True,
        }
        score, _ = score_macd(indicators)
        assert score == 5.0

    def test_below_zero_bullish_recovery(self):
        indicators = {
            "macd_line": -0.5, "macd_signal": -1.0,
            "macd_bullish": True, "macd_above_zero": False, "macd_cross_today": True,
        }
        score, meta = score_macd(indicators)
        assert score == 60.0
        assert meta["label"] == "below_zero_bullish_recovery"

    def test_no_macd_falls_back_neutral(self):
        score, _ = score_macd({"macd_line": None})
        assert score == 50.0


# ───────────────────────── RSI scoring ─────────────────────────
class TestRsiScoring:
    def test_healthy_uptrend_45_to_65_max_score(self):
        score, meta = score_rsi({"rsi_14": 55.0})
        assert score == 90.0
        assert meta["label"] == "healthy_uptrend"

    def test_extreme_overbought_lowest(self):
        score, _ = score_rsi({"rsi_14": 88.0})
        assert score == 10.0

    def test_overbought_zone(self):
        score, _ = score_rsi({"rsi_14": 78.0})
        assert score == 30.0

    def test_deep_oversold(self):
        score, _ = score_rsi({"rsi_14": 22.0})
        assert score == 25.0

    def test_oversold_bounce_candidate(self):
        score, meta = score_rsi({"rsi_14": 32.0})
        assert score == 50.0
        assert meta["label"] == "oversold_bounce"


# ───────────────────────── MACD/RSI compute on synthetic series ─────────────────────────
class TestComputeIndicators:
    def test_rising_series_macd_above_zero(self):
        closes = [100 + 0.5 * i for i in range(60)]
        ind = compute_daily_indicators(closes)
        assert ind["macd_line"] > 0
        assert ind["macd_bullish"] is True
        assert ind["macd_above_zero"] is True

    def test_falling_series_macd_below_zero(self):
        closes = [200 - 0.5 * i for i in range(60)]
        ind = compute_daily_indicators(closes)
        assert ind["macd_line"] < 0
        assert ind["macd_above_zero"] is False

    def test_short_series_returns_none_indicators(self):
        ind = compute_daily_indicators([100, 101, 102])
        assert ind["macd_line"] is None
        assert ind["rsi_14"] is None

    def test_rsi_flat_series_around_50(self):
        # No gains and no losses is neutral, not extreme overbought.
        closes = [100.0] * 50
        rsi = _rsi(closes, 14)
        assert rsi == 50.0

    def test_bearish_scores_reward_confirmed_downtrend(self):
        indicators = {
            "macd_line": -1.5,
            "macd_signal": -1.0,
            "macd_bullish": False,
            "macd_above_zero": False,
            "macd_cross_today": True,
            "rsi_14": 42.0,
        }
        macd_score, _ = score_bearish_macd(indicators)
        rsi_score, _ = score_bearish_rsi(indicators)
        assert macd_score == 95.0
        assert rsi_score == 90.0


class TestWeeklyContext:
    def test_uptrend_classified_up(self):
        # 200 daily bars rising → weekly trend up
        closes = [100 + 0.5 * i for i in range(200)]
        ctx = compute_weekly_context(closes)
        assert ctx["trend"] == "up"

    def test_downtrend_classified_down(self):
        closes = [300 - 0.5 * i for i in range(200)]
        ctx = compute_weekly_context(closes)
        assert ctx["trend"] == "down"

    def test_short_series_unknown(self):
        ctx = compute_weekly_context([100, 101, 102])
        assert ctx["trend"] == "unknown"


# ───────────────────────── Bias triple-confirmation ─────────────────────────
class TestBiasMacdRsiRrg:
    def _macd(self, bullish: bool, above_zero: bool):
        return {
            "macd_line": 1.0 if above_zero else -1.0,
            "macd_bullish": bullish,
            "macd_above_zero": above_zero,
        }

    def test_all_three_aligned_bullish(self):
        row = {
            "macd": {**self._macd(True, True), "rsi_14": 55.0},
            "weekly": {"trend": "up"},
            "stock_quadrant": "leading", "sector_quadrant": "leading",
        }
        assert _bias_from_signals(row) == "bullish"

    def test_all_three_aligned_bearish(self):
        row = {
            "macd": {**self._macd(False, False), "rsi_14": 40.0},
            "weekly": {"trend": "down"},
            "stock_quadrant": "lagging", "sector_quadrant": "lagging",
        }
        assert _bias_from_signals(row) == "bearish"

    def test_macd_bullish_but_overbought_rsi_neutral(self):
        # RSI 80 → not in healthy 45-70 zone → no entry.
        row = {
            "macd": {**self._macd(True, True), "rsi_14": 80.0},
            "weekly": {"trend": "up"},
            "stock_quadrant": "leading", "sector_quadrant": "leading",
        }
        assert _bias_from_signals(row) == "neutral"

    def test_macd_bullish_but_rrg_lagging_neutral(self):
        row = {
            "macd": {**self._macd(True, True), "rsi_14": 55.0},
            "weekly": {"trend": "up"},
            "stock_quadrant": "lagging", "sector_quadrant": "leading",
        }
        assert _bias_from_signals(row) == "neutral"

    def test_weekly_trend_down_blocks_bullish(self):
        row = {
            "macd": {**self._macd(True, True), "rsi_14": 55.0},
            "weekly": {"trend": "down"},
            "stock_quadrant": "leading", "sector_quadrant": "leading",
        }
        assert _bias_from_signals(row) == "neutral"

    def test_missing_macd_neutral(self):
        row = {"macd": {}, "weekly": {}, "stock_quadrant": "leading"}
        assert _bias_from_signals(row) == "neutral"


# ───────────────────────── Equity exposure derivation ─────────────────────────
class TestEquityExposure:
    def test_equities_winner_100_pct(self):
        layer = {"asset_rank": [{"asset": "EQUITIES"}, {"asset": "GOLD"}]}
        assert _derive_equity_exposure(layer) == 100.0

    def test_equities_second_70_pct(self):
        layer = {"asset_rank": [{"asset": "GOLD"}, {"asset": "EQUITIES"}, {"asset": "BONDS"}]}
        assert _derive_equity_exposure(layer) == 70.0

    def test_equities_third_40_pct(self):
        layer = {"asset_rank": [{"asset": "GOLD"}, {"asset": "BONDS"}, {"asset": "EQUITIES"}]}
        assert _derive_equity_exposure(layer) == 40.0

    def test_equities_bottom_20_pct(self):
        layer = {"asset_rank": [
            {"asset": "GOLD"}, {"asset": "BONDS"}, {"asset": "CASH"}, {"asset": "EQUITIES"},
        ]}
        assert _derive_equity_exposure(layer) == 20.0

    def test_stub_falls_back_to_100(self):
        assert _derive_equity_exposure({"stub": True}) == 100.0


# ───────────────────────── Normalizers + EMA ─────────────────────────
def test_normalize_rs_pct_zero_is_50():
    assert _normalize_rs_pct(0.0) == 50.0


def test_normalize_rs_pct_positive_extreme_clamps():
    assert _normalize_rs_pct(50.0) == 100.0


def test_ema_smooths():
    series = [100.0 + i for i in range(20)]
    ema = _ema(series, 5)
    assert len(ema) == 20
    assert ema[-1] < series[-1]
    assert ema[-1] > series[0]


# ───────────────────────── Config defaults ─────────────────────────
def test_alpha_engine_config_defaults():
    cfg = AlphaEngineConfig()
    assert cfg.timeframe == "weekly"
    assert cfg.sectors_to_keep == 4
    assert cfg.finalists_count == 10
    # Gate replaced with top-N ranking (relative strength, not absolute).
    assert cfg.top_n_watchlist == 10
    assert cfg.low_conviction_floor == 50.0


def test_layer_weights_sum_to_100():
    w = LayerWeights()
    assert w.total() == 100.0
    assert w.macd == 20.0 and w.rsi == 20.0


def test_balanced_watchlist_reserves_short_sleeve():
    scored = [
        {"instrument": f"L{i}", "directional_bias": "bullish", "composite_alpha_score": 90 - i}
        for i in range(8)
    ] + [
        {"instrument": f"S{i}", "directional_bias": "bearish", "composite_alpha_score": 80 - i}
        for i in range(8)
    ]
    scored.sort(key=lambda row: row["composite_alpha_score"], reverse=True)
    _, longs, shorts, watchlist = _select_balanced_watchlist(
        scored,
        top_n=10,
        minimum_score=50.0,
    )
    assert len(longs) == 8 and len(shorts) == 8
    assert sum(row["directional_bias"] == "bullish" for row in watchlist) == 5
    assert sum(row["directional_bias"] == "bearish" for row in watchlist) == 5


def test_completed_session_cutoff_excludes_intraday_session():
    # 09:30 UTC = 15:00 IST, before the 15:35 ingestion grace cutoff.
    assert _completed_session_cutoff(datetime(2026, 6, 25, 9, 30, tzinfo=timezone.utc)).isoformat() == "2026-06-24"
    assert _completed_session_cutoff(datetime(2026, 6, 25, 10, 15, tzinfo=timezone.utc)).isoformat() == "2026-06-25"
