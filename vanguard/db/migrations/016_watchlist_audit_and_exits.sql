-- Additive audit only. Never changes tickets, outcomes or MP paper positions.
ALTER TABLE vanguard_watchlist_items ADD COLUMN IF NOT EXISTS performance_audit JSONB;
ALTER TABLE vanguard_watchlist_items ADD COLUMN IF NOT EXISTS exit_analysis JSONB;
CREATE TABLE IF NOT EXISTS vanguard_watchlist_exit_policies (
    version TEXT PRIMARY KEY,
    policy JSONB NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE vanguard_model_predictions ADD COLUMN IF NOT EXISTS source_mark_ts TIMESTAMPTZ;
ALTER TABLE vanguard_model_predictions ADD COLUMN IF NOT EXISTS decision_at TIMESTAMPTZ;
ALTER TABLE vanguard_model_predictions ADD COLUMN IF NOT EXISTS timing_policy TEXT;
COMMENT ON COLUMN vanguard_watchlist_items.performance_audit IS
  'Original pre-correction marks retained once; not claimed as executable returns.';
COMMENT ON COLUMN vanguard_watchlist_items.exit_analysis IS
  'Versioned broker-free exit replay. Historical replay is not prospective validation.';
