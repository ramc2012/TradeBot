"""Shadow observation persistence

Revision ID: 008_shadow_observations
Revises: 007_validation_artifacts
Create Date: 2026-04-05 18:05:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "008_shadow_observations"
down_revision: Union[str, None] = "007_validation_artifacts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shadow_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("signal_id", sa.String(length=120), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("symbol", sa.String(length=50), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=True),
        sa.Column("snapshot_mode", sa.String(length=30), nullable=True),
        sa.Column("agent_name", sa.String(length=50), nullable=False),
        sa.Column("action", sa.String(length=10), nullable=False),
        sa.Column("regime_label", sa.String(length=50), nullable=True),
        sa.Column("setup_name", sa.String(length=100), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("entry_price", sa.Float(), nullable=True),
        sa.Column("stop_price", sa.Float(), nullable=True),
        sa.Column("target_price", sa.Float(), nullable=True),
        sa.Column("tick_size", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("risk_allowed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("kill_switch_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("simulated_fill_price", sa.Float(), nullable=True),
        sa.Column("observed_touch_price", sa.Float(), nullable=True),
        sa.Column("observed_fill_price", sa.Float(), nullable=True),
        sa.Column("fill_drift_ticks", sa.Float(), nullable=True),
        sa.Column("stale_signal", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("reconciliation_status", sa.String(length=30), nullable=False, server_default="matched"),
        sa.Column("mismatch_duration_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("kill_switch_tested", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("kill_switch_passed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("dashboard_checked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("alerts_checked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("manual_override_tested", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_shadow_observations_signal_id", "shadow_observations", ["signal_id"])
    op.create_index("ix_shadow_observations_session_date", "shadow_observations", ["session_date"])
    op.create_index("ix_shadow_observations_symbol", "shadow_observations", ["symbol"])
    op.create_index("ix_shadow_observations_recorded_at", "shadow_observations", ["recorded_at"])


def downgrade() -> None:
    op.drop_index("ix_shadow_observations_recorded_at", table_name="shadow_observations")
    op.drop_index("ix_shadow_observations_symbol", table_name="shadow_observations")
    op.drop_index("ix_shadow_observations_session_date", table_name="shadow_observations")
    op.drop_index("ix_shadow_observations_signal_id", table_name="shadow_observations")
    op.drop_table("shadow_observations")
