"""Apply the bulk_block table directly (per handoff instructions: this
module's own table is applied by a small standalone script, NOT added to
vanguard/db/migrations/ -- a later integration step composes the final
migration file from every module's DDL, and table names must not collide
with M2/M3/M5, which are being built in parallel and have already claimed
features_flow, regime, timing).

Same pattern as vanguard/db/apply.py: idempotent, additive-only, own
connection, no dependency on the live app's Alembic chain.

    python vanguard/db/apply_bulk_block.py
    python vanguard/db/apply_bulk_block.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg2

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"

DDL = """
-- bulk_block: NSE daily bulk-deals + block-deals archive, tagged by kind.
--
-- Surrogate BIGSERIAL id + a unique index on the full row tuple, NOT a
-- natural (dt, symbol) primary key like m1_participant_oi uses. That shape
-- fits participant_oi because it is one row per (dt, participant, bucket)
-- by construction -- NSE's file has exactly one number per cell. Bulk/block
-- deals are different: NSE's own CSV can and does carry multiple rows for
-- the same (dt, symbol) when more than one client traded that name that
-- day (e.g. one client bought, a different client sold, or the same client
-- executed at two different weighted-average prices across two clips).
-- Collapsing those into a single-row-per-key upsert the way participant_oi
-- does would silently drop real, distinct disclosed deals. The unique index
-- below is deliberately the full tuple NSE's file actually treats as a
-- record identity, which still gives idempotent re-runs (ON CONFLICT DO
-- UPDATE bumps synced_at) without discarding legitimate multi-client days.
CREATE TABLE IF NOT EXISTS bulk_block (
    id          BIGSERIAL PRIMARY KEY,
    dt          DATE NOT NULL,
    symbol      TEXT NOT NULL,
    client_name TEXT NOT NULL,
    deal_type   TEXT NOT NULL,   -- BUY | SELL
    kind        TEXT NOT NULL,   -- bulk | block
    quantity    BIGINT NOT NULL,
    price       DOUBLE PRECISION NOT NULL,
    source      TEXT NOT NULL DEFAULT 'nse_bulk_block_deals_csv',
    synced_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_bulk_block_row
    ON bulk_block (dt, symbol, client_name, deal_type, kind, quantity, price);
CREATE INDEX IF NOT EXISTS idx_bulk_block_dt ON bulk_block (dt DESC);
CREATE INDEX IF NOT EXISTS idx_bulk_block_symbol ON bulk_block (symbol, dt DESC);
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    if args.dry_run:
        print(f"--- would apply bulk_block DDL ({len(DDL)} bytes) ---")
        print(DDL)
        return 0

    connection = psycopg2.connect(args.dsn)
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(DDL)
        print("bulk_block: ok")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
