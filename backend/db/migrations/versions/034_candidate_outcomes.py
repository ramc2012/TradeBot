"""Outcomes for every logged candidate — the label side of the training set.

Two stages, because the data supports them very differently
───────────────────────────────────────────────────────────
STAGE A (spot) is EXACTLY computable. `market_ticks` carries every index
underlying at ~0.25s (measured: 91,374 NIFTY ticks in the 2026-08-25 session,
full 03:45-10:00 UTC coverage), so a forward spot return at an exact horizon,
a true intra-horizon MFE/MAE, and a genuine first-touch barrier are all real
measurements.

STAGE B (option) is NOT. No table in this schema holds a forward bid/ask for an
option; `option_chain_snapshots` carries LTP only, on an irregular ~2-3 minute
cadence (measured p50 139s, p95 484s, max 796s), for one expiry per underlying,
and it goes fully dark on individual trading days while the rest of the stack
looks healthy (2026-08-19: 103,440 index ticks, 48,205 option candles, ZERO
chain snapshots). So every option-side field carries the REALIZED lag and the
sample count that produced it, and a row that cannot be honestly marked is
stored with an `unlabellable_*` status rather than being dropped or filled.

This split is not a workaround — it is the plan's own Stage A / Stage B
architecture, and the data happens to make the reason for it concrete.

WHY EVERY COST FIELD IS SPLIT BY EVIDENCE STATUS
────────────────────────────────────────────────
The entry half-spread is MEASURED (the candidate snapshot captured a real
two-sided quote). The exit half-spread can only ever be ASSUMED, because no
forward option quote exists — unless the forward mark came from a later
candidate snapshot, which does carry one. Those two cases must stay
distinguishable forever, so they are separate columns with separate
`_measured` booleans and are never fused into a single "net" number.

Measured on the 2026-08-26 NIFTY monthly chain, this matters more than it
sounds: a TOP-liquidity ATM contract breaks even on a 0.76% move, while a
MID-liquidity wing needs 2.80%. A flat per-side cost — which is what every
other model in this repo uses — overcharges the liquid contract roughly 4x and
would erase real edge exactly where it is most likely to exist.

Revision ID: 034_candidate_outcomes
Revises: 033_candidate_snapshot_corrections
Create Date: 2026-08-27
"""
from typing import Sequence, Union

from alembic import op


revision: str = "034_candidate_outcomes"
down_revision: Union[str, None] = "033_candidate_snapshot_corrections"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS candidate_outcomes (
            -- the ANCHOR instant: the candidate_snapshots row being labelled
            time                        TIMESTAMPTZ      NOT NULL,
            decision_id                 UUID             NOT NULL,
            session_date                DATE             NOT NULL,

            -- contract identity. The 4-tuple, never a broker instrument_key:
            -- option_premium_candles is UNIQUE on (instrument_key, interval,
            -- time), which is broker-specific, so the same contract-bar exists
            -- twice under two brokers with DIFFERENT prices. The logical key is
            -- the only safe join.
            underlying                  TEXT             NOT NULL,
            expiry                      DATE,
            strike                      DOUBLE PRECISION,
            option_type                 TEXT             NOT NULL,

            -- which forward horizon this row measures
            horizon_seconds             INTEGER          NOT NULL,

            -- ok | no_trade | unlabellable_source_dark | unlabellable_no_forward
            -- | unlabellable_out_of_tolerance | unlabellable_no_spot
            label_status                TEXT             NOT NULL,
            label_reason                TEXT,

            -- ── STAGE A: spot, exactly computable ────────────────────────
            spot_entry                  DOUBLE PRECISION,
            spot_forward                DOUBLE PRECISION,
            spot_return_pct             DOUBLE PRECISION,
            spot_mfe_pct                DOUBLE PRECISION,
            spot_mae_pct                DOUBLE PRECISION,
            -- up | down | none — first touch of a volatility-scaled barrier
            spot_barrier_hit            TEXT,
            spot_time_to_barrier_seconds DOUBLE PRECISION,
            spot_barrier_width_pct      DOUBLE PRECISION,
            -- how many ticks the path statistic was actually built from. A
            -- "path" over two samples is not a path.
            spot_tick_count             INTEGER,

            -- ── STAGE B: option, coarse and lag-bearing ──────────────────
            option_entry_mid            DOUBLE PRECISION,
            option_forward_price        DOUBLE PRECISION,
            -- REALIZED lag, not the nominal horizon. A row labelled "5m" whose
            -- forward mark actually landed at +407s must say so, or pooling it
            -- with true 5-minute returns silently mixes two horizons.
            forward_lag_seconds         DOUBLE PRECISION,
            forward_sample_count        INTEGER,
            forward_source              TEXT,
            option_gross_return_pct     DOUBLE PRECISION,
            option_net_return_pct       DOUBLE PRECISION,
            option_mfe_pct              DOUBLE PRECISION,
            option_mae_pct              DOUBLE PRECISION,

            -- TRADE-ARRIVAL EVIDENCE. LTP is a last-traded print, not a mark:
            -- a zero forward return usually means no trade arrived, not that
            -- the price held. Measured across the NIFTY chain, 49.3% of ~6-min
            -- LTP intervals show EXACTLY zero change. Without this column a
            -- labeller silently reports non-arrival as a flat market.
            trade_arrived               BOOLEAN,
            volume_delta                BIGINT,
            oi_delta                    DOUBLE PRECISION,

            -- ── cost, never fused ────────────────────────────────────────
            entry_half_spread_pct       DOUBLE PRECISION,
            entry_half_spread_measured  BOOLEAN,
            exit_half_spread_pct        DOUBLE PRECISION,
            exit_half_spread_measured   BOOLEAN,
            cost_spread_rupees          DOUBLE PRECISION,
            cost_statutory_rupees       DOUBLE PRECISION,
            cost_total_rupees           DOUBLE PRECISION,
            cost_pct_of_notional        DOUBLE PRECISION,
            -- the move needed just to break even. sniper-phase0 calls this
            -- m_breakeven: "this single number gates every label".
            breakeven_move_pct          DOUBLE PRECISION,
            -- is the horizon economically decidable for this contract at all?
            -- FALSE when the typical move over the horizon cannot clear cost.
            economically_decidable      BOOLEAN,

            quantity                    INTEGER,
            lot_size                    INTEGER,

            label_version               TEXT             NOT NULL,
            computed_at                 TIMESTAMPTZ      NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        """
        SELECT create_hypertable(
            'candidate_outcomes', 'time',
            if_not_exists => TRUE,
            chunk_time_interval => INTERVAL '7 days'
        );
        """
    )
    # One outcome per (anchor contract, horizon). Enforced so a re-run of the
    # labeller repairs rows instead of duplicating them — the exact failure mode
    # that put two conflicting prices on the same option bar elsewhere in this
    # schema.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_candidate_outcomes_anchor
        ON candidate_outcomes (
            time, decision_id, underlying, option_type,
            COALESCE(strike, -1), COALESCE(expiry, DATE '1900-01-01'),
            horizon_seconds
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_candidate_outcomes_session_status
        ON candidate_outcomes (session_date, label_status, horizon_seconds);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_candidate_outcomes_decision
        ON candidate_outcomes (decision_id, horizon_seconds);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS candidate_outcomes;")
