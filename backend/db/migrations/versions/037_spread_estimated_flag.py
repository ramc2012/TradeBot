"""Mark rows whose spread is ESTIMATED rather than quoted.

Backfilled rows are reconstructed from `option_chain_snapshots`, which carries
LTP, OI, volume, IV and greeks but NO bid/ask — no historical table in this
schema does. Their `spread_pct` is therefore a band-calibrated estimate, not a
measurement, and a column called `spread_pct` holding an estimate is exactly the
kind of quiet substitution this pipeline refuses everywhere else.

`bid`/`ask` stay NULL on those rows, so the cost model already reports
`entry_half_spread_measured = False`. But that lives on the OUTCOME row; anyone
querying `candidate_snapshots.spread_pct` directly would see a number with
nothing to say where it came from. This flag makes the distinction visible at
the point the value is read.

Live captures leave it FALSE: their spread comes from a real two-sided quote.

Revision ID: 037_spread_estimated_flag
Revises: 036_candidate_model_versions
Create Date: 2026-08-27
"""
from typing import Sequence, Union

from alembic import op


revision: str = "037_spread_estimated_flag"
down_revision: Union[str, None] = "036_candidate_model_versions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE candidate_snapshots
            ADD COLUMN IF NOT EXISTS spread_pct_estimated BOOLEAN NOT NULL DEFAULT FALSE;
        """
    )
    # Backfilled rows are a different population from live captures and are
    # almost always queried apart from them.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_candidate_snapshots_source_time
        ON candidate_snapshots (source, time DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_candidate_snapshots_source_time;")
    op.execute(
        "ALTER TABLE candidate_snapshots DROP COLUMN IF EXISTS spread_pct_estimated;"
    )
