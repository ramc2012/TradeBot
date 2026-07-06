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
from gann_tp_delta.agent import GannTPDeltaPaperAgent
from gann_tp_delta.backtest import GannTPDeltaBacktester
from gann_tp_delta.config import clone_default_config
from gann_tp_delta.data import GannTPDeltaDataStore
from gann_tp_delta.geometry import gann_fan, nearest_angle, nearest_sq9, price_time_square, square_of_nine, time_cycles
from gann_tp_delta.paper import GannTPDeltaPaperStore
from gann_tp_delta.scaling import harmonic_speed
from gann_tp_delta.signals import alert_events, confluence_signal
from gann_tp_delta.strategy import evaluate_gann_signal


class GannTPDeltaService:
    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or clone_default_config()
        self.store = GannTPDeltaDataStore(self.config["data_root"], self.config["feature_engine"])
        self.backtester = GannTPDeltaBacktester(self.config)
        self.paper = GannTPDeltaPaperStore(self.config["paper"]["journal_root"])
        self.paper_agent = GannTPDeltaPaperAgent(self, self.config)
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
        # WS-1.1 bulkhead: the feature-frame build + Gann geometry are CPU-bound and
        # were running inline on the event loop — a fully-inline ~12.8s scan that froze
        # tick ingest / Redis publish / WS push. Offload to a worker thread so the data
        # plane stays responsive during the scan (verified via nomad_event_loop_lag_seconds).
        payload = await asyncio.to_thread(
            self._live_snapshot_compute,
            spot, underlying, timeframe, lookback_sessions, anchor_mode, h_mode, manual_h,
        )
        payload["history_source"] = source
        payload["history_symbol"] = history_symbol
        return payload

    def _live_snapshot_compute(
        self,
        spot: pd.DataFrame,
        underlying: str,
        timeframe: str,
        lookback_sessions: int,
        anchor_mode: str,
        h_mode: str,
        manual_h: float | None,
    ) -> dict[str, Any]:
        """Pure-CPU portion of live_snapshot — runs in a worker thread (WS-1.1)."""
        frame = self.store.build_feature_frame(spot, timeframe, lookback_sessions=lookback_sessions)
        return self._snapshot(
            frame, underlying=underlying, timeframe=timeframe,
            anchor_mode=anchor_mode, h_mode=h_mode, manual_h=manual_h,
        )

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
        """Trade journal for the AUTONOMOUS paper agent (the supervisor-run trades).

        Historically this read only the manual proposal store (`paper_journal.jsonl`), which is
        empty unless someone POSTs /paper-proposal — so the agent's real autonomous trades (in
        `agent_positions.json` / `paper_agent_journal.jsonl`) were invisible to the UI. It now
        surfaces the agent's closed + open positions as journal rows (and still returns any manual
        proposals separately). Shape stays backward-compatible: `{records, summary}`."""
        status = self.paper_agent.status(limit=200)
        closed = status.get("closed_positions") or []
        openp = status.get("open_positions") or []

        def _row(p: dict[str, Any], kind: str) -> dict[str, Any]:
            return {
                "recorded_at": p.get("updated_at") or p.get("opened_at"),
                "kind": kind,
                "underlying": p.get("underlying"),
                "instrument_type": p.get("instrument_type"),
                "direction": p.get("thesis_side") or p.get("direction"),
                "archetype": p.get("archetype"),
                "regime": p.get("regime"),
                "conviction": p.get("conviction"),
                "opened_at": p.get("opened_at"),
                "closed_at": p.get("updated_at") if kind == "closed" else None,
                "exit_reason": p.get("close_reason") or p.get("exit_reason"),
                "bars_held": p.get("bars_held"),
                "realized_pnl": p.get("realized_pnl"),
                "unrealized_pnl": p.get("unrealized_pnl"),
                "position": p,
            }

        records = [_row(p, "closed") for p in closed] + [_row(p, "open") for p in openp]
        if symbol:
            records = [r for r in records if str(r.get("underlying") or "").upper() == symbol.upper()]
        records.sort(key=lambda r: str(r.get("recorded_at") or ""), reverse=True)
        records = records[: int(limit)]
        manual = self.paper.list(symbol=symbol, limit=int(limit)).get("records", [])
        return {
            "records": records,
            "manual_proposals": manual,
            "summary": {
                **status.get("summary", {}),
                "count": len(records),
                "latest": records[0].get("recorded_at") if records else None,
                "last_scan_at": status.get("last_scan_at"),
                "last_message": status.get("last_message"),
            },
        }

    def paper_agent_status(self, limit: int = 50) -> dict[str, Any]:
        return self.paper_agent.status(limit=limit)

    async def run_paper_agent_once(
        self,
        timeframe: str | None = None,
        lookback_sessions: int | None = None,
        anchor_mode: str | None = None,
        h_mode: str | None = None,
        live_refresh: bool | None = None,
        max_underlyings: int | None = None,
    ) -> dict[str, Any]:
        return await self.paper_agent.run_once(
            timeframe=timeframe,
            lookback_sessions=lookback_sessions,
            anchor_mode=anchor_mode,
            h_mode=h_mode,
            live_refresh=live_refresh,
            max_underlyings=max_underlyings,
        )

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
        if self.config.get("strategy", {}).get("enabled", True):
            signal = evaluate_gann_signal(frame=frame, anchor=anchor, angles=angles, sq9_levels=sq9, cycles=cycles, square=square, h=h.value, config=self.config)
        else:
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
