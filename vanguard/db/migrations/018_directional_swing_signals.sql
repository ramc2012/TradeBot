-- Vanguard migration 018: distinguish 1-2 session direction from intraday
-- option-P&L edge.  Both remain observation-only unless a model is explicitly
-- promoted through the existing paper-active gate.

ALTER TABLE vanguard_model_predictions
    ADD COLUMN IF NOT EXISTS ranking_score DOUBLE PRECISION;

UPDATE vanguard_model_predictions
SET ranking_score = conservative_edge
WHERE ranking_score IS NULL;

ALTER TABLE vanguard_watchlist_items
    ADD COLUMN IF NOT EXISTS ranking_score DOUBLE PRECISION;

UPDATE vanguard_watchlist_items
SET ranking_score = conservative_edge
WHERE ranking_score IS NULL;

COMMENT ON COLUMN vanguard_model_predictions.ranking_score IS
    'Horizon-specific observation rank: conditional median signed underlying '
    'return for 1-2-session models; conservative option-PnL edge for one-bar models.';

COMMENT ON COLUMN vanguard_watchlist_items.ranking_score IS
    'Frozen copy of the model prediction ranking_score; membership is immutable.';
