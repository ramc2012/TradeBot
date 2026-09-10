-- Derived longitudinal evidence; frozen membership and paper books are untouched.
CREATE TABLE IF NOT EXISTS vanguard_observation_followup (
 lane TEXT NOT NULL,
 source_session DATE NOT NULL,
 rank INTEGER NOT NULL,
 symbol TEXT NOT NULL,
 expiry DATE NOT NULL,
 model_version TEXT NOT NULL,
 payload JSONB NOT NULL,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(lane, source_session, rank)
);
CREATE INDEX IF NOT EXISTS idx_vanguard_followup_symbol ON vanguard_observation_followup(symbol,source_session);
