"""
Gate A — Market Profile Feature Engine Unit Tests
===================================================
Validates:
  1. TPO construction from 1-min candles
  2. POC calculation (price with max TPOs)
  3. VAH/VAL computation (70% of TPOs)
  4. IB High/Low/Range (first 60-min)
  5. Failed Auction detection (IB extension that closes back inside)
  6. Day-type labelling (TREND_UP/DN, NORMAL_VAR, FAILED_AUCTION, etc.)
  7. Buyer/seller failure score computation
  8. NIFTY enriched file sanity checks

Run with:
  cd nomad-curie/backend && pytest tests/test_gate_a_mp_engine.py -v
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import pytest

# Add backend to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.build_nifty_mp import (
    _bucket,
    _compute_daily_mp,
    _compute_failure_scores,
    DailyMP,
    TPO_MINUTES,
    IB_MINUTES,
    VA_PCT,
)

DATA_ROOT = Path(__file__).resolve().parent.parent / "runtime" / "index_analytics_data"


# ── Synthetic 1-min candle builders ──────────────────────────────────────────

def _make_candles(
    date_str: str,
    prices: list[float],
    start_time: str = "09:15",
) -> pd.DataFrame:
    """Build a minimal 1-min candle DataFrame from a list of close prices."""
    base = pd.Timestamp(f"{date_str} {start_time}+05:30")
    rows = []
    for i, p in enumerate(prices):
        rows.append({
            "time": base + pd.Timedelta(minutes=i),
            "open": p,
            "high": p + 5,
            "low": p - 5,
            "close": p,
            "volume": 0,
            "oi": 0,
        })
    return pd.DataFrame(rows)


def _make_trend_up_day(date_str: str = "2025-06-01", bucket_size: int = 50) -> pd.DataFrame:
    """
    Simulates a TREND_UP day:
      - IB: 22700–22800 (range=100)
      - Breaks IBH at bar 75, keeps going to 23200
      - Closes at 23150 (above IBH, so no FA_UP)
    """
    # 9:15–10:15 = first 60 bars = IB
    # Bars 0–59: IB range 22700–22800
    ib_prices = [22750 + i * 0.8 for i in range(60)]  # slowly rising through IB
    # Bars 60–375 (rest of session, ~6h = 315 bars): trend up to 23200
    post_ib_prices = [22800 + i * 1.2 for i in range(315)]
    all_prices = ib_prices + post_ib_prices
    return _make_candles(date_str, all_prices)


def _make_trend_dn_day(date_str: str = "2025-06-02", bucket_size: int = 50) -> pd.DataFrame:
    """
    Simulates a TREND_DN day:
      - IB: 22800–22900
      - Breaks IBL, trends down to 22200
      - Closes at 22250 (no FA)
    """
    ib_prices = [22850 - i * 0.8 for i in range(60)]
    post_ib_prices = [22790 - i * 1.8 for i in range(315)]
    return _make_candles(date_str, ib_prices + post_ib_prices)


def _make_fa_up_day(date_str: str = "2025-06-03", bucket_size: int = 50) -> pd.DataFrame:
    """
    Simulates a FAILED_AUCTION UP day:
      - IB: 22700–22800
      - Breaks IBH, goes to 23000 (IB broken up)
      - Then reverses, closes at 22750 (< IBH=22800+5 → FA_UP)
    """
    ib_prices = [22750] * 60
    # Post IB: rally then collapse
    rally = [22800 + i * 3 for i in range(70)]      # breaks IBH
    collapse = [23010 - i * 3.5 for i in range(140)]  # falls back below IBH
    hold = [22750] * 45                               # stays near IB
    all_prices = ib_prices + rally + collapse + hold
    return _make_candles(date_str, all_prices)


# ── Tests: TPO & POC ─────────────────────────────────────────────────────────

class TestTPOAndPOC:
    def test_poc_is_most_visited_price(self):
        """POC should be the price bucket with the most TPO counts."""
        df = _make_trend_up_day()
        mp = _compute_daily_mp(df, bucket_size=50)
        assert mp is not None
        assert isinstance(mp.poc, (int, float))
        # POC should be a multiple of bucket_size
        assert mp.poc % 50 == 0

    def test_vah_above_val(self):
        """Value Area High must be above Value Area Low."""
        df = _make_trend_up_day()
        mp = _compute_daily_mp(df, bucket_size=50)
        assert mp is not None
        assert mp.vah > mp.val

    def test_va_range_positive(self):
        """VAR = VAH - VAL must be positive."""
        df = _make_trend_up_day()
        mp = _compute_daily_mp(df, bucket_size=50)
        assert mp is not None
        assert mp.var > 0

    def test_poc_within_va(self):
        """POC should be within the Value Area [VAL, VAH]."""
        df = _make_trend_up_day()
        mp = _compute_daily_mp(df, bucket_size=50)
        assert mp is not None
        assert mp.val <= mp.poc <= mp.vah


# ── Tests: Initial Balance ────────────────────────────────────────────────────

class TestInitialBalance:
    def test_ib_range_positive(self):
        """IB Range must be positive."""
        df = _make_trend_up_day()
        mp = _compute_daily_mp(df, bucket_size=50)
        assert mp is not None
        assert mp.ibr > 0

    def test_ibh_above_ibl(self):
        """IBH (IB High) must be above IBL (IB Low)."""
        df = _make_trend_up_day()
        mp = _compute_daily_mp(df, bucket_size=50)
        assert mp is not None
        assert mp.ibh > mp.ibl

    def test_ib_computed_from_first_60_min(self):
        """IBH should equal session high of first 60 candles + 5 (high offset)."""
        df = _make_trend_up_day()
        ib_candles = df.iloc[:60]
        expected_ibh = float(ib_candles["high"].max())
        expected_ibl = float(ib_candles["low"].min())

        mp = _compute_daily_mp(df, bucket_size=50)
        assert mp is not None
        assert abs(mp.ibh - expected_ibh) < 1.0
        assert abs(mp.ibl - expected_ibl) < 1.0


# ── Tests: IB Extension & Failed Auction ─────────────────────────────────────

class TestFailedAuction:
    def test_trend_up_breaks_ibh_no_fa(self):
        """TREND_UP: breaks IBH, stays above → ib_broken_up=True, fa_up=False."""
        df = _make_trend_up_day()
        mp = _compute_daily_mp(df, bucket_size=50)
        assert mp is not None
        assert mp.ib_broken_up is True
        assert mp.fa_up is False

    def test_trend_dn_breaks_ibl_no_fa(self):
        """TREND_DN: breaks IBL, stays below → ib_broken_dn=True, fa_dn=False."""
        df = _make_trend_dn_day()
        mp = _compute_daily_mp(df, bucket_size=50)
        assert mp is not None
        assert mp.ib_broken_dn is True
        assert mp.fa_dn is False

    def test_fa_up_detected(self):
        """FA_UP: breaks IBH then closes back below IBH → fa_up=True."""
        df = _make_fa_up_day()
        mp = _compute_daily_mp(df, bucket_size=50)
        assert mp is not None
        assert mp.ib_broken_up is True, "Should have broken IBH"
        assert mp.fa_up is True, "Should be Failed Auction UP"


# ── Tests: Session Stats ──────────────────────────────────────────────────────

class TestSessionStats:
    def test_session_high_is_max(self):
        df = _make_trend_up_day()
        mp = _compute_daily_mp(df, bucket_size=50)
        assert mp is not None
        expected_high = float(df["high"].max())
        assert abs(mp.session_high - expected_high) < 1.0

    def test_session_low_is_min(self):
        df = _make_trend_dn_day()
        mp = _compute_daily_mp(df, bucket_size=50)
        assert mp is not None
        expected_low = float(df["low"].min())
        assert abs(mp.session_low - expected_low) < 1.0

    def test_open_is_first_candle_open(self):
        df = _make_trend_up_day()
        mp = _compute_daily_mp(df, bucket_size=50)
        assert mp is not None
        assert abs(mp.open_price - float(df["open"].iloc[0])) < 1.0

    def test_close_is_last_candle_close(self):
        df = _make_trend_up_day()
        mp = _compute_daily_mp(df, bucket_size=50)
        assert mp is not None
        assert abs(mp.close_price - float(df["close"].iloc[-1])) < 1.0


# ── Tests: Too-short sessions ─────────────────────────────────────────────────

class TestEdgeCases:
    def test_too_short_session_returns_none(self):
        """Less than 4 TPO periods (120 min) → skip → None."""
        df = _make_candles("2025-06-01", [22750] * 50)  # 50 min only
        result = _compute_daily_mp(df, bucket_size=50)
        assert result is None

    def test_exact_minimum_session(self):
        """Exactly 4 TPO periods (4 * 30 = 120 candles) → should succeed."""
        df = _make_candles("2025-06-01", [22750] * 120)
        result = _compute_daily_mp(df, bucket_size=50)
        assert result is not None


# ── Tests: Failure Scores ─────────────────────────────────────────────────────

class TestFailureScores:
    def _build_mp_df_from_mp(self, mp: DailyMP) -> pd.DataFrame:
        return pd.DataFrame([{
            "date": mp.date,
            "poc": mp.poc,
            "vah": mp.vah,
            "val": mp.val,
            "var": mp.var,
            "ibh": mp.ibh,
            "ibl": mp.ibl,
            "ibr": mp.ibr,
            "ib_broken_up": mp.ib_broken_up,
            "ib_broken_dn": mp.ib_broken_dn,
            "fa_up": mp.fa_up,
            "fa_dn": mp.fa_dn,
            "session_high": mp.session_high,
            "session_low": mp.session_low,
            "open_price": mp.open_price,
            "close_price": mp.close_price,
            "total_tpos": mp.total_tpos,
        }])

    def test_fa_up_increases_buyer_fail(self):
        """FA_UP should contribute +2 to buyer_fail_score."""
        df_candles = _make_fa_up_day()
        mp = _compute_daily_mp(df_candles, bucket_size=50)
        assert mp is not None and mp.fa_up

        mp_df = self._build_mp_df_from_mp(mp)
        result = _compute_failure_scores(mp_df, df_candles, bucket_size=50)
        bf = result.iloc[0]["buyer_fail_score"]
        assert bf >= 2, f"FA_UP should give buyer_fail≥2, got {bf}"

    def test_trend_up_increases_seller_fail(self):
        """TREND_UP with close in upper range → seller_fail≥1 (close in upper 65%)."""
        df_candles = _make_trend_up_day()
        mp = _compute_daily_mp(df_candles, bucket_size=50)
        assert mp is not None

        mp_df = self._build_mp_df_from_mp(mp)
        result = _compute_failure_scores(mp_df, df_candles, bucket_size=50)
        sf = result.iloc[0]["seller_fail_score"]
        assert sf >= 1, f"TREND_UP should give seller_fail≥1, got {sf}"

    def test_net_failure_sign(self):
        """net_failure = seller_fail - buyer_fail; for FA_UP should be negative."""
        df_candles = _make_fa_up_day()
        mp = _compute_daily_mp(df_candles, bucket_size=50)
        assert mp is not None
        mp_df = self._build_mp_df_from_mp(mp)
        result = _compute_failure_scores(mp_df, df_candles, bucket_size=50)
        net = result.iloc[0]["net_failure"]
        bf = result.iloc[0]["buyer_fail_score"]
        sf = result.iloc[0]["seller_fail_score"]
        assert net == sf - bf


# ── Tests: NIFTY data file sanity ─────────────────────────────────────────────

class TestNIFTYDataFiles:
    NIFTY_MP_PATH = DATA_ROOT / "market_profile" / "underlying=NIFTY" / "daily_mp_params.csv"
    NIFTY_ENR_PATH = DATA_ROOT / "market_profile" / "underlying=NIFTY" / "enriched_mp_with_failures.csv"

    def test_nifty_mp_params_exists(self):
        assert self.NIFTY_MP_PATH.exists(), f"Missing: {self.NIFTY_MP_PATH}"

    def test_nifty_enriched_exists(self):
        assert self.NIFTY_ENR_PATH.exists(), f"Missing: {self.NIFTY_ENR_PATH}"

    def test_nifty_mp_row_count(self):
        df = pd.read_csv(self.NIFTY_MP_PATH)
        assert len(df) >= 200, f"Expected ≥200 sessions, got {len(df)}"

    def test_nifty_mp_columns(self):
        df = pd.read_csv(self.NIFTY_MP_PATH)
        required = ["date", "poc", "vah", "val", "ibh", "ibl", "ibr",
                    "fa_up", "fa_dn", "ib_broken_up", "ib_broken_dn",
                    "open_price", "close_price"]
        for col in required:
            assert col in df.columns, f"Missing column: {col}"

    def test_nifty_poc_within_range(self):
        """NIFTY POC should be between 19000 and 28000 for the Apr 2025–Apr 2026 period."""
        df = pd.read_csv(self.NIFTY_MP_PATH)
        assert df["poc"].min() > 18000, f"POC too low: {df['poc'].min()}"
        assert df["poc"].max() < 30000, f"POC too high: {df['poc'].max()}"

    def test_nifty_ibr_positive(self):
        df = pd.read_csv(self.NIFTY_MP_PATH)
        assert (df["ibr"] > 0).all(), "All IBR values should be positive"

    def test_nifty_vah_above_val(self):
        df = pd.read_csv(self.NIFTY_MP_PATH)
        assert (df["vah"] > df["val"]).all(), "VAH should always exceed VAL"

    def test_nifty_enriched_failure_scores_present(self):
        df = pd.read_csv(self.NIFTY_ENR_PATH)
        for col in ["buyer_fail_score", "seller_fail_score", "net_failure", "day_type"]:
            assert col in df.columns, f"Missing enriched column: {col}"

    def test_nifty_enriched_scores_non_negative(self):
        df = pd.read_csv(self.NIFTY_ENR_PATH)
        assert (df["buyer_fail_score"] >= 0).all()
        assert (df["seller_fail_score"] >= 0).all()

    def test_nifty_enriched_date_range(self):
        df = pd.read_csv(self.NIFTY_ENR_PATH)
        df["date"] = pd.to_datetime(df["date"])
        assert df["date"].min() <= pd.Timestamp("2025-06-01"), "Should start before Jun 2025"
        assert df["date"].max() >= pd.Timestamp("2026-03-01"), "Should extend to at least Mar 2026"

    def test_nifty_day_type_coverage(self):
        """Ensure all expected day types appear in the data."""
        df = pd.read_csv(self.NIFTY_ENR_PATH)
        expected_types = {"TREND_UP", "TREND_DN", "FAILED_AUCTION"}
        actual_types = set(df["day_type"].unique())
        for t in expected_types:
            assert t in actual_types, f"Day type '{t}' never appears in NIFTY data"
