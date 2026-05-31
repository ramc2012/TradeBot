"""Extend cbe_scan_results with alpha-engine columns.

Layers L2/L3/L4 introduce new per-candidate metrics: sector rank +
quadrant, stock rank within sector, composite alpha score 0-100, and
whether the candidate crossed the composite gate. Old composite_score
stays for compat (alpha score / 10).

Revision ID: 023_alpha_engine_columns
Revises: 022_index_futures_candles
Create Date: 2026-05-31
"""
from typing import Sequence, Union

from alembic import op


revision: str = "023_alpha_engine_columns"
down_revision: Union[str, None] = "022_index_futures_candles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Each column nullable + defaulted so old rows survive the migration.
    # Adding all in one ALTER for fewer table-lock cycles on production.
    op.execute(
        """
        ALTER TABLE cbe_scan_results
            ADD COLUMN IF NOT EXISTS sector_code            TEXT,
            ADD COLUMN IF NOT EXISTS sector_quadrant        TEXT,
            ADD COLUMN IF NOT EXISTS sector_rs_pct          DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS stock_quadrant         TEXT,
            ADD COLUMN IF NOT EXISTS stock_rs_pct           DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS stock_rank_in_sector   INTEGER,
            ADD COLUMN IF NOT EXISTS trend_score            DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS atr_expansion          DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS volume_score           DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS oi_score               DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS iv_score               DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS atm_strike             DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS atm_oi                 DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS atm_volume             DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS composite_alpha_score  DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS gate_passed            BOOLEAN
        """
    )
    op.execute(
        """
        ALTER TABLE cbe_scan_runs
            ADD COLUMN IF NOT EXISTS asset_winner   TEXT,
            ADD COLUMN IF NOT EXISTS composite_gate DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS engine_version TEXT
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cbe_scan_results_gate_score
            ON cbe_scan_results (gate_passed, composite_alpha_score DESC)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE cbe_scan_results
            DROP COLUMN IF EXISTS sector_code,
            DROP COLUMN IF EXISTS sector_quadrant,
            DROP COLUMN IF EXISTS sector_rs_pct,
            DROP COLUMN IF EXISTS stock_quadrant,
            DROP COLUMN IF EXISTS stock_rs_pct,
            DROP COLUMN IF EXISTS stock_rank_in_sector,
            DROP COLUMN IF EXISTS trend_score,
            DROP COLUMN IF EXISTS atr_expansion,
            DROP COLUMN IF EXISTS volume_score,
            DROP COLUMN IF EXISTS oi_score,
            DROP COLUMN IF EXISTS iv_score,
            DROP COLUMN IF EXISTS atm_strike,
            DROP COLUMN IF EXISTS atm_oi,
            DROP COLUMN IF EXISTS atm_volume,
            DROP COLUMN IF EXISTS composite_alpha_score,
            DROP COLUMN IF EXISTS gate_passed
        """
    )
    op.execute(
        """
        ALTER TABLE cbe_scan_runs
            DROP COLUMN IF EXISTS asset_winner,
            DROP COLUMN IF EXISTS composite_gate,
            DROP COLUMN IF EXISTS engine_version
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_cbe_scan_results_gate_score")
