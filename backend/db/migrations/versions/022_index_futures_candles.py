"""Index futures 1-minute candle cache.

Revision ID: 022_index_futures_candles
Revises: 021_lane_audit
Create Date: 2026-05-30
"""
from typing import Sequence, Union

from alembic import op


revision: str = "022_index_futures_candles"
down_revision: Union[str, None] = "021_lane_audit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS index_futures_candles (
            time           TIMESTAMPTZ NOT NULL,
            underlying     TEXT        NOT NULL,
            market         TEXT        NOT NULL,
            expiry         DATE,
            instrument_key TEXT        NOT NULL,
            trading_symbol TEXT,
            interval       TEXT        NOT NULL DEFAULT '1minute',
            open           NUMERIC(14,4),
            high           NUMERIC(14,4),
            low            NUMERIC(14,4),
            close          NUMERIC(14,4),
            volume         BIGINT,
            source         TEXT        NOT NULL,
            synced_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (instrument_key, interval, time)
        )
        """
    )
    op.execute(
        """
        SELECT create_hypertable(
            'index_futures_candles', 'time',
            if_not_exists => TRUE,
            chunk_time_interval => INTERVAL '1 day'
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_index_futures_candles_symbol_time
        ON index_futures_candles (underlying, interval, time DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_index_futures_candles_source_time
        ON index_futures_candles (source, underlying, interval, time DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS index_futures_candles")
