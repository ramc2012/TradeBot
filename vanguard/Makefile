# Vanguard developer targets.
#
# Vanguard is a read-only research/paper-signal system: it never runs inside
# the live nomadcurie_backend container and never touches its Alembic chain
# (see db/migrations/001_schema.sql and the db/apply_*.py header comments --
# every schema here is its own additive-only lineage, applied by Vanguard's
# own scripts, never by `alembic upgrade head`). It runs as its own process
# against the TradeBot repo-root virtualenv, which already carries every
# dependency used here (httpx, psycopg2, pandas, numpy, scipy).
#
# This worktree has no .venv of its own (git worktrees don't share one), so
# the default points at the primary checkout's. Override with `make PYTHON=...`
# if that checkout ever moves.
PYTHON ?= /Users/ramachandran/CLAUDE PROJECTS/Nomad Curie/TradeBot/.venv/bin/python

.PHONY: test db-init sector-taxonomy ingest features backfill

## test — offline unit tests only. No network, no database (matches the live
## app's own test convention).
test:
	cd .. && "$(PYTHON)" -m pytest vanguard/tests -q

## db-init — apply every Vanguard table schema. Additive-only (CREATE TABLE/
## INDEX IF NOT EXISTS throughout); safe to re-run any time, in any order.
db-init:
	"$(PYTHON)" db/apply.py
	"$(PYTHON)" db/apply_bhavcopy_delivery.py
	"$(PYTHON)" db/apply_bulk_block.py
	"$(PYTHON)" db/apply_announcements_schema.py
	"$(PYTHON)" db/apply_cross_asset.py
	"$(PYTHON)" db/apply_m3_gex.py
	"$(PYTHON)" db/apply_m5_timing.py

## sector-taxonomy — load config/fno_universe_aug2026_series.csv into
## sector_taxonomy. Run after editing the universe/sector CSV; not part of
## the daily `make ingest` cycle.
sector-taxonomy:
	"$(PYTHON)" ingest/load_sector_taxonomy.py

## ingest — run every M1 collector for today. Each is independently
## idempotent and logs to ingest_log regardless of outcome (doctrine #5).
## Order matters only for cross-asset, which reads usdinr_daily after
## m_usdinr_fx.py has written it.
ingest:
	"$(PYTHON)" ingest/m1_participant_oi.py
	"$(PYTHON)" ingest/bhavcopy_delivery.py
	"$(PYTHON)" ingest/bulk_block.py
	"$(PYTHON)" ingest/corporate_announcements.py
	"$(PYTHON)" ingest/m_usdinr_fx.py

## features — every feature module that reads already-ingested data: M2
## options flow, M3 GEX regime, M4 sector RS/lead-lag, M5 timing, and
## cross-asset beta (depends on sector-taxonomy + ingest having run first).
features:
	"$(PYTHON)" features/m2_flow.py
	"$(PYTHON)" features/m3_gex.py
	"$(PYTHON)" features/m4_sector.py
	"$(PYTHON)" features/m5_timing.py
	"$(PYTHON)" features/m_cross_asset_beta.py

## backfill — db-init + sector-taxonomy + ingest + features, in the only
## order that's actually safe end to end. Use for a first-time setup or
## after a TRUNCATE; `ingest`+`features` alone are what a daily cron needs.
backfill: db-init sector-taxonomy ingest features
