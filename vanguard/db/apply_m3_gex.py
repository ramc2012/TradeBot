"""Apply the `regime` table schema (M3 GEX regime engine).

Missing from the repo in the original build -- the build agent applied it
via a one-off scratchpad script instead of a committed one, breaking the
"own db/apply_*.py script per feature" convention every other module here
follows (apply.py, apply_m5_timing.py, apply_bulk_block.py, ...). This is
the reconstruction, matching the table's actual live schema plus the
gex_percentile column added during bug-fix review (doctrine #1: net_gex and
gamma_flip_level are raw/unnormalized -- gex_percentile is the properly
normalized reading downstream modules should actually consume as a feature;
net_gex/gamma_flip_level remain for the informational/diagnostic purpose the
spec's own Section 4 schema names them for).

    python vanguard/db/apply_m3_gex.py
"""
from __future__ import annotations

import os
import sys

import psycopg2

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"

DDL = """
CREATE TABLE IF NOT EXISTS regime (
    ts               TIMESTAMPTZ NOT NULL,
    symbol           TEXT NOT NULL,
    net_gex          DOUBLE PRECISION NOT NULL,
    regime           TEXT,
    gamma_flip_level DOUBLE PRECISION,
    gex_percentile   DOUBLE PRECISION,
    PRIMARY KEY (symbol, ts)
);
SELECT create_hypertable('regime', 'ts', if_not_exists => TRUE, migrate_data => TRUE);
CREATE INDEX IF NOT EXISTS idx_regime_symbol_ts ON regime (symbol, ts DESC);
ALTER TABLE regime ADD COLUMN IF NOT EXISTS gex_percentile DOUBLE PRECISION;
"""


def main() -> int:
    dsn = os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN)
    connection = psycopg2.connect(dsn)
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(DDL)
        print("regime table schema applied (additive-only, safe to re-run)")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
