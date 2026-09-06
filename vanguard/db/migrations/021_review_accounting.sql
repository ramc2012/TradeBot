-- Preserve membership, contracts and entry marks. Add explicit paper provenance.
ALTER TABLE vanguard_swing_watchlist_runs
    ADD COLUMN IF NOT EXISTS is_replay BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE vanguard_swing_watchlist_items
    ADD COLUMN IF NOT EXISTS cost_pct DOUBLE PRECISION NOT NULL DEFAULT 0.01,
    ADD COLUMN IF NOT EXISTS net_return_pct DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS expected_net_return DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS expected_net_lower DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS return_refusal TEXT;
-- The old decision value was the source candle stamp, retained in prediction_ts.
UPDATE vanguard_swing_watchlist_runs SET decision_at=generated_at
WHERE decision_at=prediction_ts;
