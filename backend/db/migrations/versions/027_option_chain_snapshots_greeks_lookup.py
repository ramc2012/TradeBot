"""Composite lookup index on option_chain_snapshots for greeks enrichment

The greeks-enrichment job (market_data/greeks_enrichment.py) matches each
greeks-null index option candle to the nearest chain snapshot for the same
contract via a correlated lookup on
(symbol, option_type, strike, expiry, time). The table only shipped with a
`time DESC` index, which makes that per-contract lookup a full scan of a
multi-million-row hypertable. This composite index turns each match into a
bounded index range scan.

TimescaleDB propagates a parent-table index to every chunk (and to future
chunks), so a single CREATE INDEX covers the whole hypertable.

Revision ID: 027_option_chain_snapshots_greeks_lookup
Revises: 026_macd_diffusion
Create Date: 2026-07-06
"""
from typing import Sequence, Union

from alembic import op


revision: str = "027_option_chain_snapshots_greeks_lookup"
down_revision: Union[str, None] = "026_macd_diffusion"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # This revision id is 40 chars but alembic_version.version_num defaults to
    # varchar(32). alembic stamps the new version AFTER upgrade() (same tx), so
    # widen the column here first — otherwise `alembic upgrade head` raises
    # StringDataRightTruncation and crash-loops the app on a FRESH database.
    # Idempotent: a no-op where the column is already wide enough.
    op.execute("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE varchar(128);")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_ocs_contract_time
        ON option_chain_snapshots (symbol, option_type, strike, expiry, time DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_ocs_contract_time;")
