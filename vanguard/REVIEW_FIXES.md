# VANGUARD paper-lane review fixes

This release keeps VANGUARD paper-only. The fusion paper engine and three
journals remain separate. The two unproven listwise models remain shadow;
no model was promoted and no broker-order integration was added.

## Corrected behavior

- The API, frontend and cycle service use this release checkout, independent
  of the root checkout's selected branch. Original runtime books and credential
  files are mounted separately. The original Claude worktree is preserved.
- Futures history imports deduplicate provider days, roll back failed writes,
  retry temporary HTTP failures, and respect both short and 30-minute request
  limits. Partial imports are repaired with full-window upserts, not skipped
  merely because a contract has some rows. Rate-limit reference:
  https://upstox.com/developer/api-documentation/rate-limiting/
- Baselines count actual prior observations, up to 60. Missing z-scores and
  percentiles render as a dash; the desk reports readiness and warm-up counts.
- M1 archives run after publication; disclosures/results refresh hourly during
  the day. The results query looks 45 days forward. M7 refuses stale/unknown
  calendars and uses the configured NSE holiday calendar for the prior session.
- Contract selection accepts only the same-day chain within 30 minutes of the
  source feature bar. A new prospective swing list must be generated after
  that bar completes and before its planned entry close. Replays are explicit
  observations and cannot become actionable.
- Decision time records actual generation time; prediction_ts retains the
  source candle stamp. The exact 14:45 candle close is the planned 15:15 entry.
  Missing entries are never replaced by a later candle. Stored entries remain
  fixed, incomplete bars are excluded, and pre-entry highs/lows do not affect
  excursions. Each item settles on its own D+1 or D+2 NSE session.
- Swing journal returns deduct the declared 1% premium-cost assumption. Gross
  returns remain explicitly labelled on the ranking view. Closed items never
  inherit later live quotes.
- Rank scores cannot substitute for expected returns. Optional validation-only
  payoff calibration records a session-based uncertainty bound; missing,
  sparse, nonpositive or out-of-range evidence refuses actionable status.
  Existing models have no such calibration and remain shadow observations.

Migration `db/migrations/021_review_accounting.sql` is additive, apart from
correcting the previously mislabelled decision timestamp. It preserves all
membership, contracts and entry marks. A pre-migration ledger dump is retained
in the task's review-artifact folder.

## Deployment

From the original TradeBot root, after the full backend tests pass:

```sh
docker compose -f docker-compose.yml \
  -f .claude/worktrees/vanguard-fixes/vanguard/docker-compose.release.yml \
  up -d --no-deps --no-build backend frontend-v2 vanguard-cycle
```

Use the explicit release file for recreation; `restart` uses the already pinned
mounts. The frontend image is `tradebot-frontend-v2:vanguard-review-fixes`.
The release file contains this machine's absolute checkout and persistent-state
paths. Rebuild that image after frontend edits. No other service is redeployed.

## Validation and limits

The full backend suite passed: 1,724 passed, eight legacy skips. Repairs to its
existing gate failures were limited to missing stock-spot runner registration,
fixtures using expired contracts/ambient feature flags, and test isolation.
Tests now point away from the deployed database and Redis. The initial host
run exposed an expiry-catalog write through an incompletely mocked unit test;
the affected five index catalogues were refreshed from the public instrument
master, and subsequent runs were isolated. Paper ledgers were not reset.

VANGUARD regression tests cover selected horizons, immutable entry prices,
late decisions, incomplete candles, stale chains, missing calendars, holidays,
payoff calibration and rate-limit recovery. The production frontend build and
rendered OI desk were verified. UI/API report paper-only operation and retain
all 20 September 4 research items and three journals.

Historical contract availability is provider-dependent; a request beginning
June 2024 is not proof that the provider returned June 2024. See the final
review artifact for measured coverage and unresolved external feed blockers.
USDINR's configured Fyers credential was rejected with HTTP 401; no auth
settings were changed. Its collector now has an explicit read-only credential
mount, logs the failure, and can resume after the source session is renewed.
