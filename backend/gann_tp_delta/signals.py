"""Confluence scoring and signal construction."""
from __future__ import annotations

from typing import Any

import pandas as pd

from gann_tp_delta.geometry import fan_direction, nearest_angle, nearest_sq9
from gann_tp_delta.schemas import AlertEvent, AnchorPoint, ConfluenceSignal, GannAngle, PriceTimeSquare, SquareNineLevel, TimeCycleWindow


def trend_structure(frame: pd.DataFrame, lookback: int) -> str:
    if frame.empty or len(frame.index) < max(lookback, 3):
        return "neutral"
    recent = frame.tail(int(lookback))
    first = recent.iloc[0]
    last = recent.iloc[-1]
    higher = float(last["high"]) > float(first["high"]) and float(last["low"]) > float(first["low"])
    lower = float(last["high"]) < float(first["high"]) and float(last["low"]) < float(first["low"])
    if higher:
        return "bullish"
    if lower:
        return "bearish"
    return "neutral"


def confluence_signal(
    *,
    frame: pd.DataFrame,
    anchor: AnchorPoint,
    angles: list[GannAngle],
    sq9_levels: list[SquareNineLevel],
    cycles: list[TimeCycleWindow],
    square: PriceTimeSquare,
    config: dict[str, Any],
    near_pct: float,
) -> ConfluenceSignal:
    current = frame.iloc[-1]
    close = float(current["close"])
    high = float(current["high"])
    low = float(current["low"])
    atr = max(float(current.get("atr", 0.0)), close * 0.002)
    nearest_gann = nearest_angle(angles)
    nearest_level = nearest_sq9(sq9_levels)
    active_cycle = next((item for item in cycles if item.active), None)
    structure = trend_structure(frame, int(config["structure_lookback"]))
    direction = fan_direction(anchor)
    expected_bias = "bearish" if direction == "bearish" else "bullish"

    reasons: list[str] = []
    score = 0
    if nearest_gann and nearest_gann.distance_pct <= near_pct:
        score += 1
        reasons.append(f"near {nearest_gann.direction} {nearest_gann.name}")
    if nearest_level and nearest_level.distance_pct <= near_pct:
        score += 1
        reasons.append(f"near SQ9 {nearest_level.degree} {nearest_level.direction}")
    if active_cycle:
        score += 1
        reasons.append(f"cycle {active_cycle.cycle} active")
    if square.active:
        score += 1
        reasons.append("price-time square active")
    if structure == expected_bias:
        score += 1
        reasons.append(f"{structure} structure confirms")

    threshold = int(config["score_threshold"])
    if expected_bias == "bullish":
        trigger = high
        stop = min(low, (nearest_gann.current_price if nearest_gann else low)) - atr * float(config["atr_stop_multiplier"])
        targets = _targets_above(close, angles, sq9_levels)
        state = "bullish_setup" if score >= threshold and close >= (nearest_gann.current_price if nearest_gann else close) else "watch"
    else:
        trigger = low
        stop = max(high, (nearest_gann.current_price if nearest_gann else high)) + atr * float(config["atr_stop_multiplier"])
        targets = _targets_below(close, angles, sq9_levels)
        state = "bearish_setup" if score >= threshold and close <= (nearest_gann.current_price if nearest_gann else close) else "watch"
    if score < threshold:
        state = "ignore" if score <= 1 else "watch"
    return ConfluenceSignal(score, threshold, expected_bias, state, reasons, trigger, stop, targets[:3])


def alert_events(signal: ConfluenceSignal, angles: list[GannAngle], levels: list[SquareNineLevel], cycles: list[TimeCycleWindow], square: PriceTimeSquare, near_pct: float) -> list[AlertEvent]:
    events: list[AlertEvent] = []
    for angle in angles:
        if angle.distance_pct <= near_pct:
            events.append(AlertEvent("gann_angle_touch", "info", f"Price near {angle.direction} {angle.name}", {"angle": angle.name}))
            break
    for level in levels:
        if level.distance_pct <= near_pct:
            events.append(AlertEvent("sq9_touch", "info", f"Price near SQ9 {level.degree} {level.direction}", {"degree": level.degree}))
            break
    active_cycle = next((item for item in cycles if item.active), None)
    if active_cycle:
        events.append(AlertEvent("time_window_active", "info", f"Gann time window {active_cycle.cycle} active", {"cycle": active_cycle.cycle}))
    if square.active:
        events.append(AlertEvent("price_time_square", "info", "Price-time square formed", {"ratio": square.ratio}))
    if signal.state in {"bullish_setup", "bearish_setup"}:
        events.append(AlertEvent("confluence_score_reached", "action", f"{signal.bias} confluence score {signal.score}/{signal.threshold}", {"bias": signal.bias, "score": signal.score}))
    return events


def _targets_above(close: float, angles: list[GannAngle], levels: list[SquareNineLevel]) -> list[float]:
    values = [item.current_price for item in angles if item.current_price > close]
    values.extend(item.price for item in levels if item.price > close)
    return sorted({round(value, 2) for value in values})


def _targets_below(close: float, angles: list[GannAngle], levels: list[SquareNineLevel]) -> list[float]:
    values = [item.current_price for item in angles if item.current_price < close]
    values.extend(item.price for item in levels if item.price < close)
    return sorted({round(value, 2) for value in values}, reverse=True)
