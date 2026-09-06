-- Vanguard migration 015: causal same-expiry option-premium ratios per bar.
--
-- These are descriptive model inputs, never entry gates.  Every row is built
-- only from contracts visible at `ts`, for the front expiry at that timestamp.
-- A genuine 25-delta wing must be within 0.08 delta of the target; otherwise
-- wing ratios remain NULL rather than substituting an ATM contract.

CREATE TABLE IF NOT EXISTS option_premium_ratios (
    ts                              TIMESTAMPTZ NOT NULL,
    symbol                          TEXT NOT NULL,
    expiry                          DATE NOT NULL,
    spot                            DOUBLE PRECISION NOT NULL,
    atm_strike                      DOUBLE PRECISION NOT NULL,
    atm_call                        DOUBLE PRECISION,
    atm_put                         DOUBLE PRECISION,
    atm_iv                          DOUBLE PRECISION,
    straddle_to_spot                DOUBLE PRECISION,
    normalized_straddle             DOUBLE PRECISION,
    atm_put_call_premium_ratio      DOUBLE PRECISION,
    atm_call_put_extrinsic_ratio    DOUBLE PRECISION,
    call_itm_atm_extrinsic_ratio    DOUBLE PRECISION,
    call_otm_atm_extrinsic_ratio    DOUBLE PRECISION,
    put_itm_atm_extrinsic_ratio     DOUBLE PRECISION,
    put_otm_atm_extrinsic_ratio     DOUBLE PRECISION,
    call_wing_iv_ratio              DOUBLE PRECISION,
    put_wing_iv_ratio               DOUBLE PRECISION,
    strangle_straddle_ratio         DOUBLE PRECISION,
    premium_pcr                     DOUBLE PRECISION,
    wing_valid                      BOOLEAN NOT NULL DEFAULT false,
    call_wing_delta_gap             DOUBLE PRECISION,
    put_wing_delta_gap              DOUBLE PRECISION,
    n_strikes                       INTEGER NOT NULL,
    n_contracts                     INTEGER NOT NULL,
    method                          TEXT NOT NULL,
    computed_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ts, symbol, expiry)
);
SELECT create_hypertable('option_premium_ratios', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_option_premium_ratios_symbol_ts
    ON option_premium_ratios (symbol, ts DESC);

COMMENT ON COLUMN option_premium_ratios.premium_pcr IS
    'Sum(put close*volume)/sum(call close*volume) for the same timestamp and expiry. '
    'It is premium turnover, not buyer-initiated flow; trade aggressor side is unavailable.';
COMMENT ON COLUMN option_premium_ratios.normalized_straddle IS
    '(ATM call + ATM put)/spot/sqrt(calendar DTE/365).';
COMMENT ON COLUMN option_premium_ratios.strangle_straddle_ratio IS
    '25-delta put+call premium divided by ATM straddle; NULL unless both wings are within 0.08 delta.';
