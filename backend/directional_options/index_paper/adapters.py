"""Bringing an existing directional view into this lane without coupling to it.

The established directional lane already produces a view every cycle and
persists it to `directional_paper_journal` — direction, confidence, expected
move and horizon.  Reading that row is a far smaller commitment than importing
`directional_options.service`, which drags in the RL policy, the RAG context
and the stock universe, none of which this index-only lane wants.

If the upstream lane is off, `latest_view` returns a FLAT view carrying the
reason.  A flat view is a legitimate answer that still gets journalled, which
is the difference between "the lane decided not to trade" and "the lane was
broken and nobody noticed".
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from db.database import AsyncSessionLocal
from directional_options.index_paper.schemas import DirectionalView

_LATEST_VIEW = text(
    """
    SELECT recorded_at, underlying, payload
    FROM directional_paper_journal
    WHERE underlying = :underlying
      AND recorded_at >= :lower
      AND recorded_at <= :upper
    ORDER BY recorded_at DESC
    LIMIT 1
    """
)

_DIRECTION_MAP = {"CE": "long", "PE": "short", "LONG": "long", "SHORT": "short"}


def view_from_journal_payload(
    underlying: str,
    payload: dict[str, Any],
    *,
    source: str = "directional_paper_journal",
) -> DirectionalView:
    raw_direction = str(payload.get("direction") or "").upper()
    direction = _DIRECTION_MAP.get(raw_direction, "flat")

    spot = payload.get("latest_spot")
    expected_move = payload.get("expected_move")
    expected_move_pct = 0.0
    if spot and expected_move:
        try:
            expected_move_pct = abs(float(expected_move)) / float(spot)
        except (TypeError, ValueError, ZeroDivisionError):
            expected_move_pct = 0.0

    return DirectionalView(
        underlying=underlying,
        direction=direction,
        confidence=_safe_float(payload.get("confidence"), 0.0),
        horizon_bars=int(payload.get("expected_horizon_bars") or 3),
        expected_move_pct=expected_move_pct,
        source=source,
        thesis=str(payload.get("selection_reason") or ""),
        regime=str(payload.get("regime") or ""),
        metadata={
            "timeframe": payload.get("timeframe"),
            "iv_sizing_factor": payload.get("iv_sizing_factor"),
            "execution_ready": payload.get("execution_ready"),
            "upstream_approved": payload.get("approved"),
            "latest_spot": spot,
            "expected_move_points": expected_move,
        },
    )


def flat_view(underlying: str, reason: str, source: str = "none") -> DirectionalView:
    return DirectionalView(
        underlying=underlying,
        direction="flat",
        confidence=0.0,
        horizon_bars=0,
        expected_move_pct=0.0,
        source=source,
        thesis=reason,
    )


async def latest_view(
    underlying: str,
    *,
    as_of: datetime | None = None,
    max_age_minutes: float = 120.0,
) -> DirectionalView:
    upper = as_of or datetime.now(timezone.utc)
    lower = upper - timedelta(minutes=max_age_minutes * 4)

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                _LATEST_VIEW, {"underlying": underlying, "lower": lower, "upper": upper}
            )
        ).mappings().first()

    if not row:
        return flat_view(underlying, f"no upstream view in the last {lower:%Y-%m-%d %H:%M} window")

    recorded_at = row["recorded_at"]
    age_minutes = (upper - recorded_at).total_seconds() / 60.0 if recorded_at else None
    if age_minutes is not None and age_minutes > max_age_minutes:
        return flat_view(
            underlying,
            f"upstream view is {age_minutes:.0f} minutes old, past the {max_age_minutes:.0f} minute ceiling",
            source="directional_paper_journal",
        )

    payload = row["payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return flat_view(underlying, "upstream view payload is not valid JSON")

    view = view_from_journal_payload(underlying, payload or {})
    view.metadata["view_age_minutes"] = age_minutes
    view.metadata["recorded_at"] = recorded_at.isoformat() if recorded_at else None
    return view


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
