-- Additive; no catalog, candle or ledger rows are deleted.
ALTER TABLE fo_underlying_catalog ADD COLUMN IF NOT EXISTS fno_active boolean;
ALTER TABLE fo_underlying_catalog ADD COLUMN IF NOT EXISTS fno_snapshot_at timestamptz;
