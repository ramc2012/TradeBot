"""Prune historical agent_risk_state to the latest 720 rows per market.

agent_risk_state (migration 011) was originally designed as a point-in-time
snapshot table — the *transitions* live in agent_audit_events (migration
017). But the per-cycle insert never had a retention cap, so the table
grew to 67 MB (2,355 rows × ~28 KB each, mostly the JSONB status_payload).

The application code now caps to the last 720 rows per (market,
strategy_key) — about 12 hours of 60s scan history, which is what the
dashboard ever needs. This migration trims the existing history once so
the disk reclaim happens on the deploy.

Revision ID: 019_prune_agent_risk_state
Revises: 018_fo_mwpl_ban_list
Create Date: 2026-05-16
"""
from typing import Sequence, Union

from alembic import op


revision: str = "019_prune_agent_risk_state"
down_revision: Union[str, None] = "018_fo_mwpl_ban_list"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Delete everything past the most recent 720 rows per
    # (market, strategy_key). Autovacuum reclaims the freed pages on
    # its next pass — we don't run VACUUM here because alembic wraps
    # the migration in a transaction and VACUUM can't run inside one.
    op.execute(
        """
        DELETE FROM agent_risk_state
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY market, strategy_key
                           ORDER BY created_at DESC
                       ) AS rn
                FROM agent_risk_state
            ) ranked
            WHERE rn > 720
        )
        """
    )


def downgrade() -> None:
    # No-op — we can't un-delete pruned audit rows. The transition log
    # in agent_audit_events is the authoritative trail.
    pass
