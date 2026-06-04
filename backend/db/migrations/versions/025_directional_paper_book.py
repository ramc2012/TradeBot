"""DB-persisted directional paper book (positions + journal).

The directional paper book lived in JSON files (paper_positions.json,
paper_journal.jsonl). That meant: no durable history (closed trades capped
at 250 then dropped), nothing queryable for analysis, and a fragile single
file that froze marks / lost the book on a container recreate. Move it to
TimescaleDB-friendly tables.

Each position is one row keyed by position_id with the full dict preserved
in a JSONB `payload` (so no logic depends on a fixed column set) PLUS
extracted key columns for querying (status, underlying, expiry, strike,
P&L, timestamps). Closed positions ACCUMULATE — durable trade history.

Revision ID: 025_directional_paper_book
Revises: 024_market_depth_totals
Create Date: 2026-06-04
"""
from typing import Sequence, Union

from alembic import op


revision: str = "025_directional_paper_book"
down_revision: Union[str, None] = "024_market_depth_totals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS directional_paper_positions (
            position_id     TEXT PRIMARY KEY,
            status          TEXT NOT NULL,
            underlying      TEXT,
            expiry          TEXT,
            strike          DOUBLE PRECISION,
            option_type     TEXT,
            direction       TEXT,
            quantity_units  INTEGER,
            entry_premium   DOUBLE PRECISION,
            latest_premium  DOUBLE PRECISION,
            exit_premium    DOUBLE PRECISION,
            unrealized_pnl  DOUBLE PRECISION,
            realized_pnl    DOUBLE PRECISION,
            opened_at       TIMESTAMPTZ,
            updated_at      TIMESTAMPTZ,
            closed_at       TIMESTAMPTZ,
            close_reason    TEXT,
            payload         JSONB NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_dir_paper_pos_status "
        "ON directional_paper_positions (status, closed_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_dir_paper_pos_underlying "
        "ON directional_paper_positions (underlying, status)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS directional_paper_journal (
            id            BIGSERIAL PRIMARY KEY,
            recorded_at   TIMESTAMPTZ,
            underlying    TEXT,
            approved      BOOLEAN,
            payload       JSONB NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_dir_paper_journal_time "
        "ON directional_paper_journal (recorded_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_dir_paper_journal_underlying "
        "ON directional_paper_journal (underlying, recorded_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS directional_paper_journal")
    op.execute("DROP TABLE IF EXISTS directional_paper_positions")
