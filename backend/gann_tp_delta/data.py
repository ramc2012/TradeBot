"""Data access for Gann TP Delta snapshots."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from directional_options.data import DirectionalOptionsDataStore
from directional_options.features import FeatureEngine, resample_frame
from market_data.commodity_contract_specs import get_commodity_contract_spec


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
    ) -> pd.DataFrame:
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
            return self.feature_engine.build_frame(hourly, "1minute", lookback_sessions=lookback_sessions)
        return self.feature_engine.build_frame(spot_frame, timeframe, lookback_sessions=lookback_sessions)
