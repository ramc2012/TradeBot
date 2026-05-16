"""Daily MWPL and F&O ban-list snapshots from NSE.

Two new tables back the Stage 6 (Risk & Margin) MWPL / ban-list view the
blueprint calls for:

  fo_mwpl_snapshot       — one row per (date, symbol) with market-wide
                           position limit, current OI, utilisation %.
                           Sourced from NSE's daily fao_mwpl CSV.

  fo_security_ban        — daily ban list (symbols that hit 95% MWPL).
                           Sourced from NSE's fao_security_ban CSV; a
                           symbol on the list cannot take fresh F&O
                           positions until utilisation drops below 80%.

Revision ID: 018_fo_mwpl_ban_list
Revises: 017_agent_audit_events
Create Date: 2026-05-16
"""
from typing import Sequence, Union

from alembic import op


revision: str = "018_fo_mwpl_ban_list"
down_revision: Union[str, None] = "017_agent_audit_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS fo_mwpl_snapshot (
            snapshot_date DATE NOT NULL,
            symbol TEXT NOT NULL,
            market_wide_position_limit BIGINT NULL,
            open_interest BIGINT NULL,
            utilisation_pct DOUBLE PRECISION NULL,
            source TEXT NOT NULL DEFAULT 'nse_fao_mwpl_csv',
            synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (snapshot_date, symbol)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_fo_mwpl_snapshot_symbol
            ON fo_mwpl_snapshot (symbol, snapshot_date DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS fo_security_ban (
            snapshot_date DATE NOT NULL,
            symbol TEXT NOT NULL,
            reason TEXT NULL,
            source TEXT NOT NULL DEFAULT 'nse_fao_security_ban_csv',
            synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (snapshot_date, symbol)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_fo_security_ban_symbol
            ON fo_security_ban (symbol, snapshot_date DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS fo_security_ban")
    op.execute("DROP TABLE IF EXISTS fo_mwpl_snapshot")
