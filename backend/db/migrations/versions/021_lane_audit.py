"""Per-lane per-day audit records for the lane-trustworthiness framework.

Revision ID: 021_lane_audit
Revises: 020_cbe_scanner_tables
Create Date: 2026-05-22
"""
from typing import Sequence, Union

from alembic import op


revision: str = "021_lane_audit"
down_revision: Union[str, None] = "020_cbe_scanner_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS lane_audit (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            lane TEXT NOT NULL,
            audit_date DATE NOT NULL,
            window_start TIMESTAMPTZ NOT NULL,
            window_end TIMESTAMPTZ NOT NULL,

            -- Invariant 1: data integrity
            data_gaps JSONB NOT NULL DEFAULT '[]'::jsonb,
            freshness_violations INTEGER NOT NULL DEFAULT 0,
            data_integrity_pass BOOLEAN NOT NULL DEFAULT FALSE,

            -- Invariant 2: replay parity
            replay_signals INTEGER NOT NULL DEFAULT 0,
            live_signals INTEGER NOT NULL DEFAULT 0,
            replay_match_count INTEGER NOT NULL DEFAULT 0,
            replay_mismatches JSONB NOT NULL DEFAULT '[]'::jsonb,
            replay_parity_pass BOOLEAN NOT NULL DEFAULT FALSE,

            -- Invariant 3: gate attribution
            signals_emitted INTEGER NOT NULL DEFAULT 0,
            signals_blocked_total INTEGER NOT NULL DEFAULT 0,
            gate_block_breakdown JSONB NOT NULL DEFAULT '{}'::jsonb,
            gate_attribution_pass BOOLEAN NOT NULL DEFAULT FALSE,

            -- Invariant 4: backtest⇄live parity
            backtest_live_diff JSONB NOT NULL DEFAULT '{}'::jsonb,
            backtest_parity_pass BOOLEAN NOT NULL DEFAULT FALSE,

            -- Invariant 5: trade reconciliation
            trades_booked INTEGER NOT NULL DEFAULT 0,
            trade_recon_pass_count INTEGER NOT NULL DEFAULT 0,
            trade_recon_failures JSONB NOT NULL DEFAULT '[]'::jsonb,
            trade_recon_pass BOOLEAN NOT NULL DEFAULT FALSE,

            -- Invariant 6: edge persistence
            expectancy_60d DOUBLE PRECISION,
            expectancy_baseline DOUBLE PRECISION,
            drift_pct DOUBLE PRECISION,
            edge_persistence_pass BOOLEAN NOT NULL DEFAULT FALSE,

            -- Overall
            overall_status TEXT NOT NULL DEFAULT 'red',  -- 'green' | 'yellow' | 'red'
            report_path TEXT,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            UNIQUE (lane, audit_date)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_lane_audit_lane_date
        ON lane_audit (lane, audit_date DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_lane_audit_status
        ON lane_audit (overall_status, audit_date DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS lane_audit")
