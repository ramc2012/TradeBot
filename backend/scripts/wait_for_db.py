from __future__ import annotations

import os
import sys
import time

import psycopg2


def _sync_db_url() -> str:
    raw = str(os.getenv("DATABASE_URL", "")).strip()
    if not raw:
        raise RuntimeError("DATABASE_URL is not set")
    return (
        raw.replace("postgresql+asyncpg://", "postgresql://")
        .replace("postgresql+psycopg2://", "postgresql://")
    )


def main() -> int:
    timeout_seconds = float(os.getenv("DB_WAIT_TIMEOUT_SECONDS", "90"))
    poll_seconds = float(os.getenv("DB_WAIT_POLL_SECONDS", "2"))
    deadline = time.monotonic() + max(timeout_seconds, 1.0)
    db_url = _sync_db_url()
    last_error = ""

    while time.monotonic() < deadline:
        try:
            conn = psycopg2.connect(db_url)
            conn.close()
            print("Database connection is ready.", flush=True)
            return 0
        except Exception as exc:  # pragma: no cover - exercised via container startup
            last_error = str(exc)
            print(f"Waiting for database: {last_error}", flush=True)
            time.sleep(max(poll_seconds, 0.25))

    print(f"Database did not become ready within {timeout_seconds:.0f}s: {last_error}", file=sys.stderr, flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
