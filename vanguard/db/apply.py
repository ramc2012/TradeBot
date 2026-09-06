"""Apply Vanguard's own migration lineage against the live Postgres instance.

Deliberately NOT alembic, and deliberately not wired into the live app's
`alembic upgrade head`. docker-compose.yml's own comment on the core backend
says migrations are single-owner "to avoid a two-process migration race" --
Vanguard honours that by never touching that chain at all. Every statement
here is additive (CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS),
so re-running is always safe and no live-app table is ever altered.

    python vanguard/db/apply.py                 # apply all vanguard/db/migrations/*.sql in order
    python vanguard/db/apply.py --dry-run        # print what would run, touch nothing
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg2

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print("no migrations found", file=sys.stderr)
        return 1

    for path in files:
        sql = path.read_text()
        if args.dry_run:
            print(f"--- would apply {path.name} ({len(sql)} bytes) ---")
            continue
        print(f"applying {path.name} ...")
        connection = psycopg2.connect(args.dsn)
        try:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(sql)
            print(f"  ok")
        finally:
            connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
