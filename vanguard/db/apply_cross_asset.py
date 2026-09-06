"""Apply the `usdinr_daily` and `cross_asset_beta` table schemas (M1
cross-asset / M4-adjacent sector-beta feature).

Missing from the repo in the original build -- reconstructed from the
tables' actual live schema, same gap and same fix as db/apply_m3_gex.py.

    python vanguard/db/apply_cross_asset.py
"""
from __future__ import annotations

import os
import sys

import psycopg2

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"

DDL = """
CREATE TABLE IF NOT EXISTS usdinr_daily (
    dt        DATE NOT NULL PRIMARY KEY,
    open      DOUBLE PRECISION NOT NULL,
    high      DOUBLE PRECISION NOT NULL,
    low       DOUBLE PRECISION NOT NULL,
    close     DOUBLE PRECISION NOT NULL,
    volume    BIGINT NOT NULL,
    source    TEXT NOT NULL,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cross_asset_beta (
    dt            DATE NOT NULL,
    sector_group  TEXT NOT NULL,
    driver        TEXT NOT NULL,
    beta          DOUBLE PRECISION,
    corr          DOUBLE PRECISION,
    lookback_days INTEGER NOT NULL,
    computed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (dt, sector_group, driver)
);
CREATE INDEX IF NOT EXISTS idx_cross_asset_beta_driver ON cross_asset_beta (driver, dt DESC);
"""


def main() -> int:
    dsn = os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN)
    connection = psycopg2.connect(dsn)
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(DDL)
        print("usdinr_daily + cross_asset_beta schemas applied (additive-only, safe to re-run)")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
