"""Per-candidate market snapshots — the evaluated set, not just the traded one.

Why this table exists
─────────────────────
Every book of record in this schema only ever sees a contract AFTER something
decided to act on it. `agent_signals` rows are written when a strategy agent
fires a signal; `agent_positions` when one is entered; `strategy_learning_scores`
keeps a `candidates` COUNTER per (strategy, underlying, option_type, reason) but
no per-contract row. So the contracts that were looked at and passed over —
which are most of them, and the only source of negative examples — leave no
trace anywhere.

That makes supervised learning on this system impossible in the honest
direction: a model trained only on taken trades learns the selection rule that
was already in place, not whether it was any good. This table records the whole
decision set at each timestamp, so an outcome can later be computed for every
candidate rather than only the one that happened to be chosen.

`is_selected` is deliberately NOT a foreign key into any lane's book. The lanes
are independent of this observer by design (their known fill/measurement defects
must not leak into training data), so the flag records what THIS pipeline
decided, and reconciliation against lane books, if ever wanted, is a later join
on (session_date, underlying, expiry, strike, option_type).

NO_TRADE rows
─────────────
Every decision cycle writes one row per underlying with option_type='NO_TRADE'
and a NULL strike. Abstention is a real choice with a real outcome (the return
you avoided), and a label space missing it can only ever rank contracts against
each other, never against doing nothing. Storing it from day one means the
abstain option is present in the data before any model asks for it.

Chunk interval
──────────────
7 days, matching `market_ticks` / `option_chain_snapshots`. NOT the 1-day
interval used by `option_premium_candles`, `underlying_spot_candles`,
`fo_option_chain_metrics`, `atm_option_watchlist_snapshots` and
`index_futures_candles` — those five carry thousands of chunks between them and
measurably pay for it in planning time. This table appends a few thousand rows
per session and has no reason to repeat that.

Revision ID: 032_candidate_snapshots
Revises: 031_preopen_atr_last_session
Create Date: 2026-08-26
"""
from typing import Sequence, Union

from alembic import op


revision: str = "032_candidate_snapshots"
down_revision: Union[str, None] = "031_preopen_atr_last_session"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS candidate_snapshots (
            time                    TIMESTAMPTZ      NOT NULL,
            -- One decision SET (one underlying at one capture instant) shares a
            -- decision_id. Ranking and NO_TRADE are only meaningful within a
            -- set, so the grouping has to be stored, not reconstructed.
            decision_id             UUID             NOT NULL,
            session_date            DATE             NOT NULL,

            -- ── taxonomy (candidate_capture/taxonomy.py) ──────────────────
            exchange                TEXT             NOT NULL,
            underlying              TEXT             NOT NULL,
            underlying_class        TEXT             NOT NULL,   -- INDEX | STOCK
            expiry                  DATE,
            -- WEEKLY | MONTHLY | QUARTERLY | LONG_DATED | UNKNOWN, derived from
            -- the exchange's listed set — never from an expiry-weekday rule.
            expiry_class            TEXT             NOT NULL,
            expiry_class_reason     TEXT,
            days_to_expiry          INTEGER,
            -- runs to the 15:30 IST session close on expiry day, not midnight
            hours_to_expiry         DOUBLE PRECISION,
            expiry_day_flag         BOOLEAN          NOT NULL DEFAULT FALSE,
            monthly_expiry_week_flag BOOLEAN         NOT NULL DEFAULT FALSE,
            -- CE | PE | NO_TRADE  (NO_TRADE carries a NULL strike)
            option_type             TEXT             NOT NULL,
            strike                  DOUBLE PRECISION,
            moneyness               TEXT             NOT NULL DEFAULT 'UNKNOWN',
            -- signed distance from the money in LADDER STEPS, positive = ITM
            moneyness_steps         DOUBLE PRECISION,
            -- rank WITHIN this chain, so it never certifies absolute
            -- tradability; the spread/freshness envelope does that.
            liquidity_bucket        TEXT             NOT NULL DEFAULT 'UNKNOWN',
            liquidity_percentile    DOUBLE PRECISION,

            -- ── market state at the decision instant ──────────────────────
            spot                    DOUBLE PRECISION,
            ltp                     DOUBLE PRECISION,
            bid                     DOUBLE PRECISION,
            ask                     DOUBLE PRECISION,
            spread                  DOUBLE PRECISION,
            spread_pct              DOUBLE PRECISION,
            volume                  BIGINT,
            oi                      BIGINT,
            oi_change               DOUBLE PRECISION,
            iv                      DOUBLE PRECISION,   -- PERCENT (Upstox unit)
            delta                   DOUBLE PRECISION,
            gamma                   DOUBLE PRECISION,
            theta                   DOUBLE PRECISION,
            vega                    DOUBLE PRECISION,

            -- Chain-level, underlying-level and regime features. JSONB so a new
            -- feature never needs a migration — the column set above is only
            -- the part that gets indexed or filtered.
            features                JSONB            NOT NULL DEFAULT '{}'::jsonb,

            -- ── data quality (see the "never manufacture" rule) ───────────
            -- Names of the fields this row WANTED and could not get. An empty
            -- array means complete; it is never used to imply a zero.
            missing_fields          JSONB            NOT NULL DEFAULT '[]'::jsonb,
            is_stale                BOOLEAN          NOT NULL DEFAULT FALSE,
            quote_age_seconds       DOUBLE PRECISION,
            -- why a contract was excluded from the eligible set, when it was
            eligibility_status      TEXT             NOT NULL DEFAULT 'eligible',
            eligibility_reason      TEXT,

            -- Whether THIS pipeline selected the candidate. Not a lane's book.
            is_selected             BOOLEAN          NOT NULL DEFAULT FALSE,

            -- ── provenance ────────────────────────────────────────────────
            source                  TEXT,
            capture_version         TEXT             NOT NULL,
            definition_version      TEXT             NOT NULL,
            captured_at             TIMESTAMPTZ      NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        """
        SELECT create_hypertable(
            'candidate_snapshots', 'time',
            if_not_exists => TRUE,
            chunk_time_interval => INTERVAL '7 days'
        );
        """
    )
    # The labeling pass and every per-specialist evaluation slice by
    # (underlying, expiry_class) over a time range — that is the query this
    # table is built to serve.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_candidate_snapshots_underlying_class_time
        ON candidate_snapshots (underlying, expiry_class, time DESC);
        """
    )
    # Pulling one decision set back out whole (ranking, NO_TRADE comparison).
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_candidate_snapshots_decision
        ON candidate_snapshots (decision_id);
        """
    )
    # Per-contract series: the outcome labeller walks forward from a snapshot to
    # the same contract's later prices.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_candidate_snapshots_contract_time
        ON candidate_snapshots (underlying, expiry, strike, option_type, time DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS candidate_snapshots;")
