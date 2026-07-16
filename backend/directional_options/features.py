"""Feature engineering for directional long-options selection."""
from __future__ import annotations

import math
from datetime import time
from typing import Any

import pandas as pd

from analysis.macd_engine import compute_ema, compute_macd
from analytics.technicals import compute_adx, compute_rsi
from directional_options.schemas import FeatureSnapshot


TIMEFRAME_TO_PANDAS = {
    "1minute": "1min",
    "3minute": "3min",
    "5minute": "5min",
    "15minute": "15min",
    "30minute": "30min",
    "1day": "1D",
}

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
SESSION_MINUTES = ((MARKET_CLOSE.hour * 60) + MARKET_CLOSE.minute) - ((MARKET_OPEN.hour * 60) + MARKET_OPEN.minute)


def timeframe_minutes(timeframe: str) -> int:
    mapping = {
        "1minute": 1,
        "3minute": 3,
        "5minute": 5,
        "15minute": 15,
        "30minute": 30,
        "1day": SESSION_MINUTES,
    }
    return mapping.get(timeframe, 5)


def resample_frame(frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    if frame.empty or "time" not in frame.columns:
        return frame.copy()
    if timeframe == "1minute":
        return frame.copy()
    rule = TIMEFRAME_TO_PANDAS[timeframe]
    indexed = frame.set_index("time").sort_index()
    resampled = (
        indexed.resample(rule, label="right", closed="right")
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
                "oi": "last",
            }
        )
        .dropna(subset=["open", "high", "low", "close"])
        .reset_index()
    )
    return resampled


