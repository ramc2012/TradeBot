"""Model versions, training runs, and the predictions they made.

Follows the `rl_policy_versions` precedent (migration 012): a versioned artifact
with an explicit champion/challenger lifecycle, its metrics, and the reason it
was promoted or refused. Kept separate from that table rather than reusing it,
because an RL Q-table and a calibrated supervised ranker have different
artifacts, different gates and different failure modes — sharing one row shape
would force both into whichever one was described first.

WHY PREDICTIONS ARE STORED
──────────────────────────
Evaluation here is PREQUENTIAL: predict first, observe later. That is only an
honest claim if the prediction is durable and timestamped BEFORE the outcome it
is scored against — otherwise "out of sample" is an assertion about how the code
was run, not a fact about the data. `candidate_predictions` is what makes the
claim checkable after the fact.

WHY THE GATES LIVE IN A COLUMN
──────────────────────────────
`promotion_gates` records the FULL gate result per model — every gate, its
threshold, its measured value, and whether it passed — not just the verdict.
A model refused for one gate, whose threshold later moves, must be re-judgeable
from the stored row rather than re-run from memory.

Revision ID: 036_candidate_model_versions
Revises: 035_candidate_outcome_spot_lag
Create Date: 2026-08-27
"""
from typing import Sequence, Union

from alembic import op


revision: str = "036_candidate_model_versions"
down_revision: Union[str, None] = "035_candidate_outcome_spot_lag"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS candidate_model_versions (
            id                  UUID PRIMARY KEY,
            version_name        TEXT NOT NULL UNIQUE,
            -- candidate | champion | challenger | retired | refused
            status              TEXT NOT NULL DEFAULT 'candidate',
            -- the family, so a baseline and a later neural challenger are
            -- comparable but never silently pooled
            model_family        TEXT NOT NULL,

            -- WHAT THIS MODEL IS FOR. A model is only ever valid for the
            -- (horizon, contract class) it was fitted and gated on; the plan's
            -- specialists are exactly this. Stored so a model can never be
            -- applied to a class it was never evaluated on.
            horizon_seconds     INTEGER NOT NULL,
            underlying_class    TEXT,
            expiry_class        TEXT,
            target              TEXT NOT NULL,

            feature_names       JSONB NOT NULL DEFAULT '[]'::jsonb,
            -- the fitted artifact itself (coefficients + calibrator knots)
            artifact            JSONB NOT NULL,

            train_rows          INTEGER,
            train_sessions      INTEGER,
            eval_rows           INTEGER,
            eval_sessions       INTEGER,
            train_start         DATE,
            train_end           DATE,
            eval_start          DATE,
            eval_end            DATE,

            metrics             JSONB NOT NULL DEFAULT '{}'::jsonb,
            -- every gate: threshold, measured value, pass/fail — not a verdict
            promotion_gates     JSONB NOT NULL DEFAULT '[]'::jsonb,
            gates_passed        BOOLEAN,
            promotion_reason    TEXT,

            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            promoted_at         TIMESTAMPTZ,
            retired_at          TIMESTAMPTZ
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_candidate_model_versions_status
        ON candidate_model_versions (status, horizon_seconds, created_at DESC);
        """
    )
    # At most ONE champion per specialist slot. A second champion for the same
    # (horizon, class, target) is not a race to be resolved later — it means two
    # models are both authoritative, and nothing downstream could say which.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_candidate_model_champion
        ON candidate_model_versions (
            horizon_seconds, target,
            COALESCE(underlying_class, ''), COALESCE(expiry_class, '')
        )
        WHERE status = 'champion';
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS candidate_training_runs (
            id                  UUID PRIMARY KEY,
            started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at         TIMESTAMPTZ,
            -- ok | failed | insufficient_data | no_decidable_stratum
            status              TEXT NOT NULL,
            reason              TEXT,
            requested           JSONB NOT NULL DEFAULT '{}'::jsonb,
            produced            JSONB NOT NULL DEFAULT '[]'::jsonb,
            data_summary        JSONB NOT NULL DEFAULT '{}'::jsonb
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_candidate_training_runs_started
        ON candidate_training_runs (started_at DESC);
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS candidate_predictions (
            time                TIMESTAMPTZ NOT NULL,
            model_version       TEXT NOT NULL,
            decision_id         UUID NOT NULL,
            session_date        DATE NOT NULL,
            underlying          TEXT NOT NULL,
            expiry              DATE,
            strike              DOUBLE PRECISION,
            option_type         TEXT NOT NULL,
            horizon_seconds     INTEGER NOT NULL,

            raw_score           DOUBLE PRECISION,
            calibrated_prob     DOUBLE PRECISION,
            -- the compounded-utility score the global ranker sorted on
            utility_score       DOUBLE PRECISION,
            rank_in_set         INTEGER,
            -- did the ranker pick this candidate (NO_TRADE included)
            selected            BOOLEAN NOT NULL DEFAULT FALSE,

            predicted_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        """
        SELECT create_hypertable(
            'candidate_predictions', 'time',
            if_not_exists => TRUE,
            chunk_time_interval => INTERVAL '7 days'
        );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_candidate_predictions_row
        ON candidate_predictions (
            time, model_version, decision_id, option_type,
            COALESCE(strike, -1), COALESCE(expiry, DATE '1900-01-01'),
            horizon_seconds
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_candidate_predictions_model
        ON candidate_predictions (model_version, session_date);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS candidate_predictions;")
    op.execute("DROP TABLE IF EXISTS candidate_training_runs;")
    op.execute("DROP TABLE IF EXISTS candidate_model_versions;")
