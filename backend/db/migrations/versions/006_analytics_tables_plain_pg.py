"""Create analytics tables without TimescaleDB (Cloud SQL compatible).

Revision ID: 006_analytics_tables_plain_pg
Revises: 005_atm_watchlist_history
Create Date: 2026-04-06 16:00:00.000000
"""
from typing import Sequence, Union
from alembic import op

revision: str = "006_analytics_tables_plain_pg"
down_revision: Union[str, None] = "005_atm_watchlist_history"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # fo_underlying_catalog — needed by ATM Watchlist
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

    # fo_expiry_catalog
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

    # fo_contract_catalog
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

    # underlying_spot_candles — plain PG table (no TimescaleDB hypertable)
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
        CREATE INDEX IF NOT EXISTS idx_underlying_spot_candles_symbol_time
        ON underlying_spot_candles (underlying, interval, time DESC);
    """)

    # fo_option_chain_metrics — plain PG table (no TimescaleDB hypertable)
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
        CREATE INDEX IF NOT EXISTS idx_fo_option_chain_metrics_symbol_time
        ON fo_option_chain_metrics (underlying, expiry, interval, time DESC);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS fo_option_chain_metrics;")
    op.execute("DROP TABLE IF EXISTS underlying_spot_candles;")
    op.execute("DROP TABLE IF EXISTS fo_contract_catalog;")
    op.execute("DROP TABLE IF EXISTS fo_expiry_catalog;")
    op.execute("DROP TABLE IF EXISTS fo_underlying_catalog;")
