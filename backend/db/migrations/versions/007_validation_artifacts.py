"""Validation artifact persistence

Revision ID: 007_validation_artifacts
Revises: 006_validation_runs
Create Date: 2026-04-05 00:10:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "007_validation_artifacts"
down_revision: Union[str, None] = "006_validation_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "validation_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("validation_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("artifact_type", sa.String(length=50), nullable=False),
        sa.Column("artifact_key", sa.String(length=120), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_validation_artifacts_run_id", "validation_artifacts", ["run_id"])
    op.create_index("ix_validation_artifacts_type", "validation_artifacts", ["artifact_type"])


def downgrade() -> None:
    op.drop_index("ix_validation_artifacts_type", table_name="validation_artifacts")
    op.drop_index("ix_validation_artifacts_run_id", table_name="validation_artifacts")
    op.drop_table("validation_artifacts")
