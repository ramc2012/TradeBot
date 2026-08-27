-- Vanguard schema, migration 005: a gated-out ticket genuinely has no instrument.
--
-- THE BUG THIS FIXES (confirmed by adversarial review, 2026-08-27):
-- 002 declared `tickets.instrument TEXT NOT NULL`, but M6 only resolves an
-- instrument AFTER the conviction gate, the rank gate, and instrument
-- resolution itself. A candidate stopped by any of those three is appended to
-- the results with no `instrument` key at all, and persist_tickets passes
-- `row.get("instrument")` -> None straight into a NOT NULL column.
--
-- Consequence: `m6_select.py --write` raised NotNullViolation on the FIRST
-- gated row. Since every candidate on real data is currently conviction-gated
-- (max conviction ~80 < CONVICTION_MIN 85), that meant ZERO ticket rows were
-- ever written — the near-miss audit trail doctrine #5 requires was not
-- merely empty, it was structurally unwritable. Worse, under autocommit the
-- non-empty case would commit the emitted rows and then abort on the first
-- gated one, leaving a trail that LOOKS complete but silently omits every
-- near-miss.
--
-- WHY NULLABLE RATHER THAN BACKFILLING A PLACEHOLDER: a candidate rejected on
-- conviction never had a tradable contract chosen for it, and inventing one
-- (or writing 'UNKNOWN') would assert a fact that was never computed. NULL is
-- the honest representation of "this never got far enough to have one", and
-- it is exactly the distinction `emitted` already draws. Doctrine: prefer NULL
-- over a fabricated value.
--
-- Safe to re-run: DROP NOT NULL is idempotent in effect (dropping an
-- already-dropped constraint is a no-op in Postgres).

ALTER TABLE tickets ALTER COLUMN instrument DROP NOT NULL;

COMMENT ON COLUMN tickets.instrument IS
    'The tradable ATM CE/PE symbol. NULL for a ticket gated out before '
    'instrument resolution (conviction gate, rank gate, or no contract '
    'resolvable) — such a candidate genuinely never had one. Non-NULL for '
    'every emitted ticket and for anything gated only by M7 risk.';
