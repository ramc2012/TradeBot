# Vanguard developer targets.
#
# Vanguard is a read-only research/paper-signal system: it never runs inside
# the live nomadcurie_backend container and never touches its Alembic chain
# (see db/migrations/001_schema.sql). It runs as its own process against the
# TradeBot repo-root virtualenv, which already carries every dependency used
# here (httpx, psycopg2, pandas, numpy, scipy).
#
# This worktree has no .venv of its own (git worktrees don't share one), so
# the default points at the primary checkout's. Override with `make PYTHON=...`
# if that checkout ever moves.
PYTHON ?= /Users/ramachandran/CLAUDE PROJECTS/Nomad Curie/TradeBot/.venv/bin/python

.PHONY: test db-init ingest features sector-taxonomy

## test — offline unit tests only. No network, no database (matches the live
## app's own test convention).
test:
	cd .. && "$(PYTHON)" -m pytest vanguard/tests -q

## db-init — apply vanguard/db/migrations/*.sql. Additive-only; safe to re-run.
db-init:
	"$(PYTHON)" db/apply.py

## sector-taxonomy — load config/fno_universe_aug2026_series.csv into
## sector_taxonomy. Run after editing the universe/sector CSV; not part of
## the daily `make ingest` cycle.
sector-taxonomy:
	"$(PYTHON)" ingest/load_sector_taxonomy.py

## ingest — run every M1 collector for today. Each collector is independently
## idempotent and logs to ingest_log regardless of outcome (doctrine #5).
ingest:
	"$(PYTHON)" ingest/m1_participant_oi.py

## features — M4 sector RS / sector20 / lead-lag, recomputed from
## underlying_spot_candles (existing data; no fetch).
features:
	"$(PYTHON)" features/m4_sector.py
