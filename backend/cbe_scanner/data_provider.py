"""
cbe_scanner/data_provider.py
=============================

Abstract data interface separating the CBE scoring logic from data sources.

Two implementations provided:

1. SyntheticDataProvider - for unit testing the scorer end-to-end
2. TimescaleDataProvider - skeleton for production wiring (TODO: handoff to Claude Code)

The interface is intentionally narrow. To plug in real NSE data, implement
each method to query your TimescaleDB schema.
"""

from abc import ABC, abstractmethod
import hashlib
from typing import Optional

import numpy as np
import pandas as pd


def _stable_seed(value: str) -> int:
    """Return a process-stable uint32 seed for synthetic market generation."""
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % (2**32)


class DataProvider(ABC):
    """Abstract base class. Implement these methods against your data source."""

    @abstractmethod
    def get_ohlc(self, symbol: str, lookback_days: int = 300) -> Optional[pd.DataFrame]:
        """Return DataFrame indexed by date with columns [open, high, low, close, volume]."""
        ...

    @abstractmethod
    def get_options_chain(self, symbol: str, expiry: Optional[pd.Timestamp] = None) -> Optional[pd.DataFrame]:
        """Return options chain for nearest weekly expiry by default.
        Columns: [strike, type ('CE'/'PE'), oi, oi_change_1d, volume, iv, delta]
        """
        ...

    @abstractmethod
    def get_iv_history(self, symbol: str, lookback_days: int = 300) -> Optional[pd.Series]:
        """Return ATM IV time series, indexed by date."""
        ...

    @abstractmethod
    def get_pcr_history(self, symbol: str, lookback_days: int = 300) -> Optional[pd.Series]:
        """Return put-call ratio time series."""
        ...

    @abstractmethod
    def get_sector_returns(self, symbol: str) -> Optional[pd.Series]:
        """Return daily log returns of the stock's parent sector index."""
        ...

    @abstractmethod
    def get_events(self, symbol: str, lookahead_days: int = 10) -> list:
        """Return list of {date, type, description} for upcoming events."""
        ...

    @abstractmethod
    def get_spread_history(self, symbol: str) -> Optional[pd.Series]:
        """Return daily avg bid-ask spread / mid."""
        ...

    @abstractmethod
    def get_block_deals(self, symbol: str) -> Optional[pd.DataFrame]:
        """Return block deals with columns [date, value, side]."""
        ...

    @abstractmethod
    def get_fii_dii_flow(self, symbol: str) -> Optional[pd.Series]:
        """Return daily FII+DII net flow for the stock or sector."""
        ...


# ============================================================
# SYNTHETIC PROVIDER FOR TESTING
# ============================================================

