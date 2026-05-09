"""RL agent Q-table for Market Profile parameter learning

Revision ID: 009_rl_qtable
Revises: 008_shadow_observations
Create Date: 2026-04-07 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "009_rl_qtable"
down_revision: Union[str, None] = "008_shadow_observations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Use raw SQL with IF NOT EXISTS — table may already exist if created outside Alembic
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS rl_agent_qtable (
            state_hash VARCHAR(30) NOT NULL,
            action_idx INTEGER NOT NULL,
            q_value DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            visit_count INTEGER NOT NULL DEFAULT 0,
            last_updated TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (state_hash, action_idx)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rl_agent_qtable_state_hash ON rl_agent_qtable (state_hash)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rl_agent_qtable_last_updated ON rl_agent_qtable (last_updated)"
    )


def downgrade() -> None:
    op.drop_index("ix_rl_agent_qtable_last_updated", table_name="rl_agent_qtable")
    op.drop_index("ix_rl_agent_qtable_state_hash", table_name="rl_agent_qtable")
    op.drop_table("rl_agent_qtable")
