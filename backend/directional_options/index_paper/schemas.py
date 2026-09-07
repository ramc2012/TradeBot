"""Value types shared across the index paper lane."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

Side = Literal["CE", "PE"]
Action = Literal["enter", "skip", "hold", "exit"]


@dataclass(frozen=True)
class Contract:
    underlying: str
    expiry: date
    strike: float
    option_type: Side
    lot_size: int

    @property
    def key(self) -> str:
        return f"{self.underlying}|{self.expiry.isoformat()}|{self.strike:g}|{self.option_type}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "expiry": self.expiry.isoformat(),
            "strike": self.strike,
            "option_type": self.option_type,
            "lot_size": self.lot_size,
            "key": self.key,
        }


@dataclass(frozen=True)
class DirectionalView:
    """The lane's directional input, deliberately kept minimal.

    This module supplies discipline — surface-aware strike choice, vol-aware
    sizing, honest fills, attribution — around a view it does not itself
    produce.  Adapting an existing signal engine to this shape is a few lines;
    coupling the paper engine to one is not reversible.
    """

    underlying: str
    direction: Literal["long", "short", "flat"]
    confidence: float
    horizon_bars: int
    expected_move_pct: float
    source: str
    thesis: str = ""
    regime: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def side(self) -> Side | None:
        if self.direction == "long":
            return "CE"
        if self.direction == "short":
            return "PE"
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "direction": self.direction,
            "side": self.side,
            "confidence": self.confidence,
            "horizon_bars": self.horizon_bars,
            "expected_move_pct": self.expected_move_pct,
            "source": self.source,
            "thesis": self.thesis,
            "regime": self.regime,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class GateResult:
    """One named check, its verdict, and the number behind it.

    Storing the observed value alongside the verdict is what makes a rejection
    journal analysable later — "flow_gate failed" tells you nothing, but
    "flow_gate failed at 0.31 against 0.45" tells you whether the threshold or
    the world was the problem.
    """

    name: str
    passed: bool
    observed: float | None = None
    threshold: float | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "observed": self.observed,
            "threshold": self.threshold,
            "detail": self.detail,
        }


@dataclass
class PaperPosition:
    position_id: str
    status: str
    session_date: date
    underlying: str
    expiry: date
    strike: float
    option_type: Side
    lots: int
    lot_size: int
    quantity: int
    entry_ts: datetime
    entry_premium: float
    entry_cost: float
    entry_iv: float | None = None
    entry_forward: float | None = None
    entry_spot: float | None = None
    entry_delta: float | None = None
    entry_gamma: float | None = None
    entry_vega: float | None = None
    entry_theta: float | None = None
    entry_mv_delta: float | None = None
    stop_premium: float | None = None
    target_premium: float | None = None
    max_hold_bars: int | None = None
    latest_ts: datetime | None = None
    latest_premium: float | None = None
    latest_iv: float | None = None
    unrealized_pnl: float | None = None
    exit_ts: datetime | None = None
    exit_premium: float | None = None
    exit_iv: float | None = None
    exit_cost: float | None = None
    exit_reason: str | None = None
    realized_pnl: float | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    def as_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "status": self.status,
            "session_date": self.session_date.isoformat(),
            "underlying": self.underlying,
            "expiry": self.expiry.isoformat(),
            "strike": self.strike,
            "option_type": self.option_type,
            "lots": self.lots,
            "lot_size": self.lot_size,
            "quantity": self.quantity,
            "entry_ts": self.entry_ts.isoformat(),
            "entry_premium": self.entry_premium,
            "entry_cost": self.entry_cost,
            "entry_iv": self.entry_iv,
            "entry_forward": self.entry_forward,
            "entry_spot": self.entry_spot,
            "entry_delta": self.entry_delta,
            "entry_gamma": self.entry_gamma,
            "entry_vega": self.entry_vega,
            "entry_theta": self.entry_theta,
            "entry_mv_delta": self.entry_mv_delta,
            "stop_premium": self.stop_premium,
            "target_premium": self.target_premium,
            "max_hold_bars": self.max_hold_bars,
            "latest_ts": self.latest_ts.isoformat() if self.latest_ts else None,
            "latest_premium": self.latest_premium,
            "latest_iv": self.latest_iv,
            "unrealized_pnl": self.unrealized_pnl,
            "exit_ts": self.exit_ts.isoformat() if self.exit_ts else None,
            "exit_premium": self.exit_premium,
            "exit_iv": self.exit_iv,
            "exit_cost": self.exit_cost,
            "exit_reason": self.exit_reason,
            "realized_pnl": self.realized_pnl,
            "payload": self.payload,
        }
