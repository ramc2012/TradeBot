"""MACD session watchlist — the frozen, sticky ATM strike ladder (owner spec 2026-07-20)

SIDECAR, deliberately. `atm_option_watchlist_snapshots` is a hypertable read by
20 files; it stores a TIME SERIES SAMPLE of a contract. Everything this pass
adds — the frozen pre-open ladder, the price-anchor label, sticky position pins,
warm-up readiness, no-liquid-strike marking — is SESSION STATE, one row per
session per underlying per side. Putting it in the hypertable would force a
schema AND semantic change on all 20 consumers at once. So it lives here and
every existing consumer keeps seeing identical column semantics; only the
`strike` VALUE they read stops drifting intraday, which is the moving-ATM defect
we are deliberately removing (it is the documented cause of option-premium
chart gaps).

Plain Postgres table — bounded at ~(216 underlyings x 2 sides) rows per session.

Revision ID: 028_macd_session_watchlist
Revises: 027_option_chain_snapshots_greeks_lookup
Create Date: 2026-07-20
"""
from typing import Sequence, Union

from alembic import op


revision: str = "028_macd_session_watchlist"
down_revision: Union[str, None] = "027_option_chain_snapshots_greeks_lookup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS macd_session_watchlist (
            session_date          DATE             NOT NULL,
            underlying            TEXT             NOT NULL,
            option_type           TEXT             NOT NULL,
            kind                  TEXT             NOT NULL DEFAULT 'STOCK',
            expiry                DATE,
            strike                DOUBLE PRECISION,
            instrument_key        TEXT,
            trading_symbol        TEXT,
            -- Which price the ladder was anchored on. NEVER silently mixed:
            -- preopen_equilibrium_ltp | preopen_ws_tick | prev_close
            price_anchor          TEXT,
            anchor_price          DOUBLE PRECISION,
            anchor_at             TIMESTAMPTZ,
            -- ok | no_liquid_strike | not_ready
            strike_status         TEXT             NOT NULL DEFAULT 'ok',
            liquidity_oi          DOUBLE PRECISION,
            liquidity_prior_volume DOUBLE PRECISION,
            spread_rel            DOUBLE PRECISION,
            -- history warm-up bookkeeping (so "no signal" is always
            -- distinguishable from "not enough history")
            warmup_bars           INTEGER          NOT NULL DEFAULT 0,
            warmup_path           TEXT,
            warmup_status         TEXT             NOT NULL DEFAULT 'not_ready',
            -- sticky strike: set while a position is open on this strike
            pinned_position_id    TEXT,
            frozen_at             TIMESTAMPTZ,
            repicked_at           TIMESTAMPTZ,
            repick_seq            INTEGER          NOT NULL DEFAULT 0,
            expiry_anchor         TEXT,
            expiry_rolled         BOOLEAN          NOT NULL DEFAULT FALSE,
            notes                 TEXT,
            created_at            TIMESTAMPTZ      NOT NULL DEFAULT now(),
            updated_at            TIMESTAMPTZ      NOT NULL DEFAULT now(),
            PRIMARY KEY (session_date, underlying, option_type)
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_macd_session_watchlist_session
            ON macd_session_watchlist (session_date, strike_status);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_macd_session_watchlist_pinned
            ON macd_session_watchlist (pinned_position_id)
            WHERE pinned_position_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS macd_session_watchlist;")
