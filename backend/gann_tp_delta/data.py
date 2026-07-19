"""Data access for Gann TP Delta snapshots."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from directional_options.data import DirectionalOptionsDataStore
from directional_options.features import FeatureEngine, resample_frame
from market_data.commodity_contract_specs import get_commodity_contract_spec

_IST_OFFSET = pd.Timedelta(hours=5, minutes=30)


class GannTPDeltaDataStore:
    """Small adapter over existing spot/history loaders."""

    def __init__(self, data_root: Path, feature_config: dict[str, Any]):
        self.data_root = Path(data_root)
        self.directional_store = DirectionalOptionsDataStore(self.data_root)
        self.feature_engine = FeatureEngine(feature_config)

    def available_underlyings(self) -> list[str]:
        return self.directional_store.available_underlyings()

    def load_spot_frame(self, underlying: str) -> pd.DataFrame:
        return self.directional_store.load_spot_frame(underlying)

    async def load_live_spot_frame(self, underlying: str, lookback_days: int = 10) -> tuple[pd.DataFrame, str, str]:
        commodity_spec = get_commodity_contract_spec(underlying)
        if commodity_spec.root and commodity_spec.root != "UNKNOWN":
            # Commodities: prefer the DEEP timescale 1-minute history. The shared
            # directional loader short-circuits commodities to a shallow ~1-session
            # broker fetch (~800 bars), which starves the Gann geometry (pivots,
            # cycles, regime all need lookback). underlying_spot_candles holds tens
            # of thousands of bars per commodity — use that, falling back to the
            # broker path only if the deep source is thin/unavailable.
            try:
                from directional_options.data import _frame_from_rows
                from market_data.market_intelligence_runtime import market_intelligence_runtime

                # Cap the deep 1-min commodity load: ~20 sessions is ample for
                # 15-min pivots/cycles/regime and keeps the in-memory frame
                # bounded (a full 60-day 1-min commodity pull is ~50k rows and,
                # six-wide in the scan, OOMs the box).
                commodity_lookback = min(max(int(lookback_days), 1), 20)
                rows, source, history_symbol = await market_intelligence_runtime.load_local_spot_rows(
                    underlying, lookback_days=commodity_lookback
                )
                frame = _frame_from_rows(rows)
                if frame is not None and not frame.empty and len(frame.index) > 200:
                    return frame, source, history_symbol
            except Exception:
                pass
        return await self.directional_store.load_live_spot_frame(underlying, lookback_days=lookback_days)

    def build_feature_frame(
        self,
        spot_frame: pd.DataFrame,
        timeframe: str,
        *,
        lookback_sessions: int | None,
        underlying: str | None = None,
    ) -> pd.DataFrame:
        spot_frame = self._session_frame(spot_frame, underlying)
        source_latest = (
            pd.Timestamp(spot_frame["time"].max())
            if not spot_frame.empty and "time" in spot_frame.columns
            else None
        )
        if timeframe == "1hour":
            frame = resample_frame(spot_frame, "30minute")
            if frame.empty:
                return frame
            indexed = frame.set_index("time").sort_index()
            hourly = (
                indexed.resample("60min", label="right", closed="right")
                .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "oi": "last"})
                .dropna(subset=["open", "high", "low", "close"])
                .reset_index()
            )
            built = self.feature_engine.build_frame(hourly, "1minute", lookback_sessions=lookback_sessions)
        else:
            built = self.feature_engine.build_frame(
                spot_frame, timeframe, lookback_sessions=lookback_sessions
            )

        # Right-labelled resampling creates the current, not-yet-complete bucket
        # (e.g. a 09:30 label from data only through 09:17). Gann entries require
        # a closed confirmation bar, so exclude labels beyond the latest source
        # observation. The 1-minute and daily research views are left unchanged.
        if (
            source_latest is not None
            and timeframe in {"3minute", "5minute", "15minute", "30minute", "1hour"}
            and not built.empty
        ):
            built = built.loc[built["time"] <= source_latest].reset_index(drop=True)
        return built

    @staticmethod
    def _session_frame(frame: pd.DataFrame, underlying: str | None) -> pd.DataFrame:
        """Remove off-session observations before geometry and resampling.

        Live database frames are tz-naive UTC while local research frames are
        commonly tz-naive IST. A multi-session live frame always contains
        pre-09:00 UTC rows, which gives us the same basis discriminator used by
        the shared directional feature engine.
        """
        if frame.empty or "time" not in frame.columns:
            return frame.copy()
        cleaned = frame.copy()
        times = pd.to_datetime(cleaned["time"], errors="coerce")
        valid = times.notna()
        if not valid.any():
            return cleaned.iloc[0:0].copy()
        local_times = times + _IST_OFFSET if (times[valid].dt.hour <= 8).any() else times
        symbol = str(underlying or "").upper()
        spec = get_commodity_contract_spec(symbol)
        is_commodity = bool(spec.root and spec.root != "UNKNOWN")
        open_minute = 9 * 60 if is_commodity else 9 * 60 + 15
        close_minute = 23 * 60 + 30 if is_commodity else 15 * 60 + 30
        wall_minute = local_times.dt.hour * 60 + local_times.dt.minute
        session_mask = valid & wall_minute.between(open_minute, close_minute)
        return cleaned.loc[session_mask].reset_index(drop=True)
