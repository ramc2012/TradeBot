"""Gann lane persistence — watchlist snapshots + time-cycle prominence map

Two plain Postgres tables (both low volume — one row per instrument per
session, and one row per instrument per cycle per mapping run; no hypertable).

`gann_watchlist_snapshots` materialises the per-instrument view the owner
asked for: spot, current Gann regime, next turn date, price-time squaring
date, nearest angle support/resistance, and the anchor everything is measured
from.  Until this table existed the whole view was computed transiently inside
the paper agent and thrown away, so nothing could read it.

Every value is COMPUTED.  A field that cannot be derived for an instrument is
NULL and the reason is recorded in `null_reasons` — never a fabricated
default.

`gann_cycle_prominence` holds the per-instrument cycle map, including the
untestable cells (with their reason) and the placebo arm, so the lane can be
told to trade only demonstrably prominent cycles and an auditor can see the
whole grid the correction was applied over rather than the survivors only.

Revision ID: 029_gann_lane
Revises: 028_macd_session_watchlist
Create Date: 2026-07-21
"""
from typing import Sequence, Union

from alembic import op


revision: str = "029_gann_lane"
down_revision: Union[str, None] = "028_macd_session_watchlist"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS gann_watchlist_snapshots (
            session_date            DATE             NOT NULL,
            underlying              TEXT             NOT NULL,
            instrument_class        TEXT             NOT NULL,
            timeframe               TEXT             NOT NULL DEFAULT '1day',
            spot                    DOUBLE PRECISION,
            regime                  TEXT,
            regime_strength         DOUBLE PRECISION,
            anchor_kind             TEXT,
            anchor_time             TIMESTAMPTZ,
            anchor_price            DOUBLE PRECISION,
            anchor_confirmed_at     TIMESTAMPTZ,
            price_unit              DOUBLE PRECISION,
            next_turn_date          DATE,
            next_turn_cycle_key     TEXT,
            next_turn_cycle_days    INTEGER,
            next_turn_prominence    TEXT,
            price_time_square_date  DATE,
            nearest_angle_support   DOUBLE PRECISION,
            nearest_angle_resistance DOUBLE PRECISION,
            nearest_angle_support_name    TEXT,
            nearest_angle_resistance_name TEXT,
            nearest_sq9_support     DOUBLE PRECISION,
            nearest_sq9_resistance  DOUBLE PRECISION,
            nearest_sq9_support_degree    INTEGER,
            nearest_sq9_resistance_degree INTEGER,
            conviction              DOUBLE PRECISION,
            setup_state             TEXT,
            archetype               TEXT,
            side                    TEXT,
            blockers                JSONB            NOT NULL DEFAULT '[]'::jsonb,
            active_cycles           JSONB            NOT NULL DEFAULT '[]'::jsonb,
            null_reasons            JSONB            NOT NULL DEFAULT '{}'::jsonb,
            daily_bars              INTEGER,
            computed_at             TIMESTAMPTZ      NOT NULL DEFAULT now(),
            PRIMARY KEY (session_date, underlying)
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_gann_watchlist_underlying_date
        ON gann_watchlist_snapshots (underlying, session_date DESC);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_gann_watchlist_date
        ON gann_watchlist_snapshots (session_date DESC);
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS gann_cycle_prominence (
            run_id                  TEXT             NOT NULL,
            underlying              TEXT             NOT NULL,
            cycle_key               TEXT             NOT NULL,
            family                  TEXT             NOT NULL,
            cycle_days              INTEGER          NOT NULL,
            arm                     TEXT             NOT NULL DEFAULT 'genuine',
            status                  TEXT             NOT NULL,
            untestable_reason       TEXT,
            is_observations         INTEGER          NOT NULL DEFAULT 0,
            is_hits                 INTEGER          NOT NULL DEFAULT 0,
            is_hit_rate             DOUBLE PRECISION,
            null_rate               DOUBLE PRECISION,
            lift                    DOUBLE PRECISION,
            p_value                 DOUBLE PRECISION,
            p_value_fdr             DOUBLE PRECISION,
            fdr_significant         BOOLEAN          NOT NULL DEFAULT FALSE,
            era1_observations       INTEGER          NOT NULL DEFAULT 0,
            era1_hit_rate           DOUBLE PRECISION,
            era2_observations       INTEGER          NOT NULL DEFAULT 0,
            era2_hit_rate           DOUBLE PRECISION,
            era_stable              BOOLEAN          NOT NULL DEFAULT FALSE,
            oos_observations        INTEGER          NOT NULL DEFAULT 0,
            oos_hits                INTEGER          NOT NULL DEFAULT 0,
            oos_hit_rate            DOUBLE PRECISION,
            oos_null_rate           DOUBLE PRECISION,
            oos_p_value             DOUBLE PRECISION,
            oos_confirms            BOOLEAN          NOT NULL DEFAULT FALSE,
            median_turn_magnitude_pct DOUBLE PRECISION,
            history_sessions        INTEGER,
            history_start           DATE,
            history_end             DATE,
            created_at              TIMESTAMPTZ      NOT NULL DEFAULT now(),
            PRIMARY KEY (run_id, underlying, arm, cycle_key)
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_gann_cycle_prominence_lookup
        ON gann_cycle_prominence (underlying, arm, status, run_id);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_gann_cycle_prominence_run
        ON gann_cycle_prominence (run_id, created_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS gann_cycle_prominence;")
    op.execute("DROP TABLE IF EXISTS gann_watchlist_snapshots;")
