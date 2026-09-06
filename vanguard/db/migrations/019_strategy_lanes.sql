-- Three explicit Vanguard strategy lanes with independent journals.
-- Existing model-watchlist and MP-paper tables remain authoritative history;
-- these tables are additive and never rewrite prior membership or marks.

CREATE TABLE IF NOT EXISTS vanguard_rank_model_versions (
    version                 TEXT PRIMARY KEY,
    role                    TEXT NOT NULL CHECK (role IN ('direction', 'contract')),
    family                  TEXT NOT NULL,
    status                  TEXT NOT NULL CHECK (status IN ('shadow', 'refused')),
    feature_names           TEXT[] NOT NULL,
    artifact                JSONB NOT NULL,
    artifact_sha256         TEXT NOT NULL,
    dataset_sha256          TEXT NOT NULL,
    training_start          DATE NOT NULL,
    training_end            DATE NOT NULL,
    validation_start        DATE NOT NULL,
    validation_end          DATE NOT NULL,
    test_start              DATE NOT NULL,
    test_end                DATE NOT NULL,
    n_train                 INTEGER NOT NULL,
    n_validation            INTEGER NOT NULL,
    n_test                  INTEGER NOT NULL,
    decision_time_ist       TIME NOT NULL DEFAULT TIME '14:15',
    planned_entry_time_ist  TIME NOT NULL DEFAULT TIME '14:45',
    metrics                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_vanguard_rank_models_role_created
    ON vanguard_rank_model_versions (role, created_at DESC);

CREATE TABLE IF NOT EXISTS vanguard_swing_watchlist_runs (
    source_session          DATE PRIMARY KEY,
    prediction_ts           TIMESTAMPTZ NOT NULL,
    direction_model_version TEXT NOT NULL REFERENCES vanguard_rank_model_versions(version),
    contract_model_version  TEXT NOT NULL REFERENCES vanguard_rank_model_versions(version),
    top_n                   INTEGER NOT NULL,
    item_count              INTEGER NOT NULL DEFAULT 0,
    status                  TEXT NOT NULL DEFAULT 'awaiting_entry',
    decision_at             TIMESTAMPTZ NOT NULL,
    entry_session           DATE,
    generated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (status IN ('awaiting_entry', 'tracking', 'closed', 'partial', 'missing_data'))
);

CREATE TABLE IF NOT EXISTS vanguard_swing_watchlist_items (
    id                      BIGSERIAL PRIMARY KEY,
    source_session          DATE NOT NULL REFERENCES vanguard_swing_watchlist_runs(source_session),
    rank                    INTEGER NOT NULL,
    symbol                  TEXT NOT NULL,
    option_type             TEXT NOT NULL CHECK (option_type IN ('CE', 'PE')),
    horizon_sessions        INTEGER NOT NULL CHECK (horizon_sessions IN (1, 2)),
    instrument              TEXT NOT NULL,
    strike                  NUMERIC NOT NULL,
    expiry                  DATE NOT NULL,
    contract_kind           TEXT NOT NULL CHECK (contract_kind IN ('ATM', 'WING_25D')),
    direction_score         DOUBLE PRECISION NOT NULL,
    contract_score          DOUBLE PRECISION NOT NULL,
    combined_score          DOUBLE PRECISION NOT NULL,
    source_mark_ts          TIMESTAMPTZ NOT NULL,
    source_mark             NUMERIC NOT NULL,
    entry_ts                TIMESTAMPTZ,
    entry_mark              NUMERIC,
    latest_ts               TIMESTAMPTZ,
    latest_mark             NUMERIC,
    return_pct              DOUBLE PRECISION,
    max_return_pct          DOUBLE PRECISION,
    min_return_pct          DOUBLE PRECISION,
    day_1_ts                TIMESTAMPTZ,
    day_1_mark              NUMERIC,
    day_1_return_pct        DOUBLE PRECISION,
    day_2_ts                TIMESTAMPTZ,
    day_2_mark              NUMERIC,
    day_2_return_pct        DOUBLE PRECISION,
    status                  TEXT NOT NULL DEFAULT 'awaiting_entry',
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_session, rank),
    UNIQUE (source_session, symbol, option_type, horizon_sessions),
    CHECK (status IN ('awaiting_entry', 'tracking', 'closed', 'expired', 'missing_contract'))
);

CREATE INDEX IF NOT EXISTS idx_vanguard_swing_items_status
    ON vanguard_swing_watchlist_items (status, source_session, rank);

CREATE TABLE IF NOT EXISTS vanguard_strategy_journal (
    id                      BIGSERIAL PRIMARY KEY,
    strategy                TEXT NOT NULL CHECK (strategy IN ('gap_overnight', 'swing_1_2d', 'oversold_mtf')),
    event_key               TEXT NOT NULL,
    event_type              TEXT NOT NULL,
    source_session          DATE,
    event_ts                TIMESTAMPTZ NOT NULL,
    symbol                  TEXT NOT NULL,
    option_type             TEXT,
    instrument              TEXT,
    horizon_sessions        INTEGER,
    rank                    INTEGER,
    score                   DOUBLE PRECISION,
    status                  TEXT NOT NULL,
    entry_mark              NUMERIC,
    latest_mark             NUMERIC,
    realized_return_pct     DOUBLE PRECISION,
    payload                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (strategy, event_key)
);

CREATE INDEX IF NOT EXISTS idx_vanguard_strategy_journal_lookup
    ON vanguard_strategy_journal (strategy, event_ts DESC, id DESC);

CREATE OR REPLACE VIEW vanguard_overnight_journal AS
    SELECT * FROM vanguard_strategy_journal WHERE strategy='gap_overnight';

CREATE OR REPLACE VIEW vanguard_swing_journal AS
    SELECT * FROM vanguard_strategy_journal WHERE strategy='swing_1_2d';

CREATE OR REPLACE VIEW vanguard_oversold_mtf_journal AS
    SELECT * FROM vanguard_strategy_journal WHERE strategy='oversold_mtf';
