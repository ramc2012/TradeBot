-- Vanguard schema, migration 004: M8 backtest results.
--
-- Named vanguard_backtest_runs, not backtest_runs -- this shared Postgres
-- instance already has an UNRELATED `backtest_runs` table (columns
-- underlying/market/option_type/macd_fast/macd_slow/sl_pct/profit_factor,
-- nothing to do with Vanguard). CREATE TABLE IF NOT EXISTS backtest_runs
-- would have silently no-op'd against that table instead of creating
-- Vanguard's own -- caught live when the following CREATE INDEX failed with
-- "column run_at does not exist" against the other schema.
--
-- CORRECTION (2026-08-27, adversarial review): this header originally
-- attributed that table to the sibling MACD mini project. That was wrong,
-- and the error matters. `backtest_runs` is created by THIS repo's own live
-- backend, in its Alembic chain
-- (backend/db/migrations/versions/002_options_macd_tables.py). So the
-- collision was not with an unrelated neighbouring project that merely
-- shares a database -- it was with the live application's own migration
-- lineage, which 001_schema.sql's header claims Vanguard "can never
-- collide" with. It plainly can, and did. Treat 001's isolation claim as
-- describing an intent, not a guarantee: any new Vanguard table must still
-- be checked against information_schema.tables before it is created.
--
-- Every other Vanguard table name was cross-checked afterward and confirmed
-- collision-free. Prefixing this one (the one generic enough to collide
-- again) is cheap insurance against a repeat.
--
-- Deliberately separate from attribution_runs (002/M10's table) --
-- attribution_runs rolls up REAL paper-trading history (M9's own closed
-- outcomes); vanguard_backtest_runs holds a historical REPLAY's results.
-- Keeping them apart means a backtest can never be mistaken for a
-- live-performance claim, and vice versa.

CREATE TABLE IF NOT EXISTS vanguard_backtest_runs (
    id          BIGSERIAL PRIMARY KEY,
    run_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    start_ts    TIMESTAMPTZ NOT NULL,
    end_ts      TIMESTAMPTZ NOT NULL,
    report      JSONB NOT NULL   -- compute_metrics()'s full dict: hit rate, expectancy
                                  -- (gross+net), max DD, conviction-decile breakdown
);
CREATE INDEX IF NOT EXISTS idx_vanguard_backtest_runs_run_at ON vanguard_backtest_runs (run_at DESC);
