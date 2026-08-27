# Vanguard — Phase 1 status (2026-08-26)

Trade-selection research system per `PROJECT VANGUARD — Claude Code Handoff
Specification v1.0`. This file records what Phase 1 actually delivered
against that spec, where reality diverged from its assumptions, and what a
reviewer should check before trusting any of it.

## Decisions this build made (asked and answered before writing code)

1. **Data source**: reads Fyers via MACD mini's already-authenticated daily
   token (`~/CLAUDE PROJECTS/MACD mini/runtime/credentials.json`, read-only),
   not a revived Nomad Curie Fyers session. Nomad Curie's own credentials are
   Upstox-only by a deliberate 12-Aug-2026 decision; the spec's assumed
   "fyers-apiv3 primary" stack does not match this repo.
2. **Module reuse**: `auction_intelligence` / `cbe_scanner` /
   `fractal_market_profile` are full live subsystems (RL versions, position
   discipline, tolerance scaling), not simple classifiers. Vanguard does not
   import or depend on them. Where it needs comparable feature logic (M3
   GEX regime, M5 timing) it will be reimplemented independently in a later
   phase, at the cost of not reusing their production-hardened code.
3. **Branch**: `vanguard/phase-1`, off `origin/main`, in an isolated
   `git worktree` (`.claude/worktrees/vanguard-phase-1`) so it never touches
   the dirty, in-flight `ui/consolidation-0-3` checkout.
4. **Missing "Phase 0" files**: `fno_universe_aug2026_series.csv`,
   `fyers_intraday_pipeline.py`, `build_sector_indices.py` and
   `sector_proxy_daily_closes.csv` do not exist anywhere on this machine.
   Built fresh below — except the bars pipeline, which turned out to be
   unnecessary (see next section).

## The spec assumed less existing infrastructure than this repo actually has

Before writing any collector, the live `nomadcurie` Postgres was inventoried
(72 tables). Five things the spec calls "new" already exist, live, and
current as of today:

| Spec asset | Already exists as | Coverage found |
|---|---|---|
| `bars_30m` / `bars_1d` | `underlying_spot_candles` | 225 underlyings, 30m/15m/5m/3m/1m, 2021-present |
| `option_chain_snap` | `option_chain_snapshots` | 22.5M rows; narrowed to 4 index underlyings recently |
| PCR/OI ingredient (M2.5) | `fo_option_chain_metrics` | oi_pcr, volume_pcr, ce/pe oi+volume, 30m, 476 chunks |
| Universe + lot sizes | `fo_underlying_catalog` | 211 stocks + 7 indices, instrument keys, lot sizes |
| Ban list | `fo_security_ban` | Sourced from NSE's `fo_secban.csv` archive already |

**Vanguard reads all five directly rather than duplicating them.** This is
why there is no `fyers_intraday_pipeline.py` in this build: the 3-month OHLCV
fetch it would perform is already running, has been for years, and covers
5 years rather than 3 months. Re-fetching it would be pure waste and a second
source of truth to keep in sync.

Genuinely absent from the schema, and built fresh:
`participant_oi`, `sector_taxonomy` (sector/sector_group/sector20 — no
existing table carries this), `ingest_log`, `results_calendar`, `sector_rs`,
`leadlag`. See `db/migrations/001_schema.sql` for the full reasoning.

## Status (2026-08-27)

**M1 through M10 are all built and running.** An earlier revision of this file
declared M2/M3/M5 and M6–M10 "not started"; that was true when it was written
and false by the time anyone read it. Corrected here rather than left to
mislead the next reader into re-implementing modules that already exist.

