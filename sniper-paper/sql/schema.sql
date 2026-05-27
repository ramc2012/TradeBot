-- Sniper Paper schema. Lives in the nomad-curie TimescaleDB. All tables prefixed paper_*.
-- Idempotent: safe to run repeatedly.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ─────────────────────────────────────────────────────────────────
-- Live ticks (hypertable)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS paper_ticks (
    ts            TIMESTAMPTZ      NOT NULL,
    symbol        TEXT             NOT NULL,
    instrument    TEXT             NOT NULL,  -- NIFTY | SENSEX | CRUDE
    ltp           DOUBLE PRECISION NOT NULL,
    last_qty      INTEGER,
    bid_px_1      DOUBLE PRECISION,
    ask_px_1      DOUBLE PRECISION,
    bid_qty_1     INTEGER,
    ask_qty_1     INTEGER,
    oi            BIGINT,
    raw           JSONB
);

SELECT create_hypertable('paper_ticks', 'ts', if_not_exists => TRUE, chunk_time_interval => INTERVAL '1 day');
CREATE INDEX IF NOT EXISTS ix_paper_ticks_symbol_ts ON paper_ticks(symbol, ts DESC);
CREATE INDEX IF NOT EXISTS ix_paper_ticks_instrument_ts ON paper_ticks(instrument, ts DESC);

-- ─────────────────────────────────────────────────────────────────
-- Every signal evaluation, taken or skipped
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS paper_signals (
    signal_id     BIGSERIAL        PRIMARY KEY,
    decision_ts   TIMESTAMPTZ      NOT NULL,
    instrument    TEXT             NOT NULL,
    symbol        TEXT             NOT NULL,
    setup_name    TEXT             NOT NULL,
    side          TEXT             NOT NULL CHECK (side IN ('long', 'short')),
    entry_price   DOUBLE PRECISION NOT NULL,
    stop_price    DOUBLE PRECISION NOT NULL,
    target_price  DOUBLE PRECISION NOT NULL,
    p_win         DOUBLE PRECISION,
    expected_net_R DOUBLE PRECISION,
    in_distribution BOOLEAN        NOT NULL,
    gate_decision TEXT             NOT NULL,  -- 'take' | 'skip' (with reason in gate_reason)
    gate_reason   TEXT,
    features      JSONB            NOT NULL,
    model_artifact TEXT            NOT NULL,
    run_id        UUID             NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_paper_signals_decision_ts ON paper_signals(decision_ts DESC);
CREATE INDEX IF NOT EXISTS ix_paper_signals_instrument ON paper_signals(instrument, decision_ts DESC);

-- ─────────────────────────────────────────────────────────────────
-- Paper orders (placed) and paper positions (open / closed)
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS paper_orders (
    order_id      BIGSERIAL        PRIMARY KEY,
    signal_id     BIGINT           REFERENCES paper_signals(signal_id),
    placed_ts     TIMESTAMPTZ      NOT NULL,
    instrument    TEXT             NOT NULL,
    symbol        TEXT             NOT NULL,
    side          TEXT             NOT NULL,
    qty           INTEGER          NOT NULL,
    intended_price DOUBLE PRECISION NOT NULL,
    fill_ts       TIMESTAMPTZ,
    fill_price    DOUBLE PRECISION,
    slippage_inr  DOUBLE PRECISION,
    status        TEXT             NOT NULL  -- 'pending' | 'filled' | 'cancelled'
);
CREATE INDEX IF NOT EXISTS ix_paper_orders_placed_ts ON paper_orders(placed_ts DESC);

CREATE TABLE IF NOT EXISTS paper_positions (
    position_id   BIGSERIAL        PRIMARY KEY,
    signal_id     BIGINT           REFERENCES paper_signals(signal_id),
    open_order_id BIGINT           REFERENCES paper_orders(order_id),
    close_order_id BIGINT          REFERENCES paper_orders(order_id),
    instrument    TEXT             NOT NULL,
    symbol        TEXT             NOT NULL,
    side          TEXT             NOT NULL,
    qty           INTEGER          NOT NULL,
    entry_ts      TIMESTAMPTZ      NOT NULL,
    entry_price   DOUBLE PRECISION NOT NULL,
    stop_price    DOUBLE PRECISION NOT NULL,
    target_price  DOUBLE PRECISION NOT NULL,
    exit_ts       TIMESTAMPTZ,
    exit_price    DOUBLE PRECISION,
    outcome       TEXT,                       -- 'target' | 'stop' | 'timeout' | 'manual'
    gross_pnl     DOUBLE PRECISION,
    costs_inr     DOUBLE PRECISION,
    net_pnl       DOUBLE PRECISION,
    net_R         DOUBLE PRECISION,
    mae           DOUBLE PRECISION,
    mfe           DOUBLE PRECISION,
    status        TEXT             NOT NULL   -- 'open' | 'closed'
);
CREATE INDEX IF NOT EXISTS ix_paper_positions_status ON paper_positions(status, entry_ts DESC);

-- ─────────────────────────────────────────────────────────────────
-- Daily P&L roll-up + run metadata
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS paper_daily_pnl (
    date          DATE             PRIMARY KEY,
    n_signals     INTEGER          NOT NULL DEFAULT 0,
    n_taken       INTEGER          NOT NULL DEFAULT 0,
    n_skipped     INTEGER          NOT NULL DEFAULT 0,
    gross_pnl     DOUBLE PRECISION NOT NULL DEFAULT 0,
    costs_inr     DOUBLE PRECISION NOT NULL DEFAULT 0,
    net_pnl       DOUBLE PRECISION NOT NULL DEFAULT 0,
    consec_losses INTEGER          NOT NULL DEFAULT 0,
    kill_switch_tripped BOOLEAN    NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS paper_runs (
    run_id        UUID             PRIMARY KEY,
    started_ts    TIMESTAMPTZ      NOT NULL,
    stopped_ts    TIMESTAMPTZ,
    model_artifact TEXT            NOT NULL,
    config_hash   TEXT             NOT NULL,
    git_sha       TEXT             NOT NULL,
    notes         TEXT
);
