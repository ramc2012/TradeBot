"""The four journals.

A journal here is append-only and explicitly NOT the book.  Nothing in this
module is ever read to decide what the lane is holding; `book.py` owns that.
That separation is the whole design — the recurring failure in this stack is a
"journal" that looks like a book, strands rows at status='open', and then gets
believed.

The FACTOR journal is what turns this lane into an instrument. Every factor
records its value at every decision — acting or shadowed, available or not — so
a per-factor information coefficient can be computed months later against
outcomes this lane actually experienced, rather than by re-fitting on the same
history that suggested the factor. A factor with weight zero is still measured.

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


_UPSERT_FACTOR = text(
    """
    INSERT INTO index_paper_factors (
        bar_ts, underlying, factor, run_id, session_date, family, role, value,
        score, sign, confidence, weight, status, acting, direction_score,
        size_score, detail
    ) VALUES (
        :bar_ts, :underlying, :factor, :run_id, :session_date, :family, :role, :value,
        :score, :sign, :confidence, :weight, :status, :acting, :direction_score,
        :size_score, :detail
    )
    ON CONFLICT (bar_ts, underlying, factor) DO UPDATE SET
        run_id = EXCLUDED.run_id,
        value = EXCLUDED.value,
        score = EXCLUDED.score,
        weight = EXCLUDED.weight,
        status = EXCLUDED.status,
        acting = EXCLUDED.acting,
        direction_score = EXCLUDED.direction_score,
        size_score = EXCLUDED.size_score,
        detail = EXCLUDED.detail
    """
)


async def record_factor_panel(panel: Any, *, run_id: str | None, bar_ts: datetime) -> int:
    """Write every factor in the panel, including the ones that did nothing."""
    payload = panel.as_dict()
    direction_score = payload.get("direction_score")
    size_score = payload.get("size_score")
    rows = [
        {
            "bar_ts": bar_ts,
            "underlying": panel.underlying,
            "factor": name,
            "run_id": run_id,
            "session_date": panel.session_date,
            "family": f.get("family"),
            "role": f.get("role"),
            "value": f.get("value"),
            "score": f.get("score"),
            "sign": f.get("sign"),
            "confidence": f.get("confidence"),
            "weight": f.get("weight"),
            "status": f.get("status"),
            "acting": f.get("acting"),
            "direction_score": direction_score,
            "size_score": size_score,
            "detail": (f.get("detail") or "")[:500],
        }
        for name, f in (payload.get("factors") or {}).items()
    ]
    if not rows:
        return 0
    async with AsyncSessionLocal() as session:
        await session.execute(_UPSERT_FACTOR, rows)
        await session.commit()
    return len(rows)


_FACTOR_IC_SQL = text(
    """
    SELECT f.factor,
           count(*)                                   AS n,
           corr(f.score, o.signed_outcome)            AS ic_signed,
           corr(f.score, abs(o.signed_outcome))       AS ic_magnitude,
           avg(f.weight)                              AS avg_weight
    FROM index_paper_factors f
    JOIN (
        SELECT underlying, session_date,
               CASE WHEN option_type = 'CE' THEN 1 ELSE -1 END
                 * (realized_pnl / NULLIF(entry_premium * quantity, 0)) AS signed_outcome
        FROM index_paper_positions
        WHERE status = 'closed'
    ) o ON o.underlying = f.underlying AND o.session_date = f.session_date
    WHERE f.score IS NOT NULL
    GROUP BY f.factor
    HAVING count(*) >= :min_n
    ORDER BY abs(coalesce(corr(f.score, o.signed_outcome), 0)) DESC
    """
)


async def factor_ic(min_n: int = 20) -> list[dict[str, Any]]:
    """Per-factor information coefficient against this lane's own outcomes.

    The whole point of the factor journal. Returns nothing useful until enough
    trades have closed — which is the honest state for months, and is why the
    `min_n` floor refuses rather than reporting a correlation over five points.
    """
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(_FACTOR_IC_SQL, {"min_n": min_n})).mappings().all()
    return [dict(r) for r in rows]
