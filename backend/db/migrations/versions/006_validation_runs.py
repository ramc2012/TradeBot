"""Validation run persistence tables

Revision ID: 006_validation_runs
Revises: 005_atm_watchlist_history
Create Date: 2026-04-05 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "006_validation_runs"
down_revision: Union[str, None] = "006_analytics_tables_plain_pg"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "validation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("gate", sa.String(length=50), nullable=False),
        sa.Column("label", sa.String(length=150), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("symbol", sa.String(length=50), nullable=True),
        sa.Column("mode", sa.String(length=30), nullable=True),
        sa.Column("scenario", sa.String(length=50), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=True),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_validation_runs_gate", "validation_runs", ["gate"])
    op.create_index("ix_validation_runs_created_at", "validation_runs", ["created_at"])

    op.create_table(
        "validation_metrics",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("validation_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("metric_name", sa.String(length=120), nullable=False),
        sa.Column("metric_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_validation_metrics_run_id", "validation_metrics", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_validation_metrics_run_id", table_name="validation_metrics")
    op.drop_table("validation_metrics")
    op.drop_index("ix_validation_runs_created_at", table_name="validation_runs")
    op.drop_index("ix_validation_runs_gate", table_name="validation_runs")
    op.drop_table("validation_runs")
