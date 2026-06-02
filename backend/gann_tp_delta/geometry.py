"""Gann geometry, Square of Nine, cycles, and price-time squaring."""
from __future__ import annotations

import math
from typing import Any

from gann_tp_delta.schemas import AnchorPoint, GannAngle, PriceTimeSquare, SquareNineLevel, TimeCycleWindow


def fan_direction(anchor: AnchorPoint) -> str:
    return "bearish" if "high" in anchor.kind else "bullish"


def gann_fan(
    *,
    anchor: AnchorPoint,
    h: float,
    current_bar_index: int,
    current_price: float,
    ratios: list[tuple[str, float]],
    projection_bars: int,
) -> list[GannAngle]:
    elapsed = max(current_bar_index - anchor.bar_index, 0)
    direction = fan_direction(anchor)
    sign = -1.0 if direction == "bearish" else 1.0
    angles: list[GannAngle] = []
    for name, ratio in ratios:
        slope = h * float(ratio) * sign
        current_level = anchor.price + slope * elapsed
        projected = anchor.price + slope * max(elapsed + projection_bars, 0)
        distance = current_price - current_level
        angles.append(
            GannAngle(
                name=name,
                ratio=float(ratio),
                direction=direction,
                anchor_price=anchor.price,
                anchor_bar_index=anchor.bar_index,
                slope=slope,
                current_price=current_level,
                projected_price=projected,
                distance=distance,
                distance_pct=abs(distance) / max(abs(current_price), 1.0),
            )
        )
    return angles


def square_of_nine(
    *,
    anchor_price: float,
    current_price: float,
    price_unit: float,
    degrees: list[int],
) -> list[SquareNineLevel]:
    unit = max(float(price_unit), 0.0001)
    root = math.sqrt(max(anchor_price / unit, 0.0001))
    levels: list[SquareNineLevel] = []
    for direction in ("upside", "downside"):
        sign = 1.0 if direction == "upside" else -1.0
        for degree in degrees:
            price = ((root + (float(degree) / 180.0) * sign) ** 2) * unit
            distance = current_price - price
            levels.append(
                SquareNineLevel(
                    degree=int(degree),
                    direction=direction,
                    price=price,
                    level_type="cardinal" if degree in {90, 180, 270, 360} else "ordinal",
                    distance=distance,
                    distance_pct=abs(distance) / max(abs(current_price), 1.0),
                )
            )
    return sorted(levels, key=lambda item: item.price)


def time_cycles(
    *,
    anchor: AnchorPoint,
    current_bar_index: int,
    cycles: list[int],
    window_bars: int,
) -> list[TimeCycleWindow]:
    windows: list[TimeCycleWindow] = []
    tolerance = max(int(window_bars), 0)
    for cycle in cycles:
        center = anchor.bar_index + int(cycle)
        distance = current_bar_index - center
        windows.append(
            TimeCycleWindow(
                cycle=int(cycle),
                start_bar_index=center - tolerance,
                center_bar_index=center,
                end_bar_index=center + tolerance,
                active=abs(distance) <= tolerance,
                distance_bars=distance,
            )
        )
    return windows


def price_time_square(
    *,
    anchor: AnchorPoint,
    current_bar_index: int,
    current_price: float,
    h: float,
    tolerance: float,
) -> PriceTimeSquare:
    elapsed = max(current_bar_index - anchor.bar_index, 0)
    if elapsed <= 0 or h <= 0:
        return PriceTimeSquare(False, 0.0, 0.0, elapsed, float(tolerance))
    scaled = abs(current_price - anchor.price) / h
    ratio = scaled / elapsed
    return PriceTimeSquare(abs(ratio - 1.0) <= float(tolerance), ratio, scaled, elapsed, float(tolerance))


def angle_by_name(angles: list[GannAngle], name: str) -> GannAngle | None:
    return next((item for item in angles if item.name == name), None)


def nearest_angle(angles: list[GannAngle]) -> GannAngle | None:
    return min(angles, key=lambda item: item.distance_pct, default=None)


def nearest_sq9(levels: list[SquareNineLevel]) -> SquareNineLevel | None:
    return min(levels, key=lambda item: item.distance_pct, default=None)
