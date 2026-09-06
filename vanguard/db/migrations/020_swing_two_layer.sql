-- The swing lane's daily output has two layers (owner plan, 2026-09-04):
--
--   1. a MANDATORY research ranking -- top ten CE and top ten PE; and
--   2. an ACTIONABLE list -- zero to ten contracts, after expected-return,
--      confidence, liquidity and M7 risk gates.
--
-- Layer 1 was already stored; it was a single mixed top-ten rather than a
-- ranking per side, and it carried no notion of layer 2 at all. Additive only:
-- existing rows keep their global `rank` and simply have no side_rank.

ALTER TABLE vanguard_swing_watchlist_items
    -- Rank WITHIN the side, so "top ten CE" and "top ten PE" are each a real
    -- ranking rather than whatever survived a mixed list.
    ADD COLUMN IF NOT EXISTS side_rank           INTEGER,
    -- Layer 2. NULL reason on an actionable row; on a refused one it says
    -- which gate refused, because "not actionable" with no reason is how a
    -- lane goes quiet without anyone noticing.
    ADD COLUMN IF NOT EXISTS actionable          BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS actionable_reason   TEXT,
    -- M7's answer, kept whether or not the row cleared: a refused sizing is
    -- evidence about the gate, not something to discard.
    ADD COLUMN IF NOT EXISTS lot_size            INTEGER,
    ADD COLUMN IF NOT EXISTS sizing_lots         INTEGER,
    ADD COLUMN IF NOT EXISTS sizing_notional     NUMERIC,
    ADD COLUMN IF NOT EXISTS sizing_risk_rupees  NUMERIC,
    ADD COLUMN IF NOT EXISTS sizing_method       TEXT,
    -- Liquidity as observed at the decision bar, not re-read later.
    ADD COLUMN IF NOT EXISTS option_volume       NUMERIC,
    ADD COLUMN IF NOT EXISTS option_oi           NUMERIC;

CREATE INDEX IF NOT EXISTS idx_vanguard_swing_items_side
    ON vanguard_swing_watchlist_items (source_session, option_type, side_rank);

ALTER TABLE vanguard_swing_watchlist_runs
    ADD COLUMN IF NOT EXISTS actionable_count    INTEGER NOT NULL DEFAULT 0,
    -- Why layer 2 was empty. With both rankers shadow this is the whole
    -- story of the day, and it must survive in the run row itself.
    ADD COLUMN IF NOT EXISTS actionable_note     TEXT;
