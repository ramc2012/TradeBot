-- Vanguard schema, migration 008: computed implied volatility and greeks.
--
-- `option_premium_candles.iv` / `.delta` stopped for the equity universe on
-- 2026-07-28 and `.gamma` around 2026-06-23, which left M2's first three
-- ingredients uncomputable and M3 index-only. Everything needed to SOLVE for
-- IV -- premium, strike, expiry, option type, spot -- has been sitting in
-- Postgres the whole time, for years back.
--
-- This table is Vanguard's own. It deliberately does NOT back-fill
-- `option_premium_candles`: that table belongs to the live application, and a
-- research lane writing into a live-app column is precisely the cross-writing
-- this whole schema lineage exists to avoid.
--
-- `quality` / `quality_flags` are first-class columns, not diagnostics. An IV
-- solved from a stale last-trade print on an untraded strike is a number and
-- not a measurement, and the earlier review of this lane named that failure
-- directly. Aggregation reads `good` rows only.

CREATE TABLE IF NOT EXISTS option_iv (
    dt              DATE NOT NULL,
    symbol          TEXT NOT NULL,
    expiry          DATE NOT NULL,
    strike          DOUBLE PRECISION NOT NULL,
    option_type     TEXT NOT NULL,          -- CE | PE

    premium         DOUBLE PRECISION,       -- the price solved from
    spot            DOUBLE PRECISION,       -- from underlying_spot_candles, NOT
                                            -- option_premium_candles.underlying_price
                                            -- (that column died 2026-07-30)
    oi              BIGINT,
    volume          BIGINT,
    days_to_expiry  DOUBLE PRECISION,
    log_moneyness   DOUBLE PRECISION,       -- ln(K / forward)

    iv              DOUBLE PRECISION,       -- NULL when no sigma reproduces the price,
                                            -- or when the price does not identify one
    iv_uncertainty  DOUBLE PRECISION,       -- sigma range one 5-paise tick spans at
                                            -- the solution. Always populated, including
                                            -- where iv is NULL, so a rejection is
                                            -- inspectable rather than mysterious.
    delta           DOUBLE PRECISION,
    gamma           DOUBLE PRECISION,
    vega            DOUBLE PRECISION,       -- per 1 vol point
    theta           DOUBLE PRECISION,       -- per calendar day

    quality         TEXT NOT NULL,          -- good | weak | unusable
    quality_flags   TEXT NOT NULL DEFAULT '',
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (dt, symbol, expiry, strike, option_type)
);
CREATE INDEX IF NOT EXISTS idx_option_iv_symbol_dt ON option_iv (symbol, dt DESC);
CREATE INDEX IF NOT EXISTS idx_option_iv_dt ON option_iv (dt DESC);
CREATE INDEX IF NOT EXISTS idx_option_iv_good ON option_iv (symbol, dt DESC) WHERE quality = 'good';

COMMENT ON COLUMN option_iv.iv IS
    'Black-Scholes-Merton implied volatility, European exercise (NSE index and '
    'stock options are European-style cash-settled). Dividends are not modelled; '
    'for a name going ex-dividend inside the contract life this biases call IV '
    'down and put IV up. NULL means the price sits outside the no-arbitrage band '
    'and no sigma reproduces it -- never a boundary value standing in for one.';

-- Additive column, applied separately: CREATE TABLE IF NOT EXISTS is a no-op
-- once the table exists, so editing the CREATE above does nothing to a database
-- that already ran an earlier version of this file. Every column added after
-- the first apply needs its own ALTER, or the migration silently succeeds and
-- the column is silently absent -- which is exactly how this one was found.
ALTER TABLE option_iv ADD COLUMN IF NOT EXISTS iv_uncertainty DOUBLE PRECISION;
COMMENT ON COLUMN option_iv.iv_uncertainty IS
    'Sigma range that one 5-paise price tick spans at the solution (TICK_SIZE/vega). '
    'Always populated, including where iv is NULL, so a refusal is inspectable. '
    'iv is NULL wherever this exceeds MAX_IV_UNCERTAINTY: the price did not identify '
    'a volatility, and answering anyway returns a plausible-looking wrong number.';
