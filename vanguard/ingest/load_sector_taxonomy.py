"""Load vanguard/config/fno_universe_aug2026_series.csv into `sector_taxonomy`.

This is a one-time/occasional load (the universe changes when NSE adds or
removes F&O eligibility, not intraday), so it is separate from the recurring
`make ingest` collectors.

    python vanguard/ingest/load_sector_taxonomy.py
"""
from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
CSV_PATH = Path(__file__).parents[1] / "config" / "fno_universe_aug2026_series.csv"


def main() -> int:
    dsn = os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN)
    with open(CSV_PATH, newline="") as f:
        rows = list(csv.DictReader(f))
    payload = [
        (r["symbol"], r["exchange"], r["instrument_type"], r["sector"],
         r["sector_group"], r["sector20"] or None, r["notes"])
        for r in rows
    ]
    connection = psycopg2.connect(dsn)
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            psycopg2.extras.execute_values(
                cursor,
                """INSERT INTO sector_taxonomy
                   (symbol, exchange, instrument_type, sector, sector_group, sector20, notes)
                   VALUES %s
                   ON CONFLICT (symbol) DO UPDATE SET
                     exchange = EXCLUDED.exchange, instrument_type = EXCLUDED.instrument_type,
                     sector = EXCLUDED.sector, sector_group = EXCLUDED.sector_group,
                     notes = EXCLUDED.notes, updated_at = now()
                     -- sector20 is deliberately NOT overwritten here: it is
                     -- computed by build_sector_indices.py from realised
                     -- correlation, which this loader has no opinion on.
                """,
                payload,
            )
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT count(*), count(*) FILTER (WHERE t.symbol IS NULL)
                FROM sector_taxonomy s LEFT JOIN fo_underlying_catalog t
                  ON t.symbol = s.symbol
            """)
            total, unmatched = cursor.fetchone()
        print(f"loaded {len(payload)} rows into sector_taxonomy")
        print(f"  {total - unmatched}/{total} join fo_underlying_catalog by symbol"
              f" ({unmatched} do not -- expected for BSE-only names like SENSEX/BANKEX)")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
