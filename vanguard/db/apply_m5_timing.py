"""Apply the `timing` table (M5) directly against the live Postgres instance.

NOT part of vanguard/db/migrations/ -- per the M5 task instructions this
table's DDL is applied standalone here and folded into
vanguard/db/migrations/002_features.sql by a later integration step that
composes one migration file from every M2-M5 module's DDL together. Same
CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS additive-only
pattern as db/apply.py and db/migrations/001_schema.sql, so re-running is
always safe and no live-app table is ever touched.

    python vanguard/db/apply_m5_timing.py
    python vanguard/db/apply_m5_timing.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg2

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"

DDL = """
CREATE TABLE IF NOT EXISTS timing (
    ts            TIMESTAMPTZ NOT NULL,
    symbol        TEXT NOT NULL,
    timing_state  TEXT NOT NULL,   -- IGNITION | COMPRESSION | BALANCED | EXHAUST
    timing_score  DOUBLE PRECISION,
    rvol          DOUBLE PRECISION,
    va_position   DOUBLE PRECISION,
    PRIMARY KEY (symbol, ts)
);
SELECT create_hypertable('timing', 'ts', if_not_exists => TRUE);
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    if args.dry_run:
        print(DDL)
        return 0

    connection = psycopg2.connect(args.dsn)
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(DDL)
        print("applied: timing table + hypertable")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