class SyntheticDataProvider(DataProvider):
    """Generates realistic synthetic data so the scanner can be tested
    end-to-end without live market access.

    Embeds known "spring-loaded" instruments so we can validate that
    the scorer correctly identifies them.
    """

    def __init__(self, seed: int = 42, today: pd.Timestamp = pd.Timestamp("2024-12-27")):
        self.rng = np.random.default_rng(seed)
        self.today = today
        self._cache = {}

        # Define which symbols are "compressed and about to expand" vs random
        self.spring_loaded_bullish = ["RELIANCE", "TCS", "ICICIBANK"]
        self.spring_loaded_bearish = ["TATAMOTORS", "BAJFINANCE"]
        # The rest are uncompressed random walks

    def _make_ohlc(self, symbol: str, compressed: bool, direction: int):
        if symbol in self._cache:
            return self._cache[symbol]["ohlc"]

        n_days = 300
        dates = pd.bdate_range(end=self.today, periods=n_days)
        rng = np.random.default_rng(_stable_seed(symbol))

        # Base regime: high vol -> low vol if compressed
        if compressed:
            # First 200 days: normal vol. Last 100 days: compressing vol.
            vols = np.concatenate([
                np.full(200, 0.020),
                np.linspace(0.020, 0.008, 100),  # compressing
            ])
        else:
            vols = rng.uniform(0.012, 0.025, n_days)

        returns = rng.standard_normal(n_days) * vols
        # Add a small directional drift in the last 30 days if compressed (the "loading")
        if compressed:
            returns[-30:] += direction * 0.0005

        prices = 1000 * np.exp(np.cumsum(returns))
        # Build OHLC from close
        opens = prices * (1 + rng.standard_normal(n_days) * 0.002)
        highs = np.maximum(opens, prices) * (1 + np.abs(rng.standard_normal(n_days)) * 0.003)
        lows = np.minimum(opens, prices) * (1 - np.abs(rng.standard_normal(n_days)) * 0.003)
        # For compressed regime, narrow the daily range in last 30 days
        if compressed:
            range_factor = np.concatenate([np.ones(270), np.linspace(1.0, 0.4, 30)])
            highs = prices + (highs - prices) * range_factor
            lows = prices + (lows - prices) * range_factor

        volumes = rng.integers(100000, 1000000, n_days)
        # Volume contracts during compression
        if compressed:
            volumes = (volumes * np.concatenate([np.ones(270), np.linspace(1.0, 0.6, 30)])).astype(int)

        ohlc = pd.DataFrame({
            "open": opens, "high": highs, "low": lows,
            "close": prices, "volume": volumes,
        }, index=dates)

        self._cache[symbol] = {"ohlc": ohlc, "compressed": compressed, "direction": direction}
        return ohlc

    def _get_regime(self, symbol: str):
        if symbol in self.spring_loaded_bullish:
            return True, 1
        if symbol in self.spring_loaded_bearish:
            return True, -1
        return False, 0

    def get_ohlc(self, symbol: str, lookback_days: int = 300) -> pd.DataFrame:
        compressed, direction = self._get_regime(symbol)
        return self._make_ohlc(symbol, compressed, direction)

    def get_options_chain(self, symbol: str, expiry=None) -> pd.DataFrame:
        compressed, direction = self._get_regime(symbol)
        ohlc = self.get_ohlc(symbol)
        spot = ohlc["close"].iloc[-1]
        rng = np.random.default_rng(_stable_seed(symbol + "opts"))

        strikes = np.arange(int(spot * 0.92), int(spot * 1.08), max(int(spot * 0.005), 1))
        rows = []
        for k in strikes:
            for typ in ["CE", "PE"]:
                moneyness = (k - spot) / spot
                delta = 0.5 + (moneyness * 5 if typ == "CE" else -moneyness * 5)
                delta = max(0.05, min(0.95, delta))
                if typ == "PE":
                    delta = -delta
                base_iv = 0.20 if not compressed else 0.13  # compressed = low IV
                iv = base_iv + abs(moneyness) * 0.10 + rng.normal(0, 0.005)

                base_oi = 100000 * np.exp(-abs(moneyness) * 8)
                oi = int(base_oi * (1 + rng.normal(0, 0.2)))

                # Inject OI buildup at OTM strikes in the spring direction
                oi_change = 0
                if compressed and direction != 0:
                    if direction == 1 and typ == "CE" and 0.01 < moneyness < 0.05:
                        oi_change = int(oi * rng.uniform(0.25, 0.4))
                    elif direction == -1 and typ == "PE" and -0.05 < moneyness < -0.01:
                        oi_change = int(oi * rng.uniform(0.25, 0.4))
                    else:
                        oi_change = int(oi * rng.normal(0, 0.05))
                else:
                    oi_change = int(oi * rng.normal(0, 0.05))

                rows.append({
                    "strike": k, "type": typ, "iv": round(iv, 4),
                    "oi": oi, "oi_change_1d": oi_change,
                    "volume": int(oi * rng.uniform(0.05, 0.3)),
                    "delta": round(delta, 3),
                })
        return pd.DataFrame(rows)

    def get_iv_history(self, symbol: str, lookback_days: int = 300) -> pd.Series:
        compressed, _ = self._get_regime(symbol)
        ohlc = self.get_ohlc(symbol)
        rng = np.random.default_rng(_stable_seed(symbol + "iv"))

        if compressed:
            iv = np.concatenate([
                rng.uniform(0.22, 0.30, 200),
                np.linspace(0.25, 0.12, 100),  # compressing
            ])
        else:
            iv = rng.uniform(0.18, 0.32, 300)

        return pd.Series(iv, index=ohlc.index)

    def get_pcr_history(self, symbol: str, lookback_days: int = 300) -> pd.Series:
        compressed, direction = self._get_regime(symbol)
        ohlc = self.get_ohlc(symbol)
        rng = np.random.default_rng(_stable_seed(symbol + "pcr"))

        pcr = 0.9 + rng.normal(0, 0.1, 300)
        if compressed and direction == 1:
            pcr[-20:] -= 0.2  # bullish setup: PCR drops
        elif compressed and direction == -1:
            pcr[-20:] += 0.25  # bearish setup: PCR rises
        return pd.Series(pcr, index=ohlc.index)

    def get_sector_returns(self, symbol: str) -> pd.Series:
        compressed, direction = self._get_regime(symbol)
        ohlc = self.get_ohlc(symbol)
        rng = np.random.default_rng((_stable_seed(symbol) + 100) % (2**32))

        sector_ret = rng.normal(0, 0.012, 300)
        if compressed:
            # Sector trends in spring direction
            sector_ret[-20:] += direction * 0.003
        return pd.Series(sector_ret, index=ohlc.index)

    def get_events(self, symbol: str, lookahead_days: int = 10) -> list:
        compressed, _ = self._get_regime(symbol)
        rng = np.random.default_rng(_stable_seed(symbol + "evt"))

        # Spring-loaded instruments have an event in the next 2-4 days
        if compressed:
            days_ahead = rng.integers(2, 5)
            return [{
                "date": self.today + pd.Timedelta(days=int(days_ahead)),
                "type": "earnings",
                "description": f"{symbol} Q2 results",
            }]
        # Random other instruments may have unrelated events
        if rng.random() < 0.2:
            return [{
                "date": self.today + pd.Timedelta(days=int(rng.integers(7, 15))),
                "type": "board_meeting",
                "description": f"{symbol} board meeting",
            }]
        return []

    def get_spread_history(self, symbol: str) -> pd.Series:
        compressed, _ = self._get_regime(symbol)
        ohlc = self.get_ohlc(symbol)
        rng = np.random.default_rng(_stable_seed(symbol + "spread"))

        spread = rng.uniform(0.0010, 0.0020, 300)
        if compressed:
            spread[-15:] *= 0.7  # tightening
        return pd.Series(spread, index=ohlc.index)

    def get_block_deals(self, symbol: str) -> pd.DataFrame:
        compressed, direction = self._get_regime(symbol)
        ohlc = self.get_ohlc(symbol)
        rng = np.random.default_rng(_stable_seed(symbol + "blk"))

        if compressed:
            n_deals = rng.integers(2, 6)
            rows = []
            for _ in range(n_deals):
                days_back = rng.integers(0, 8)
                rows.append({
                    "date": ohlc.index[-1] - pd.Timedelta(days=int(days_back)),
                    "value": rng.uniform(5e7, 5e8),
                    "side": "buy" if direction == 1 else "sell",
                })
            return pd.DataFrame(rows)
        return pd.DataFrame(columns=["date", "value", "side"])

    def get_fii_dii_flow(self, symbol: str) -> pd.Series:
        compressed, direction = self._get_regime(symbol)
        ohlc = self.get_ohlc(symbol)
        rng = np.random.default_rng(_stable_seed(symbol + "ff"))

        flow = rng.normal(0, 100, 300)
        if compressed:
            flow[-5:] = abs(flow[-5:]) * direction * rng.uniform(2, 4, 5)  # consistent direction
        return pd.Series(flow, index=ohlc.index)
