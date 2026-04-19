"""Historical bridge revision for orphan Cloud SQL stamp.

Revision ID: 006_analytics_tables_plain_pg
Revises: 005_atm_watchlist_history
Create Date: 2026-04-16 23:10:00.000000
"""
from typing import Sequence, Union


revision: str = "006_analytics_tables_plain_pg"
down_revision: Union[str, None] = "005_atm_watchlist_history"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Bridge old Cloud SQL revision IDs into the current Alembic chain."""


def downgrade() -> None:
    """No-op historical bridge."""
