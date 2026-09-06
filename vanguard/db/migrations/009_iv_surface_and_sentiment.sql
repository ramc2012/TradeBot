-- Vanguard schema, migration 009: per-symbol IV surface, and market-wide
-- sentiment.
--
-- ── iv_surface ────────────────────────────────────────────────────────────
-- Per-underlying aggregation of the contract-level IVs in `option_iv`, built
-- from `quality = 'good'` rows only.
--
-- THE BREADTH FINDING THIS TABLE IS SHAPED BY. Measured 2026-08-27 over the
-- whole collected history, contracts per symbol per day:
--
--     late May      3.1        early July     6.6   <- the widest it ever got
--     mid June      4.1        late July      3.9
--     late June     6.5        late August    1.2
--
-- The chain has NEVER carried enough strikes to locate a genuine 25-delta
-- contract on either wing. M2 computes its "25-delta skew" with
-- `idxmin(|delta - 0.25|)` and no tolerance band, so it took whatever strike
-- was nearest however far away -- and with three to six strikes clustered
-- around the money, that is a near-ATM contract. Its SKEW ingredient was
-- therefore measuring approximately the same quantity as its IVS ingredient,
-- at a further 25% weight, in a composite where the two together are 55%.
--
-- So `skew_25d` here is populated ONLY when contracts genuinely within
-- SKEW_DELTA_TOLERANCE of ±0.25 exist on both wings, and is NULL otherwise
-- with `skew_reason` naming what was missing. `n_strikes` and
-- `delta_span` are stored beside every row so the breadth behind a number is
-- never invisible.
--
-- ── market_sentiment ──────────────────────────────────────────────────────
-- One row per session for the market as a whole. `participant_oi` is an
-- AGGREGATE publication -- FII/DII/Pro/Client by instrument class, never per
-- symbol -- so it can only ever be a market-wide overlay, and modelling it as
-- anything else would be inventing per-name detail NSE does not publish.

CREATE TABLE IF NOT EXISTS iv_surface (
    dt              DATE NOT NULL,
    symbol          TEXT NOT NULL,
    expiry          DATE,                   -- the front series this describes

    atm_iv          DOUBLE PRECISION,
    atm_strike      DOUBLE PRECISION,
    spot            DOUBLE PRECISION,
    call_iv         DOUBLE PRECISION,       -- mean near-ATM call IV
    put_iv          DOUBLE PRECISION,       -- mean near-ATM put IV
    -- Cremers-Weinbaum implied-volatility spread: call IV minus put IV near
    -- the money. THIS is the informed-flow quantity M2 wants.
    ivs             DOUBLE PRECISION,
    skew_25d        DOUBLE PRECISION,       -- NULL unless both wings genuinely exist
    skew_reason     TEXT,                   -- why skew_25d is NULL, when it is

    -- Trailing context. A raw IV is not a signal; where it sits in its own
    -- history is.
    iv_percentile   DOUBLE PRECISION,       -- rank of atm_iv in its own trailing window
    iv_rank         DOUBLE PRECISION,       -- (iv - min) / (max - min), same window
    d_atm_iv        DOUBLE PRECISION,

    n_strikes       INTEGER,
    n_good          INTEGER,                -- contracts of quality='good' behind this row
    delta_span      DOUBLE PRECISION,       -- max(delta) - min(delta) over the chain;
                                            -- how much of the wing structure exists at all
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (dt, symbol)
);
CREATE INDEX IF NOT EXISTS idx_iv_surface_symbol ON iv_surface (symbol, dt DESC);
CREATE INDEX IF NOT EXISTS idx_iv_surface_dt ON iv_surface (dt DESC);

CREATE TABLE IF NOT EXISTS market_sentiment (
    dt                  DATE NOT NULL PRIMARY KEY,

    -- Participant positioning. Net = long - short, in contracts, exactly as
    -- NSE publishes it. Market-wide by construction: this file has no
    -- per-symbol dimension and never has.
    fii_fut_index_net       BIGINT,
    fii_fut_stock_net       BIGINT,
    fii_opt_index_net       BIGINT,     -- long calls + short puts, net directional
    fii_index_long_ratio    DOUBLE PRECISION,  -- long / (long + short), index futures
    dii_fut_index_net       BIGINT,
    client_fut_index_net    BIGINT,
    client_opt_index_net    BIGINT,
    pro_fut_index_net       BIGINT,
    d_fii_fut_index_net     BIGINT,     -- session change; the flow, not the stock

    -- Options sentiment, aggregated across the universe from live contract OI.
    market_oi_pcr           DOUBLE PRECISION,
    market_volume_pcr       DOUBLE PRECISION,
    d_market_oi_pcr         DOUBLE PRECISION,

    -- Volatility, from Vanguard's own computed surface.
    index_atm_iv            DOUBLE PRECISION,   -- NIFTY front-series ATM
    d_index_atm_iv          DOUBLE PRECISION,
    median_stock_iv         DOUBLE PRECISION,
    iv_percentile           DOUBLE PRECISION,

    -- Breadth. Sentiment that comes from prices rather than from positioning.
    advances                INTEGER,
    declines                INTEGER,
    advance_decline_ratio   DOUBLE PRECISION,
    pct_above_20d           DOUBLE PRECISION,
    median_ret_1d           DOUBLE PRECISION,

    -- Positioning conjunction, rolled up across the universe.
    long_buildup_count      INTEGER,
    short_buildup_count     INTEGER,
    short_covering_count    INTEGER,
    long_unwind_count       INTEGER,
    net_oi_bias             DOUBLE PRECISION,   -- (long build - short build) / classified

    -- A single -100..+100 read, and the components that produced it, so the
    -- headline can always be taken apart. Never stored without them.
    sentiment_score         DOUBLE PRECISION,
    sentiment_components    JSONB NOT NULL DEFAULT '{}'::jsonb,
    computed_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_market_sentiment_dt ON market_sentiment (dt DESC);

COMMENT ON COLUMN iv_surface.skew_25d IS
    'Risk reversal: 25-delta put IV minus 25-delta call IV. NULL unless contracts '
    'genuinely within tolerance of ±0.25 delta exist on BOTH wings — the collected '
    'chain has never been wide enough for this on most names, and substituting the '
    'nearest available strike (which is what M2 did) measures near-ATM IVS again '
    'under a different name.';
COMMENT ON TABLE market_sentiment IS
    'Market-wide only. participant_oi is an aggregate publication by instrument '
    'class with no per-symbol dimension, so FII/DII positioning can never be '
    'attributed to a name.';
