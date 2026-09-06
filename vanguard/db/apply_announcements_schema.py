"""Apply the corporate-announcements/pledge/insider schema against the live
Postgres instance.

Deliberately NOT added to vanguard/db/migrations/ -- a later integration step
composes one migration file from every module's DDL (M2/M3/M5 are being
built in parallel and claim their own table names), so this module applies
its own tables directly, same pattern as vanguard/db/apply.py. All statements
are CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS -- additive-only,
safe to re-run, never touches a live-app-owned table.

    python vanguard/db/apply_announcements_schema.py                # apply
    python vanguard/db/apply_announcements_schema.py --dry-run       # print only
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg2

DEFAULT_DSN = "postgresql://nomadcurie:nomadcurie@localhost:5433/nomadcurie"

# announcements: the primary target (spec's bundled "corporate announcements
# + insider (PIT) + pledge" hourly-poll feed). Schema per the handoff spec
# exactly: id, symbol, dt, subject, description, attachment_url, category,
# source, synced_at, UNIQUE(symbol, dt, subject).
#
#   subject  = NSE's own `desc` field -- this is literally what NSE's own
#              corporate-announcements UI labels as the announcement's
#              "Subject" column (e.g. "Outcome of Board Meeting", "Financial
#              Results"), not a value invented here.
#   category = a coarse bucket derived from `subject` by simple keyword
#              matching (results / board_meeting / corporate_action /
#              dividend / credit_rating / general) -- purely a text
#              classification for convenient querying (e.g. M7's event
#              guard filtering on category='results'), not a numeric
#              feature, so doctrine #1's normalization requirement (which
#              applies to derived numeric price/volume features) does not
#              apply to it. Documented here so a reviewer knows it is
#              derived, not NSE-sourced.
SQL = """
CREATE TABLE IF NOT EXISTS announcements (
    id              BIGSERIAL PRIMARY KEY,
    symbol          TEXT NOT NULL,
    dt              TIMESTAMPTZ NOT NULL,
    subject         TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    attachment_url  TEXT NOT NULL DEFAULT '',
    category        TEXT NOT NULL DEFAULT 'general',
    source          TEXT NOT NULL DEFAULT 'nse_corporate_announcements_api',
    synced_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (symbol, dt, subject)
);
CREATE INDEX IF NOT EXISTS idx_announcements_symbol_dt ON announcements (symbol, dt DESC);
CREATE INDEX IF NOT EXISTS idx_announcements_category ON announcements (category, dt DESC);

-- pledge_disclosures: verified live via https://www.nseindia.com/api/corp-encumbrance
-- (NSE's SAST Regulation 31 promoter-encumbrance disclosure feed). This
-- endpoint is a current point-in-time snapshot of every open pledge
-- disclosure NSE is currently carrying (not a date-range-queryable daily
-- feed -- broadcastDate is per-disclosure and often "-" for legacy rows
-- with no broadcast timestamp on file), so each poll re-fetches the full
-- snapshot and upserts on (symbol, promoter_name).
CREATE TABLE IF NOT EXISTS pledge_disclosures (
    id                  BIGSERIAL PRIMARY KEY,
    symbol              TEXT NOT NULL,
    company_name        TEXT NOT NULL DEFAULT '',
    promoter_name       TEXT NOT NULL,
    encumbered_gt_20pct BOOLEAN,
    encumbered_gt_50pct BOOLEAN,
    broadcast_dt        TIMESTAMPTZ,
    attachment_url      TEXT NOT NULL DEFAULT '',
    source              TEXT NOT NULL DEFAULT 'nse_corp_encumbrance_api',
    synced_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (symbol, promoter_name)
);
CREATE INDEX IF NOT EXISTS idx_pledge_disclosures_symbol ON pledge_disclosures (symbol);

-- insider_trades: verified live via https://www.nseindia.com/api/corporates-pit
-- (NSE's PIT Regulation 7(2) insider-trading disclosure feed). Only returns
-- data reliably when queried per-symbol with an explicit date range -- a
-- bare/no-symbol query silently ignores from_date/to_date and returns a
-- stale, date-inconsistent dump (verified live 2026-08-26; see collector
-- module docstring), so the collector loops the Vanguard universe rather
-- than trusting a single bulk call. `nse_disclosure_id` is NSE's own `did`
-- field, a stable per-disclosure id -- used as the natural dedup key.
CREATE TABLE IF NOT EXISTS insider_trades (
    id                   BIGSERIAL PRIMARY KEY,
    symbol               TEXT NOT NULL,
    acquirer_name        TEXT NOT NULL DEFAULT '',
    person_category      TEXT NOT NULL DEFAULT '',
    transaction_type     TEXT NOT NULL DEFAULT '',
    security_type        TEXT NOT NULL DEFAULT '',
    mode                 TEXT NOT NULL DEFAULT '',
    acq_from_dt          DATE,
    acq_to_dt            DATE,
    intimation_dt        DATE,
    securities_acquired  BIGINT,
    value_acquired       NUMERIC,
    shares_before_pct    NUMERIC,
    shares_after_pct     NUMERIC,
    nse_disclosure_id    TEXT NOT NULL,
    source               TEXT NOT NULL DEFAULT 'nse_corporates_pit_api',
    synced_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (nse_disclosure_id)
);
CREATE INDEX IF NOT EXISTS idx_insider_trades_symbol ON insider_trades (symbol, intimation_dt DESC);
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--dsn", default=os.environ.get("VANGUARD_DATABASE_URL", DEFAULT_DSN))
    args = parser.parse_args()

    if args.dry_run:
        print(SQL)
        return 0

    connection = psycopg2.connect(args.dsn)
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(SQL)
        print("ok -- announcements, pledge_disclosures, insider_trades applied")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
