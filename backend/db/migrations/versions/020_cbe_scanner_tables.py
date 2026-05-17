"""Persist CBE scanner runs and ranked results.

Revision ID: 020_cbe_scanner_tables
Revises: 019_prune_agent_risk_state
Create Date: 2026-05-17
"""
from typing import Sequence, Union

from alembic import op


revision: str = "020_cbe_scanner_tables"
down_revision: Union[str, None] = "019_prune_agent_risk_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS cbe_scan_runs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            source TEXT NOT NULL,
            scan_date DATE NOT NULL,
            universe_size INTEGER NOT NULL DEFAULT 0,
            scored_count INTEGER NOT NULL DEFAULT 0,
            watchlist_count INTEGER NOT NULL DEFAULT 0,
            config JSONB,
            source_status JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cbe_scan_runs_created
            ON cbe_scan_runs (created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cbe_scan_runs_scan_date
            ON cbe_scan_runs (scan_date DESC, source)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS cbe_scan_results (
            run_id UUID NOT NULL REFERENCES cbe_scan_runs(id) ON DELETE CASCADE,
            rank INTEGER NOT NULL,
            instrument TEXT NOT NULL,
            composite_score DOUBLE PRECISION NOT NULL DEFAULT 0,
            directional_bias TEXT NOT NULL DEFAULT 'neutral',
            bias_conviction DOUBLE PRECISION NOT NULL DEFAULT 0,
            f1_vc_score DOUBLE PRECISION NOT NULL DEFAULT 0,
            f2_omp_score DOUBLE PRECISION NOT NULL DEFAULT 0,
            f3_csmd_score DOUBLE PRECISION NOT NULL DEFAULT 0,
            f4_cp_score DOUBLE PRECISION NOT NULL DEFAULT 0,
            f5_mp_score DOUBLE PRECISION NOT NULL DEFAULT 0,
            is_watchlist BOOLEAN NOT NULL DEFAULT FALSE,
            details JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (run_id, instrument)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cbe_scan_results_watchlist
            ON cbe_scan_results (is_watchlist, composite_score DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cbe_scan_results_instrument
            ON cbe_scan_results (instrument, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS cbe_scan_results")
    op.execute("DROP TABLE IF EXISTS cbe_scan_runs")
