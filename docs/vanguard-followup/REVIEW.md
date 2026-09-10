# VANGUARD longitudinal prediction evidence — 9 September 2026

The old tracker stops grading a frozen observation when its declared one/two-session horizon ends. This misses delayed moves and prevents a fair comparison of horizons. Keep those original grades and frozen membership; a separate archive-only observer now follows the same contract and entry through expiry.

## Implemented

- `vanguard_observation_followup`: derived, versioned evidence keyed by lane, source session and rank. Original watchlists, journal outcomes and paper positions are not overwritten.
- One PostgreSQL advisory-locked writer runs after the live-cycle journals and at end of day. It uses existing option and spot candles; no broker calls, extra subscriptions or duplicate UI computation. First backfill covers 126 observations. Subsequent runs revisit original closed observations and late data through expiry plus a seven-day reconciliation window. `--reconcile-all` explicitly revisits older expired evidence.
- Each daily row records the exact contract premium, actual completed-bar observation time, gross held return, underlying return and direction-adjusted underlying return (CE up, PE down). Separate stock and premium returns avoid confusing directional skill with volatility/decay effects.
- Fixed horizons are +1, +2, +3, +5 and +10 exchange sessions after entry day (day 0). Missing dates remain gaps; an absent day does not shift the horizon to the next available quote. At least a 15:15 IST observation is required for a completed horizon; this is not a guaranteed 15:30 settlement.
- First completed closes above +20%, +40%, +60% and +100% are timestamped. Post-entry 30-minute highs/lows measure observed favourable/adverse excursion; entry-bar extremes are excluded. These are opportunities, not assumed fills. Missing bars may hide larger excursions.
- New **Follow-through** tab on the VANGUARD desk: daily option/stock chart, every observation, filters, milestone times, mark freshness and per-model horizon quality. The read-only API uses only the persisted table; UI requests cannot run a backfill or fit a model.
- Quality reports separate frozen model versions, mean and median premium returns, premium win rates, underlying direction hit rates, sample counts, independent source-day counts and qualified counts. Late first entries are excluded from the fixed-horizon quality aggregates but remain visible in each observation's history. Pending horizons are unavailable rather than zero.

## GVT&D evidence

Stored underlying 30-minute last-session closes: 7 September ₹4,367.20; 8 September ₹4,750.00; 9 September ₹4,673.00. These imply +8.77% on 8 September and -1.62% on 9 September. Its 4 September swing observation is 4300 CE, September 29 expiry, original premium ₹188. The latest available 9 September premium is ₹400 (+112.77%). The first archived completed close above +100% occurred on 8 September at 09:45 IST, session +2. A lone late winner is not proof that extending every holding horizon improves predictive quality.

## Frozen improvement protocol

1. Keep current ranks, thresholds, model versions and both original holding-horizon grades immutable. The new observer does not retrain or promote models.
2. Evaluate a predefined family of horizons (+1/+2/+3/+5/+10) on the same eligible observations. Show mean/median, coverage, source-day count, adverse movement and direction separately. This release is descriptive, not an out-of-sample claim; the existing histories contain only a few independent days.
3. A later horizon-selection/model experiment must use full point-in-time candidate captures (including unselected controls), split chronologically with overlapping holding labels purged across boundaries, and cluster uncertainty by source session. Select a horizon and parameters on training/validation only, then freeze them before an untouched forward block.
4. Compare the frozen candidate against the current model and simple direction/holding baselines with actual liquidity estimates and 2–3× cost stress. Rank/direction calibration and conditional volatility forecasts need their own held-out labels. The 126 selected observations alone cannot establish this.
5. Retain all declines, missing marks and negative outcomes. Promotion remains unavailable until independent evidence supports it; all watchlists and follow-through panels remain paper/shadow only.

## Deployment and verification

Apply additive migration `vanguard/db/migrations/022_observation_followup.sql` before starting the new writer. Layer `docker-compose.followup.yml` after the existing root, auction release, freshness and directional release overrides. It points the VANGUARD process at this same pinned worktree while inheriting the canonical runtime/credentials mounts. The core API and frontend already use this worktree. Deploy backend, frontend-v2 and vanguard-cycle; no paper-book reset.

Checks: 511 VANGUARD tests passed; focused timing tests include future/pre-entry exclusion, post-horizon continuation, weekend offsets, missing fixed horizons, no future spot anchor, expiry capping, zero option premiums and absent entries. Frontend production image built successfully. Full backend: **1,848 passed, 8 skipped**, 17 warnings. Deployed and browser-verified on 10 September before the NSE session. The API returned 60 swing and 66 next-session observations; GVT&D chart, milestone labels, filters and per-model horizon table rendered successfully. An unopened session is pending, not missing. Core and VANGUARD container code matched this worktree. Hashes/counts for all four frozen watchlist tables, directional positions and M9 outcomes remained unchanged through deployment. The container writer completed successfully on all 126 observations; its next automatic market-session pass remains to be observed.
