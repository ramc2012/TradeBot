"""Record HOW RECENT the ATR sample behind gap_vs_atr actually is.

Verification finding, 2026-07-27
────────────────────────────────
`atr_pct_14` is computed from broker-history spot bars. For the five index
roots the broker-history series in `underlying_spot_candles` is labelled
`upstox_spot_index` / `fyers_spot_index`, and the newest such bars are weeks
older than the snapshot session (index broker-history ingestion last wrote
2026-07-08). The ATR is still real data, but a denominator built from bars two
weeks old must be VISIBLE, not silently presented as current — that is exactly
the "stale mark shown as live" defect class this codebase has shipped before.

`atr_last_session` is the session date of the newest bar that entered the ATR
sample. `atr_sessions_n` already says how many; this says how old.

Revision ID: 031_preopen_atr_last_session
Revises: 030_preopen_spot_snapshot
Create Date: 2026-07-27
"""
from typing import Sequence, Union

from alembic import op


revision: str = "031_preopen_atr_last_session"
down_revision: Union[str, None] = "030_preopen_spot_snapshot"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE preopen_spot_snapshots
            ADD COLUMN IF NOT EXISTS atr_last_session DATE;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE preopen_spot_snapshots
            DROP COLUMN IF EXISTS atr_last_session;
        """
    )
