"""Tables for the index paper lane.

Five tables, and the split between them is the point:

  index_paper_positions    THE BOOK.  Authoritative.  Current state only.
  index_paper_decisions    every evaluation, including every skip and refusal
  index_paper_fills        what each entry and exit actually cost
  index_paper_marks        the mark-to-market trail while a position is open
  index_paper_attribution  the greek decomposition of each closed trade

The separation exists because this stack has repeatedly been misled by
write-only journals — `agent_positions` and `agent_signals` strand rows at
status='open' forever and do not describe any real book.  Here the journals are
explicitly NOT the book: nothing reads them to decide what is held, and the
book is never rebuilt by replaying them.
"""
from __future__ import annotations

from sqlalchemy import text

from db.database import AsyncSessionLocal

_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS index_paper_positions (
        position_id     text        PRIMARY KEY,
        status          text        NOT NULL,
        session_date    date        NOT NULL,
        underlying      text        NOT NULL,
        expiry          date        NOT NULL,
        strike          double precision NOT NULL,
        option_type     text        NOT NULL,
        lots            integer     NOT NULL,
        lot_size        integer     NOT NULL,
        quantity        integer     NOT NULL,
        entry_ts        timestamptz NOT NULL,
        entry_premium   double precision NOT NULL,
        entry_cost      double precision NOT NULL DEFAULT 0,
        entry_iv        double precision,
        entry_forward   double precision,
        entry_spot      double precision,
        entry_delta     double precision,
        entry_gamma     double precision,
        entry_vega      double precision,
        entry_theta     double precision,
        entry_mv_delta  double precision,
        stop_premium    double precision,
        target_premium  double precision,
        max_hold_bars   integer,
        latest_ts       timestamptz,
        latest_premium  double precision,
        latest_iv       double precision,
        unrealized_pnl  double precision,
        exit_ts         timestamptz,
        exit_premium    double precision,
        exit_iv         double precision,
        exit_cost       double precision,
        exit_reason     text,
        realized_pnl    double precision,
        payload         jsonb       NOT NULL DEFAULT '{}'::jsonb,
        updated_at      timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ipp_status ON index_paper_positions (status, underlying)",
    "CREATE INDEX IF NOT EXISTS idx_ipp_session ON index_paper_positions (session_date DESC)",
    """
    CREATE TABLE IF NOT EXISTS index_paper_decisions (
        id            bigserial   PRIMARY KEY,
        run_id        text,
        decided_at    timestamptz NOT NULL,
        session_date  date        NOT NULL,
        bar_ts        timestamptz,
        underlying    text        NOT NULL,
        action        text        NOT NULL,
        reason_code   text        NOT NULL,
        reason        text,
        position_id   text,
        view          jsonb,
        gates         jsonb       NOT NULL DEFAULT '[]'::jsonb,
        candidate     jsonb,
        sizing        jsonb,
        vol_context   jsonb,
        created_at    timestamptz NOT NULL DEFAULT now()
    )
    """,
    "ALTER TABLE index_paper_decisions ADD COLUMN IF NOT EXISTS run_id text",
    "CREATE INDEX IF NOT EXISTS idx_ipd_time ON index_paper_decisions (decided_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ipd_run ON index_paper_decisions (run_id, decided_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ipd_reason ON index_paper_decisions (reason_code, decided_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ipd_underlying ON index_paper_decisions (underlying, decided_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS index_paper_fills (
        id              bigserial   PRIMARY KEY,
        position_id     text        NOT NULL,
        filled_at       timestamptz NOT NULL,
        intent          text        NOT NULL,
        side            text        NOT NULL,
        quantity        integer     NOT NULL,
        reference_price double precision NOT NULL,
        fill_price      double precision NOT NULL,
        half_spread     double precision,
        lag_slippage    double precision,
        impact          double precision,
        charges_total   double precision,
        total_cost      double precision,
        spread_vol_points double precision,
        liquidity_bucket text,
        detail          jsonb       NOT NULL DEFAULT '{}'::jsonb,
        created_at      timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ipf_position ON index_paper_fills (position_id, filled_at)",
    """
    CREATE TABLE IF NOT EXISTS index_paper_marks (
        ts              timestamptz NOT NULL,
        position_id     text        NOT NULL,
        premium         double precision,
        iv              double precision,
        forward         double precision,
        spot            double precision,
        delta           double precision,
        gamma           double precision,
        vega            double precision,
        theta           double precision,
        mv_delta        double precision,
        unrealized_pnl  double precision,
        source          text,
        created_at      timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (ts, position_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ipm_position ON index_paper_marks (position_id, ts DESC)",
    """
    CREATE TABLE IF NOT EXISTS index_paper_attribution (
        position_id   text        PRIMARY KEY,
        closed_at     timestamptz NOT NULL,
        underlying    text        NOT NULL,
        gross_pnl     double precision,
        costs         double precision,
        net_pnl       double precision,
        delta_pnl     double precision,
        gamma_pnl     double precision,
        vega_pnl      double precision,
        theta_pnl     double precision,
        residual_pnl  double precision,
        d_spot        double precision,
        d_iv          double precision,
        d_sessions    double precision,
        explained_fraction double precision,
        detail        jsonb       NOT NULL DEFAULT '{}'::jsonb,
        created_at    timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ipa_closed ON index_paper_attribution (closed_at DESC)",
)


async def ensure_tables() -> None:
    async with AsyncSessionLocal() as session:
        for statement in _DDL:
            await session.execute(text(statement))
        await session.commit()
