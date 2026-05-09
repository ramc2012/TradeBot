"""Add market column to fo_contract_catalog for NSE/BSE routing.

Revision ID: 014_fo_contract_catalog_market
Revises: 013_fractal_market_profile
Create Date: 2026-04-16 23:40:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "014_fo_contract_catalog_market"
down_revision: Union[str, None] = "013_fractal_market_profile"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE fo_contract_catalog
        ADD COLUMN IF NOT EXISTS market TEXT NOT NULL DEFAULT 'NSE'
        """
    )
    op.execute(
        """
        UPDATE fo_contract_catalog
        SET market = CASE
            WHEN underlying IN ('SENSEX', 'BANKEX') THEN 'BSE'
            ELSE 'NSE'
        END
        WHERE market IS NULL OR market = '' OR market = 'NSE'
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_fo_contract_catalog_market_underlying_expiry
        ON fo_contract_catalog (market, underlying, expiry)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_fo_contract_catalog_market_underlying_expiry")
    op.execute("ALTER TABLE fo_contract_catalog DROP COLUMN IF EXISTS market")
