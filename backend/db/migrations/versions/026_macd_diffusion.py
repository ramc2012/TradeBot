"""MACD diffusion snapshots — hourly CE/PE breadth (count above zero)

Stores, per hour, how many ATM CE and PE legs have MACD > 0 across the tracked
F&O universe. This is a market-breadth / diffusion index used to sense
sentiment: many CE above zero + few PE above zero ⇒ bullish, and vice-versa.

Plain Postgres table (low volume — one row per hour per market), no hypertable.

Revision ID: 026_macd_diffusion
Revises: 025_directional_paper_book
Create Date: 2026-06-20
"""
from typing import Sequence, Union

from alembic import op


revision: str = "026_macd_diffusion"
down_revision: Union[str, None] = "025_directional_paper_book"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS macd_diffusion_snapshots (
            bucket_time   TIMESTAMPTZ      NOT NULL,
            market        TEXT             NOT NULL DEFAULT 'NSE',
            ce_total      INTEGER          NOT NULL DEFAULT 0,
            ce_above_zero INTEGER          NOT NULL DEFAULT 0,
            pe_total      INTEGER          NOT NULL DEFAULT 0,
            pe_above_zero INTEGER          NOT NULL DEFAULT 0,
            ce_pct        DOUBLE PRECISION,
            pe_pct        DOUBLE PRECISION,
            net_diffusion DOUBLE PRECISION,
            source        TEXT             NOT NULL DEFAULT 'live',
            created_at    TIMESTAMPTZ      NOT NULL DEFAULT now(),
            PRIMARY KEY (market, bucket_time)
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_macd_diffusion_time
        ON macd_diffusion_snapshots (bucket_time DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS macd_diffusion_snapshots;")

