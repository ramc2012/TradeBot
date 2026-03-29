"""Persist selection-day spot price for expiry prioritisation.

Revision ID: 004_expiry_selection_spot
Revises: 003_fo_research_cache
Create Date: 2026-03-28 23:05:00.000000
"""
from typing import Sequence, Union

from alembic import op

revision: str = "004_expiry_selection_spot"
down_revision: Union[str, None] = "003_fo_research_cache"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE fo_expiry_catalog
        ADD COLUMN IF NOT EXISTS selection_spot_time TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS selection_spot_price NUMERIC(12,4);
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_fo_expiry_selection_priority
        ON fo_expiry_catalog (underlying, expiry DESC, selection_spot_price);
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_fo_expiry_selection_priority;")
    op.execute("""
        ALTER TABLE fo_expiry_catalog
        DROP COLUMN IF EXISTS selection_spot_time,
        DROP COLUMN IF EXISTS selection_spot_price;
    """)
