"""Anchor selection for Gann TP Delta geometry."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pandas as pd

from gann_tp_delta.schemas import AnchorPoint


def _anchor_from_row(frame: pd.DataFrame, index: int, *, mode: str, kind: str, price_column: str, strength: str) -> AnchorPoint:
    row = frame.iloc[index]
    return AnchorPoint(
        mode=mode,
        kind=kind,
        bar_index=int(index),
        time=pd.Timestamp(row["time"]).isoformat(),
        price=float(row[price_column]),
        strength=strength,
    )


def confirmed_pivots(frame: pd.DataFrame, left: int, right: int) -> list[AnchorPoint]:
    pivots: list[AnchorPoint] = []
    if frame.empty or len(frame.index) < left + right + 1:
        return pivots
    highs = frame["high"].astype(float).tolist()
    lows = frame["low"].astype(float).tolist()
    for index in range(left, len(frame.index) - right):
        high_window = highs[index - left : index + right + 1]
        low_window = lows[index - left : index + right + 1]
        if highs[index] >= max(high_window):
            pivots.append(_anchor_from_row(frame, index, mode="auto_pivot", kind="swing_high", price_column="high", strength="confirmed"))
        if lows[index] <= min(low_window):
            pivots.append(_anchor_from_row(frame, index, mode="auto_pivot", kind="swing_low", price_column="low", strength="confirmed"))
    return sorted(pivots, key=lambda item: item.bar_index)


def select_anchor(
    frame: pd.DataFrame,
    *,
    mode: str,
    config: dict[str, Any],
    manual_time: str | None = None,
    manual_price: float | None = None,
    session_mode: str | None = None,
) -> AnchorPoint | None:
    if frame.empty:
        return None
    normalized = str(mode or "auto_pivot").lower()
    if normalized == "manual" and manual_price is not None:
        if manual_time:
            ts = pd.Timestamp(manual_time)
            distances = (frame["time"] - ts).abs()
            index = int(distances.idxmin())
        else:
            index = max(len(frame.index) - 1, 0)
        row = frame.iloc[index]
        return AnchorPoint(
            mode="manual",
            kind="manual",
            bar_index=index,
            time=pd.Timestamp(row["time"]).isoformat(),
            price=float(manual_price),
            strength="manual",
        )
    if normalized == "session":
        return _session_anchor(frame, session_mode or str(config.get("session_mode") or "previous_day"))
    pivots = confirmed_pivots(frame, int(config["pivot_left"]), int(config["pivot_right"]))
    tradeable = [pivot for pivot in pivots if pivot.bar_index < len(frame.index) - int(config["pivot_right"])]
    if tradeable:
        return tradeable[-1]
    last = len(frame.index) - 1
    return _anchor_from_row(frame, last, mode="fallback", kind="last_close", price_column="close", strength="fallback")


def pivot_vectors(frame: pd.DataFrame, config: dict[str, Any]) -> list[dict[str, Any]]:
    pivots = confirmed_pivots(frame, int(config["pivot_left"]), int(config["pivot_right"]))
    vectors: list[dict[str, Any]] = []
    for prev, current in zip(pivots, pivots[1:]):
        bars = current.bar_index - prev.bar_index
        if bars <= 0:
            continue
        price_delta = current.price - prev.price
        vectors.append(
            {
                "from": asdict(prev),
                "to": asdict(current),
                "bars": bars,
                "price_delta": price_delta,
                "tpd": price_delta / bars,
                "abs_tpd": abs(price_delta / bars),
            }
        )
    return vectors[-int(config["pivot_vector_count"]) :]


def _session_anchor(frame: pd.DataFrame, session_mode: str) -> AnchorPoint:
    dates = frame["time"].dt.date
    unique_dates = sorted(set(dates))
    if len(unique_dates) >= 2 and session_mode == "previous_day":
        target = unique_dates[-2]
        session = frame.loc[dates == target]
        low_index = int(session["low"].idxmin())
        high_index = int(session["high"].idxmax())
        latest_close = float(frame.iloc[-1]["close"])
        low = float(frame.loc[low_index, "low"])
        high = float(frame.loc[high_index, "high"])
        return _anchor_from_row(
            frame,
            low_index if abs(latest_close - low) <= abs(latest_close - high) else high_index,
            mode="session",
            kind="previous_day_low" if abs(latest_close - low) <= abs(latest_close - high) else "previous_day_high",
            price_column="low" if abs(latest_close - low) <= abs(latest_close - high) else "high",
            strength="session",
        )
    if session_mode == "monthly_open":
        row = frame.iloc[0]
        month = pd.Timestamp(frame.iloc[-1]["time"]).month
        rows = frame.loc[frame["time"].dt.month == month]
        index = int(rows.index[0]) if not rows.empty else 0
        row = frame.iloc[index]
        return AnchorPoint("session", "monthly_open", index, pd.Timestamp(row["time"]).isoformat(), float(row["open"]), "session")
    return _anchor_from_row(frame, 0, mode="session", kind="range_start", price_column="open", strength="session")
