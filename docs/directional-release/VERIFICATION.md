# Directional paper release verification — 9 September 2026

Release branch: `codex/directional-paper-desk`, based on `cb98043d`; main implementation commit `352da4ec`, followed by final runtime and UI corrections on the same branch.

## Checks

- Full backend suite: **1,846 passed, 8 skipped** (17 warnings); includes cross-process funding, stale and untimestamped exits, observed zero premiums, fee dates, tick rounding, density invariants, Decimal database cache inputs and read-only book timestamps.
- VANGUARD regression suite: **504 passed**.
- Frontend production build and Docker image build passed. Image: `tradebot-frontend-v2:directional-paper`.
- Core and strategy containers mount the directional-paper worktree; sampled code hashes match. Canonical credentials, runtime and state mounts remain under the main project. `PAPER_TRADING_ONLY=true`; summary reports `execution_mode=paper`, `allow_live_orders=false`. Index substrate/index paper remain enabled.
- Actual PostgreSQL BANKNIFTY chain: 190 rows at 8 September 09:15 UTC, expiry 29 September. Core probe computed once; strategy probe succeeded with its compute function replaced by an exception. Shared hit count 1, computes 0, Redis errors 0; matching SHA256 `f81da05ca08218a4f5f11eb7bdbf2f7f4a075c5ae01bf5a1fae814b3b4d40338`.
- Browser: responsive IV/RV, RR/BF, smile and density charts render; constant-30-day and ATM-put controls change the displayed series/grid. Four stock holdings visible in the all-symbol book; NIFTY-only scope shows zero opens while retaining explicitly labelled whole-book capital. Stock selector includes the configured 50 stock symbols alongside the three indices; catalog/readiness checks still govern admission.
- Distribution API: BANKNIFTY/SENSEX curves available; NIFTY latest batch quarantined rather than silently substituted. ITC stock surface explicitly unsupported. Observed surface data remain historical, dated 8 September. Participant OI publication advanced to 9 September. No broker request or surface fit is triggered by the distribution endpoint.

## Ledger preservation and session evidence

Before release: 499 position IDs, comprising 14 open and 485 closed. SQL data backup and per-status hashes were captured outside the repository. After the intervening 9 September session: the same 499 IDs, four open and 495 closed. All 485 previously closed payloads compare exactly; all original entry times, premiums, quantities and symbols are unchanged. Ten original holdings closed at stops/targets between 03:46 and 05:17 UTC, with recorded quote observations within 40 seconds of closure. These were existing legacy-fill positions, not examples of the new fill cohort. No reset or restoration was performed.

The final release is not a validation of new-entry execution during market hours. There were no new entries on 9 September in the checked summary. Active feed/transport status is not proof of strategy readiness; the global kill switch remains armed. No live-order setting was enabled.

Evidence directory: `/Users/ramachandran/.codex/visualizations/2026/09/05/01a06f50-c52a-7e70-99b8-78c610b7ba18/directional-release`. It contains the SQL backup and `ledger-verification.json`; do not commit durable ledger exports.

## Deployment

Layer `docker-compose.directional.yml` after the existing root, Auction release and freshness Compose files with project `tradebot`. Recreate only backend, backend-strategies and frontend-v2; VANGUARD and research-sync retain their existing mounts. For a new checkout, nested bind targets `backend/nse_strategy_state.json` and `backend/commodity_strategy.json` must exist as regular files before Docker mounts the canonical files. Create empty placeholders only if absent; never overwrite the canonical state files. The initial missing-target failure was corrected and the services recreated successfully.

The simulator limitations and proposal-by-proposal decisions are in [REVIEW.md](REVIEW.md). In particular, impact coefficients are priors, MV delta is unavailable, finite SVI checks are not a global arbitrage guarantee, and anonymous aggregate data do not identify dealer inventory or spread counterparties.
