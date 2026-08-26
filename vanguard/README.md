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

## What is done and verified against live data (2026-08-26)

- **Universe**: `config/fno_universe_aug2026_series.csv` — 218 rows (210
  equities + 8 indices), pulled from the live Fyers NSE/BSE derivatives
  master, not an assumed list. 214/218 cross-check against
  `fo_underlying_catalog` by symbol (the 4 that don't are BSE-only names
  SENSEX/BANKEX plus two indices `fo_underlying_catalog` hasn't picked up
  yet). Sector / sector_group is curated NSE domain knowledge, not sourced
  from an official classification file — treat it as a starting point, not
  ground truth (see the file's own header comment for four names flagged as
  inferred-not-confirmed: NIFTYFPI, VMM, TMPV, GVT&D).
- **`db/migrations/001_schema.sql`**: applied. Additive-only; confirmed the
  live app's Alembic head (`031_preopen_atr_last_session`) is untouched.
- **M1 — participant OI** (`ingest/m1_participant_oi.py`): live NSE archive,
  run for 2026-08-20 through 2026-08-26. Trading days return 24 rows
  (4 participants × 6 buckets); weekends correctly 404. Idempotency verified
  both ways — re-running an `ok` day updates in place, re-running a
  `not_a_trading_day` accumulates retry history in `ingest_log`. 7 offline
  unit tests, including one that asserts the parser fails loudly (not
  silently) if NSE reshuffles a column, and one that reconciles the parsed
  sum against the file's own TOTAL row rather than trusting it.
- **M4 — sector RS, sector20, lead-lag** (`features/m4_sector.py`): run
  against `underlying_spot_candles` with a 90-day lookback. 207/210 equities
  had bars (3 missing: ATHERENERG, MAHABANK, SAGILITY — newly listed,
  presumably short history). sector20 is genuinely computed by hierarchical
  clustering of sector_group correlations, not asserted — 25 sector_group
  buckets clustered to 20. `sector_rs`: 960 rows. `leadlag`: 207 rows.
- `make test`, `make db-init`, `make ingest`, `make features` all run clean
  end to end (see Makefile).

## What is explicitly NOT done

- **M2 (options informed-flow scanner)**: not started. `fo_option_chain_metrics`
  already gives PCR; IV spread, skew delta, O/S ratio and ΔOI conjunction are
  new computation, not yet written.
- **M3 (GEX regime)**, **M5 (microstructure timing)**: not started, per the
  "reimplement fresh" decision — these need independent logic, not a wrap.
- **Bhavcopy+delivery%, bulk/block deals, corporate announcements,
  cross-asset (Brent/LME/USDINR/IN10Y)**: not checked against the existing
  schema and not built. Before building fresh collectors for these, repeat
  the inventory step above — given how much of M1's other feeds turned out
  to already exist, it would not be surprising if some of these do too.
- **M6 (fusion) through M10 (journal)**: not started. All depend on M2/M3/M5.
- **The bars/indicator pipeline** (EMA/RSI/MACD/BB/ATR/ADX/VWAP/OBV/RVOL/
  realized-vol/Supertrend) described under "Existing Foundations": not built.
  `underlying_spot_candles` supplies OHLCV; the indicator overlay on top of it
  does not exist yet in Vanguard and will be needed once M2/M3/M5 start.

## The P1 acceptance gate that cannot be satisfied in one sitting

Section 6 of the spec requires "5 consecutive sessions with zero missed EOD
feeds" before P1 is considered exited. That is 5 calendar trading days of
`make ingest` actually running unattended — it cannot be produced
synchronously regardless of how much code is written today. `ingest_log` is
the evidence trail for it; as of this writing it has 5 trading-day rows for
`m1_participant_oi`, all `ok`, from a manual backfill run today rather than 5
separate days of an actually-scheduled unattended job. Wiring an unattended
daily run (cron / research-sync scheduler, matching how `fo_risk_ingest.py`'s
own daily cadence is enforced by its caller) is the next concrete step before
this gate can start being satisfied for real.

## Reproduce

```
cd vanguard
make db-init          # idempotent
make sector-taxonomy  # load config/fno_universe_aug2026_series.csv
make ingest           # today's participant OI
make features          # sector20 / sector_rs / leadlag, from existing bars
make test              # offline unit tests
```
