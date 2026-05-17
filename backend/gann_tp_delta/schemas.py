"""Typed payloads for the Gann TP Delta harmonic module."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AnchorPoint:
    mode: str
    kind: str
    bar_index: int
    time: str
    price: float
    strength: str = "confirmed"


@dataclass(frozen=True)
class HarmonicSpeed:
    mode: str
    value: float
    unit: str
    sample_count: int
    source: str


@dataclass(frozen=True)
class GannAngle:
    name: str
    ratio: float
    direction: str
    anchor_price: float
    anchor_bar_index: int
    slope: float
    current_price: float
    projected_price: float
    distance: float
    distance_pct: float


@dataclass(frozen=True)
class SquareNineLevel:
    degree: int
    direction: str
    price: float
    level_type: str
    distance: float
    distance_pct: float


@dataclass(frozen=True)
class TimeCycleWindow:
    cycle: int
    start_bar_index: int
    center_bar_index: int
    end_bar_index: int
    active: bool
    distance_bars: int


@dataclass(frozen=True)
class PriceTimeSquare:
    active: bool
    ratio: float
    scaled_price_move: float
    time_bars: int
    tolerance: float


@dataclass(frozen=True)
class ConfluenceSignal:
    score: int
    threshold: int
    bias: str
    state: str
    reasons: list[str] = field(default_factory=list)
    trigger: float | None = None
    stop: float | None = None
    targets: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class AlertEvent:
    key: str
    severity: str
    message: str
    payload: dict[str, Any]