| Module | State | Output |
|---|---|---|
| M1 participant OI + bhavcopy/delivery, bulk-block, announcements, USDINR | built | `participant_oi`, `bhavcopy_delivery`, `bulk_block_deals`, `corporate_announcements`, `usdinr_daily` |
| M2 options informed flow | built | `features_flow` — 9,240 rows, 44 sessions (2026-05-25 → 07-28), 210 names |
| M3 GEX regime | built | `regime` — 13,231 rows |
| M4 sector RS + lead-lag | built | `sector_rs` (960), `leadlag` (207) |
| M5 microstructure timing | built | `timing` — 128,459 rows |
| M6 fusion & selection | built | `tickets` (emitted AND gated near-misses) |
| M7 risk & sizing | built | consumed by M6; no table of its own |
| M8 backtest harness | built | `vanguard_backtest_runs` |
| M9 paper execution | built | `decisions`, `fills`, `outcomes`, `paper_capital_daily` |
| M10 journal & attribution | built | `attribution_runs` |
| UI | built | `/strategies/vanguard` + `/api/vanguard/*` (read-only), on branch `vanguard/ui` |

239 offline tests pass (`make test`).

## The lane emits nothing, and the reason is DATA, not tuning

This is the single most important fact about Vanguard today, and the desk's
Pipeline tab exists to keep it visible.

M6 needs four inputs to coincide: flow + sector RS (prior session) and regime
+ timing (same bar). They do not coincide any more:

- `features_flow` ends **2026-07-28**. Stock-level option-chain collection was
  retired around 2026-08-12, so there is no newer input to compute it from.
- `regime` collapses to 0–5 symbols per session from 2026-07-29 onward.
- `timing` alone stays healthy (~2,800 rows/session, 213 names).

Result: only **9 of the last 30 sessions** carry all three per-symbol inputs,
and none of them is recent. Across the entire healthy window (2026-05-25 →
07-28, 1,022 bars) exactly **4 candidates** ever cleared the filter, none
reaching `CONVICTION_MIN = 85` (max 79.6). Every one is journaled in `tickets`
with its gating reason.

Lowering the threshold would manufacture trades from a four-observation
sample. The honest next step is restoring a flow feed, not retuning a filter
against data that no longer arrives.

## Known-and-unfixed (from the 2026-08-27 adversarial review)

Fixed in this branch: the exit rule's stop-ordering / gap-through / tie-break
defects, M7's missing `as_of` bounds (which let M8 size historical bars with
present-day state), the M8-vs-M9 R-multiple disagreement, the unenforced
weekly loss stop, `resolve_instrument` selecting expiring/far-month series,
and `tickets.instrument NOT NULL` making the entire gated audit trail
unwritable (migration 005).

Still open, in rough priority order:

- **M2 does not store `n_ingredients`.** ~42% of the candidate pool is a
  saturated ±100 flow score derived from a SINGLE ingredient, and M6 cannot
  tell those from corroborated ones. Doctrine #1 (no raw/unqualified features
  in stored outputs) argues for persisting the count.
- **`timing` contains off-hours bars on a second 15-minute-offset grid**, so
  an exact-timestamp evaluation swings between a ~208-symbol and a ~55-symbol
  universe. `load_bars` needs a market-hours filter.
- **`make db-init` never creates `features_flow`** — its DDL lives inside
  `features/m2_flow.py` and is applied as a side effect of running it.
- **`daily-cycle` re-runs full-history recomputation every 30 minutes**
  (m2_flow defaults to a 130-day lookback). Fine nightly, wasteful per bar.
- **M9 fill/decision/outcome writes are not atomic** under autocommit, and
  `fill_pending_tickets` re-checks no concurrency/heat cap.
- **`db/apply.py` has no migration version tracking** and re-applies every
  file each run.
- **001_schema.sql's isolation claim is wrong.** `backtest_runs` collided with
  the live app's OWN Alembic chain, not a neighbouring project's — see
  004's header. Check `information_schema.tables` before adding any table.

## The P1 acceptance gate that cannot be satisfied in one sitting

Section 6 of the spec requires "5 consecutive sessions with zero missed EOD
feeds" before P1 is exited. That is 5 calendar trading days of `make ingest`
running unattended — it cannot be produced synchronously. `ingest_log` is the
evidence trail. Wiring an unattended daily run is still the next concrete step
before this gate can start being satisfied for real.

## Reproduce

```
cd vanguard
make db-init          # idempotent
make sector-taxonomy  # load config/fno_universe_aug2026_series.csv
make ingest           # today's participant OI
make features          # sector20 / sector_rs / leadlag, from existing bars
make test              # offline unit tests
```
