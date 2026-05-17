"""Service orchestration for Gann TP Delta harmonic research."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from functools import lru_cache
from time import monotonic
from typing import Any

import pandas as pd

from core.config import settings
from gann_tp_delta.anchors import select_anchor
from gann_tp_delta.backtest import GannTPDeltaBacktester
from gann_tp_delta.config import clone_default_config
from gann_tp_delta.data import GannTPDeltaDataStore
from gann_tp_delta.geometry import gann_fan, nearest_angle, nearest_sq9, price_time_square, square_of_nine, time_cycles
from gann_tp_delta.paper import GannTPDeltaPaperStore
from gann_tp_delta.scaling import harmonic_speed
from gann_tp_delta.signals import alert_events, confluence_signal


class GannTPDeltaService:
    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or clone_default_config()
        self.store = GannTPDeltaDataStore(self.config["data_root"], self.config["feature_engine"])
        self.backtester = GannTPDeltaBacktester(self.config)
        self.paper = GannTPDeltaPaperStore(self.config["paper"]["journal_root"])
        self._summary_cache: tuple[float, dict[str, Any]] | None = None

    def summary(self) -> dict[str, Any]:
        if self._summary_cache and self._summary_cache[0] > monotonic():
            return self._summary_cache[1]
        data_underlyings = set(self.store.available_underlyings())
        available = [
            item
            for item in self.config["universe"]
            if item in data_underlyings or self.config["data_root"].exists() or settings.PAPER_TRADING_ONLY
        ]
        payload = {
            "key": self.config["key"],
            "label": self.config["label"],
            "description": self.config["description"],
            "underlyings": available or list(self.config["universe"]),
            "timeframes": list(self.config["timeframes"]),
            "defaults": {
                "anchor_mode": "auto_pivot",
                "h_mode": self.config["scaling"]["default_h_mode"],
                "score_threshold": self.config["signals"]["score_threshold"],
                "squaring_tolerance": self.config["geometry"]["squaring_tolerance"],
                "cycle_window_bars": self.config["geometry"]["cycle_window_bars"],
            },
        }
        self._summary_cache = (monotonic() + 60.0, payload)
        return payload

    @lru_cache(maxsize=64)
    def workspace(
        self,
        underlying: str,
        timeframe: str,
        lookback_sessions: int,
        anchor_mode: str = "auto_pivot",
        h_mode: str = "median_tpd",
        manual_h: float | None = None,
    ) -> dict[str, Any]:
        spot = self.store.load_spot_frame(underlying)
        frame = self.store.build_feature_frame(spot, timeframe, lookback_sessions=lookback_sessions)
        snapshot = self._snapshot(frame, underlying=underlying, timeframe=timeframe, anchor_mode=anchor_mode, h_mode=h_mode, manual_h=manual_h)
        return {
            "module": self.summary(),
            "selection": {
                "underlying": underlying,
                "timeframe": timeframe,
                "lookback_sessions": lookback_sessions,
                "anchor_mode": anchor_mode,
                "h_mode": h_mode,
            },
            "snapshot": snapshot,
            "backtest": self.backtester.run(frame, anchor_mode=anchor_mode, h_mode=h_mode),
        }

    async def live_snapshot(
        self,
        underlying: str,
        timeframe: str,
        lookback_sessions: int,
        anchor_mode: str = "auto_pivot",
        h_mode: str = "median_tpd",
        manual_h: float | None = None,
    ) -> dict[str, Any]:
        spot, source, history_symbol = await self.store.load_live_spot_frame(underlying, lookback_days=max(int(lookback_sessions), 1))
        frame = self.store.build_feature_frame(spot, timeframe, lookback_sessions=lookback_sessions)
        payload = self._snapshot(frame, underlying=underlying, timeframe=timeframe, anchor_mode=anchor_mode, h_mode=h_mode, manual_h=manual_h)
        payload["history_source"] = source
        payload["history_symbol"] = history_symbol
        return payload

    def backtest(self, underlying: str, timeframe: str, lookback_sessions: int, anchor_mode: str, h_mode: str) -> dict[str, Any]:
        spot = self.store.load_spot_frame(underlying)
        frame = self.store.build_feature_frame(spot, timeframe, lookback_sessions=lookback_sessions)
        return self.backtester.run(frame, anchor_mode=anchor_mode, h_mode=h_mode)

    async def record_paper_snapshot(self, underlying: str, timeframe: str, lookback_sessions: int, anchor_mode: str, h_mode: str) -> dict[str, Any]:
        snapshot = await self.live_snapshot(underlying, timeframe, lookback_sessions, anchor_mode, h_mode)
        signal = snapshot.get("signal") or {}
        record = self.paper.record(
            {
                "underlying": underlying,
                "timeframe": timeframe,
                "anchor": snapshot.get("anchor"),
                "h": snapshot.get("h"),
                "signal": signal,
                "nearest_angle": snapshot.get("nearest_angle"),
                "nearest_sq9_level": snapshot.get("nearest_sq9_level"),
                "active_time_cycle": snapshot.get("active_time_cycle"),
                "price_time_square": snapshot.get("price_time_square"),
            }
        )
        return {"recorded": True, "record": record}

    def paper_journal(self, symbol: str | None = None, limit: int = 50) -> dict[str, Any]:
        return self.paper.list(symbol=symbol, limit=limit)

    def _snapshot(
        self,
        frame: pd.DataFrame,
        *,
        underlying: str,
        timeframe: str,
        anchor_mode: str,
        h_mode: str,
        manual_h: float | None,
    ) -> dict[str, Any]:
        if frame.empty:
            return {
                "status": "degraded",
                "reason": "No candles were available for the selected symbol/timeframe.",
                "bars": [],
                "anchor": None,
                "signal": None,
            }
        frame = frame.reset_index(drop=True)
        latest = frame.iloc[-1]
        current_index = len(frame.index) - 1
        current_price = float(latest["close"])
        anchor = select_anchor(frame, mode=anchor_mode, config=self.config["anchors"])
        if anchor is None:
            return {"status": "degraded", "reason": "No usable anchor was available.", "bars": self._bars(frame), "signal": None}
        h, vectors = harmonic_speed(frame, mode=h_mode, anchor_config=self.config["anchors"], scaling_config=self.config["scaling"], manual_h=manual_h)
        geometry_cfg = self.config["geometry"]
        angles = gann_fan(anchor=anchor, h=h.value, current_bar_index=current_index, current_price=current_price, ratios=geometry_cfg["gann_ratios"], projection_bars=int(geometry_cfg["projection_bars"]))
        sq9 = square_of_nine(anchor_price=anchor.price, current_price=current_price, price_unit=float(geometry_cfg["price_unit"]), degrees=geometry_cfg["sq9_degrees"])
        cycles = time_cycles(anchor=anchor, current_bar_index=current_index, cycles=geometry_cfg["bar_cycles"], window_bars=int(geometry_cfg["cycle_window_bars"]))
        square = price_time_square(anchor=anchor, current_bar_index=current_index, current_price=current_price, h=h.value, tolerance=float(geometry_cfg["squaring_tolerance"]))
        signal = confluence_signal(frame=frame, anchor=anchor, angles=angles, sq9_levels=sq9, cycles=cycles, square=square, config=self.config["signals"], near_pct=float(geometry_cfg["near_pct"]))
        alerts = alert_events(signal, angles, sq9, cycles, square, float(geometry_cfg["near_pct"]))
        active_cycle = next((item for item in cycles if item.active), None)
        return {
            "status": "ready",
            "as_of": pd.Timestamp(latest["time"]).isoformat(),
            "underlying": underlying,
            "timeframe": timeframe,
            "spot_price": current_price,
            "bars": self._bars(frame),
            "anchor": asdict(anchor),
            "h": asdict(h),
            "pivot_vectors": vectors,
            "gann_angles": [asdict(item) for item in angles],
            "sq9_levels": [asdict(item) for item in sq9],
            "time_cycles": [asdict(item) for item in cycles],
            "price_time_square": asdict(square),
            "nearest_angle": asdict(nearest_angle(angles)) if nearest_angle(angles) else None,
            "nearest_sq9_level": asdict(nearest_sq9(sq9)) if nearest_sq9(sq9) else None,
            "active_time_cycle": asdict(active_cycle) if active_cycle else None,
            "signal": asdict(signal),
            "alerts": [asdict(item) for item in alerts],
        }

    @staticmethod
    def _bars(frame: pd.DataFrame) -> list[dict[str, Any]]:
        keep = frame.tail(180)
        return [
            {
                "index": int(index),
                "time": pd.Timestamp(row["time"]).isoformat(),
                "open": round(float(row["open"]), 4),
                "high": round(float(row["high"]), 4),
                "low": round(float(row["low"]), 4),
                "close": round(float(row["close"]), 4),
            }
            for index, row in keep.iterrows()
        ]


gann_tp_delta_service = GannTPDeltaService()
