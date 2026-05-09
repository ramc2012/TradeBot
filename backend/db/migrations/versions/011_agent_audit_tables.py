"""Add strategy agent audit tables.

Revision ID: 011_agent_audit_tables
Revises: 010_underlying_lot_size
Create Date: 2026-04-11 18:45:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "011_agent_audit_tables"
down_revision: Union[str, None] = "010_underlying_lot_size"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_signals (
            id UUID PRIMARY KEY,
            session_id UUID NULL,
            market TEXT NOT NULL,
            strategy_key TEXT NOT NULL,
            strategy_label TEXT NOT NULL,
            signal_key TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            underlying TEXT NOT NULL,
            expiry DATE NULL,
            strike DOUBLE PRECISION NULL,
            option_type TEXT NULL,
            signal_reason TEXT NULL,
            signal_strength DOUBLE PRECISION NULL,
            spot_setup TEXT NULL,
            regime TEXT NULL,
            status TEXT NOT NULL,
            entry_price DOUBLE PRECISION NULL,
            entry_iv_pct DOUBLE PRECISION NULL,
            tte_days INTEGER NULL,
            option_ma20 DOUBLE PRECISION NULL,
            option_ma50 DOUBLE PRECISION NULL,
            above_option_ma20 BOOLEAN NOT NULL DEFAULT FALSE,
            above_option_ma50 BOOLEAN NOT NULL DEFAULT FALSE,
            signal_bar_time TIMESTAMPTZ NULL,
            entered_at TIMESTAMPTZ NULL,
            closed_at TIMESTAMPTZ NULL,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_signals_market_created_at
        ON agent_signals (market, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_positions (
            id UUID PRIMARY KEY,
            signal_id UUID NULL REFERENCES agent_signals(id) ON DELETE SET NULL,
            session_id UUID NULL,
            market TEXT NOT NULL,
            strategy_key TEXT NOT NULL,
            strategy_label TEXT NOT NULL,
            symbol TEXT NOT NULL UNIQUE,
            underlying TEXT NOT NULL,
            expiry DATE NULL,
            strike DOUBLE PRECISION NULL,
            option_type TEXT NULL,
            qty INTEGER NOT NULL,
            initial_qty INTEGER NOT NULL,
            entry_price DOUBLE PRECISION NOT NULL,
            current_price DOUBLE PRECISION NOT NULL,
            peak_price DOUBLE PRECISION NOT NULL,
            realized_pnl DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            unrealized_pnl DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            entry_iv_pct DOUBLE PRECISION NULL,
            spot_setup TEXT NULL,
            regime TEXT NULL,
            signal_reason TEXT NULL,
            phase TEXT NULL,
            status TEXT NOT NULL,
            option_ma20 DOUBLE PRECISION NULL,
            option_ma50 DOUBLE PRECISION NULL,
            above_option_ma20 BOOLEAN NOT NULL DEFAULT FALSE,
            above_option_ma50 BOOLEAN NOT NULL DEFAULT FALSE,
            entered_at TIMESTAMPTZ NULL,
            closed_at TIMESTAMPTZ NULL,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_positions_market_status
        ON agent_positions (market, status, updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_risk_state (
            id UUID PRIMARY KEY,
            market TEXT NOT NULL,
            strategy_key TEXT NOT NULL,
            strategy_label TEXT NOT NULL,
            trading_allowed BOOLEAN NOT NULL DEFAULT TRUE,
            kill_switch_active BOOLEAN NOT NULL DEFAULT FALSE,
            auto_run_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            loop_active BOOLEAN NOT NULL DEFAULT FALSE,
            running BOOLEAN NOT NULL DEFAULT FALSE,
            scan_interval_seconds INTEGER NOT NULL DEFAULT 0,
            open_positions INTEGER NOT NULL DEFAULT 0,
            active_windows INTEGER NOT NULL DEFAULT 0,
            last_run_at TIMESTAMPTZ NULL,
            broker_ready BOOLEAN NOT NULL DEFAULT FALSE,
            connected_brokers JSONB NOT NULL DEFAULT '[]'::jsonb,
            status_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_risk_state_market_created_at
        ON agent_risk_state (market, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_risk_state")
    op.execute("DROP TABLE IF EXISTS agent_positions")
    op.execute("DROP TABLE IF EXISTS agent_signals")
