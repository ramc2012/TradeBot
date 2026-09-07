"""The authoritative position book.

One row per position, current state only.  This table is the answer to "what
is the lane holding" — not the journals, not a reconstruction, not a blob.

Two rules are enforced structurally rather than by convention:

  * NOTHING CLEARS THE BOOK.  There is no bulk delete on the write path.  A
    fail-closed guard in this stack once emptied a live risk book from 15
    positions to 0 because it could not verify them; "I could not verify" is
    not "it is invalid", and a book that can be emptied by a verification
    failure is a book that will be.  `reset` exists, takes an explicit
    confirmation token and an actor, and archives before it deletes.

  * A CLOSE IS AN UPDATE, never a delete.  Closed rows stay, because the
    attribution and the honest P&L are computed from them.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime, timezone
from typing import Any, Sequence
from uuid import uuid4

from loguru import logger
from sqlalchemy import text

from db.database import AsyncSessionLocal
from directional_options.index_paper.schemas import PaperPosition

_INSERT = text(
    """
    INSERT INTO index_paper_positions (
        position_id, status, session_date, underlying, expiry, strike,
        option_type, lots, lot_size, quantity, entry_ts, entry_premium,
        entry_cost, entry_iv, entry_forward, entry_spot, entry_delta,
        entry_gamma, entry_vega, entry_theta, entry_mv_delta, stop_premium,
        target_premium, max_hold_bars, latest_ts, latest_premium, latest_iv,
        unrealized_pnl, payload, updated_at
    ) VALUES (
        :position_id, :status, :session_date, :underlying, :expiry, :strike,
        :option_type, :lots, :lot_size, :quantity, :entry_ts, :entry_premium,
        :entry_cost, :entry_iv, :entry_forward, :entry_spot, :entry_delta,
        :entry_gamma, :entry_vega, :entry_theta, :entry_mv_delta, :stop_premium,
        :target_premium, :max_hold_bars, :latest_ts, :latest_premium, :latest_iv,
        :unrealized_pnl, CAST(:payload AS jsonb), now()
    )
    """
)

_UPDATE_MARK = text(
    """
    UPDATE index_paper_positions
    SET latest_ts = :latest_ts,
        latest_premium = :latest_premium,
        latest_iv = :latest_iv,
        unrealized_pnl = :unrealized_pnl,
        updated_at = now()
    WHERE position_id = :position_id AND status = 'open'
    """
)

_CLOSE = text(
    """
    UPDATE index_paper_positions
    SET status = 'closed',
        exit_ts = :exit_ts,
        exit_premium = :exit_premium,
        exit_iv = :exit_iv,
        exit_cost = :exit_cost,
        exit_reason = :exit_reason,
        realized_pnl = :realized_pnl,
        latest_ts = :exit_ts,
        latest_premium = :exit_premium,
        latest_iv = :exit_iv,
        unrealized_pnl = 0,
        updated_at = now()
    WHERE position_id = :position_id AND status = 'open'
    """
)

_SELECT_OPEN = text(
    """
    SELECT * FROM index_paper_positions
    WHERE status = 'open'
      AND (CAST(:underlying AS text) IS NULL OR underlying = CAST(:underlying AS text))
    ORDER BY entry_ts
    """
)

_SELECT_ONE = text("SELECT * FROM index_paper_positions WHERE position_id = :position_id")

_SELECT_CLOSED = text(
    """
    SELECT * FROM index_paper_positions
    WHERE status = 'closed'
      AND (CAST(:underlying AS text) IS NULL OR underlying = CAST(:underlying AS text))
    ORDER BY exit_ts DESC
    LIMIT :limit
    """
)


def new_position_id(underlying: str) -> str:
    return f"idxp-{underlying.lower()}-{uuid4().hex[:12]}"


def _row_to_position(row: Any) -> PaperPosition:
    payload = row["payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    return PaperPosition(
        position_id=row["position_id"],
        status=row["status"],
        session_date=row["session_date"],
        underlying=row["underlying"],
        expiry=row["expiry"],
        strike=float(row["strike"]),
        option_type=row["option_type"],
        lots=int(row["lots"]),
        lot_size=int(row["lot_size"]),
        quantity=int(row["quantity"]),
        entry_ts=row["entry_ts"],
        entry_premium=float(row["entry_premium"]),
        entry_cost=float(row["entry_cost"] or 0.0),
        entry_iv=row["entry_iv"],
        entry_forward=row["entry_forward"],
        entry_spot=row["entry_spot"],
        entry_delta=row["entry_delta"],
        entry_gamma=row["entry_gamma"],
        entry_vega=row["entry_vega"],
        entry_theta=row["entry_theta"],
        entry_mv_delta=row["entry_mv_delta"],
        stop_premium=row["stop_premium"],
        target_premium=row["target_premium"],
        max_hold_bars=row["max_hold_bars"],
        latest_ts=row["latest_ts"],
        latest_premium=row["latest_premium"],
        latest_iv=row["latest_iv"],
        unrealized_pnl=row["unrealized_pnl"],
        exit_ts=row["exit_ts"],
        exit_premium=row["exit_premium"],
        exit_iv=row["exit_iv"],
        exit_cost=row["exit_cost"],
        exit_reason=row["exit_reason"],
        realized_pnl=row["realized_pnl"],
        payload=payload or {},
    )


async def open_position(position: PaperPosition) -> PaperPosition:
    params = position.as_dict()
    params["session_date"] = position.session_date
    params["expiry"] = position.expiry
    params["entry_ts"] = position.entry_ts
    params["latest_ts"] = position.latest_ts
    params["exit_ts"] = None
    params["payload"] = json.dumps(position.payload, default=str)
    for key in ("exit_premium", "exit_iv", "exit_cost", "exit_reason", "realized_pnl"):
        params.pop(key, None)
    async with AsyncSessionLocal() as session:
        await session.execute(_INSERT, params)
        await session.commit()
    logger.info(
        f"[index_paper] OPEN {position.position_id} {position.underlying} "
        f"{position.strike:g}{position.option_type} x{position.lots} lots "
        f"@ {position.entry_premium:.2f} (cost {position.entry_cost:.0f})"
    )
    return position


async def mark_position(
    position_id: str,
    *,
    latest_ts: datetime,
    latest_premium: float,
    latest_iv: float | None,
    unrealized_pnl: float,
) -> bool:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            _UPDATE_MARK,
            {
                "position_id": position_id,
                "latest_ts": latest_ts,
                "latest_premium": latest_premium,
                "latest_iv": latest_iv,
                "unrealized_pnl": unrealized_pnl,
            },
        )
        await session.commit()
    return result.rowcount > 0


async def close_position(
    position_id: str,
    *,
    exit_ts: datetime,
    exit_premium: float,
    exit_iv: float | None,
    exit_cost: float,
    exit_reason: str,
    realized_pnl: float,
) -> bool:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            _CLOSE,
            {
                "position_id": position_id,
                "exit_ts": exit_ts,
                "exit_premium": exit_premium,
                "exit_iv": exit_iv,
                "exit_cost": exit_cost,
                "exit_reason": exit_reason,
                "realized_pnl": realized_pnl,
            },
        )
        await session.commit()
    closed = result.rowcount > 0
    if closed:
        logger.info(
            f"[index_paper] CLOSE {position_id} @ {exit_premium:.2f} "
            f"({exit_reason}) realised {realized_pnl:+,.0f}"
        )
    else:
        logger.warning(f"[index_paper] close skipped — {position_id} not open")
    return closed


async def get_position(position_id: str) -> PaperPosition | None:
    async with AsyncSessionLocal() as session:
        row = (await session.execute(_SELECT_ONE, {"position_id": position_id})).mappings().first()
    return _row_to_position(row) if row else None


async def list_open(underlying: str | None = None) -> list[PaperPosition]:
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(_SELECT_OPEN, {"underlying": underlying})).mappings().all()
    return [_row_to_position(r) for r in rows]


async def list_closed(underlying: str | None = None, limit: int = 100) -> list[PaperPosition]:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(_SELECT_CLOSED, {"underlying": underlying, "limit": limit})
        ).mappings().all()
    return [_row_to_position(r) for r in rows]


_SUMMARY = text(
    """
    SELECT
        count(*) FILTER (WHERE status = 'open')                     AS open_positions,
        count(*) FILTER (WHERE status = 'closed')                   AS closed_positions,
        coalesce(sum(unrealized_pnl) FILTER (WHERE status='open'), 0)  AS unrealized_pnl,
        coalesce(sum(realized_pnl)   FILTER (WHERE status='closed'), 0) AS realized_pnl,
        coalesce(sum(entry_cost) + sum(coalesce(exit_cost, 0)), 0)  AS total_costs,
        count(*) FILTER (WHERE status='closed' AND realized_pnl > 0) AS wins,
        count(*) FILTER (WHERE status='closed' AND realized_pnl <= 0) AS losses,
        coalesce(sum(entry_premium * quantity) FILTER (WHERE status='open'), 0) AS open_premium_outlay
    FROM index_paper_positions
    """
)


async def summary(initial_capital: float) -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        row = (await session.execute(_SUMMARY)).mappings().first()
    data = dict(row) if row else {}
    realized = float(data.get("realized_pnl") or 0.0)
    unrealized = float(data.get("unrealized_pnl") or 0.0)
    wins = int(data.get("wins") or 0)
    losses = int(data.get("losses") or 0)
    closed = wins + losses
    return {
        "initial_capital": initial_capital,
        "equity": initial_capital + realized + unrealized,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "total_costs": float(data.get("total_costs") or 0.0),
        "open_positions": int(data.get("open_positions") or 0),
        "closed_positions": int(data.get("closed_positions") or 0),
        "open_premium_outlay": float(data.get("open_premium_outlay") or 0.0),
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / closed) if closed else None,
    }


async def reset(*, actor: str, confirm_token: str) -> dict[str, Any]:
    """Archive-then-clear. Requires an explicit token; never called by the engine.

    The archive is a timestamped table copy, so a reset that turns out to have
    been a mistake is recoverable.  This is the only path that removes rows,
    and no automated guard is allowed to call it.
    """
    if confirm_token != "RESET-INDEX-PAPER-BOOK":
        return {"reset": False, "reason": "confirmation token missing or wrong"}

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive = f"index_paper_positions_bkp_{stamp}"
    async with AsyncSessionLocal() as session:
        await session.execute(text(f'CREATE TABLE "{archive}" AS SELECT * FROM index_paper_positions'))
        result = await session.execute(text("DELETE FROM index_paper_positions"))
        await session.commit()
    logger.warning(f"[index_paper] BOOK RESET by {actor}; {result.rowcount} rows archived to {archive}")
    return {"reset": True, "archived_to": archive, "rows": result.rowcount, "actor": actor}
