"""Add directional long-options research tables.

Revision ID: 015_directional_long_options
Revises: 014_fo_contract_catalog_market
Create Date: 2026-04-19 18:10:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "015_directional_long_options"
down_revision: Union[str, None] = "014_fo_contract_catalog_market"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS directional_option_runs (
            id BIGSERIAL PRIMARY KEY,
            strategy_key TEXT NOT NULL DEFAULT 'directional_long_options',
            run_type TEXT NOT NULL,
            underlying TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            config_hash TEXT,
            status TEXT NOT NULL DEFAULT 'completed',
            artifact_root TEXT,
            summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            dashboard_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_directional_option_runs_lookup
        ON directional_option_runs (underlying, timeframe, started_at DESC)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS directional_option_candidates (
            id BIGSERIAL PRIMARY KEY,
            run_id BIGINT NOT NULL REFERENCES directional_option_runs(id) ON DELETE CASCADE,
            snapshot_time TIMESTAMPTZ NOT NULL,
            trading_symbol TEXT NOT NULL,
            option_type TEXT NOT NULL,
            expiry DATE NOT NULL,
            expiry_kind TEXT NOT NULL,
            strike DOUBLE PRECISION NOT NULL,
            option_price DOUBLE PRECISION NOT NULL DEFAULT 0,
            days_to_expiry DOUBLE PRECISION NOT NULL DEFAULT 0,
            delta DOUBLE PRECISION NOT NULL DEFAULT 0,
            gamma DOUBLE PRECISION NOT NULL DEFAULT 0,
            theta DOUBLE PRECISION NOT NULL DEFAULT 0,
            vega DOUBLE PRECISION NOT NULL DEFAULT 0,
            liquidity_score DOUBLE PRECISION NOT NULL DEFAULT 0,
            spread_pct DOUBLE PRECISION NOT NULL DEFAULT 0,
            expected_pnl DOUBLE PRECISION NOT NULL DEFAULT 0,
            contract_score DOUBLE PRECISION NOT NULL DEFAULT 0,
            selected BOOLEAN NOT NULL DEFAULT FALSE,
            metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_directional_option_candidates_run_selected
        ON directional_option_candidates (run_id, selected, contract_score DESC)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS directional_option_trades (
            id BIGSERIAL PRIMARY KEY,
            run_id BIGINT NOT NULL REFERENCES directional_option_runs(id) ON DELETE CASCADE,
            underlying TEXT NOT NULL,
            trading_symbol TEXT NOT NULL,
            option_type TEXT NOT NULL,
            expiry DATE NOT NULL,
            expiry_kind TEXT NOT NULL,
            strike DOUBLE PRECISION NOT NULL,
            qty_lots INTEGER NOT NULL DEFAULT 0,
            qty_units INTEGER NOT NULL DEFAULT 0,
            entry_time TIMESTAMPTZ NOT NULL,
            exit_time TIMESTAMPTZ NOT NULL,
            entry_spot DOUBLE PRECISION NOT NULL DEFAULT 0,
            exit_spot DOUBLE PRECISION NOT NULL DEFAULT 0,
            entry_price DOUBLE PRECISION NOT NULL DEFAULT 0,
            exit_price DOUBLE PRECISION NOT NULL DEFAULT 0,
            pnl DOUBLE PRECISION NOT NULL DEFAULT 0,
            return_pct DOUBLE PRECISION NOT NULL DEFAULT 0,
            premium_paid DOUBLE PRECISION NOT NULL DEFAULT 0,
            expected_pnl DOUBLE PRECISION NOT NULL DEFAULT 0,
            expected_move DOUBLE PRECISION NOT NULL DEFAULT 0,
            realized_move DOUBLE PRECISION NOT NULL DEFAULT 0,
            confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
            regime TEXT,
            delta_bucket TEXT,
            exit_reason TEXT,
            spread_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
            slippage_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
            theta_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
            metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_directional_option_trades_lookup
        ON directional_option_trades (underlying, entry_time DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_directional_option_trades_lookup")
    op.execute("DROP TABLE IF EXISTS directional_option_trades")
    op.execute("DROP INDEX IF EXISTS ix_directional_option_candidates_run_selected")
    op.execute("DROP TABLE IF EXISTS directional_option_candidates")
    op.execute("DROP INDEX IF EXISTS ix_directional_option_runs_lookup")
    op.execute("DROP TABLE IF EXISTS directional_option_runs")
