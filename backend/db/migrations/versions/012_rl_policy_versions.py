"""Persist RL policy snapshots and promotion metadata

Revision ID: 012_rl_policy_versions
Revises: 011_agent_audit_tables
Create Date: 2026-04-11 22:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "012_rl_policy_versions"
down_revision: Union[str, None] = "011_agent_audit_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rl_policy_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("version_name", sa.String(length=120), nullable=False, unique=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="candidate"),
        sa.Column("source", sa.String(length=30), nullable=False, server_default="manual"),
        sa.Column("symbol", sa.String(length=50), nullable=True),
        sa.Column("trained_on", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("average_reward", sa.Float(), nullable=False, server_default="0"),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("qtable_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("promotion_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_rl_policy_versions_status", "rl_policy_versions", ["status"])
    op.create_index("ix_rl_policy_versions_created_at", "rl_policy_versions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_rl_policy_versions_created_at", table_name="rl_policy_versions")
    op.drop_index("ix_rl_policy_versions_status", table_name="rl_policy_versions")
    op.drop_table("rl_policy_versions")
