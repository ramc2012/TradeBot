"""Add lot_size to fo_underlying_catalog; seed NSE-mandated index lot sizes.

Revision ID: 010_underlying_lot_size
Revises: 009_rl_qtable
Create Date: 2026-04-07 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "010_underlying_lot_size"
down_revision: Union[str, None] = "009_rl_qtable"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# NSE-mandated lot sizes (as of 2026-Q1)
_INDEX_LOT_SIZES = {
    "NIFTY":       25,
    "BANKNIFTY":   15,
    "FINNIFTY":    40,
    "MIDCPNIFTY":  75,
    "NIFTYNXT50":  25,
    "SENSEX":      10,
    "BANKEX":      15,
}


def upgrade() -> None:
    # Add lot_size column (nullable so existing rows aren't broken)
    op.execute(
        "ALTER TABLE fo_underlying_catalog ADD COLUMN IF NOT EXISTS lot_size INTEGER"
    )
    # Seed known NSE index lot sizes
    for symbol, lot_size in _INDEX_LOT_SIZES.items():
        op.execute(
            f"UPDATE fo_underlying_catalog SET lot_size = {lot_size} WHERE symbol = '{symbol}'"
        )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE fo_underlying_catalog DROP COLUMN IF EXISTS lot_size"
    )
