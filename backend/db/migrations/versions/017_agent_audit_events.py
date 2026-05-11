"""Add cross-agent audit-event log.

The agent_signals / agent_positions / agent_risk_state tables (migration 011)
capture point-in-time state. They do not capture the *transitions* the user
needs for traceability — kill-switch trips and releases, bucket transitions
on a watchlist row, broker session changes, manual operator interventions.

This migration adds a single append-only audit_events log keyed by
(market, strategy_key, event_type) with a JSONB payload and an actor field.
The AuditAgent (`agentic_rag.audit_agent`) writes to it.

Revision ID: 017_agent_audit_events
Revises: 016_strategy_learning_scores
Create Date: 2026-05-11 22:30:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "017_agent_audit_events"
down_revision: Union[str, None] = "016_strategy_learning_scores"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_audit_events (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            market TEXT NOT NULL,
            strategy_key TEXT NULL,
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT 'system',
            symbol TEXT NULL,
            underlying TEXT NULL,
            severity TEXT NOT NULL DEFAULT 'info',
            message TEXT NULL,
            previous_state TEXT NULL,
            new_state TEXT NULL,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_audit_events_market_created_at
        ON agent_audit_events (market, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_audit_events_event_type
        ON agent_audit_events (event_type, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_audit_events_symbol
        ON agent_audit_events (symbol, created_at DESC)
        WHERE symbol IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_audit_events")
