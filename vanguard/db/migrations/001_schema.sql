-- Vanguard schema, migration 001.
--
-- This is a SEPARATE, additive-only migration lineage from the live app's
-- Alembic chain (backend/db/migrations, currently at head 031). It is never
-- wired into `alembic upgrade head` and the live backend never imports or
-- runs it -- Vanguard applies it itself via `make db-init` / vanguard/db/apply.py
-- against the SAME `nomadcurie` Postgres instance the live app uses (per
-- "Use existing data"), so restarting nomadcurie_backend can never trip over
-- it and a live-app migration can never collide with Vanguard's numbering.
--
-- Only tables genuinely absent from the existing schema are created here.
-- A pre-build inventory of the live database (2026-08-26) found five tables
-- that already cover what the spec's Section 4 schema asks for, under
-- different names -- Vanguard reads those directly rather than duplicating
-- their storage:
--
--   spec name          -> existing table                  (owner: live app)
--   bars_30m / bars_1d -> underlying_spot_candles          (225 underlyings, 2021-present, multi-interval)
--   option_chain_snap  -> option_chain_snapshots           (22.5M rows; narrowed to 4 index underlyings recently)
--   (PCR/OI ingredient) -> fo_option_chain_metrics         (oi_pcr, volume_pcr, ce/pe oi+volume, 30m)
--   universe/lot sizes -> fo_underlying_catalog            (211 stocks + 7 indices, lot_size, instrument keys)
--   ban list           -> fo_security_ban                  (sourced from NSE's fo_secban.csv archive already)
--
-- What is genuinely new, and lives here:
--   participant_oi     -- NSE daily participant-wise OI archive. No existing
--                          table anywhere in the schema carries this.
--   sector_taxonomy     -- sector / sector_group / sector20 for fo_underlying_catalog's
--                          symbols. fo_underlying_catalog has no sector columns.
--                          Kept as its own table (FK-joined by symbol) rather
--                          than an ALTER on a live-app-owned table.
--   ingest_log          -- Vanguard's own collector run log (idempotency +
--                          the "5 clean sessions" acceptance-gate evidence).
--   results_calendar    -- results-date guard for M7's event-guard rule.
--   sector_rs, leadlag  -- M4 outputs, computed from underlying_spot_candles.
--
-- Everything from M2 onward (features_flow, regime, timing, tickets,
-- decisions, fills, outcomes) is deliberately NOT created yet. Those are
-- P2-P5 concerns; creating empty tables for phases not yet built invites
-- them silently going stale. Add them in a 002_ migration when M2/M3/M6/M9/M10
-- actually start writing to them.

CREATE TABLE IF NOT EXISTS participant_oi (
    dt              DATE NOT NULL,
    participant     TEXT NOT NULL,   -- Client | DII | FII | Pro
    bucket          TEXT NOT NULL,   -- fut_index | fut_stock | opt_index_call | opt_index_put | opt_stock_call | opt_stock_put
    long_contracts  BIGINT NOT NULL,
    short_contracts BIGINT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'nse_fao_participant_oi_csv',
    synced_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (dt, participant, bucket)
);
CREATE INDEX IF NOT EXISTS idx_participant_oi_dt ON participant_oi (dt DESC);

CREATE TABLE IF NOT EXISTS sector_taxonomy (
    symbol       TEXT PRIMARY KEY,   -- joins to fo_underlying_catalog.symbol
    exchange     TEXT NOT NULL,
    instrument_type TEXT NOT NULL,   -- Equity | Index
    sector       TEXT NOT NULL,      -- fine tier (~110 buckets observed)
    sector_group TEXT NOT NULL,      -- broad tier (~26 buckets observed)
    sector20     TEXT,               -- correlation-reduced tier; NULL until
                                      -- build_sector_indices.py computes it
    notes        TEXT NOT NULL DEFAULT '',
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ingest_log (
    id           BIGSERIAL PRIMARY KEY,
    collector    TEXT NOT NULL,       -- e.g. 'm1_participant_oi'
    run_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    target_date  DATE,
    status       TEXT NOT NULL,       -- ok | empty | error
    rows_written INTEGER NOT NULL DEFAULT 0,
    detail       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ingest_log_collector_date
    ON ingest_log (collector, target_date DESC);
-- One successful row per (collector, target_date): re-running a collector
-- for a day it already completed is a no-op, matching the resumable-download
-- pattern MACD mini's research.py uses for the same reason.
CREATE UNIQUE INDEX IF NOT EXISTS uq_ingest_log_ok
    ON ingest_log (collector, target_date) WHERE status = 'ok';

CREATE TABLE IF NOT EXISTS results_calendar (
    symbol       TEXT NOT NULL,
    results_date DATE NOT NULL,
    source       TEXT NOT NULL DEFAULT 'manual',
    synced_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, results_date)
);

CREATE TABLE IF NOT EXISTS sector_rs (
    ts        TIMESTAMPTZ NOT NULL,
    sector20  TEXT NOT NULL,
    rs_z5     DOUBLE PRECISION,
    rs_z20    DOUBLE PRECISION,
    rs_z60    DOUBLE PRECISION,
    PRIMARY KEY (sector20, ts)
);
SELECT create_hypertable('sector_rs', 'ts', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS leadlag (
    dt        DATE NOT NULL,
    symbol    TEXT NOT NULL,
    sector20  TEXT NOT NULL,
    best_lag  INTEGER NOT NULL,   -- bars, -2..+2 at 30m per the spec
    corr      DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (dt, symbol)
);
CREATE INDEX IF NOT EXISTS idx_leadlag_sector ON leadlag (sector20, dt DESC);
