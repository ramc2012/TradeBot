-- 011: Market Profile + order-flow structure features, one row per
--      (underlying, session).
--
-- WHY. The 2026-08-28 research pass established, with walk-forward and
-- adversarial checks, what the auction structure on this data can and cannot
-- say. This table persists exactly that: the profile metrics as CONTEXT (they
-- predict RANGE, not direction -- ib_width/atr rank IC +0.46 on |move|, |t|<1.8
-- on signed move), plus the two signals that actually survived out-of-sample
-- scrutiny, stored as flags so fusion and research can consume them without
-- recomputing profiles.
--
-- WHAT DELIBERATELY IS NOT HERE. No VWAP and no absorption for indices --
-- underlying_spot_candles carries zero index volume, so those would be
-- fabrications. Order-flow proxies are populated for stocks only and
-- of_available says so per row. The MTF-alignment flag ("above all three") is
-- omitted on purpose: it was tested and it REDUCES hit rates monotonically.
--
-- Every statement is additive, per db/apply.py's contract.

CREATE TABLE IF NOT EXISTS features_mp (
    dt                DATE        NOT NULL,
    underlying        TEXT        NOT NULL,
    -- profile of the session (TPO over 30m bars, 70% value area)
    poc               NUMERIC,
    vah               NUMERIC,
    val               NUMERIC,
    va_width_pct      NUMERIC,      -- (vah-val)/close * 100
    ib_width_pct      NUMERIC,      -- first-hour range as % of price
    ib_pct_rank       NUMERIC,      -- vs the name's own trailing 120 sessions
    range_over_ib     NUMERIC,
    close_pos         NUMERIC,      -- (close-low)/(high-low)
    day_type          TEXT,         -- trend / normal / normal_variation / ...
    poor_high         BOOLEAN,
    poor_low          BOOLEAN,
    tail_high_pct     NUMERIC,
    tail_low_pct      NUMERIC,
    single_prints     INTEGER,
    -- comparative (vs prior session)
    value_shift       TEXT,         -- higher_outside / lower_outside / inside / overlapping
    poc_migration_pct NUMERIC,
    va_overlap        NUMERIC,
    failed_high       BOOLEAN,
    failed_low        BOOLEAN,
    -- multi-timeframe location (prior COMPLETED week / month value areas)
    w_loc             TEXT,         -- above / inside / below
    m_loc             TEXT,
    -- order-flow proxies (STOCKS ONLY -- indices have no volume here)
    of_available      BOOLEAN     NOT NULL DEFAULT FALSE,
    of_delta_share    NUMERIC,      -- (up-vol - down-vol) / total, close-vs-open sign
    of_close_vs_vwap  NUMERIC,      -- % of price; session VWAP from 30m bars
    of_rvol20         NUMERIC,      -- session volume vs trailing 20-session mean (lagged)
    -- the two validated signals, as flags the lane can read directly
    sig_strong_close  BOOLEAN,      -- close > VAH AND close_pos in [0.70, 0.90]
                                    -- (acceptance, not spike; overnight-gap edge)
    sig_oversold_mtf  BOOLEAN,      -- close below day AND week AND month value
                                    -- (contrarian up-move signal, replicated x3)
    -- range context: what the structure IS entitled to say
    exp_range_pct     NUMERIC,      -- lagged ATR20 as % -- the sizing input
    computed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (dt, underlying)
);

CREATE INDEX IF NOT EXISTS idx_features_mp_underlying_dt
    ON features_mp (underlying, dt DESC);

-- the two flags are what fusion will scan for
CREATE INDEX IF NOT EXISTS idx_features_mp_signals
    ON features_mp (dt) WHERE sig_strong_close OR sig_oversold_mtf;