def _compute_atr(frame: pd.DataFrame, period: int) -> pd.Series:
    prev_close = frame["close"].shift(1)
    tr = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - prev_close).abs(),
            (frame["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean().fillna(0.0)


def _session_progress(timestamp: pd.Timestamp) -> float:
    current = (timestamp.hour * 60) + timestamp.minute
    session_open = (MARKET_OPEN.hour * 60) + MARKET_OPEN.minute
    progress = (current - session_open) / max(SESSION_MINUTES, 1)
    return float(min(max(progress, 0.0), 1.0))


def _bounded_zscore(series: pd.Series, window: int) -> pd.Series:
    mean = series.rolling(window).mean()
    stdev = series.rolling(window).std().replace(0.0, float("nan"))
    return ((series - mean) / stdev).clip(-5.0, 5.0).fillna(0.0)


class FeatureEngine:
    """Resample spot history and compute regime/signal features."""

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def build_frame(
        self,
        spot_frame: pd.DataFrame,
        timeframe: str,
        *,
        lookback_sessions: int | None = None,
    ) -> pd.DataFrame:
        frame = resample_frame(spot_frame, timeframe)
        if lookback_sessions:
            session_dates = sorted({pd.Timestamp(value).date() for value in frame["time"]})
            keep = set(session_dates[-lookback_sessions:])
            frame = frame.loc[frame["time"].dt.date.isin(keep)].reset_index(drop=True)

        closes = frame["close"].astype(float).tolist()
        highs = frame["high"].astype(float).tolist()
        lows = frame["low"].astype(float).tolist()
        period_cfg = self.config
        frame["ema_fast"] = pd.Series(compute_ema(closes, int(period_cfg["ema_fast"])), dtype="float64")
        frame["ema_slow"] = pd.Series(compute_ema(closes, int(period_cfg["ema_slow"])), dtype="float64")
        adx, plus_di, minus_di = compute_adx(highs, lows, closes, int(period_cfg["adx_period"]))
        frame["adx"] = pd.Series(adx, dtype="float64")
        frame["plus_di"] = pd.Series(plus_di, dtype="float64")
        frame["minus_di"] = pd.Series(minus_di, dtype="float64")
        frame["atr"] = _compute_atr(frame, int(period_cfg["atr_period"]))
        frame["ema_spread_pct"] = ((frame["ema_fast"] - frame["ema_slow"]) / frame["close"]).fillna(0.0)
        frame["atr_pct"] = (frame["atr"] / frame["close"].replace(0.0, float("nan"))).fillna(0.0)
        frame["ema_fast_slope_pct"] = (
            (frame["ema_fast"] - frame["ema_fast"].shift(3))
            / frame["close"].replace(0.0, float("nan"))
        ).fillna(0.0)
        macd, macd_signal, macd_hist = compute_macd(closes)
        frame["macd"] = pd.Series(macd, dtype="float64")
        frame["macd_signal"] = pd.Series(macd_signal, dtype="float64")
        frame["macd_hist"] = pd.Series(macd_hist, dtype="float64")
        frame["macd_hist_pct"] = (frame["macd_hist"] / frame["close"].replace(0.0, float("nan"))).fillna(0.0)
        frame["rsi_14"] = pd.Series(
            compute_rsi(closes, int(period_cfg.get("rsi_period", 14))),
            dtype="float64",
        )
        typical_price = (frame["high"] + frame["low"] + frame["close"]) / 3.0
        session_key = frame["time"].dt.date
        volume = frame["volume"].astype(float).clip(lower=0.0)
        cumulative_volume = volume.groupby(session_key).cumsum().replace(0.0, float("nan"))
        cumulative_pv = (typical_price * volume).groupby(session_key).cumsum()
        frame["vwap"] = (cumulative_pv / cumulative_volume).fillna(frame["close"])
        frame["vwap_deviation_pct"] = (
            (frame["close"] - frame["vwap"]) / frame["close"].replace(0.0, float("nan"))
        ).fillna(0.0)
        frame["volume_zscore"] = _bounded_zscore(volume, int(period_cfg.get("volume_z_window", 20)))
        frame["body_pct"] = ((frame["close"] - frame["open"]) / frame["close"].replace(0.0, float("nan"))).fillna(0.0)
        bar_range = (frame["high"] - frame["low"]).replace(0.0, float("nan"))
        frame["close_location"] = (((frame["close"] - frame["low"]) / bar_range) * 2.0 - 1.0).clip(-1.0, 1.0).fillna(0.0)
        frame["range_pct"] = ((frame["high"] - frame["low"]) / frame["close"]).fillna(0.0)
        high_roll = frame["high"].rolling(int(period_cfg["breakout_lookback"])).max().shift(1)
        low_roll = frame["low"].rolling(int(period_cfg["breakout_lookback"])).min().shift(1)
        atr_denom = frame["atr"].replace(0.0, float("nan"))
        frame["breakout_up"] = ((frame["close"] - high_roll) / atr_denom).fillna(0.0)
        frame["breakout_down"] = ((low_roll - frame["close"]) / atr_denom).fillna(0.0)

        returns = frame["close"].pct_change().fillna(0.0)
        bars_per_day = max(1.0, SESSION_MINUTES / max(timeframe_minutes(timeframe), 1))
        annualizer = math.sqrt(252.0 * bars_per_day)
        rv = returns.rolling(int(period_cfg["rv_window"])).std().fillna(0.0) * annualizer
        frame["rv_annualized"] = rv
        rv_min = float(rv.min()) if not rv.empty else 0.0
        rv_max = float(rv.max()) if not rv.empty else 0.0
        denom = max(rv_max - rv_min, 1e-9)
        frame["rv_percentile"] = ((rv - rv_min) / denom).clip(0.0, 1.0)
        frame["range_expansion"] = (
            frame["range_pct"] / frame["range_pct"].rolling(int(period_cfg["range_window"])).mean().replace(0.0, pd.NA)
        ).fillna(1.0)
        frame["momentum_3"] = frame["close"].pct_change(3).fillna(0.0)
        frame["momentum_8"] = frame["close"].pct_change(8).fillna(0.0)
        frame["session_progress"] = frame["time"].map(_session_progress)
        opening_bars = max(
            1,
            int(math.ceil(float(period_cfg.get("opening_range_minutes", 30)) / max(timeframe_minutes(timeframe), 1))),
        )
        session_bar = frame.groupby(session_key).cumcount()
        opening_mask = session_bar < opening_bars
        opening_high = frame.loc[opening_mask].groupby(session_key[opening_mask])["high"].max()
        opening_low = frame.loc[opening_mask].groupby(session_key[opening_mask])["low"].min()
        frame["_opening_high"] = session_key.map(opening_high)
        frame["_opening_low"] = session_key.map(opening_low)
        opening_range = (frame["_opening_high"] - frame["_opening_low"]).replace(0.0, float("nan"))
        frame["opening_range_position"] = (
            (frame["close"] - frame["_opening_low"]) / opening_range
        ).clip(-1.0, 2.0).fillna(0.5)
        frame = frame.drop(columns=["_opening_high", "_opening_low"])
        di_total = (frame["plus_di"].abs() + frame["minus_di"].abs()).replace(0.0, float("nan"))
        di_separation = ((frame["plus_di"] - frame["minus_di"]).abs() / di_total).fillna(0.0)
        frame["trend_quality"] = (
            (frame["adx"] / 50.0).clip(0.0, 1.0) * 0.45
            + (frame["ema_spread_pct"].abs() / 0.01).clip(0.0, 1.0) * 0.30
            + di_separation.clip(0.0, 1.0) * 0.25
        ).clip(0.0, 1.0)

        warmup = int(self.config["warmup_bars"])
        if len(frame.index) > warmup:
            frame = frame.iloc[warmup:].reset_index(drop=True)
        frame = frame.fillna(0.0)
        return frame

    @staticmethod
    def snapshot(row: pd.Series) -> FeatureSnapshot:
        return FeatureSnapshot(
            timestamp=pd.Timestamp(row["time"]).isoformat(),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row.get("volume", 0.0)),
            ema_fast=float(row.get("ema_fast", 0.0)),
            ema_slow=float(row.get("ema_slow", 0.0)),
            ema_spread_pct=float(row.get("ema_spread_pct", 0.0)),
            adx=float(row.get("adx", 0.0)),
            plus_di=float(row.get("plus_di", 0.0)),
            minus_di=float(row.get("minus_di", 0.0)),
            atr=float(row.get("atr", 0.0)),
            range_pct=float(row.get("range_pct", 0.0)),
            breakout_up=float(row.get("breakout_up", 0.0)),
            breakout_down=float(row.get("breakout_down", 0.0)),
            rv_annualized=float(row.get("rv_annualized", 0.0)),
            rv_percentile=float(row.get("rv_percentile", 0.0)),
            range_expansion=float(row.get("range_expansion", 0.0)),
            session_progress=float(row.get("session_progress", 0.0)),
            momentum_3=float(row.get("momentum_3", 0.0)),
            momentum_8=float(row.get("momentum_8", 0.0)),
            atr_pct=float(row.get("atr_pct", 0.0)),
            ema_fast_slope_pct=float(row.get("ema_fast_slope_pct", 0.0)),
            macd=float(row.get("macd", 0.0)),
            macd_signal=float(row.get("macd_signal", 0.0)),
            macd_hist=float(row.get("macd_hist", 0.0)),
            macd_hist_pct=float(row.get("macd_hist_pct", 0.0)),
            rsi_14=float(row.get("rsi_14", 50.0)),
            vwap=float(row.get("vwap", 0.0)),
            vwap_deviation_pct=float(row.get("vwap_deviation_pct", 0.0)),
            volume_zscore=float(row.get("volume_zscore", 0.0)),
            body_pct=float(row.get("body_pct", 0.0)),
            close_location=float(row.get("close_location", 0.0)),
            opening_range_position=float(row.get("opening_range_position", 0.5)),
            trend_quality=float(row.get("trend_quality", 0.0)),
        )
