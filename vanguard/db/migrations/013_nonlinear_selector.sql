-- Vanguard migration 013: versioned nonlinear option-P&L selector.
--
-- The original M6 joined M2-M5 with a sequence of boolean gates.  This store
-- keeps the replacement model reproducible: every artifact includes its
-- feature contract, chronological split, cost assumption and holdout metrics.
-- It is intentionally paper-only; no broker/execution table is referenced.

CREATE TABLE IF NOT EXISTS vanguard_model_versions (
    id                  BIGSERIAL PRIMARY KEY,
    version             TEXT NOT NULL UNIQUE,
    family              TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN
                            ('paper_active', 'shadow', 'refused', 'retired')),
    horizon_bars        INTEGER NOT NULL,
    cost_pct            DOUBLE PRECISION NOT NULL,
    cost_provenance     TEXT NOT NULL,
    training_start      DATE NOT NULL,
    training_end        DATE NOT NULL,
    validation_start    DATE NOT NULL,
    validation_end      DATE NOT NULL,
    test_start          DATE NOT NULL,
    test_end            DATE NOT NULL,
    n_train             INTEGER NOT NULL,
    n_validation        INTEGER NOT NULL,
    n_test              INTEGER NOT NULL,
    feature_names       JSONB NOT NULL,
    metrics             JSONB NOT NULL,
    artifact            JSONB NOT NULL,
    artifact_sha256     TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- At most one model can drive PAPER ticket selection.  Shadow/refused models
-- may coexist for comparison without changing decisions.
CREATE UNIQUE INDEX IF NOT EXISTS uq_vanguard_one_paper_model
    ON vanguard_model_versions ((status)) WHERE status = 'paper_active';
CREATE INDEX IF NOT EXISTS idx_vanguard_models_created
    ON vanguard_model_versions (created_at DESC);

CREATE TABLE IF NOT EXISTS vanguard_model_predictions (
    ts                  TIMESTAMPTZ NOT NULL,
    symbol              TEXT NOT NULL,
    option_type         TEXT NOT NULL CHECK (option_type IN ('CE', 'PE')),
    model_version       TEXT NOT NULL REFERENCES vanguard_model_versions(version),
    q10_return          DOUBLE PRECISION NOT NULL,
    q50_return          DOUBLE PRECISION NOT NULL,
    q90_return          DOUBLE PRECISION NOT NULL,
    conservative_edge   DOUBLE PRECISION NOT NULL,
    selection_threshold DOUBLE PRECISION NOT NULL,
    selected            BOOLEAN NOT NULL,
    reason              TEXT,
    instrument          TEXT,
    strike              NUMERIC,
    expiry              DATE,
    entry_mark          DOUBLE PRECISION,
    realized_return     DOUBLE PRECISION,
    realized_net_return DOUBLE PRECISION,
    resolved_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (ts, symbol, option_type, model_version)
);
SELECT create_hypertable('vanguard_model_predictions', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_vanguard_predictions_selected
    ON vanguard_model_predictions (ts DESC, selected, conservative_edge DESC);

-- Additive rerun path for installations that applied an earlier draft of 013.
ALTER TABLE vanguard_model_predictions ADD COLUMN IF NOT EXISTS strike NUMERIC;
ALTER TABLE vanguard_model_predictions ADD COLUMN IF NOT EXISTS expiry DATE;
ALTER TABLE vanguard_model_predictions ADD COLUMN IF NOT EXISTS entry_mark DOUBLE PRECISION;
ALTER TABLE vanguard_model_predictions ADD COLUMN IF NOT EXISTS realized_return DOUBLE PRECISION;
ALTER TABLE vanguard_model_predictions ADD COLUMN IF NOT EXISTS realized_net_return DOUBLE PRECISION;
ALTER TABLE vanguard_model_predictions ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;

-- Recover contract identity for draft rows from their matching shadow ticket.
UPDATE vanguard_model_predictions p
SET strike = t.strike,
    expiry = t.expiry,
    entry_mark = (t.entry_zone_low + t.entry_zone_high) / 2.0
FROM tickets t
WHERE p.ts=t.ts AND p.symbol=t.symbol AND p.option_type=t.option_type
  AND p.model_version=t.evidence->>'model_version'
  AND (p.strike IS NULL OR p.expiry IS NULL OR p.entry_mark IS NULL);

COMMENT ON COLUMN vanguard_model_versions.cost_provenance IS
    'Historical bid/ask is unavailable. v1 targets mark-to-mark option return '
    'and deducts the declared assumed round-trip cost; it is not measured execution P&L.';
