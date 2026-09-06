"""The four journals.

A journal here is append-only and explicitly NOT the book.  Nothing in this
module is ever read to decide what the lane is holding; `book.py` owns that.
That separation is the whole design — the recurring failure in this stack is a
"journal" that looks like a book, strands rows at status='open', and then gets
believed.

The decision journal is the one that earns its keep.  A lane that records only
its fills cannot answer "why did nothing trade this week", and the answer is
usually one gate doing all the killing.  Every evaluation lands here with the
gate results and the observed values attached, so that question is a GROUP BY.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Sequence

from loguru import logger
from sqlalchemy import text

from db.database import AsyncSessionLocal
from directional_options.index_paper.costs import FillEstimate
from directional_options.index_paper.schemas import GateResult

_INSERT_DECISION = text(
    """
    INSERT INTO index_paper_decisions (
        run_id, decided_at, session_date, bar_ts, underlying, action, reason_code,
        reason, position_id, view, gates, candidate, sizing, vol_context
    ) VALUES (
        :run_id, :decided_at, :session_date, :bar_ts, :underlying, :action, :reason_code,
        :reason, :position_id, CAST(:view AS jsonb), CAST(:gates AS jsonb),
        CAST(:candidate AS jsonb), CAST(:sizing AS jsonb), CAST(:vol_context AS jsonb)
    )
    RETURNING id
    """
)

_INSERT_FILL = text(
    """
    INSERT INTO index_paper_fills (
        position_id, filled_at, intent, side, quantity, reference_price,
        fill_price, half_spread, lag_slippage, impact, charges_total,
        total_cost, spread_vol_points, liquidity_bucket, detail
    ) VALUES (
        :position_id, :filled_at, :intent, :side, :quantity, :reference_price,
        :fill_price, :half_spread, :lag_slippage, :impact, :charges_total,
        :total_cost, :spread_vol_points, :liquidity_bucket, CAST(:detail AS jsonb)
    )
    RETURNING id
    """
)

_UPSERT_MARK = text(
    """
    INSERT INTO index_paper_marks (
        ts, position_id, premium, iv, forward, spot, delta, gamma, vega, theta,
        mv_delta, unrealized_pnl, source
    ) VALUES (
        :ts, :position_id, :premium, :iv, :forward, :spot, :delta, :gamma, :vega,
        :theta, :mv_delta, :unrealized_pnl, :source
    )
    ON CONFLICT (ts, position_id) DO UPDATE SET
        premium = EXCLUDED.premium,
        iv = EXCLUDED.iv,
        forward = EXCLUDED.forward,
        spot = EXCLUDED.spot,
        delta = EXCLUDED.delta,
        gamma = EXCLUDED.gamma,
        vega = EXCLUDED.vega,
        theta = EXCLUDED.theta,
        mv_delta = EXCLUDED.mv_delta,
        unrealized_pnl = EXCLUDED.unrealized_pnl,
        source = EXCLUDED.source
    """
)

_UPSERT_ATTRIBUTION = text(
    """
    INSERT INTO index_paper_attribution (
        position_id, closed_at, underlying, gross_pnl, costs, net_pnl,
        delta_pnl, gamma_pnl, vega_pnl, theta_pnl, residual_pnl,
        d_spot, d_iv, d_sessions, explained_fraction, detail
    ) VALUES (
        :position_id, :closed_at, :underlying, :gross_pnl, :costs, :net_pnl,
        :delta_pnl, :gamma_pnl, :vega_pnl, :theta_pnl, :residual_pnl,
        :d_spot, :d_iv, :d_sessions, :explained_fraction, CAST(:detail AS jsonb)
    )
    ON CONFLICT (position_id) DO UPDATE SET
        closed_at = EXCLUDED.closed_at,
        gross_pnl = EXCLUDED.gross_pnl,
        costs = EXCLUDED.costs,
        net_pnl = EXCLUDED.net_pnl,
        delta_pnl = EXCLUDED.delta_pnl,
        gamma_pnl = EXCLUDED.gamma_pnl,
        vega_pnl = EXCLUDED.vega_pnl,
        theta_pnl = EXCLUDED.theta_pnl,
        residual_pnl = EXCLUDED.residual_pnl,
        d_spot = EXCLUDED.d_spot,
        d_iv = EXCLUDED.d_iv,
        d_sessions = EXCLUDED.d_sessions,
        explained_fraction = EXCLUDED.explained_fraction,
        detail = EXCLUDED.detail
    """
)


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str)


async def record_decision(
    *,
    decided_at: datetime,
    session_date: date,
    underlying: str,
    action: str,
    reason_code: str,
    reason: str = "",
    bar_ts: datetime | None = None,
    position_id: str | None = None,
    view: dict[str, Any] | None = None,
    gates: Sequence[GateResult] | None = None,
    candidate: dict[str, Any] | None = None,
    sizing: dict[str, Any] | None = None,
    vol_context: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> int | None:
    """Journal one evaluation. Called for skips as often as for entries."""
    params = {
        "run_id": run_id,
        "decided_at": decided_at,
        "session_date": session_date,
        "bar_ts": bar_ts,
        "underlying": underlying,
        "action": action,
        "reason_code": reason_code,
        "reason": reason or None,
        "position_id": position_id,
        "view": _dumps(view) if view is not None else None,
        "gates": _dumps([g.as_dict() for g in (gates or [])]),
        "candidate": _dumps(candidate) if candidate is not None else None,
        "sizing": _dumps(sizing) if sizing is not None else None,
        "vol_context": _dumps(vol_context) if vol_context is not None else None,
    }
    async with AsyncSessionLocal() as session:
        row = (await session.execute(_INSERT_DECISION, params)).first()
        await session.commit()
    return int(row.id) if row else None


async def record_fill(
    *,
    position_id: str,
    filled_at: datetime,
    estimate: FillEstimate,
    detail: dict[str, Any] | None = None,
) -> int | None:
    params = {
        "position_id": position_id,
        "filled_at": filled_at,
        "intent": estimate.intent,
        "side": estimate.side,
        "quantity": estimate.quantity,
        "reference_price": estimate.reference_price,
        "fill_price": estimate.fill_price,
        "half_spread": estimate.half_spread,
        "lag_slippage": estimate.lag_slippage,
        "impact": estimate.impact,
        "charges_total": estimate.charges.get("total"),
        "total_cost": estimate.total_cost,
        "spread_vol_points": estimate.spread_vol_points,
        "liquidity_bucket": estimate.liquidity_bucket,
        "detail": _dumps({**(detail or {}), "charges": estimate.charges}),
    }
    async with AsyncSessionLocal() as session:
        row = (await session.execute(_INSERT_FILL, params)).first()
        await session.commit()
    return int(row.id) if row else None


async def record_mark(
    *,
    ts: datetime,
    position_id: str,
    premium: float | None,
    unrealized_pnl: float | None,
    iv: float | None = None,
    forward: float | None = None,
    spot: float | None = None,
    delta: float | None = None,
    gamma: float | None = None,
    vega: float | None = None,
    theta: float | None = None,
    mv_delta: float | None = None,
    source: str = "chain_bar",
) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            _UPSERT_MARK,
            {
                "ts": ts,
                "position_id": position_id,
                "premium": premium,
                "iv": iv,
                "forward": forward,
                "spot": spot,
                "delta": delta,
                "gamma": gamma,
                "vega": vega,
                "theta": theta,
                "mv_delta": mv_delta,
                "unrealized_pnl": unrealized_pnl,
                "source": source,
            },
        )
        await session.commit()


async def record_attribution(payload: dict[str, Any]) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            _UPSERT_ATTRIBUTION,
            {**payload, "detail": _dumps(payload.get("detail") or {})},
        )
        await session.commit()


# ── read side ───────────────────────────────────────────────────────────────

_REJECTION_TALLY = text(
    """
    SELECT reason_code, action, count(*) AS n,
           count(DISTINCT underlying) AS underlyings,
           max(decided_at) AS last_seen
    FROM index_paper_decisions
    WHERE decided_at >= :lower AND decided_at <= :upper
      AND (CAST(:run_id AS text) IS NULL OR run_id = CAST(:run_id AS text))
    GROUP BY reason_code, action
    ORDER BY n DESC
    """
)


async def rejection_tally(
    lower: datetime, upper: datetime, run_id: str | None = None
) -> list[dict[str, Any]]:
    """Where the candidates die.

    The one query this whole journal exists to answer.  When a single
    reason_code owns most of the rows, that gate IS the strategy, whatever the
    design document says.
    """
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                _REJECTION_TALLY, {"lower": lower, "upper": upper, "run_id": run_id}
            )
        ).mappings().all()
    return [dict(r) for r in rows]


_GATE_TALLY = text(
    """
    SELECT g->>'name' AS gate,
           count(*) FILTER (WHERE (g->>'passed')::boolean IS FALSE) AS failed,
           count(*) AS evaluated
    FROM index_paper_decisions d
    CROSS JOIN LATERAL jsonb_array_elements(d.gates) AS g
    WHERE d.decided_at >= :lower AND d.decided_at <= :upper
      AND (CAST(:run_id AS text) IS NULL OR d.run_id = CAST(:run_id AS text))
    GROUP BY 1
    ORDER BY failed DESC
    """
)


async def gate_tally(
    lower: datetime, upper: datetime, run_id: str | None = None
) -> list[dict[str, Any]]:
    """Per-gate failure counts — finer grained than the reason code."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                _GATE_TALLY, {"lower": lower, "upper": upper, "run_id": run_id}
            )
        ).mappings().all()
    return [dict(r) for r in rows]
