"""Lightweight research backtest for Gann TP Delta confluence events."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pandas as pd

from gann_tp_delta.anchors import select_anchor
from gann_tp_delta.geometry import gann_fan, price_time_square, square_of_nine, time_cycles
from gann_tp_delta.scaling import harmonic_speed
from gann_tp_delta.signals import confluence_signal


class GannTPDeltaBacktester:
    def __init__(self, config: dict[str, Any]):
        self.config = config

    def run(self, frame: pd.DataFrame, *, anchor_mode: str, h_mode: str) -> dict[str, Any]:
        if frame.empty or len(frame.index) < 30:
            return self._empty("Not enough candles for backtest.")
        events: list[dict[str, Any]] = []
        cfg = self.config
        geometry_cfg = cfg["geometry"]
        for end in range(30, len(frame.index)):
            window = frame.iloc[: end + 1].reset_index(drop=True)
            anchor = select_anchor(window, mode=anchor_mode, config=cfg["anchors"])
            if anchor is None:
                continue
            h, _ = harmonic_speed(window, mode=h_mode, anchor_config=cfg["anchors"], scaling_config=cfg["scaling"])
            current = window.iloc[-1]
            close = float(current["close"])
            angles = gann_fan(
                anchor=anchor,
                h=h.value,
                current_bar_index=end,
                current_price=close,
                ratios=geometry_cfg["gann_ratios"],
                projection_bars=int(geometry_cfg["projection_bars"]),
            )
            sq9 = square_of_nine(anchor_price=anchor.price, current_price=close, price_unit=float(geometry_cfg["price_unit"]), degrees=geometry_cfg["sq9_degrees"])
            cycles = time_cycles(anchor=anchor, current_bar_index=end, cycles=geometry_cfg["bar_cycles"], window_bars=int(geometry_cfg["cycle_window_bars"]))
            square = price_time_square(anchor=anchor, current_bar_index=end, current_price=close, h=h.value, tolerance=float(geometry_cfg["squaring_tolerance"]))
            signal = confluence_signal(
                frame=window,
                anchor=anchor,
                angles=angles,
                sq9_levels=sq9,
                cycles=cycles,
                square=square,
                config=cfg["signals"],
                near_pct=float(geometry_cfg["near_pct"]),
            )
            if signal.state not in {"bullish_setup", "bearish_setup"}:
                continue
            next_index = min(end + 3, len(frame.index) - 1)
            future_close = float(frame.iloc[next_index]["close"])
            pnl = future_close - close if signal.bias == "bullish" else close - future_close
            events.append(
                {
                    "time": pd.Timestamp(current["time"]).isoformat(),
                    "bias": signal.bias,
                    "score": signal.score,
                    "entry": close,
                    "future_close": future_close,
                    "pnl_points": round(pnl, 2),
                    "state": signal.state,
                }
            )
        events = events[-int(cfg["backtest"]["max_events"]) :]
        wins = [event for event in events if float(event["pnl_points"]) > 0]
        losses = [event for event in events if float(event["pnl_points"]) < 0]
        total = sum(float(event["pnl_points"]) for event in events)
        return {
            "summary": {
                "event_count": len(events),
                "total_points": round(total, 2),
                "win_rate_pct": round((len(wins) / len(events)) * 100.0, 2) if events else 0.0,
                "avg_win": round(sum(float(event["pnl_points"]) for event in wins) / len(wins), 2) if wins else 0.0,
                "avg_loss": round(sum(float(event["pnl_points"]) for event in losses) / len(losses), 2) if losses else 0.0,
            },
            "events": events,
        }

    @staticmethod
    def _empty(reason: str) -> dict[str, Any]:
        return {
            "summary": {"event_count": 0, "total_points": 0.0, "win_rate_pct": 0.0, "avg_win": 0.0, "avg_loss": 0.0},
            "events": [],
            "reason": reason,
        }
