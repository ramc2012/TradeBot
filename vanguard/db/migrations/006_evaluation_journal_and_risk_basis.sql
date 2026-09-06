-- Vanguard schema, migration 006: the evaluation journal, an honest risk
-- basis, and the cross-section IC store.
--
-- Three defects from the 2026-08-27 lane review are addressed here. Each one
-- needs a schema change, so they land together rather than as three migrations
-- that must be applied in lockstep anyway.
--
-- (A) THE NEAR-MISS JOURNAL DID NOT JOURNAL THE NEAR-MISSES THAT MATTER.
--     m6_select.load_candidates_at dropped every symbol that failed the
--     sector-RS, regime or timing leg with a bare `continue`, BEFORE a
--     Candidate was ever built. Only survivors of the full AND-filter reached
--     `tickets`. Since the filter is the binding constraint (4 candidates in
--     1,022 bars over the whole healthy window), `tickets` could explain why
--     four things failed the conviction gate and nothing at all about why
--     thousands failed the filter -- which is the only question the lane
--     actually needs answered. `candidate_evaluations` is the fix: ONE ROW PER
--     (bar, symbol), every input as joined, every leg's own verdict, and the
--     first leg that failed. Nothing is dropped silently any more.
--
-- (B) THE EOD JOINS HAD NO MAX AGE. M6 took the newest features_flow /
--     sector_rs / leadlag row strictly before the bar's day with no lower
--     bound at all. features_flow ends 2026-07-28 and the cycle daemon runs
--     M6 every 30 minutes, so live evaluations were joining a month-old flow
--     score as "yesterday's reading". The age of every joined input is now
--     stored per row (flow_age_sessions / rs_age_sessions / regime_age_bars)
--     and freshness is an explicit, journaled LEG rather than an unstated
--     assumption.
--
-- (C) SIZING CONFLATED "0.75% AT STOP" WITH "0.75% AS PREMIUM", which made
--     every risk limit non-binding by ~6.7x (a stop at -15% of premium means
--     a stop-out costs 0.1125% of capital, so the -2% daily stand-down needed
--     ~18 stop-outs with max 3 concurrent -- it could never fire). Risk and
--     premium are now two separate numbers on the ticket, so the heat/daily/
--     weekly limits can be denominated in the one that actually measures loss
--     while premium-at-risk stays visible as its own cap.
--
-- Additive-only, same lineage as 001-005, never wired into the live app's
-- Alembic chain. Safe to re-run.

-- ── (C) ticket sizing gains an explicit basis ───────────────────────────────
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS sizing_premium_rupees NUMERIC;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS sizing_risk_basis TEXT;

COMMENT ON COLUMN tickets.sizing_risk_rupees IS
    'Capital at risk if the stop fills as intended: (entry - stop) * lots * lot_size. '
    'This is the number portfolio heat, the daily stand-down and the weekly stop are '
    'all denominated in. Before migration 006 this column held the FULL premium, '
    'which made all three limits ~6.7x too loose.';
COMMENT ON COLUMN tickets.sizing_premium_rupees IS
    'Total premium paid: entry * lots * lot_size. The maximum conceivable loss if the '
    'option gaps through the stop to zero. Capped separately at '
    'MAX_PREMIUM_PER_TRADE_PCT of capital; never used as the heat denominator.';
COMMENT ON COLUMN tickets.sizing_risk_basis IS
    'risk_at_stop (migration 006 onward) | full_premium (pre-006 rows). Read this '
    'before comparing sizing_risk_rupees across the 006 boundary -- the two eras '
    'store different quantities in the same column.';

-- Pre-006 rows genuinely stored premium in sizing_risk_rupees. Label them
-- rather than rewriting them: the number they were sized on is a historical
-- fact, and silently re-deriving it would falsify what the lane actually did.
UPDATE tickets SET sizing_risk_basis = 'full_premium'
 WHERE sizing_risk_basis IS NULL AND sizing_risk_rupees IS NOT NULL;

