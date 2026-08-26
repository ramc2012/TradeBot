"""Apply the `bhavcopy_delivery` table directly, bypassing db/migrations/.

Not added to db/migrations/001_schema.sql or a new numbered file there --
a later integration step composes the migration lineage from every module's
DDL (M2/M3/M5 building in parallel right now), so this module applies its
own table the same way db/apply.py applies the numbered lineage: additive
CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS, safe to re-run.

    python vanguard/db/apply_bhavcopy_delivery.py
    python vanguard/db/apply_bhavcopy_delivery.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg2

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"

DDL = """
CREATE TABLE IF NOT EXISTS bhavcopy_delivery (
    dt              DATE NOT NULL,
    symbol          TEXT NOT NULL,
    open            NUMERIC,
    high            NUMERIC,
    low             NUMERIC,
    close           NUMERIC,
    prev_close      NUMERIC,
    volume          BIGINT,
    value           NUMERIC,   -- turnover in rupees (TURNOVER_LACS * 100000)
    deliverable_qty BIGINT,
    delivery_pct    NUMERIC,
    source          TEXT NOT NULL DEFAULT 'nse_sec_bhavdata_full_csv',
    synced_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (dt, symbol)
);
CREATE INDEX IF NOT EXISTS idx_bhavcopy_delivery_dt ON bhavcopy_delivery (dt DESC);
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
        print("applied bhavcopy_delivery")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
