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
    # ── Regime-gated engine (v2) — all optional for backward-compat ──────────
    # `bias`/`state`/`score` above are still populated so the existing
    # service/agent/frontend keep working; these add the richer decision.
    regime: str = "neutral"            # bull | bear | neutral
    regime_strength: float = 0.0       # 0..1 trend-strength proxy (ADX-scaled)
    archetype: str | None = None       # continuation | reversal | None
    side: str | None = None            # long | short | None
    conviction: float = 0.0            # weighted, exactness-scaled (~0..10)
    size_factor: float = 1.0           # <1 shrinks counter-trend reversals
    confirmation: bool = False         # reversal confirmation bar present
    stop_underlying: float | None = None   # invalidating level on the UNDERLYING
    targets_underlying: list[float] = field(default_factory=list)
    risk_per_unit: float | None = None     # |entry_underlying - stop_underlying|
    score_breakdown: dict[str, float] = field(default_factory=dict)
    # ── Rule-contract engine (v3) ────────────────────────────────────────────
    # `state` remains backward-compatible. `setup_state` is the desk/execution
    # lifecycle and `candidate_archetype` keeps a blocked near-miss auditable.
    setup_state: str = "SEARCHING"       # SEARCHING | WATCHING | ARMED | ACTIONABLE | BLOCKED
    candidate_archetype: str | None = None
    minimum_conviction: float = 0.0
    conviction_gap: float = 0.0
    selected_level: str | None = None
    rule_checks: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    regime_votes: dict[str, int] = field(default_factory=dict)
    adx: float | None = None
    active_timing: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AlertEvent:
    key: str
    severity: str
    message: str
    payload: dict[str, Any]
