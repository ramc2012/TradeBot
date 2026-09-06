-- Vanguard migration 017: per-contract stock/index futures daily candles WITH
-- open interest, plus per-symbol OI baselines from the stitched front series.
--
-- Closes the gap documented in features/m2_flow.py: stock-futures OI had no
-- source in this schema (underlying_spot_candles.oi is 0/NULL for stocks,
-- index_futures_candles covers 3 indices and drops the OI field). Upstox's
-- candle arrays carry OI at index 6; this table is the first to keep it.
--
-- stock_futures_daily rows are per (symbol, expiry, session date). The `source`
-- column distinguishes settled daily candles ('upstox_daily',
-- 'upstox_expired') from the live intraday-refresh row for today
-- ('upstox_intraday_live'), which the EOD pass overwrites with the settled
-- candle on the next --eod run.

CREATE TABLE IF NOT EXISTS stock_futures_daily (
    ts              DATE NOT NULL,
    symbol          TEXT NOT NULL,
    expiry          DATE NOT NULL,
    instrument_key  TEXT NOT NULL,
    open            DOUBLE PRECISION,
    high            DOUBLE PRECISION,
    low             DOUBLE PRECISION,
    close           DOUBLE PRECISION,
    volume          BIGINT,
    oi              BIGINT,
    source          TEXT NOT NULL,
    synced_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, expiry, ts)
);
CREATE INDEX IF NOT EXISTS idx_stock_futures_daily_symbol_ts
    ON stock_futures_daily (symbol, ts DESC);

COMMENT ON TABLE stock_futures_daily IS
    'Per-contract NSE futures daily candles with open interest (Upstox candle '
    'index 6). Stocks + NIFTY/BANKNIFTY/SENSEX. History from ~2024-06 via the '
    'expired-instruments endpoints (one-time token-gated backfill); steady '
    'state from the public daily + intraday endpoints.';

CREATE TABLE IF NOT EXISTS futures_oi_baselines (
    symbol              TEXT NOT NULL,
    ts                  DATE NOT NULL,
    expiry              DATE NOT NULL,
    close               DOUBLE PRECISION,
    d_price_pct         DOUBLE PRECISION,
    oi                  BIGINT,
    d_oi                BIGINT,
    d_oi_pct            DOUBLE PRECISION,
    d_oi_pct_z          DOUBLE PRECISION,
    oi_z                DOUBLE PRECISION,
    volume_z            DOUBLE PRECISION,
    oi_pctile           DOUBLE PRECISION,
    oi_state            TEXT,
    activity_surge      BOOLEAN NOT NULL DEFAULT false,
    is_rollover         BOOLEAN NOT NULL DEFAULT false,
    lookback_sessions   INTEGER NOT NULL,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_futures_oi_baselines_ts
    ON futures_oi_baselines (ts DESC);

COMMENT ON COLUMN futures_oi_baselines.is_rollover IS
    'True when the front contract expiry changed vs the prior session. Deltas '
    '(d_oi, d_oi_pct, d_price_pct, oi_state) are NULL on rollover rows: an '
    'expiry change makes consecutive OI levels incomparable (m2_flow doctrine).';
COMMENT ON COLUMN futures_oi_baselines.d_oi_pct_z IS
    'Z-score of d_oi_pct over the trailing lookback_sessions non-rollover '
    'sessions. All signal thresholds are z/percentile-relative by design; no '
    'absolute OI level is ever a gate.';
COMMENT ON COLUMN futures_oi_baselines.oi_state IS
    'Four-state buildup classification from features/m2_flow.classify_oi_state: '
    'long_buildup, short_buildup, short_covering, long_unwind; NULL on zero '
    'delta or rollover.';
COMMENT ON COLUMN futures_oi_baselines.activity_surge IS
    'True when d_oi_pct_z and volume_z both exceed the surge threshold '
    '(module default 1.5): OI build with participation, the "increase in '
    'OI/activity may precede a move" flag.';
