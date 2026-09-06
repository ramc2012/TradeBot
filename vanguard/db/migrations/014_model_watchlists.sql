-- Vanguard migration 014: immutable daily model watchlists and next-session marks.
--
-- A watchlist is observation, not execution.  The final model ranking for a
-- session is frozen once, then the exact option contracts are marked through
-- the next observed NSE session.  No ticket, outcome or broker table is
-- referenced by this lineage.

CREATE TABLE IF NOT EXISTS vanguard_watchlist_runs (
    source_session      DATE PRIMARY KEY,
    model_version       TEXT NOT NULL REFERENCES vanguard_model_versions(version),
    prediction_ts       TIMESTAMPTZ NOT NULL,
    track_session       DATE,
    item_count          INTEGER NOT NULL DEFAULT 0,
    top_n               INTEGER NOT NULL,
    selection_rule      TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN
                            ('awaiting_next_session', 'tracking', 'closed')),
    generated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at          TIMESTAMPTZ,
    closed_at           TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS vanguard_watchlist_items (
    id                  BIGSERIAL PRIMARY KEY,
    source_session      DATE NOT NULL REFERENCES vanguard_watchlist_runs(source_session)
                            ON DELETE RESTRICT,
    rank                INTEGER NOT NULL,
    symbol              TEXT NOT NULL,
    option_type         TEXT NOT NULL CHECK (option_type IN ('CE', 'PE')),
    direction           TEXT NOT NULL CHECK (direction IN ('bullish', 'bearish')),
    instrument          TEXT NOT NULL,
    strike              NUMERIC NOT NULL,
    expiry              DATE NOT NULL,
    source_mark_ts      TIMESTAMPTZ,
    source_mark         DOUBLE PRECISION,
    q10_return          DOUBLE PRECISION NOT NULL,
    q50_return          DOUBLE PRECISION NOT NULL,
    q90_return          DOUBLE PRECISION NOT NULL,
    conservative_edge   DOUBLE PRECISION NOT NULL,
    selection_threshold DOUBLE PRECISION NOT NULL,
    entry_ts            TIMESTAMPTZ,
    entry_mark          DOUBLE PRECISION,
    latest_ts           TIMESTAMPTZ,
    latest_mark         DOUBLE PRECISION,
    return_pct          DOUBLE PRECISION,
    max_return_pct      DOUBLE PRECISION,
    min_return_pct      DOUBLE PRECISION,
    close_ts            TIMESTAMPTZ,
    close_mark          DOUBLE PRECISION,
    close_return_pct    DOUBLE PRECISION,
    status              TEXT NOT NULL CHECK (status IN
                            ('awaiting_next_session', 'tracking', 'closed',
                             'missing_contract', 'expired')),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_session, rank),
    UNIQUE (source_session, symbol)
);

CREATE INDEX IF NOT EXISTS idx_vanguard_watchlist_runs_latest
    ON vanguard_watchlist_runs (source_session DESC);
CREATE INDEX IF NOT EXISTS idx_vanguard_watchlist_items_status
    ON vanguard_watchlist_items (status, source_session DESC);

-- Additive rerun path for installations that applied the first draft of 014.
ALTER TABLE vanguard_watchlist_items
    ADD COLUMN IF NOT EXISTS source_mark_ts TIMESTAMPTZ;

COMMENT ON TABLE vanguard_watchlist_runs IS
    'One immutable end-of-session nonlinear model ranking; never an order list.';
COMMENT ON COLUMN vanguard_watchlist_items.entry_mark IS
    'First exact-contract 30-minute close in the next observed NSE session.';
COMMENT ON COLUMN vanguard_watchlist_items.return_pct IS
    'Latest exact-contract close divided by next-session entry_mark minus one; mark-to-mark, before costs.';
