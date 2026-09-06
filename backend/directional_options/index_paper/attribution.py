"""Greek P&L attribution for closed positions.

Without this you cannot tell a correct directional call from a wrong one that
was rescued by a vol expansion, and feature selection then runs on noise.  The
lane's whole learning loop depends on knowing WHICH greek paid.

Second-order Taylor expansion of the premium change, evaluated at the entry
greeks:

    dV  ~  delta*dS  +  0.5*gamma*dS^2  +  vega*d_sigma  +  theta*d_days  +  e

Everything is per unit of underlying and per 1.00 of sigma, matching
`OptionGreeks`, so no percent-versus-fraction conversion appears anywhere — a
100x rendering error from exactly that confusion has already cost this stack a
month of misread market-profile returns.

The residual `e` is reported, not hidden.  A large residual is information: it
means the move was big enough that entry greeks stopped describing the
position, which is itself a finding about the holding period.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text

from db.database import AsyncSessionLocal
from directional_options.index_paper.schemas import PaperPosition

SECONDS_PER_DAY = 86400.0


@dataclass
class Attribution:
    position_id: str
    underlying: str
    closed_at: datetime
    gross_pnl: float
    costs: float
    net_pnl: float
    delta_pnl: float
    gamma_pnl: float
    vega_pnl: float
    theta_pnl: float
    residual_pnl: float
    d_spot: float | None
    d_iv: float | None
    d_sessions: float | None
    explained_fraction: float | None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "underlying": self.underlying,
            "closed_at": self.closed_at,
            "gross_pnl": self.gross_pnl,
            "costs": self.costs,
            "net_pnl": self.net_pnl,
            "delta_pnl": self.delta_pnl,
            "gamma_pnl": self.gamma_pnl,
            "vega_pnl": self.vega_pnl,
            "theta_pnl": self.theta_pnl,
            "residual_pnl": self.residual_pnl,
            "d_spot": self.d_spot,
            "d_iv": self.d_iv,
            "d_sessions": self.d_sessions,
            "explained_fraction": self.explained_fraction,
            "detail": self.detail,
        }

    def dominant_driver(self) -> str:
        parts = {
            "delta": abs(self.delta_pnl),
            "gamma": abs(self.gamma_pnl),
            "vega": abs(self.vega_pnl),
            "theta": abs(self.theta_pnl),
            "residual": abs(self.residual_pnl),
        }
        return max(parts, key=parts.get)


def attribute(
    position: PaperPosition,
    *,
    exit_spot: float | None,
    exit_iv: float | None,
) -> Attribution:
    """Decompose one closed position's P&L.

    `gross_pnl` is the premium move times quantity, before costs.  `net_pnl`
    subtracts the modelled entry and exit costs and is the number that should
    be believed — the difference between the two is exactly what a mid-priced
    paper book would have overstated.
    """
    quantity = position.quantity
    entry_premium = position.entry_premium
    exit_premium = position.exit_premium if position.exit_premium is not None else entry_premium

    gross = (exit_premium - entry_premium) * quantity
    costs = float(position.entry_cost or 0.0) + float(position.exit_cost or 0.0)
    net = gross - costs

    d_spot = None
    if exit_spot is not None and position.entry_spot:
        d_spot = float(exit_spot) - float(position.entry_spot)

    d_iv = None
    if exit_iv is not None and position.entry_iv is not None:
        d_iv = float(exit_iv) - float(position.entry_iv)

    d_sessions = None
    if position.exit_ts and position.entry_ts:
        d_sessions = (position.exit_ts - position.entry_ts).total_seconds() / SECONDS_PER_DAY

    delta_pnl = gamma_pnl = vega_pnl = theta_pnl = 0.0
    if d_spot is not None and position.entry_delta is not None:
        delta_pnl = float(position.entry_delta) * d_spot * quantity
    if d_spot is not None and position.entry_gamma is not None:
        gamma_pnl = 0.5 * float(position.entry_gamma) * d_spot * d_spot * quantity
    if d_iv is not None and position.entry_vega is not None:
        vega_pnl = float(position.entry_vega) * d_iv * quantity
    if d_sessions is not None and position.entry_theta is not None:
        theta_pnl = float(position.entry_theta) * d_sessions * quantity

    explained = delta_pnl + gamma_pnl + vega_pnl + theta_pnl
    residual = gross - explained

    explained_fraction: float | None = None
    if abs(gross) > 1e-9:
        explained_fraction = 1.0 - abs(residual) / abs(gross)

    missing = [
        name
        for name, value in (
            ("exit_spot", exit_spot),
            ("exit_iv", exit_iv),
            ("entry_delta", position.entry_delta),
            ("entry_vega", position.entry_vega),
        )
        if value is None
    ]

    return Attribution(
        position_id=position.position_id,
        underlying=position.underlying,
        closed_at=position.exit_ts or position.entry_ts,
        gross_pnl=gross,
        costs=costs,
        net_pnl=net,
        delta_pnl=delta_pnl,
        gamma_pnl=gamma_pnl,
        vega_pnl=vega_pnl,
        theta_pnl=theta_pnl,
        residual_pnl=residual,
        d_spot=d_spot,
        d_iv=d_iv,
        d_sessions=d_sessions,
        explained_fraction=explained_fraction,
        detail={
            "entry_premium": entry_premium,
            "exit_premium": exit_premium,
            "quantity": quantity,
            "entry_greeks": {
                "delta": position.entry_delta,
                "gamma": position.entry_gamma,
                "vega": position.entry_vega,
                "theta": position.entry_theta,
                "mv_delta": position.entry_mv_delta,
            },
            "exit_reason": position.exit_reason,
            "incomplete_inputs": missing,
        },
    )


# ── path-wise attribution ───────────────────────────────────────────────────

_MARKS_SQL = text(
    """
    SELECT ts, premium, iv, spot, delta, gamma, vega, theta
    FROM index_paper_marks
    WHERE position_id = :position_id
    ORDER BY ts
    """
)


async def load_marks(position_id: str) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(_MARKS_SQL, {"position_id": position_id})).mappings().all()
    return [dict(r) for r in rows]


def attribute_pathwise(
    position: PaperPosition,
    marks: list[dict[str, Any]],
    *,
    exit_spot: float | None,
    exit_iv: float | None,
) -> Attribution:
    """Sum the expansion over the MARK TRAIL instead of entry-to-exit.

    A single expansion evaluated at the entry greeks is only accurate while the
    greeks stay put, and over a multi-bar hold on a short-dated option they do
    not — which is why the entry-to-exit form leaves a residual worth roughly a
    third of the P&L.  Walking the path re-evaluates each leg at the greeks that
    were actually live for it, and pushes almost all of that residual into the
    terms where it belongs.

    Falls back to the single-step form when fewer than two path points exist.
    """
    path: list[dict[str, Any]] = [
        {
            "ts": position.entry_ts,
            "premium": position.entry_premium,
            "iv": position.entry_iv,
            "spot": position.entry_spot,
            "delta": position.entry_delta,
            "gamma": position.entry_gamma,
            "vega": position.entry_vega,
            "theta": position.entry_theta,
        }
    ]
    for mark in marks:
        if position.entry_ts and mark["ts"] <= position.entry_ts:
            continue
        if position.exit_ts and mark["ts"] > position.exit_ts:
            continue
        path.append(dict(mark))

    if position.exit_ts and (not path or path[-1]["ts"] != position.exit_ts):
        path.append(
            {
                "ts": position.exit_ts,
                "premium": position.exit_premium,
                "iv": exit_iv,
                "spot": exit_spot,
                "delta": None, "gamma": None, "vega": None, "theta": None,
            }
        )

    if len(path) < 2:
        return attribute(position, exit_spot=exit_spot, exit_iv=exit_iv)

    quantity = position.quantity
    delta_pnl = gamma_pnl = vega_pnl = theta_pnl = 0.0
    legs: list[dict[str, Any]] = []

    for start, end in zip(path, path[1:]):
        d_spot = _diff(end.get("spot"), start.get("spot"))
        d_iv = _diff(end.get("iv"), start.get("iv"))
        d_days = None
        if start.get("ts") and end.get("ts"):
            d_days = (end["ts"] - start["ts"]).total_seconds() / SECONDS_PER_DAY

        leg_delta = _term(start.get("delta"), d_spot) * quantity
        leg_gamma = 0.5 * _term(start.get("gamma"), (d_spot * d_spot) if d_spot is not None else None) * quantity
        leg_vega = _term(start.get("vega"), d_iv) * quantity
        leg_theta = _term(start.get("theta"), d_days) * quantity

        delta_pnl += leg_delta
        gamma_pnl += leg_gamma
        vega_pnl += leg_vega
        theta_pnl += leg_theta
        legs.append(
            {
                "from": start.get("ts"),
                "to": end.get("ts"),
                "d_spot": d_spot,
                "d_iv": d_iv,
                "d_days": d_days,
                "delta": leg_delta,
                "gamma": leg_gamma,
                "vega": leg_vega,
                "theta": leg_theta,
            }
        )

    exit_premium = position.exit_premium if position.exit_premium is not None else position.entry_premium
    gross = (exit_premium - position.entry_premium) * quantity
    costs = float(position.entry_cost or 0.0) + float(position.exit_cost or 0.0)
    explained = delta_pnl + gamma_pnl + vega_pnl + theta_pnl
    residual = gross - explained

    explained_fraction = None
    if abs(gross) > 1e-9:
        explained_fraction = 1.0 - abs(residual) / abs(gross)

    total_d_spot = _diff(path[-1].get("spot"), path[0].get("spot"))
    total_d_iv = _diff(path[-1].get("iv"), path[0].get("iv"))
    total_days = None
    if path[0].get("ts") and path[-1].get("ts"):
        total_days = (path[-1]["ts"] - path[0]["ts"]).total_seconds() / SECONDS_PER_DAY

    return Attribution(
        position_id=position.position_id,
        underlying=position.underlying,
        closed_at=position.exit_ts or position.entry_ts,
        gross_pnl=gross,
        costs=costs,
        net_pnl=gross - costs,
        delta_pnl=delta_pnl,
        gamma_pnl=gamma_pnl,
        vega_pnl=vega_pnl,
        theta_pnl=theta_pnl,
        residual_pnl=residual,
        d_spot=total_d_spot,
        d_iv=total_d_iv,
        d_sessions=total_days,
        explained_fraction=explained_fraction,
        detail={
            "method": "pathwise",
            "path_points": len(path),
            "entry_premium": position.entry_premium,
            "exit_premium": exit_premium,
            "quantity": quantity,
            "exit_reason": position.exit_reason,
            "legs": legs[-12:],
        },
    )


def _diff(end: Any, start: Any) -> float | None:
    if end is None or start is None:
        return None
    try:
        return float(end) - float(start)
    except (TypeError, ValueError):
        return None


def _term(greek: Any, change: float | None) -> float:
    if greek is None or change is None:
        return 0.0
    try:
        return float(greek) * float(change)
    except (TypeError, ValueError):
        return 0.0
