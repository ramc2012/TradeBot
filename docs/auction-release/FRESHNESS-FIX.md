# Freshness repair — 8 September 2026

VANGUARD updated existing marks while new rankings stopped: standalone M5 and futures-OI scripts could not import the shared cache, and M4 tried to write into the protected source mount. Auction indices traded, but stock three-minute candles arrived only after the close.

## Changes

- Filename entry points explicitly add the VANGUARD package root. Isolated Python processes test these imports without pytest's augmented import path.
- M4 writes its reproducibility CSV atomically under `VANGUARD_RUNTIME_DIR`. A dedicated runtime mount persists it while the source remains read-only.
- The existing core stock sweeper runs every 180 seconds and fetches three-minute bars once per symbol. Complete contiguous inputs also produce thirty-minute bars for VANGUARD, including the final fifteen-minute session period. Gaps refuse aggregation; unfinished candles are never inserted.
- A 170-second work budget bounds each sweep. Partial runs report `partial` and resume after the last attempted symbol, avoiding starvation. Existing pacing and bulk admission remain in place.
- A separate Compose override preserves unrelated index-swing configuration. No model promotion, trading gate, decision timestamp or paper book is relaxed.

## Deployment

From the main TradeBot directory, layer `docker-compose.yml`, this worktree's `docs/auction-release/docker-compose.release.yml`, and `docs/auction-release/docker-compose.freshness.yml`. Run the split profile for backend, backend-strategies and vanguard-cycle with `up -d --no-deps --no-build`.

## Historical repair

Old sweep rows could be inserted before their thirty-minute bar closed and never corrected. For 7 September, 1,534 such rows had complete three-minute replacements. Exact prior rows were backed up before correction. Updates required the recorded source and write timestamp still to match; other providers and already-complete rows were preserved. Repaired rows carry `upstox_sweep_repaired` provenance.

M4 and M5 are rerun on corrected data. The missed watchlist is not backdated, and held exact-contract marks are not rewritten. Detailed verification and backups are in the task's auction-release artifact directory.


## Verified during the 8 September session

- Full backend suite: 1,825 passed, 8 existing skips. VANGUARD suite: 504 passed, including isolated standalone-script regressions.
- Automatic swing emission: 20 items at 14:53:32 IST, source session 8 September. Daily model freeze: 6 items at 15:49:10 IST. Rankers remain shadow; actionable count is zero by design.
- Latest M5 timing: 8 September 15:15 IST. M4 writes its runtime CSV successfully; futures-OI standalone execution completes without import errors.
- Stock sweep: 214 retained catalog names, zero failures, 26,536 three-minute input rows and 2,568 derived thirty-minute rows in the last intraday pass; 100.4 seconds. Observed cadence 180.4 seconds, median duration 98.4 seconds.
- Auction's final scan uses 8 September data for stocks, with zero replay blocks and zero processing failures. Gates are 15 risk-blocked and 12 flat decisions across 27 names.
- The original three held entries remain present with unchanged entry timestamps and premiums. Normal session activity advanced the journals; no book/history was reset. Runtime remains paper-only with no live manager.
