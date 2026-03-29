"""Initial schema + TimescaleDB hypertables

Revision ID: 001_initial
Revises:
Create Date: 2024-01-01 00:00:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # broker_sessions
    op.create_table(
        "broker_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("broker", sa.String(20), nullable=False),
        sa.Column("user_id", sa.String(100)),
        sa.Column("access_token_enc", sa.Text),
        sa.Column("refresh_token_enc", sa.Text),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("is_active", sa.Boolean, default=True),
    )

    # paper_sessions
    op.create_table(
        "paper_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("broker", sa.String(20), nullable=False),
        sa.Column("initial_capital", sa.Float, default=1_000_000.0),
        sa.Column("current_capital", sa.Float, default=1_000_000.0),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("is_active", sa.Boolean, default=True),
    )

    # orders
    op.create_table(
        "orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("paper_sessions.id"), nullable=True),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("broker", sa.String(20), nullable=False),
        sa.Column("symbol", sa.String(50), nullable=False),
        sa.Column("exchange", sa.String(20), default="NSE"),
        sa.Column("instrument_type", sa.String(10), nullable=False),
        sa.Column("strike", sa.Float),
        sa.Column("expiry", sa.String(20)),
        sa.Column("option_type", sa.String(5)),
        sa.Column("action", sa.String(5), nullable=False),
        sa.Column("order_type", sa.String(10), nullable=False),
        sa.Column("qty", sa.Integer, nullable=False),
        sa.Column("price", sa.Float),
        sa.Column("sl", sa.Float),
        sa.Column("target", sa.Float),
        sa.Column("status", sa.String(20), default="PENDING"),
        sa.Column("broker_order_id", sa.String(100)),
        sa.Column("fill_price", sa.Float),
        sa.Column("fill_time", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # positions
    op.create_table(
        "positions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("broker", sa.String(20), nullable=False),
        sa.Column("symbol", sa.String(50), nullable=False),
        sa.Column("strike", sa.Float),
        sa.Column("expiry", sa.String(20)),
        sa.Column("option_type", sa.String(5)),
        sa.Column("qty", sa.Integer, nullable=False),
        sa.Column("avg_price", sa.Float, nullable=False),
        sa.Column("realized_pnl", sa.Float, default=0.0),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # agent_proposals
    op.create_table(
        "agent_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("symbol", sa.String(50), nullable=False),
        sa.Column("strategy", sa.String(100), nullable=False),
        sa.Column("entry", sa.Float, nullable=False),
        sa.Column("sl", sa.Float, nullable=False),
        sa.Column("target", sa.Float, nullable=False),
        sa.Column("qty", sa.Integer, nullable=False),
        sa.Column("rationale", sa.Text),
        sa.Column("confidence", sa.String(5), nullable=False),
        sa.Column("status", sa.String(20), default="PENDING"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("acted_at", sa.DateTime(timezone=True)),
    )

    # agent_logs
    op.create_table(
        "agent_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tier", sa.Integer, nullable=False),
        sa.Column("input_context", postgresql.JSONB),
        sa.Column("reasoning", sa.Text),
        sa.Column("output", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── TimescaleDB hypertables ──────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS market_ticks (
            time        TIMESTAMPTZ NOT NULL,
            symbol      TEXT NOT NULL,
            ltp         FLOAT,
            open        FLOAT,
            high        FLOAT,
            low         FLOAT,
            close       FLOAT,
            volume      BIGINT,
            oi          BIGINT,
            bid         FLOAT,
            ask         FLOAT,
            bid_qty     BIGINT,
            ask_qty     BIGINT
        );
    """)
    op.execute("SELECT create_hypertable('market_ticks', 'time', if_not_exists => TRUE);")

    op.execute("""
        CREATE TABLE IF NOT EXISTS market_profiles (
            time        TIMESTAMPTZ NOT NULL,
            symbol      TEXT NOT NULL,
            timeframe   TEXT NOT NULL,
            poc         FLOAT,
            vah         FLOAT,
            val         FLOAT,
            ib_high     FLOAT,
            ib_low      FLOAT,
            tpo_data    JSONB
        );
    """)
    op.execute("SELECT create_hypertable('market_profiles', 'time', if_not_exists => TRUE);")

    op.execute("""
        CREATE TABLE IF NOT EXISTS option_chain_snapshots (
            time        TIMESTAMPTZ NOT NULL,
            symbol      TEXT NOT NULL,
            expiry      TEXT NOT NULL,
            strike      FLOAT NOT NULL,
            option_type TEXT NOT NULL,
            ltp         FLOAT,
            oi          BIGINT,
            volume      BIGINT,
            iv          FLOAT,
            delta       FLOAT,
            gamma       FLOAT,
            theta       FLOAT,
            vega        FLOAT
        );
    """)
    op.execute("SELECT create_hypertable('option_chain_snapshots', 'time', if_not_exists => TRUE);")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS option_chain_snapshots;")
    op.execute("DROP TABLE IF EXISTS market_profiles;")
    op.execute("DROP TABLE IF EXISTS market_ticks;")
    op.drop_table("agent_logs")
    op.drop_table("agent_proposals")
    op.drop_table("positions")
    op.drop_table("orders")
    op.drop_table("paper_sessions")
    op.drop_table("broker_sessions")
