# Auction and MP shared paper release

Scope: Auction Intelligence remains a simulated, buy-only options lane. MP Intelligence presents its auction insights. VANGUARD remains paper/shadow and retains its model feature definitions.

## Review findings and changes

- Profile caching ignored corrected OHLC/volume, initial-balance settings and most prior-profile fields. The engine now keys on complete inputs and effective settings, isolates returned objects, and shares results through Redis with a computation lease.
- Auction bypassed the public profile cache; API, strategy worker and MP widgets built separate snapshots. Caching now lives at the profile and order-flow engines, and a shared live snapshot serves Auction and `/api/mp/unified/snapshot`.
- Thirty-minute TPO periods started at 09:00 for a 09:15 input session. Periods now follow the session anchor, preserving the first-hour initial balance.
- The automatic scan only covered NIFTY/BANKNIFTY. It now includes NIFTY, BANKNIFTY, SENSEX and active F&O equities from the canonical master-backed catalog. Retired catalog rows remain addressable for held positions. The core ingestion service refreshes membership; chart requests never download a master or market history.
- Stocks use existing canonical three-minute candles, without expanding them into fabricated minute rows. Index profiles use the existing minute archive. All selected input candles must have completed. Missing history is a refusal.
- Scan batches are bounded (24 stocks plus indices and held names) and rotate every 180-second window. Held names are revisited every cycle. The whole stock universe is a coverage set, not a promise of simultaneous execution readiness.
- Upstox order-book subscriptions can use the existing strategy-to-core subscription forwarding and instrument-key resolver. No extra strategy websocket is introduced.
- Paper mode previously erased staleness and could initialize from configured capital after a ledger-read failure. Source age is now checked again before writes; unavailable books fail closed. Paper loss, drawdown and exposure limits are enforced even if other experimental lanes use uncapped validation.
- Expiry mapping could fall back to an ineligible shorter DTE. It now refuses. New fills require fresh timestamped quotes, a valid two-sided spread, and an affordable whole lot. Local ATM fallback no longer invents a spread from LTP.
- The paper book used per-instance locks, non-atomic JSON writes, swallowed corruption and truncated history to 250 trades. It now locks across processes on the shared runtime volume, replaces flushed files atomically, raises on corrupt state and retains all closed records.
- New paper positions freeze the estimated cost model and report net P/L after modeled slippage and fixed fees. Historical rows are preserved and are not retroactively relabeled as clean performance. The new cohort is `shared_auction_costs_v2`.
- Price-only index updates previously produced synthetic unit-volume trades. Those fallbacks are removed; absent traded volume produces no CVD or absorption claims.
- The UI now includes linked price/developing value/POC charts, a selectable auction-period ribbon, a candle-pressure proxy chart, explicit unavailable volume, per-sleeve decisions and searchable universe. Auction and MP use the same snapshot and query identity; expensive research tabs load only when requested.

## Shared computation contract

Identical input/configuration/version shares a result across processes. Candle corrections change the key. Research's relative-grid TPO and M5's fixed-bin profile retain separate formula versions, avoiding an unvalidated change to trained models. They use the same shared cache; VANGUARD's instrument master also shares the core fetch. Existing canonical candle, chain and tick owners remain responsible for ingestion.

Redis outages are surfaced in cache telemetry and permit local memoized analysis; cross-process deduplication cannot be guaranteed while Redis is unavailable. Cache identities include source data rather than only the newest timestamp.

Instrument identity is taken from `instrument_key`; cash mappings require NSE_EQ + EQ rather than a same-symbol debt alias. [Upstox instrument documentation](https://upstox.com/developer/api-documentation/instruments/) defines these fields and filters.

## Operational boundaries

This is a paper module, not a profitability certification. Candle pressure and quote-attributed flow do not identify aggressor trades. New cost estimates include configured slippage and fixed per-order fees; statutory taxes/charges remain excluded and must not be mistaken for broker-confirmed costs. Lifetime ledger figures contain previously identified contaminated trades and are retained for audit.

Release is performed outside market hours. No post-release market-session fills can be demonstrated until the next exchange session. Names without fresh history or exact-contract quotes stay blocked; membership alone never makes an entry ready.

## Deployment

Start from the completed VANGUARD fix revision `8c1b3454`. Apply `038_fno_membership.sql` (additive), then use the main project's Compose file with this directory's `docker-compose.release.yml`. It pins core, strategy worker, frontend and VANGUARD to this checkout and preserves the main project's runtime and state mounts. Restore the prior VANGUARD release override for rollback; do not delete ledger rows or the additive catalog fields.

Validation and runtime evidence are recorded alongside the delivered verification report.

## Verified release evidence — 6 September 2026

- Full backend suite: **1,735 passed, 8 skipped** (existing retired-behavior skips). VANGUARD: **501 passed**. Frontend production build passed.
- Core and strategy services are healthy; critical source hashes match this checkout. Runtime trading mode is `paper`, `paper_only=true`, `live_manager_active=false`.
- Public master refresh produced **210 active F&O stocks + NIFTY, BANKNIFTY and SENSEX**. It added ATHERENERG, MAHABANK and SAGILITY while retaining retired catalog records for audit/held positions. The core membership daemon is running.
- Auction and MP snapshot IDs match for all three indices, RELIANCE and MOTHERSON. Indices correctly have zero trade prints and unavailable volume; stock charts use the existing three-minute archive. ATHERENERG returns an explicit missing-history refusal.
- A cross-process cache probe computed once in core and returned a Redis hit with zero computations in the strategy process. A second probe replaced the strategy live builder with a failing function and still retrieved the core's RELIANCE snapshot.
- All **10** backed-up Auction ledger/history files retain identical SHA-256 hashes after deployment. No holdings or historical records were reset.
- Rendered Auction and MP pages show 213 symbols. Verified interactive period selection, developing value/POC overlays, equity pressure chart and the no-volume index state.
- Both daily broker sessions are expired. Historical snapshots are labeled replay and cannot create entries. Fresh quotes and exchange-session paper execution still require broker reconnection and the next market session. VANGUARD historical promotion gates remain unchanged.

Machine-readable verification, source parity and cache proofs are saved in the task's `auction-release` artifact directory. The release is pinned by the Compose override; main is not merged or overwritten.
