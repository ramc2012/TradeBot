-- Vanguard schema, migration 002: M6 tickets through M10 journal.
--
-- Same lineage as 001_schema.sql -- additive-only, applied by Vanguard's own
-- db/apply.py, never wired into the live app's Alembic chain. Table names
-- match the spec's Section 4 schema (tickets/decisions/fills/outcomes)
-- exactly.
--
-- Paper-trading semantics, stated once here since every downstream table
-- depends on it: doctrine #4 forbids an execution layer and describes the
-- eventual LIVE flow as a human clicking TAKEN/SKIPPED/MODIFIED on a
-- Telegram ticket. Vanguard has no such human-facing delivery yet (that is
-- M9's "shadow mode" concept, further out than this build). What exists
-- here is a broker-free, read-only PAPER simulation: every ticket that
-- clears M7's risk gate is automatically journaled as decision
-- 'AUTO_PAPER_TAKEN' (never 'TAKEN' -- that word is reserved for an actual
-- human click and must never be used for an automated decision, so the two
-- are never confused when this eventually grows a live ticket UI), and its
-- fill/outcome are computed deterministically from REAL SUBSEQUENT MARKET
-- DATA already sitting in option_premium_candles/underlying_spot_candles --
-- never from a live tick stream, never via any broker API call. This is a
-- backtest-shaped construct (walk forward against historical-now-current
-- bars) that happens to run close to real time, not a trading system.

CREATE TABLE IF NOT EXISTS tickets (
    id                     BIGSERIAL PRIMARY KEY,
    ts                     TIMESTAMPTZ NOT NULL,       -- when M6 generated it
    symbol                 TEXT NOT NULL,               -- the underlying (equity/index)
    instrument             TEXT NOT NULL,               -- the actual tradable: ATM CE/PE symbol
    direction              TEXT NOT NULL,                -- bullish | bearish
    entry_zone_low         NUMERIC,
    entry_zone_high        NUMERIC,
    stop                   NUMERIC,
    target1                NUMERIC,
    target2                NUMERIC,
    conviction             DOUBLE PRECISION NOT NULL,   -- 0-100
    rank_in_session        INTEGER NOT NULL,             -- 1..N among that session's candidates, by conviction
    regime_at_ts           TEXT,
    evidence               JSONB NOT NULL,               -- {flow_score, sector_rs_z, timing_score, timing_state, gex_regime, leadlag_bonus, component_scores: {...}}
    sizing_lots            INTEGER,
    sizing_notional        NUMERIC,
    sizing_risk_rupees     NUMERIC,
    sizing_method          TEXT,                         -- 'kelly_0.25x' | 'fixed_fractional' | NULL if gated out before sizing
    emitted                BOOLEAN NOT NULL,              -- true = ticket actually issued; false = would-be candidate suppressed by a gate (audit trail)
    gated_reason           TEXT,                          -- populated when emitted=false: which gate stopped it
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tickets_ts ON tickets (ts DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_symbol_ts ON tickets (symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_emitted ON tickets (emitted, ts DESC);

CREATE TABLE IF NOT EXISTS decisions (
    ticket_id   BIGINT PRIMARY KEY REFERENCES tickets(id),
    decision    TEXT NOT NULL,             -- 'AUTO_PAPER_TAKEN' today; 'TAKEN'/'SKIPPED'/'MODIFIED' reserved for a future human-facing UI, never written by this code
    decided_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS fills (
    ticket_id    BIGINT PRIMARY KEY REFERENCES tickets(id),
    fill_price   NUMERIC NOT NULL,
    fill_ts      TIMESTAMPTZ NOT NULL,
    fill_method  TEXT NOT NULL             -- always a 'simulated_*' value -- see module docstrings; never a broker fill
);

CREATE TABLE IF NOT EXISTS outcomes (
    ticket_id     BIGINT PRIMARY KEY REFERENCES tickets(id),
    exit_price    NUMERIC,
    exit_ts       TIMESTAMPTZ,
    exit_reason   TEXT,                    -- stop | target1 | target2 | time_stop_eod | time_stop_t3 | daily_standdown_flatten
    pnl_rupees    NUMERIC,
    r_multiple    NUMERIC,                 -- pnl / risk-at-entry
    holding_bars  INTEGER,
    closed        BOOLEAN NOT NULL DEFAULT false,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_outcomes_open ON outcomes (closed) WHERE closed = false;

-- M7 risk state: capital and daily/weekly P&L bookkeeping, one row per
-- trading day. STAND-DOWN is derived (daily_pnl_pct <= -2.0), not stored as
-- a separate flag, so it can never drift out of sync with the number that
-- defines it.
CREATE TABLE IF NOT EXISTS paper_capital_daily (
    dt              DATE NOT NULL PRIMARY KEY,
    starting_equity NUMERIC NOT NULL,
    ending_equity   NUMERIC,
    realized_pnl    NUMERIC NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- M10 attribution: nightly rollup, one row per run.
CREATE TABLE IF NOT EXISTS attribution_runs (
    id                BIGSERIAL PRIMARY KEY,
    run_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    as_of_date        DATE NOT NULL,
    n_tickets_closed  INTEGER NOT NULL,
    hit_rate          DOUBLE PRECISION,
    avg_r             DOUBLE PRECISION,
    conviction_decile_monotonic BOOLEAN,
    report            JSONB NOT NULL      -- full breakdown: per-decile R, per-component IC, per-symbol/sector attribution
);
CREATE INDEX IF NOT EXISTS idx_attribution_runs_date ON attribution_runs (as_of_date DESC);
