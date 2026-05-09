"""Persist NSE strategy learning scores.

Revision ID: 016_strategy_learning_scores
Revises: 015_directional_long_options
Create Date: 2026-05-09 23:55:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "016_strategy_learning_scores"
down_revision: Union[str, None] = "015_directional_long_options"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS strategy_learning_scores (
            market TEXT NOT NULL DEFAULT 'NSE',
            strategy_key TEXT NOT NULL,
            underlying TEXT NOT NULL,
            option_type TEXT NOT NULL,
            signal_reason TEXT NOT NULL DEFAULT 'all',
            observations INTEGER NOT NULL DEFAULT 0,
            candidates INTEGER NOT NULL DEFAULT 0,
            entries INTEGER NOT NULL DEFAULT 0,
            open_count INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            realized_pnl DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            unrealized_pnl DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            avg_realized_pnl DOUBLE PRECISION NULL,
            win_rate DOUBLE PRECISION NULL,
            expectancy DOUBLE PRECISION NULL,
            score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            confidence DOUBLE PRECISION NOT NULL DEFAULT 0.5,
            risk_multiplier DOUBLE PRECISION NOT NULL DEFAULT 1.0,
            size_multiplier DOUBLE PRECISION NOT NULL DEFAULT 1.0,
            block_new_entries BOOLEAN NOT NULL DEFAULT FALSE,
            last_signal_at TIMESTAMPTZ NULL,
            last_trade_at TIMESTAMPTZ NULL,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (market, strategy_key, underlying, option_type, signal_reason)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_strategy_learning_scores_rank
        ON strategy_learning_scores (market, strategy_key, score DESC, updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_strategy_learning_scores_symbol
        ON strategy_learning_scores (market, underlying, option_type)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS strategy_learning_scores")
