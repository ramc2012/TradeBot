"""Options MACD tables — option_premium_candles, macd_signals, backtest_trades

Revision ID: 002_options_macd
Revises: 001_initial
Create Date: 2026-03-28 00:00:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "002_options_macd"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── option_premium_candles (TimescaleDB hypertable) ──────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS option_premium_candles (
            time             TIMESTAMPTZ NOT NULL,
            underlying       TEXT        NOT NULL,
            market           TEXT        NOT NULL DEFAULT 'NSE',
            expiry           DATE,
            strike           DECIMAL(12,2),
            option_type      TEXT,
            open             DECIMAL(12,4),
            high             DECIMAL(12,4),
            low              DECIMAL(12,4),
            close            DECIMAL(12,4),
            volume           BIGINT,
            oi               BIGINT,
            iv               DECIMAL(8,6),
            delta            DECIMAL(8,6),
            underlying_price DECIMAL(12,4)
        );
    """)
    op.execute("""
        SELECT create_hypertable(
            'option_premium_candles', 'time',
            if_not_exists => TRUE,
            chunk_time_interval => INTERVAL '1 day'
        );
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_opc_underlying_time
        ON option_premium_candles (underlying, time DESC);
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_opc_contract
        ON option_premium_candles (underlying, expiry, strike, option_type, time DESC);
    """)

    # ── macd_signals ─────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS macd_signals (
            time             TIMESTAMPTZ NOT NULL,
            underlying       TEXT        NOT NULL,
            market           TEXT        NOT NULL DEFAULT 'NSE',
            expiry           DATE,
            strike           DECIMAL(12,2),
            option_type      TEXT,
            macd_value       DECIMAL(12,6),
            signal_value     DECIMAL(12,6),
            histogram        DECIMAL(12,6),
            signal_type      TEXT,
            premium_at_signal DECIMAL(12,4)
        );
    """)
    op.execute("""
        SELECT create_hypertable(
            'macd_signals', 'time',
            if_not_exists => TRUE,
            chunk_time_interval => INTERVAL '7 days'
        );
    """)

    # ── backtest_trades ───────────────────────────────────────────────────────
    op.create_table(
        "backtest_trades",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(50), nullable=False, index=True),
        sa.Column("underlying", sa.String(50), nullable=False),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("expiry", sa.String(20)),
        sa.Column("strike", sa.Float),
        sa.Column("option_type", sa.String(5)),
        sa.Column("entry_time", sa.DateTime(timezone=True)),
        sa.Column("entry_premium", sa.Float),
        sa.Column("exit_time", sa.DateTime(timezone=True)),
        sa.Column("exit_premium", sa.Float),
        sa.Column("exit_reason", sa.String(20)),
        sa.Column("pnl_points", sa.Float),
        sa.Column("pnl_pct", sa.Float),
        sa.Column("pnl_rupees", sa.Float),
        sa.Column("lots", sa.Integer),
        sa.Column("holding_bars", sa.Integer),
        sa.Column("signal_type", sa.String(30)),
        sa.Column("macd_fast", sa.Integer),
        sa.Column("macd_slow", sa.Integer),
        sa.Column("macd_signal", sa.Integer),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── backtest_runs (metadata per backtest run) ─────────────────────────────
    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.String(50), primary_key=True),
        sa.Column("underlying", sa.String(50), nullable=False),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("option_type", sa.String(5)),
        sa.Column("total_trades", sa.Integer),
        sa.Column("win_rate", sa.Float),
        sa.Column("profit_factor", sa.Float),
        sa.Column("sharpe_ratio", sa.Float),
        sa.Column("max_drawdown_pct", sa.Float),
        sa.Column("total_pnl_rupees", sa.Float),
        sa.Column("macd_fast", sa.Integer),
        sa.Column("macd_slow", sa.Integer),
        sa.Column("macd_signal", sa.Integer),
        sa.Column("sl_pct", sa.Float),
        sa.Column("target_1_pct", sa.Float),
        sa.Column("config_json", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("backtest_runs")
    op.drop_table("backtest_trades")
    op.execute("DROP TABLE IF EXISTS macd_signals;")
    op.execute("DROP TABLE IF EXISTS option_premium_candles;")
