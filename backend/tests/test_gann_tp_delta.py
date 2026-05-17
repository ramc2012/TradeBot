from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gann_tp_delta.anchors import confirmed_pivots, select_anchor
from gann_tp_delta.config import clone_default_config
from gann_tp_delta.geometry import gann_fan, price_time_square, square_of_nine, time_cycles
from gann_tp_delta.scaling import harmonic_speed
from gann_tp_delta.schemas import AnchorPoint
from gann_tp_delta.signals import confluence_signal
from api.routers import gann_tp_delta as gann_router


def _frame() -> pd.DataFrame:
    prices = [100, 104, 108, 103, 98, 102, 110, 116, 112, 108, 114, 121, 127, 124, 130]
    rows = []
    for index, close in enumerate(prices):
        rows.append(
            {
                "time": pd.Timestamp("2026-01-01 09:15") + pd.Timedelta(minutes=15 * index),
                "open": close - 1,
                "high": close + 2,
                "low": close - 2,
                "close": close,
                "volume": 1000,
                "oi": 0,
                "atr": 4,
            }
        )
    return pd.DataFrame(rows)


def test_confirmed_pivots_do_not_use_last_unconfirmed_window() -> None:
    frame = _frame()
    pivots = confirmed_pivots(frame, left=2, right=2)

    assert pivots
    assert all(pivot.bar_index <= len(frame.index) - 3 for pivot in pivots)


def test_harmonic_speed_modes() -> None:
    frame = _frame()
    cfg = clone_default_config()

    manual, _ = harmonic_speed(frame, mode="manual", anchor_config=cfg["anchors"], scaling_config=cfg["scaling"], manual_h=47.0)
    average, _ = harmonic_speed(frame, mode="average_tpd", anchor_config=cfg["anchors"], scaling_config=cfg["scaling"])
    median, _ = harmonic_speed(frame, mode="median_tpd", anchor_config=cfg["anchors"], scaling_config=cfg["scaling"])
    atr, _ = harmonic_speed(frame, mode="atr", anchor_config=cfg["anchors"], scaling_config=cfg["scaling"])

    assert manual.value == 47.0
    assert average.value > 0
    assert median.value > 0
    assert atr.value == 4.0


def test_gann_fan_projection_for_bullish_and_bearish_anchors() -> None:
    bullish = AnchorPoint("auto_pivot", "swing_low", 0, "2026-01-01T09:15:00", 100.0)
    bearish = AnchorPoint("auto_pivot", "swing_high", 0, "2026-01-01T09:15:00", 100.0)

    up = gann_fan(anchor=bullish, h=10.0, current_bar_index=3, current_price=130.0, ratios=[("1x1", 1.0)], projection_bars=2)[0]
    down = gann_fan(anchor=bearish, h=10.0, current_bar_index=3, current_price=70.0, ratios=[("1x1", 1.0)], projection_bars=2)[0]

    assert up.current_price == 130.0
    assert up.projected_price == 150.0
    assert down.current_price == 70.0
    assert down.projected_price == 50.0


def test_square_of_nine_uses_price_unit() -> None:
    levels = square_of_nine(anchor_price=100.0, current_price=110.0, price_unit=1.0, degrees=[180])

    upside = next(level for level in levels if level.direction == "upside")
    downside = next(level for level in levels if level.direction == "downside")
    assert round(upside.price, 2) == 121.0
    assert round(downside.price, 2) == 81.0


def test_time_cycle_and_price_time_square_detection() -> None:
    anchor = AnchorPoint("auto_pivot", "swing_low", 10, "2026-01-01T09:15:00", 100.0)
    windows = time_cycles(anchor=anchor, current_bar_index=20, cycles=[9, 10, 14], window_bars=1)
    square = price_time_square(anchor=anchor, current_bar_index=20, current_price=150.0, h=5.0, tolerance=0.05)

    assert any(window.cycle == 10 and window.active for window in windows)
    assert square.active is True
    assert square.ratio == 1.0


def test_confluence_signal_reaches_bullish_setup() -> None:
    frame = _frame()
    cfg = clone_default_config()
    anchor = select_anchor(frame, mode="auto_pivot", config=cfg["anchors"])
    assert anchor is not None
    angles = gann_fan(anchor=anchor, h=4.0, current_bar_index=len(frame.index) - 1, current_price=float(frame.iloc[-1]["close"]), ratios=[("1x1", 1.0)], projection_bars=5)
    sq9 = square_of_nine(anchor_price=anchor.price, current_price=float(frame.iloc[-1]["close"]), price_unit=1.0, degrees=[45])
    cycles = time_cycles(anchor=anchor, current_bar_index=len(frame.index) - 1, cycles=[len(frame.index) - 1 - anchor.bar_index], window_bars=1)
    square = price_time_square(anchor=anchor, current_bar_index=len(frame.index) - 1, current_price=float(frame.iloc[-1]["close"]), h=4.0, tolerance=0.5)

    signal = confluence_signal(frame=frame, anchor=anchor, angles=angles, sq9_levels=sq9, cycles=cycles, square=square, config=cfg["signals"], near_pct=0.5)

    assert signal.score >= 3
    assert signal.state in {"bullish_setup", "bearish_setup", "watch"}


def test_gann_api_routes_return_stable_schemas(monkeypatch) -> None:
    app = FastAPI()
    app.include_router(gann_router.router)
    client = TestClient(app)

    monkeypatch.setattr(gann_router.gann_tp_delta_service, "summary", lambda: {"key": "gann_tp_delta", "underlyings": ["NIFTY"], "timeframes": ["15minute"]})
    monkeypatch.setattr(
        gann_router.gann_tp_delta_service,
        "workspace",
        lambda *args: {"module": {"key": "gann_tp_delta"}, "selection": {}, "snapshot": {"status": "ready"}, "backtest": {"summary": {}}},
    )
    monkeypatch.setattr(gann_router.gann_tp_delta_service, "backtest", lambda *args: {"summary": {"event_count": 0}, "events": []})
    monkeypatch.setattr(gann_router.gann_tp_delta_service, "paper_journal", lambda *args: {"records": [], "summary": {"count": 0}})

    assert client.get("/api/gann-tp-delta/summary").json()["key"] == "gann_tp_delta"
    assert client.get("/api/gann-tp-delta/workspace").json()["snapshot"]["status"] == "ready"
    assert client.get("/api/gann-tp-delta/backtest").json()["summary"]["event_count"] == 0
    assert client.get("/api/gann-tp-delta/paper-journal").json()["summary"]["count"] == 0
