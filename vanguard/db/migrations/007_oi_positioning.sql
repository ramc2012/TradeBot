-- Vanguard schema, migration 007: per-symbol open interest, positioning state
-- and price performance.
--
-- WHY THIS EXISTS AT ALL: m2_flow.py's module docstring states, as a verified
-- finding, that "No stock-level futures-OI source exists anywhere in the
-- 72-table schema", and on that basis its fourth ingredient -- the delta-OI
-- conjunction, worth 15% of the flow composite -- has been hardcoded NULL for
-- every row the lane has ever written. `classify_oi_state()` was implemented
-- and unit-tested and then never called with real data.
--
-- That finding is wrong. Two live per-symbol OI sources exist, both fresh on
-- 2026-08-27 when this was checked:
--
--   fo_mwpl_snapshot.open_interest   aggregate F&O open interest per symbol,
--                                    daily, 211 symbols, back to 2026-07-09.
--                                    This is NSE's own MWPL publication --
--                                    the same file the ban list comes from,
--                                    which the lane already ingests and reads
--                                    only for the ban flag.
--   option_premium_candles.oi        per-CONTRACT chain OI, 213 underlyings,
--                                    latest print 2026-08-27 09:57 UTC. Only
--                                    the GREEKS on that table died in July;
--                                    close/oi/volume never stopped.
--
-- The second one also revives a feed everyone had written off:
-- `fo_option_chain_metrics` (ce_oi / pe_oi / oi_pcr) tops out at 2026-08-03
-- and is treated as dead, but its INPUTS are alive and the aggregate is
-- recomputed here from them.
--
-- WHAT THIS BUYS. The four-state OI/price conjunction is the single most
-- widely used positioning read on Indian F&O and the desk had none of it:
--
--   price up,   OI up    long buildup      new longs, conviction behind the move
--   price down, OI up    short buildup     new shorts, conviction behind the move
--   price up,   OI down  short covering    shorts closing -- a weaker rally
--   price down, OI down  long unwinding    longs closing -- a weaker decline
--
-- MWPL utilisation comes along for free, and it is the number that matters
-- most operationally: past 95% NSE bans fresh F&O positions in the name.
--
-- Additive-only, same lineage as 001-006. Safe to re-run.

CREATE TABLE IF NOT EXISTS oi_positioning (
    dt                DATE NOT NULL,
    symbol            TEXT NOT NULL,

    -- Aggregate F&O open interest, in shares. `oi_source` records WHICH of the
    -- two feeds a row came from, because they are not the same quantity --
    -- mwpl is exchange-published across every series, chain_sum is this lane's
    -- own aggregation of the contracts it happens to collect. A row is never
    -- silently promoted from one to the other.
    total_oi          BIGINT,
    prev_total_oi     BIGINT,
    d_oi              BIGINT,
    d_oi_pct          DOUBLE PRECISION,
    oi_source         TEXT,            -- mwpl | chain_sum

    -- Ban proximity. NSE bans fresh F&O in a name once utilisation crosses 95%.
    mwpl              BIGINT,
    mwpl_pct          DOUBLE PRECISION,

    -- Call/put split, recomputed from option_premium_candles' live contract OI
    -- rather than read from the stalled fo_option_chain_metrics aggregate.
    ce_oi             BIGINT,
    pe_oi             BIGINT,
    oi_pcr            DOUBLE PRECISION,
    d_oi_pcr          DOUBLE PRECISION,

    -- Price, and the performance the desk had no column for at all.
    close             DOUBLE PRECISION,
    prev_close        DOUBLE PRECISION,
    d_price_pct       DOUBLE PRECISION,
    ret_5d            DOUBLE PRECISION,
    ret_20d           DOUBLE PRECISION,
    ret_60d           DOUBLE PRECISION,

    -- The conjunction. NULL -- never a guess -- when either leg is missing or
    -- exactly zero, which is what m2_flow.classify_oi_state() already returns
    -- and why this uses that function rather than restating the rule.
    oi_state          TEXT,            -- long_buildup | short_buildup | short_covering | long_unwind
    -- |d_oi_pct| x |d_price_pct|: how emphatic the conjunction is, so a 0.1%
    -- drift in both legs is not rendered with the same weight as a real build.
    oi_state_strength DOUBLE PRECISION,

    computed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (dt, symbol)
);
CREATE INDEX IF NOT EXISTS idx_oi_positioning_symbol ON oi_positioning (symbol, dt DESC);
CREATE INDEX IF NOT EXISTS idx_oi_positioning_dt ON oi_positioning (dt DESC);
CREATE INDEX IF NOT EXISTS idx_oi_positioning_state ON oi_positioning (oi_state, dt DESC);

COMMENT ON COLUMN oi_positioning.oi_source IS
    'mwpl = NSE aggregate F&O OI (fo_mwpl_snapshot). chain_sum = this lane''s own '
    'sum of collected contract OI. Different quantities; never mixed within a row, '
    'and a symbol''s d_oi is only computed against a previous row of the SAME source.';
