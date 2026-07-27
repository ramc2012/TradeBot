"""Pre-open spot snapshot + activeness flag (owner spec 2026-07-27)

Owner: "For spot add pre-open historical values also. add activeness flag to
mark interesting activeness in pre-open trades."

The NSE cash pre-open call auction (09:00-09:08 IST order collection,
09:08-09:12 matching, 09:12-09:15 buffer) publishes an EQUILIBRIUM price and a
MATCHED quantity. Those two numbers, plus the auction order book, are the only
pre-session evidence of where the day's interest actually sits.

The raw material already lands in `market_ticks` when the WS is alive — but a
tick tape is not a queryable per-session record: it is unbounded, it mixes cash
auction prints with F&O frames that are NOT auction prints (NSE F&O opens
09:15) and with MCX (which has no call auction at all), and it is compressed
after 3 days. This table is the DURABLE, BOUNDED, one-row-per
(session_date, underlying) distillation.

SIDECAR, deliberately. `underlying_spot_candles` is a bar hypertable read by
dozens of modules; a pre-open auction print is not a bar (no meaningful OHLC —
open=high=low=ltp) and the activeness verdict is session state, not a time
series. Bounded at ~217 rows per session (211 F&O stocks + 6 index roots).

NEVER FABRICATE. Every derived field is NULL when it cannot be computed, and
`data_status` / `components_unknown` record WHY. A name with no pre-open data
is `unknown`, never `quiet`.

Revision ID: 030_preopen_spot_snapshot
Revises: 029_gann_lane
Create Date: 2026-07-27
"""
from typing import Sequence, Union

from alembic import op


revision: str = "030_preopen_spot_snapshot"
down_revision: Union[str, None] = "029_gann_lane"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS preopen_spot_snapshots (
            session_date            DATE             NOT NULL,
            underlying              TEXT             NOT NULL,
            -- INDEX | STOCK. MCX is deliberately absent: there is no MCX call
            -- auction, so an MCX "pre-open" row would be a fabricated concept.
            kind                    TEXT             NOT NULL,
            -- the market_ticks symbol this row was read from (provenance)
            tick_symbol             TEXT,

            -- ── capture provenance ────────────────────────────────────────
            -- ok | no_match | no_preopen_ticks | stale_carry |
            -- price_band_reject | session_dark
            data_status             TEXT             NOT NULL,
            data_status_reason      TEXT,
            source                  TEXT,
            -- session_catalog (live runner: full universe, absence recorded)
            -- ticks_only     (backfill: only names that actually ticked, so a
            --                 past session is never judged against today's
            --                 catalog)
            universe_source         TEXT,
            window_start            TIMESTAMPTZ      NOT NULL,
            window_end              TIMESTAMPTZ      NOT NULL,
            -- tick_count is a CAPTURE artifact (how many WS frames arrived),
            -- NOT market participation. Stored for provenance; deliberately
            -- excluded from the activeness score.
            tick_count              INTEGER          NOT NULL DEFAULT 0,
            distinct_price_count    INTEGER          NOT NULL DEFAULT 0,
            first_tick_at           TIMESTAMPTZ,
            last_tick_at            TIMESTAMPTZ,

            -- ── the auction print ─────────────────────────────────────────
            preopen_price           DOUBLE PRECISION,
            preopen_price_at        TIMESTAMPTZ,
            -- matched quantity. NULL for indices (an index has no traded
            -- volume) with the reason recorded, never 0.
            preopen_volume          BIGINT,
            preopen_bid             DOUBLE PRECISION,
            preopen_ask             DOUBLE PRECISION,
            total_buy_qty           BIGINT,
            total_sell_qty          BIGINT,
            prev_close              DOUBLE PRECISION,
            -- tick_close_field | spot_30m_prior_session |
            -- spot_30m_prior_session_anchor_mismatch | unavailable
            prev_close_source       TEXT,
            gap_pct                 DOUBLE PRECISION,

            -- ── activeness ────────────────────────────────────────────────
            -- active | quiet | unknown   (NEVER 'quiet' on missing data)
            activeness_state        TEXT             NOT NULL DEFAULT 'unknown',
            activeness_score        DOUBLE PRECISION,
            -- which component(s) TRIGGERED — the queryable REASON
            activeness_reasons      JSONB            NOT NULL DEFAULT '[]'::jsonb,
            components_available    JSONB            NOT NULL DEFAULT '[]'::jsonb,
            -- {component: why it could not be computed}
            components_unknown      JSONB            NOT NULL DEFAULT '{}'::jsonb,
            -- per-component raw values, so the verdict is auditable
            rel_volume              DOUBLE PRECISION,
            rel_volume_baseline     DOUBLE PRECISION,
            rel_volume_baseline_n   INTEGER,
            gap_atr_ratio           DOUBLE PRECISION,
            atr_pct_14              DOUBLE PRECISION,
            atr_sessions_n          INTEGER,
            -- raw (total_buy_qty - total_sell_qty) / (sum). MEASURED to carry a
            -- market-wide bias (93% of prints negative, session mean -0.4 every
            -- session), so the SCORED quantity is the cross-sectional z-score
            -- below, not this level. The raw value is kept so the z is auditable.
            book_imbalance          DOUBLE PRECISION,
            book_imbalance_z        DOUBLE PRECISION,
            peer_median_book_imbalance DOUBLE PRECISION,
            peer_sigma_book_imbalance  DOUBLE PRECISION,
            peer_n                  INTEGER          NOT NULL DEFAULT 0,
            definition_version      TEXT             NOT NULL,

            computed_at             TIMESTAMPTZ      NOT NULL DEFAULT now(),
            PRIMARY KEY (session_date, underlying)
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_preopen_spot_snapshots_session_state
            ON preopen_spot_snapshots (session_date, activeness_state);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_preopen_spot_snapshots_underlying
            ON preopen_spot_snapshots (underlying, session_date DESC);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_preopen_spot_snapshots_ok_volume
            ON preopen_spot_snapshots (underlying, session_date DESC)
            WHERE data_status = 'ok' AND preopen_volume IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS preopen_spot_snapshots;")
