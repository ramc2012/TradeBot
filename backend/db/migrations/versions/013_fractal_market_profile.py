"""Add hourly profiles table for Fractal Market Profile strategy

Revision ID: 013_fractal_market_profile
Revises: 012_rl_policy_versions
Create Date: 2026-04-12 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "013_fractal_market_profile"
down_revision: Union[str, None] = "012_rl_policy_versions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "hourly_profiles",
        sa.Column("time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("hour_num", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ib_high", sa.Float(), nullable=False),
        sa.Column("ib_low", sa.Float(), nullable=False),
        sa.Column("vah", sa.Float(), nullable=False),
        sa.Column("val", sa.Float(), nullable=False),
        sa.Column("poc", sa.Float(), nullable=False),
        sa.Column("shape", sa.String(length=24), nullable=False),
        sa.Column("direction_bias", sa.String(length=16), nullable=False),
        sa.Column("single_prints", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("tpo_rows", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("poor_high", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("poor_low", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("tick_size", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("value_migration_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source", sa.String(length=40), nullable=False, server_default="fmp_live_snapshot"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("time", "symbol"),
        sa.UniqueConstraint("symbol", "session_date", "hour_num", name="uq_hourly_profiles_symbol_session_hour"),
    )
    op.create_index("ix_hourly_profiles_symbol_time", "hourly_profiles", ["symbol", "time"])
    op.create_index("ix_hourly_profiles_session_hour", "hourly_profiles", ["session_date", "hour_num"])


def downgrade() -> None:
    op.drop_index("ix_hourly_profiles_session_hour", table_name="hourly_profiles")
    op.drop_index("ix_hourly_profiles_symbol_time", table_name="hourly_profiles")
    op.drop_table("hourly_profiles")
