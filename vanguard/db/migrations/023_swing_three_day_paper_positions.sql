-- Extend the paper-only CE/PE swing lane to a causal D+1/D+2/D+3 hold.
-- Existing rows retain their membership and marks; new columns are additive.

ALTER TABLE vanguard_swing_watchlist_items
    DROP CONSTRAINT IF EXISTS vanguard_swing_watchlist_items_horizon_sessions_check;

ALTER TABLE vanguard_swing_watchlist_items
    ADD CONSTRAINT vanguard_swing_watchlist_items_horizon_sessions_check
        CHECK (horizon_sessions IN (1, 2, 3)),
    ADD COLUMN IF NOT EXISTS day_3_ts             TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS day_3_mark           NUMERIC,
    ADD COLUMN IF NOT EXISTS day_3_return_pct     DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS paper_position      BOOLEAN NOT NULL DEFAULT true;

COMMENT ON COLUMN vanguard_swing_watchlist_items.paper_position IS
    'Paper-only simulated entry for the immutable top-10 CE/PE research ranking; never a broker order.';

COMMENT ON COLUMN vanguard_swing_watchlist_items.day_3_return_pct IS
    'Exact-contract mark-to-market return at the third observed NSE session close, before costs.';
