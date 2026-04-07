"""Add ATM watchlist history and expired contract archive tables.

Revision ID: 005_atm_watchlist_history
Revises: 004_expiry_selection_spot
Create Date: 2026-03-29 19:05:00.000000
"""
from typing import Sequence, Union

from alembic import op

revision: str = "005_atm_watchlist_history"
down_revision: Union[str, None] = "004_expiry_selection_spot"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS atm_option_watchlist_snapshots (
            time               TIMESTAMPTZ NOT NULL,
            underlying         TEXT        NOT NULL,
            kind               TEXT        NOT NULL,
            expiry             DATE        NOT NULL,
            strike             NUMERIC(12,2) NOT NULL,
            option_type        TEXT        NOT NULL,
            source_broker      TEXT        NOT NULL,
            instrument_key     TEXT,
            trading_symbol     TEXT,
            underlying_price   NUMERIC(12,4),
            ltp                NUMERIC(12,4),
            prev_close         NUMERIC(12,4),
            change             NUMERIC(12,4),
            change_pct         NUMERIC(12,4),
            oi                 BIGINT,
            prev_oi            BIGINT,
            oi_change          BIGINT,
            oi_change_pct      NUMERIC(12,4),
            volume             BIGINT,
            iv                 NUMERIC(12,6),
            macd               NUMERIC(12,6),
            macd_signal        NUMERIC(12,6),
            macd_histogram     NUMERIC(12,6),
            rsi                NUMERIC(12,4),
            PRIMARY KEY (underlying, expiry, option_type, time)
        );
    """)
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'create_hypertable') THEN
                PERFORM create_hypertable(
                    'atm_option_watchlist_snapshots', 'time',
                    if_not_exists => TRUE,
                    chunk_time_interval => INTERVAL '1 day'
                );
            END IF;
        END $$;
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_atm_watchlist_snapshots_lookup
        ON atm_option_watchlist_snapshots (underlying, expiry, option_type, time DESC);
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_atm_watchlist_snapshots_instrument
        ON atm_option_watchlist_snapshots (instrument_key, time DESC);
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS expired_option_contract_archive (
            instrument_key       TEXT PRIMARY KEY,
            underlying           TEXT        NOT NULL,
            kind                 TEXT        NOT NULL,
            expiry               DATE        NOT NULL,
            strike               NUMERIC(12,2) NOT NULL,
            option_type          TEXT        NOT NULL,
            source_broker        TEXT        NOT NULL,
            trading_symbol       TEXT,
            first_seen_at        TIMESTAMPTZ,
            last_seen_at         TIMESTAMPTZ,
            last_underlying_price NUMERIC(12,4),
            last_ltp             NUMERIC(12,4),
            last_change_pct      NUMERIC(12,4),
            last_oi              BIGINT,
            last_oi_change       BIGINT,
            last_volume          BIGINT,
            last_iv              NUMERIC(12,6),
            last_macd            NUMERIC(12,6),
            last_macd_signal     NUMERIC(12,6),
            last_macd_histogram  NUMERIC(12,6),
            last_rsi             NUMERIC(12,4),
            snapshot_count       INTEGER     NOT NULL DEFAULT 0,
            archived_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_expired_option_archive_underlying_expiry
        ON expired_option_contract_archive (underlying, expiry DESC, option_type);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS expired_option_contract_archive;")
    op.execute("DROP TABLE IF EXISTS atm_option_watchlist_snapshots;")