-- ── (A)+(B) the evaluation journal ─────────────────────────────────────────
-- One row per (bar, symbol) for EVERY symbol M5 wrote a timing bar for --
-- roughly 210 symbols x 13 bars = ~2,700 rows/session. This is deliberately
-- the widest table in the lane: it is simultaneously the funnel's evidence,
-- the per-symbol market view the desk renders, and the unfiltered
-- cross-section the IC study needs. Storing it once serves all three; deriving
-- it three ways would let them disagree.
CREATE TABLE IF NOT EXISTS candidate_evaluations (
    ts                  TIMESTAMPTZ NOT NULL,
    symbol              TEXT NOT NULL,
    sector20            TEXT,

    -- inputs exactly as joined, with the age of each one. An age is in the
    -- unit that input is PRODUCED in: sessions for the EOD tables, bars for
    -- the intraday ones. NULL age = the input itself was absent.
    flow_score          DOUBLE PRECISION,
    flow_ts             TIMESTAMPTZ,
    flow_age_sessions   INTEGER,
    flow_n_ingredients  INTEGER,
    rs_z20              DOUBLE PRECISION,
    rs_ts               TIMESTAMPTZ,
    rs_age_sessions     INTEGER,
    regime              TEXT,
    gex_percentile      DOUBLE PRECISION,
    regime_ts           TIMESTAMPTZ,
    regime_age_bars     INTEGER,
    timing_state        TEXT,
    timing_score        DOUBLE PRECISION,
    rvol                DOUBLE PRECISION,
    va_position         DOUBLE PRECISION,
    best_lag            INTEGER,
    leadlag_corr        DOUBLE PRECISION,

    -- leg verdicts. TRUE = passed, FALSE = failed, NULL = not evaluable
    -- because a prior leg already ended the evaluation (short-circuit), so a
    -- NULL is never confused with a fail.
    leg_flow_present    BOOLEAN,
    leg_flow_fresh      BOOLEAN,
    leg_flow_strength   BOOLEAN,
    leg_sector_rs       BOOLEAN,
    leg_regime          BOOLEAN,
    leg_timing          BOOLEAN,
    first_failed_leg    TEXT,           -- NULL only when every leg passed
    survived_filter     BOOLEAN NOT NULL,

    direction           TEXT,           -- bullish | bearish | NULL when no flow
    conviction          DOUBLE PRECISION,
    component_scores    JSONB,

    -- signed, unaligned values for the cross-sectional IC study. The
    -- component_scores above are direction-ALIGNED magnitudes (that is what
    -- M6 fuses), and correlating an aligned magnitude with a signed forward
    -- return is a category error. These are the raw signed readings.
    signed_flow         DOUBLE PRECISION,
    signed_rs           DOUBLE PRECISION,
    signed_timing       DOUBLE PRECISION,
    signed_regime       DOUBLE PRECISION,

    config_hash         TEXT NOT NULL,  -- see fusion/m6_select.config_hash()
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, ts)
);
SELECT create_hypertable('candidate_evaluations', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_cand_eval_ts ON candidate_evaluations (ts DESC);
CREATE INDEX IF NOT EXISTS idx_cand_eval_failed_leg
    ON candidate_evaluations (first_failed_leg, ts DESC);
CREATE INDEX IF NOT EXISTS idx_cand_eval_survived
    ON candidate_evaluations (ts DESC) WHERE survived_filter;

-- ── features_flow gets a real migration at last ────────────────────────────
-- Its DDL lived only inside features/m2_flow.py and was applied as a SIDE
-- EFFECT of running that module, so `make db-init` on a fresh database never
-- created it -- and this migration's ALTER below would then fail, taking 006
-- with it. Created here (identical to m2_flow's own CREATE, which stays where
-- it is so the module remains runnable standalone) before it is altered.
CREATE TABLE IF NOT EXISTS features_flow (
    ts          TIMESTAMPTZ NOT NULL,
    symbol      TEXT NOT NULL,
    ivs         DOUBLE PRECISION,
    ivs_z       DOUBLE PRECISION,
    skew        DOUBLE PRECISION,
    skew_z      DOUBLE PRECISION,
    os_pctile   DOUBLE PRECISION,
    oi_state    TEXT,
    pcr_z       DOUBLE PRECISION,
    flow_score  DOUBLE PRECISION,
    PRIMARY KEY (symbol, ts)
);
SELECT create_hypertable('features_flow', 'ts', if_not_exists => TRUE);

-- ── (fix) features_flow finally records how many ingredients it had ────────
-- ~42% of the candidate pool is a saturated +/-100 flow score derived from a
-- SINGLE ingredient, and M6 could not tell those from corroborated ones
-- because the count was computed and then thrown away. It is now stored, and
-- M6 gates on it (FLOW_MIN_INGREDIENTS).
ALTER TABLE features_flow ADD COLUMN IF NOT EXISTS n_ingredients INTEGER;
COMMENT ON COLUMN features_flow.n_ingredients IS
    'How many of the five ingredients (ivs/skew/os/oi/pcr) were non-NULL and '
    'therefore actually contributed to flow_score. A score of +/-100 built from '
    'one ingredient is not the same reading as one built from five; NULL means '
    'the row predates migration 006 and the count was not retained.';

-- BACKFILL, and why it is a reconstruction rather than an estimate.
-- features_flow already stores each of the five ingredients in its own column,
-- and m2_flow's compute_flow_score() counts precisely those five being
-- non-None. So the count is EXACTLY recoverable from rows already on disk --
-- this is not a guess, it is the same arithmetic replayed. Re-running M2 to
-- regenerate it would mean a 130-day chain pull per session for a number
-- already implied by the row.
--
-- Only NULL rows are touched, so re-running cannot overwrite a count M2 wrote
-- itself. oi_state is counted the way M2 counts it: a non-NULL state
-- contributes, and it is NULL for every row ever written (no stock-level
-- futures-OI source exists), which the count then reflects honestly.
UPDATE features_flow SET n_ingredients =
      (ivs_z     IS NOT NULL)::int
    + (skew_z    IS NOT NULL)::int
    + (os_pctile IS NOT NULL)::int
    + (oi_state  IS NOT NULL)::int
    + (pcr_z     IS NOT NULL)::int
WHERE n_ingredients IS NULL;

-- ── the cross-section IC store ─────────────────────────────────────────────
-- Per-session rank IC of each raw component against forward return, over the
-- FULL cross-section (every symbol with a bar), not the post-filter pool.
-- Correlating a component inside its own filter (flow >= 60, timing >= 70,
-- regime in three buckets) restricts its range to near-nothing and guarantees
-- an uninformative coefficient -- which is what attribution_runs was doing.
CREATE TABLE IF NOT EXISTS cross_section_ic (
    id              BIGSERIAL PRIMARY KEY,
    run_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    as_of_date      DATE NOT NULL,
    window_start    DATE NOT NULL,
    window_end      DATE NOT NULL,
    component       TEXT NOT NULL,
    horizon_bars    INTEGER NOT NULL,
    n_obs           INTEGER NOT NULL,
    n_sessions      INTEGER NOT NULL,
    mean_ic         DOUBLE PRECISION,
    -- SE across SESSION means, not across observations. Same-session
    -- observations share a market-wide shock, so the naive per-observation SE
    -- runs several times too small -- this lane's own directional research
    -- measured 1.6x-4.7x too small on exactly this mistake.
    ic_se_clustered DOUBLE PRECISION,
    t_stat          DOUBLE PRECISION,
    ci_low          DOUBLE PRECISION,
    ci_high         DOUBLE PRECISION,
    report          JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_cross_section_ic_date
    ON cross_section_ic (as_of_date DESC, component, horizon_bars);
