"""Expand F&O storage for research cache and chain metrics.

Revision ID: 003_fo_research_cache
Revises: 002_options_macd
Create Date: 2026-03-28 22:20:00.000000
"""
from typing import Sequence, Union

from alembic import op

revision: str = "003_fo_research_cache"
down_revision: Union[str, None] = "002_options_macd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE option_premium_candles
        ADD COLUMN IF NOT EXISTS instrument_key TEXT,
        ADD COLUMN IF NOT EXISTS trading_symbol TEXT,
        ADD COLUMN IF NOT EXISTS interval TEXT NOT NULL DEFAULT '30minute',
        ADD COLUMN IF NOT EXISTS gamma NUMERIC(12,6),
        ADD COLUMN IF NOT EXISTS theta NUMERIC(12,6),
        ADD COLUMN IF NOT EXISTS vega NUMERIC(12,6),
        ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'upstox',
        ADD COLUMN IF NOT EXISTS synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        ADD COLUMN IF NOT EXISTS time_to_expiry_years NUMERIC(12,8);
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_opc_instrument_interval_time
        ON option_premium_candles (instrument_key, interval, time DESC);
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_opc_instrument_interval_time
        ON option_premium_candles (instrument_key, interval, time);
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS fo_underlying_catalog (
            symbol               TEXT PRIMARY KEY,
            kind                 TEXT NOT NULL,
            spot_instrument_key  TEXT,
            underlying_key       TEXT,
            expiries_synced_at   TIMESTAMPTZ,
            spot_synced_at       TIMESTAMPTZ,
            spot_range_start     DATE,
            spot_range_end       DATE,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS fo_expiry_catalog (
            underlying                TEXT NOT NULL,
            expiry                    DATE NOT NULL,
            previous_monthly_expiry   DATE,
            selection_date            DATE,
            contracts_discovered_at   TIMESTAMPTZ,
            contract_count            INTEGER NOT NULL DEFAULT 0,
            created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (underlying, expiry)
        );
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS fo_contract_catalog (
            instrument_key        TEXT PRIMARY KEY,
            trading_symbol        TEXT,
            underlying            TEXT NOT NULL,
            expiry                DATE NOT NULL,
            strike                NUMERIC(12,2) NOT NULL,
            option_type           TEXT NOT NULL,
            lot_size              INTEGER,
            tick_size             NUMERIC(12,6),
            minimum_lot           INTEGER,
            freeze_quantity       INTEGER,
            candle_from_date      DATE,
            candle_to_date        DATE,
            sync_status           TEXT NOT NULL DEFAULT 'pending',
            candle_count          INTEGER NOT NULL DEFAULT 0,
            first_candle_time     TIMESTAMPTZ,
            last_candle_time      TIMESTAMPTZ,
            last_synced_at        TIMESTAMPTZ,
            last_error            TEXT,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_fo_contract_catalog_pending
        ON fo_contract_catalog (sync_status, underlying, expiry, option_type, strike);
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS underlying_spot_candles (
            time             TIMESTAMPTZ NOT NULL,
            instrument_key   TEXT        NOT NULL,
            underlying       TEXT        NOT NULL,
            interval         TEXT        NOT NULL DEFAULT '30minute',
            open             NUMERIC(12,4),
            high             NUMERIC(12,4),
            low              NUMERIC(12,4),
            close            NUMERIC(12,4),
            volume           BIGINT,
            oi               BIGINT,
            source           TEXT        NOT NULL DEFAULT 'upstox',
            synced_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (instrument_key, interval, time)
        );
    """)
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'create_hypertable') THEN
                PERFORM create_hypertable(
                    'underlying_spot_candles', 'time',
                    if_not_exists => TRUE,
                    chunk_time_interval => INTERVAL '1 day'
                );
            END IF;
        END $$;
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_underlying_spot_candles_symbol_time
        ON underlying_spot_candles (underlying, interval, time DESC);
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS fo_option_chain_metrics (
            time             TIMESTAMPTZ NOT NULL,
            underlying       TEXT        NOT NULL,
            expiry           DATE        NOT NULL,
            interval         TEXT        NOT NULL DEFAULT '30minute',
            ce_contracts     INTEGER     NOT NULL DEFAULT 0,
            pe_contracts     INTEGER     NOT NULL DEFAULT 0,
            ce_oi            BIGINT      NOT NULL DEFAULT 0,
            pe_oi            BIGINT      NOT NULL DEFAULT 0,
            ce_volume        BIGINT      NOT NULL DEFAULT 0,
            pe_volume        BIGINT      NOT NULL DEFAULT 0,
            oi_pcr           NUMERIC(12,6),
            volume_pcr       NUMERIC(12,6),
            underlying_price NUMERIC(12,4),
            synced_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (underlying, expiry, interval, time)
        );
    """)
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'create_hypertable') THEN
                PERFORM create_hypertable(
                    'fo_option_chain_metrics', 'time',
                    if_not_exists => TRUE,
                    chunk_time_interval => INTERVAL '1 day'
                );
            END IF;
        END $$;
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_fo_option_chain_metrics_symbol_time
        ON fo_option_chain_metrics (underlying, expiry, interval, time DESC);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS fo_option_chain_metrics;")
    op.execute("DROP TABLE IF EXISTS underlying_spot_candles;")
    op.execute("DROP TABLE IF EXISTS fo_contract_catalog;")
    op.execute("DROP TABLE IF EXISTS fo_expiry_catalog;")
    op.execute("DROP TABLE IF EXISTS fo_underlying_catalog;")

    op.execute("DROP INDEX IF EXISTS uq_opc_instrument_interval_time;")
    op.execute("DROP INDEX IF EXISTS idx_opc_instrument_interval_time;")
    op.execute("""
        ALTER TABLE option_premium_candles
        DROP COLUMN IF EXISTS instrument_key,
        DROP COLUMN IF EXISTS trading_symbol,
        DROP COLUMN IF EXISTS interval,
        DROP COLUMN IF EXISTS gamma,
        DROP COLUMN IF EXISTS theta,
        DROP COLUMN IF EXISTS vega,
        DROP COLUMN IF EXISTS source,
        DROP COLUMN IF EXISTS synced_at,
        DROP COLUMN IF EXISTS time_to_expiry_years;
    """)
