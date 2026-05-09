#!/bin/sh
set -eu

python scripts/wait_for_db.py

if [ "${RUN_DB_MIGRATIONS:-1}" = "1" ]; then
  if ! alembic upgrade head; then
    if [ "${ALLOW_MIGRATION_FAILURE:-0}" = "1" ]; then
      echo "Alembic upgrade failed; continuing because ALLOW_MIGRATION_FAILURE=1" >&2
    else
      exit 1
    fi
  fi
fi

exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
