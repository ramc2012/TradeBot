"""Correct two candidate_snapshots fields that claimed more than they measured.

1. `quote_age_seconds` / `is_stale` were CHAIN-level values stamped identically
   onto every contract row in a decision set. They are derived from the chain
   payload's single `timestamp`, so they describe how old the POLL is, not how
   old this contract's last print is. Left under their original names they read
   as a per-contract freshness flag and would have been used as one — including
   as a model feature, where they carry exactly zero within-decision-set
   variance. Renamed to say what they are.

   The genuine per-contract staleness fact (did a trade actually arrive in this
   contract) is a volume delta between consecutive snapshots of the same
   contract. That is a labelling-time computation, not a capture-time one, so it
   lives in candidate_outcomes rather than here.

2. `lot_size` was absent, which makes cost-as-a-fraction-of-premium
   uncomputable from a row: the flat per-order brokerage has to be divided by
   (lot_size x premium), so the same contract has a very different cost profile
   at NIFTY's 65 than at a 30-lot underlying. Captured per row rather than
   joined at read time because the catalog's lot size changes between expiries
   and a later join would silently apply today's lot to an old row.

`candidate_snapshots` is empty at the time of writing, so no backfill is needed.

Revision ID: 033_candidate_snapshot_corrections
Revises: 032_candidate_snapshots
Create Date: 2026-08-27
"""
from typing import Sequence, Union

from alembic import op


revision: str = "033_candidate_snapshot_corrections"
down_revision: Union[str, None] = "032_candidate_snapshots"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE candidate_snapshots
            RENAME COLUMN quote_age_seconds TO chain_quote_age_seconds;
        """
    )
    op.execute(
        """
        ALTER TABLE candidate_snapshots
            RENAME COLUMN is_stale TO chain_is_stale;
        """
    )
    op.execute(
        """
        ALTER TABLE candidate_snapshots
            ADD COLUMN IF NOT EXISTS lot_size INTEGER;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE candidate_snapshots
            DROP COLUMN IF EXISTS lot_size;
        """
    )
    op.execute(
        """
        ALTER TABLE candidate_snapshots
            RENAME COLUMN chain_is_stale TO is_stale;
        """
    )
    op.execute(
        """
        ALTER TABLE candidate_snapshots
            RENAME COLUMN chain_quote_age_seconds TO quote_age_seconds;
        """
    )
