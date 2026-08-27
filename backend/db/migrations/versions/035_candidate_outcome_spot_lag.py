"""Give the SPOT leg the same lag honesty the option leg already had.

The option side has carried `forward_lag_seconds` from the start, plus a
tolerance band that downgrades an off-horizon mark to
`unlabellable_out_of_tolerance`. The spot side had neither: `build_spot_path`
took the last tick inside the window and stored its move as the horizon's
return, with only `spot_tick_count` as a hint — and a count cannot distinguish
200 ticks packed into the first fifty seconds from 200 spread across an hour.

That gap matters here specifically. `market_ticks` is documented to lose the
MIDDLE FOUR HOURS of the NSE session on some days with no WS error and no
restart, and the reconnect that is supposed to fix it is a logged no-op. On such
a day a 60-minute label would have been computed from a 10-minute window and
stored as though the hour had completed.

`spot_forward_lag_seconds` records where in the window the last usable tick
actually fell. `spot_window_complete` is False when that lag falls short of the
horizon's own tolerance band — the same band the option leg uses, so there is
one convention rather than two.

Revision ID: 035_candidate_outcome_spot_lag
Revises: 034_candidate_outcomes
Create Date: 2026-08-27
"""
from typing import Sequence, Union

from alembic import op


revision: str = "035_candidate_outcome_spot_lag"
down_revision: Union[str, None] = "034_candidate_outcomes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE candidate_outcomes
            ADD COLUMN IF NOT EXISTS spot_forward_lag_seconds DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS spot_window_complete BOOLEAN;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE candidate_outcomes
            DROP COLUMN IF EXISTS spot_window_complete,
            DROP COLUMN IF EXISTS spot_forward_lag_seconds;
        """
    )
