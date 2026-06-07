# Broker Data Fetch & Persist — Authoritative Design (Balanced)

_Last updated: 2026-06-07. Supersedes `backend/docs/broker_data_fetch_plan.md`. Requirements
supplied by the user; budget verified against live Fyers/Upstox limits; code claims verified
against the current tree. Status legend: **[BUILT]** = exists today, **[PARTIAL]** = exists but
incomplete/wrong, **[TO-BUILD]** = does not exist._

## 0. Brokers & hard limits (live state)

- **Fyers** — ACTIVE trading session: live WebSocket + full REST. **The LIVE lane.** Single point
  of failure (Upstox trading token expired).
- **Upstox** — analytics token VALID, read-only historical. **The BACKFILL lane** (off-hours only).
- 5Paisa / ICICI Breeze — saved, disconnected, not used for data.

| Lane | Rate | Lookback / cap |
|---|---|---|
| **Fyers REST** (ONE bucket, ALL endpoints) | **10/sec · 200/min · 100,000/day** | `/history` minute = 100 d/req, 1D = 366 d/req, seconds = 30 trading days |
| **Fyers WebSocket** | tick push (0 REST) | **5,000 unique symbols / API key** |
| **Upstox analytics REST** | **50/sec · 2,000/30min · no daily cap** | 30min ≤ 1yr, day ≤ 10yr (historical only) |

Both adapters today have **ZERO throttle/backoff/429 handling** (`brokers/fyers.py`, `brokers/upstox.py`
have only fixed httpx timeouts). This is the **P0 blocker** for everything that scales fetches.

---

## 1. Feed model — the 4 shared feeds (F1–F4)

Design rule: **one fetch → many consumers.** Every strategy maps to one or more of F1–F4; no
strategy fetches its own duplicate of a shared feed.

| Feed | Consumers | Broker / lane | Endpoint | Cadence | Target table · interval | Source tag |
|---|---|---|---|---|---|---|
| **F1 — Option chain** | S1 (NSE premium-MACD), Directional | **Fyers REST** (live) + Upstox (off-hours history backfill) | `/options-chain-v3` (strikecount band), 1 call/underlying → app-built OHLC+OI | **Tiered:** liquid+indices @60s, rest @180s (3m); eager 09:15 one-shot | `option_premium_candles` · **3m** (S1) and **1m** (Directional subset) | `fyers_chain` (live), `upstox` (backfill) |
| **F2 — Index tick + order flow** | Auction IQ, FMP, Sniper | **Fyers WS** (live) | WS `SymbolUpdate` + L2 depth — NIFTY/BANKNIFTY/SENSEX spot (+ATM band for book) | continuous (flush 5s); **0 REST** | `market_ticks` (tick) + `orderflow_snapshots` (1m, **new**) + `underlying_spot_candles` 1m/3m | `live_tick` / `fyers_ws` |
| **F3 — Commodity futures** | Commodity (MCX MP + OF) | **Fyers WS** (live) + **Upstox** (1m backfill, off-hours) | Fyers WS futures+depth; Upstox `/historical-candle` 1m | WS continuous 09:00–23:30; Upstox one-shot off-hours | `underlying_spot_candles` · **1m** + `orderflow_snapshots` (1m) | `fyers_ws` (live), `upstox` (backfill) |
| **F4 — Spot / EOD** | Gann, CBE | **Fyers** (1m live/REST gap-fill) + **Upstox** (EOD + 1m backfill, off-hours) | Fyers WS/REST 1m spot; Upstox `/historical-candle` day+1m | Gann: WS live + Fyers gap-fill (5–10m cooldown); CBE: Upstox off-hours | `underlying_spot_candles` · **1m** (+ daily EOD snapshot) | `fyers_ws`/`live_tick` (live), `upstox` (backfill) |

### Strategy → feed coverage (proves requirement satisfied)

| Strategy | Requirement (verbatim) | Feed(s) | How satisfied |
|---|---|---|---|
| **S1** | 3m OHLC for ALL F&O, **CE+PE**, ATM + full chain + incremental strikes if band exceeded | **F1** | Per-underlying `/options-chain-v3` poll (1 call returns whole near-money band incl. ltp/oi/oich/volume/bid/ask) → app builds 3m CE+PE OHLC+OI; widen `strikecount` (or 2 centered calls) when spot exits band. **[TO-BUILD]** |
| **Directional** | 1m OHLC + greeks/IV/OI | **F1** (shared) | Derive greeks/IV/OI app-side from the **same** chain snapshot; run a 1m **tiered subset** (liquid @60s) — **NOT** all 227 @1m. **[TO-BUILD]** |
| **Commodity** | 1m FUTURES OHLC + granular order flow (MCX) | **F3** | Fyers WS 1m futures + in-memory OF from `market_ticks`, persisted to new `orderflow_snapshots`; Upstox 1m backfill. **[PARTIAL]** |
| **Auction IQ + FMP + Sniper** | TICK + granular ORDER FLOW for NIFTY/BANKNIFTY/SENSEX | **F2** (shared) | One Fyers WS depth subscription → `market_ticks` (tick is **[BUILT]**) + new persisted `orderflow_snapshots` (OF is **[TO-BUILD]**); Sniper `u_of_*` becomes backfillable. |
| **Gann** | 1m spot/futures for its universe | **F4** (shared) | `underlying_spot_candles` 1m (live + Upstox backfill). **[PARTIAL]** — no scheduled full-universe backfill. |
| **CBE** | EOD analytics + same 1m spot for portfolio refresh | **F4** (shared) | Upstox off-hours EOD + 1m for ~300 equities; shares 1m spot table with Gann. **[PARTIAL]** |

---

## 2. Broker request budget

The binding constraint is **200/min and 10/sec burst**, NOT the 100k/day cap. With even
staggering, all live Fyers REST lands at ~28–40k/day (≈30–40% of cap).

| Feed | Broker | Calls/day | Peak/min (if staggered) | % of daily cap | Notes |
|---|---|---|---|---|---|
| **F1 — S1 chain @3m** (227 underlyings) | Fyers | 227 × (375min ÷ 3) = **28,375** | 227 ÷ 180s ≈ **1.26/s ≈ 76/min** | 28.4% | Replaces infeasible per-contract `/history` (~1.375M/day = 14× cap). |
| **F1 — Directional (tiered subset, shared)** | Fyers | ~15 liquid @60s = 5,625; preferred = **0 incremental** (derive from S1 3m snapshot) | derive = 0; tiered ≈ 86/min | **0–32%** | **Do NOT promote all 227 to 1m** (= 85,125/day = 85%, kills retry headroom + risks 200/min). |
| **F1 — eager 09:15 open poll** | Fyers | +227 one-shot | spread over ~3 min at open | <1% | Kills open-bar gap. |
| **F1 — 1m spot/futures gap-fill** | Fyers | a few k/day | low-priority lane, ≤ a few/min | ~3% | 5–10 min cooldown per symbol. |
| **F2 — index tick + OF** | Fyers WS | **0 REST** | 0 | 0% | Few hundred symbols ≪ 5,000 WS cap. |
| **F3 — commodity (live)** | Fyers WS | **0 REST** + occasional gap-fill | 0 | ~0% | MCX runs to 23:30 IST at 0 REST cost. |
| **F3 — commodity 1m backfill** | Upstox | ~40–80 one-shot | ≤8/s | n/a (no daily cap) | Off-hours. |
| **F4 — Gann (live)** | Fyers WS + REST gap-fill | few k/day | ≤ few/min | >95% spare | |
| **F4 — CBE EOD + 1m** | Upstox | ~300–600 one-shot | ≤8/s, <90s total | n/a (no daily cap) | Off-hours only. |
| **Aggregate live Fyers REST** | Fyers | **~35–40k/day** (Directional derived) | sum staggered ≤ ~120/min | **~40%** | **~60k headroom** for retries/re-bands. |

### Poll-scheduling / staggering rules (mandatory — P0)

1. **One global Fyers limiter** shared across ALL REST callers (chain poll, gap-fill, open poll):
   token bucket at **10/sec** + sliding **200/min** governor, in `brokers/fyers.py:_get_data_json`.
2. **Round-robin the 227 chain calls** across each 180s window: emit ~1 call every **0.79s**
   (180 ÷ 227). The current `OptionChainService._poll_loop` is a naive sequential `await` loop with
   no rate governor — it must pull from the shared limiter, never fire all 227 in one minute.
3. **Priority lanes:** chain poll = normal; gap-fill = low (yields to chain); 09:15 open poll =
   one-shot high, spread over the first 3 minutes.
4. **429 → exponential backoff** (base 1s, cap ~30s) + **concatenated-JSON guard** (Fyers can
   return multiple JSON objects on burst; parse defensively).
5. **Upstox ~8/sec throttle** + **hard gate OUTSIDE 09:15–15:35 IST** so backfill never competes
   with the live Fyers bucket.
6. **Directional = derive, not re-fetch.** Reuse the S1 3m chain snapshot for greeks/IV/OI (0
   incremental calls). Only if true 1m is required, run the **liquid-subset** tier — never all 227.

---

## 3. Persistence spec

### `option_premium_candles` (TimescaleDB hypertable, chunk 1d) — F1 target
- **Columns:** time, underlying, market, expiry, strike, option_type, interval, instrument_key,
  trading_symbol, open, high, low, close, volume, oi, iv, delta, gamma, theta, vega,
  underlying_price, source, synced_at, time_to_expiry_years.
- **Key / dedup:** `PRIMARY KEY (instrument_key, interval, time)`.
- **Source tagging FIX (high):** `option_history.py:762` hardcodes the literal `'upstox'` in the
  INSERT VALUES (verified — and it uses `ON CONFLICT … DO NOTHING`, **not** DO UPDATE as the map
  claimed). Thread the real broker:
  - Pass `source='fyers'` when the Fyers branch (`option_history.py:423`) ran, `'upstox'` for the
    Upstox branch (`:364`), `'fyers_chain'` from the new chain builder.
  - Replace the literal `'upstox'` at `:762` with a `:source` bind param in `_persist_broker_candles`.
  - `source_policy.py` (verified: exposes `route_order`/`choose_active_adapter`, but **no
    source-priority for dedup**) is currently dead-code for this path — wire `route_order` into
    `_persist_broker_candles` source selection.
- **Dedup tie-break FIX:** read path uses `DISTINCT ON (time) ORDER BY synced_at DESC`. Add an
  explicit precedence: `ORDER BY synced_at DESC NULLS LAST, source_priority` where
  `fyers_chain > fyers > upstox > live_tick` (greeks-bearing chain rows must win over greeks-null
  live rows). Switch the live upsert to `DO UPDATE` **with precedence** so a greeks-null `live_tick`
  row cannot clobber a greeks-bearing chain row.
- **Phantom-expiry gate [BUILT, partial]:** `option_history.py:733-735` calls
  `is_valid_index_expiry(underlying, expiry)` before persist; `live_candle_store.py:291-301` also
  gates. Era-aware (NSE Thu→Tue cutover 2025-09-01). Some rows still slipped (32,387 rows = 0.23%
  of 13.8M) → run one-off `db/maintenance/phantom_expiry_cleanup.sql` (do NOT auto-run in prod).
- **Retention:** no policy today — add a compression policy on chunks >7d; keep candles long-term.

### `underlying_spot_candles` (hypertable, chunk 1d) — F3/F4 target
- **Columns:** time, instrument_key, underlying, interval, open, high, low, close, volume, oi,
  source, synced_at. **Key:** `(instrument_key, interval, time)`.
- **Corruption guards [BUILT]:** ingest `live_candle_store.py` `SPOT_DEVIATION_THRESHOLD=0.5`
  (±50% from 30-tick rolling median, 5-tick warmup) rejects cross-symbol contamination before
  aggregation; rejects metered to `nomad_ingest_rejected_total{reason='spot_magnitude'}`. Read
  guards: `analysis/safe_candles.py::guard_ohlc()` (dedup keep='last', RTH filter, ±20% band) +
  `market_intelligence_runtime._drop_contaminated_spot_rows()` (±50% band). `CLEAN_SOURCES`
  filters to `timescaledb_spot_1minute`.
- **Documented data debt:** ~44% duplicate timestamps + cross-symbol contamination (NIFTY rows
  carrying BANKNIFTY/SENSEX prices). **TO-DO:** extend the same dedup/outlier guard to the **Fyers
  REST gap-fill path** before widening CBE's 300-day lookback; DELETE the known bad
  `local_csv_spot` / `live_tick` contaminated rows at source (hand SQL to user; don't auto-run).

### `market_ticks` (hypertable, chunk 1d) — F2/F3 source [BUILT]
- Columns incl. `total_buy_qty`/`total_sell_qty` (migration 024). Retention **14 days** (raw ticks
  distilled into candles + OF snapshots). Kept (powers OF reconstruction) — documented deviation
  from "don't persist raw ticks".

### `orderflow_snapshots` — **NEW** (F2 + F3, Tier-3)
- **Columns:** time, symbol, interval (`1minute`), top_imbalance, depth_imbalance, book_pressure,
  cvd, anchored_cvd, trade_intensity_per_minute, aggressive_buy_volume, aggressive_sell_volume,
  toxicity_score, source, synced_at.
- **Key:** `(symbol, interval, time)`, hypertable chunk 1d.
- **Writer:** per-1m aggregator fed from `market_ticks` (+ true L2 depth once subscribed) for the 3
  indices + commodity. Unblocks Sniper `u_of_*` backfill (historically null/dropped at train).

### `index_futures_candles` / metadata catalogs — unchanged [BUILT]
- `index_futures_candles` key `(instrument_key, interval, time)`. `fo_*_catalog` plain Postgres;
  add `is_valid_index_expiry` gate at catalog population (currently gated only at candle ingest).

---

## 4. Streaming per-second tier

Four tiers; **per-second raw ticks are NOT bulk-persisted to Postgres long-term** (distilled into
bars + OF snapshots; `market_ticks` is the 14-day rolling exception that powers OF math).

| Tier | What | Where | Persisted? |
|---|---|---|---|
| **T1 Hot** | last N min ticks + L2 depth/greeks per symbol; powers live UI + OF math (imbalance/CVD/MP) | **Redis** bounded ring buffer (currently just pub/sub `ticks:{symbol}` + hot-cache — **upgrade to a bounded buffer**) | ephemeral |
| **T2 Warm** | aggregate ticks → **1m + 3m** OHLC (+OI, greeks where available) | Postgres `*_candles`, upsert **`source='fyers_ws'`** | durable |
| **T3 OF snapshots** | per-1m order-flow metrics (top/depth imbalance, book pressure, CVD, trade intensity) for 3 indices + commodity | Postgres `orderflow_snapshots` (**new**) | durable |
| **T4 EOD** | daily close snapshot for CBE analytics + close-of-day OI/greeks | Postgres (daily snapshot) | durable |

**Fetch:** Fyers WS pushes tick/sub-second (depth+greeks → F2; LTP/OI → F1 band + F3 futures) into
T1. Aggregators roll T1 → T2 bars (via `live_candle_store`, **already does 1m/3m** [BUILT]) and T1 →
T3 OF snapshots (**[TO-BUILD]**).

**Frontend (reuse existing wiring):** `frontend/src/lib/websocket.ts` already exposes
`createPositionsOverviewSocket()` → **`/ws/positions-overview`** and the per-desk
`useStrategyPositionsStream` hook + `createCommodityWatchlistSocket`/`createMarketWatchlistSocket`.
Generalize the commodity-overview pattern to every desk (nse, directional, fractal, gann, mp,
auction, cbe, sector): push **watchlist** (live LTP/greeks/OI from T1) + **open positions** (live
mark + unrealized PnL deltas) off the hot buffer, and set **default tab = Open Positions** on every
desk. Frontend is **image-baked** → these changes require an image rebuild (not bind-mount).

---

## 5. GAP LIST (ranked)

**BLOCKER**
1. **Fyers throttle/backoff/JSON-guard** — none exists. File: `brokers/fyers.py`
   (`_get_data_json`). Also add Upstox ~8/s throttle in `brokers/upstox.py`. *Prerequisite for the
   227-name poller; cold-start without it = 429 storm against the sole live lane.*
2. **F1 chain-poll candle builder** — does not exist. `option_chain.py` polls only the tracked ATM
   expiries and writes point-in-time `option_chain_snapshots`, never 3m OHLC, never the full
   universe. **New module** `backend/market_data/chain_candle_builder.py`: iterate
   `fo_underlying_catalog`, tiered poll `/options-chain-v3`, build 3m CE+PE OHLC+OI, upsert
   `option_premium_candles` source=`fyers_chain`, widen band on exit.
3. **S1 consumes the live feed** — `analysis/s1_strategy.py` reads ATM-only offline. Refactor to
   consume full-chain CE+PE 3m once the builder exists. (Depends on #2.)

**HIGH**
4. **Source-tagging bug** — `option_history.py:762` hardcodes `'upstox'` (verified; `DO NOTHING`).
   Thread real broker through `_persist_broker_candles` (`:716`); wire `source_policy.route_order`;
   add `source_priority` dedup tie-break (`:~542`).
5. **Persisted order-flow (F2/F3)** — no L2 depth subscription, no `orderflow_snapshots` table. OF
   is reconstructed in-memory (`auction_intelligence/live.py:1082`); `analytics/orderflow.py`
   computes but never persists. Add migration + per-1m writer; subscribe true L2 depth (Fyers/Upstox
   depth endpoints exist but are unimplemented in the adapters).
6. **Upstox off-hours backfill lane** — no `get_historical_candles` on the Upstox adapter (raw HTTP
   inside `option_history._fetch_upstox_rows`); no scheduled job (`RESEARCH_SYNC_EMBEDDED_ENABLED`
   off by default). Formalize adapter method + scheduled gated daemon.

**MEDIUM**
7. **Live greeks/OI on WS candles** — `live_candle_store.py:319-321` writes iv/delta/gamma NULL;
   `DO UPDATE` has no precedence (greeks-null can clobber greeks-bearing). Extract greeks from Upstox
   `firstLevelWithGreeks` / Fyers chain; add precedence.
8. **Directional tiered subset** — wire the 60s/3m shared-snapshot path (`directional_options/`).
9. **Commodity 1m background ingester** — `commodity_runtime_history.py` is on-demand only → gaps.
   Add scheduled 1m persist + route into `orderflow_snapshots`.
10. **Frontend streaming generalization + default Open-Positions tab** — only commodity desk has the
    overview WS today. Generalize via existing `createPositionsOverviewSocket`/`useStrategyPositionsStream`
    (image rebuild).

**LOW**
11. **F4 scheduled spot/EOD backfill** for Gann universe + CBE equities (enable + gate embedded
    research-sync); run contamination cleanup first.
12. **30m spot aggregation**, phantom-expiry DELETE automation, widen Upstox 30m lookback,
    `is_valid_index_expiry` gate at catalog population.

---

## 6. Implementation roadmap (file-level, phased)

**Phase 0 — Enablers (do first; nothing scales without these)**
- `brokers/fyers.py::_get_data_json`: shared 10/s token bucket + 200/min + 100k/day budget + 429
  backoff + concatenated-JSON guard.
- `brokers/upstox.py`: ~8/s throttle; add `get_historical_candles` adapter method (formalize the
  raw path).
- `option_history.py:716/762`: thread real `source`; wire `source_policy.route_order`.

**Phase 1 — F1 core (S1, the headline requirement)**
- New `market_data/chain_candle_builder.py`: tiered `/options-chain-v3` poll over
  `fo_underlying_catalog`, 3m CE+PE OHLC+OI, source=`fyers_chain`, band-widen logic, eager 09:15
  poll, staggered via the shared limiter.
- `analysis/s1_strategy.py`: consume full-chain 3m feed.
- Dedup tie-break + `source_priority` on the read query (`option_history.py:~542`).

**Phase 2 — F2/F3 order flow + Directional**
- Migration: `orderflow_snapshots` hypertable + per-1m writer fed from `market_ticks` (3 indices +
  commodity).
- Subscribe true L2 depth in `brokers/fyers.py`/`brokers/upstox.py` WS handlers.
- `directional_options/`: tiered subset / derive from S1 snapshot.
- `live_candle_store.py:319`: live greeks + `DO UPDATE` precedence.
- `commodity_runtime_history.py`: background 1m persist.

**Phase 3 — F4 backfill + frontend**
- Upstox off-hours backfill daemon (enable `RESEARCH_SYNC_EMBEDDED_ENABLED`, hard-gate outside
  09:15–15:35), Gann universe + CBE EOD/1m.
- Contamination/phantom cleanup before widening lookbacks.
- Frontend: generalize `createPositionsOverviewSocket`/`useStrategyPositionsStream` to all desks,
  default tab = Open Positions (**image rebuild**).

**Prod constraints (from hard-won memory — respect strictly):**
- **Do NOT heavy-exec in the prod `nomadcurie_backend` container** — backtests/tuning/feature builds
  OOM (rc 137) → container recreate → bind-mount `/app` re-syncs from image → silent revert + DB
  connection leak (cap 25). Run backtests/sweeps LOCALLY off DB-pulled candles or in an **isolated
  sidecar** (the proven sniper pattern: `docker run --rm --network tradebot_default`).
- **Bind-mount edits are ephemeral** — permanent deploy = commit to repo + image rebuild; bind-mount
  push (base64-over-SSM, chunk files >70KB) is live only until the next recreate.
- **Frontend is image-baked** (`COPY . .`) — all frontend changes need an image rebuild.
- **Backfill via Upstox off-session** so it never enters the live Fyers REST window.

---

## 7. Risks & open questions

- **CBE identity — CONFIRM.** Memory's project index calls it the **Cross-Boundary Equity Scanner**
  (`backend/cbe_scanner/service.py`), an EOD + intraday **equity** scanner over ~300 equities/ETFs
  using `underlying_spot_candles` (300-day lookback) + optional IV/PCR. It is **not** commodity. The
  requirement ("EOD analytics + same 1m spot for portfolio refresh") matches the equity-scanner
  reading. **Open:** confirm with user that "CBE" = equity cross-boundary scanner (not a commodity
  variant) and which universe loader feeds it.
- **MCX order-flow availability.** MCX has **no WebSocket**; live granularity is bounded by the 12s
  LTP poll (`commodity_mark_refresh_loop`) + whatever ticks land in `market_ticks`. "Granular order
  flow" for commodity is **best-available**, not true L2. Confirm whether Fyers/Upstox expose MCX
  depth at all; if not, F3 OF is LTP/trade-intensity-derived only.
- **`strikecount` adequacy.** Code uses `strikecount=12` (`fyers.py:509`). **Confirm the max** and
  whether 12 covers the widest index band (BANKNIFTY/SENSEX wide strikes). Fallback: raise
  strikecount or issue 2 centered calls per underlying (doubles that name's call cost).
- **Upstox live option-chain entitlement.** Audit states "likely lacks" live chain on the analytics
  token → **live chain MUST come from Fyers**; Upstox = history only. Confirm before any routing
  decision that would put live chain on Upstox.
- **Fyers SPOF + re-login cadence.** Fyers is the sole live lane (Upstox trading token expired). DB-
  first reads + the P0 throttle are essential. **Open:** daily Fyers re-login cadence (live-lane
  dependency).
- **Directional 1m vs shared 3m snapshot.** Preferred = derive Directional analytics from the S1 3m
  chain snapshot (0 incremental calls). Confirm Directional truly needs 1m granularity; if so, cap it
  to the liquid subset (never all 227 @1m — the 85k/day budget trap).
- **`underlying_spot_candles` data debt.** ~44% dup timestamps + cross-symbol contamination must be
  cleaned at source and the Fyers gap-fill path must inherit the dedup/outlier guard **before**
  widening CBE's 300-day lookback.
- **Tier-1 ring buffer.** Today it's pub/sub + hot-cache, not the prescribed bounded ring buffer —
  confirm whether to build the bounded abstraction or keep pub/sub + document the deviation.

---

## 8. Implementation status (2026-06-07) — Phase 0 + Phase 1 BUILT (off-prod, flag-gated)

**Phase 0 — Enablers [BUILT, verified: py_compile + unit tests + import smoke]:**
- `brokers/rate_limiter.py` (NEW) — `AsyncRateLimiter` (multi-window token bucket) + `parse_first_json`
  (concatenated-JSON guard). Process-global singletons: `FYERS_DATA_LIMITER` (9/s · 190/min · 95k/day,
  set under the hard 10/200/100k caps) and `UPSTOX_DATA_LIMITER` (8/s · 1800/30min). Unit-tested:
  windowing, day-cap, JSON guard.
- `brokers/fyers.py::_get_data_json` — now acquires `FYERS_DATA_LIMITER` before EVERY data REST call
  (/history, /options-chain-v3, /quotes all funnel here), with 429 (Retry-After aware) + 5xx + transport
  exponential backoff (5 attempts, cap 30s) and the concatenated-JSON guard.
- `brokers/upstox.py` backfill path (`option_history._fetch_upstox_rows`) — acquires `UPSTOX_DATA_LIMITER`
  + 429/503 retry, so an off-hours full-universe backfill cannot trip Upstox's 2000/30min governor.
- `market_data/option_history.py` source-tag FIX — `_persist_broker_candles(source=...)` threads the real
  broker (`'upstox'` if `_is_upstox_key` else `'fyers'`; chain builder passes `'fyers_chain'`); the literal
  `'upstox'` at the INSERT is now a `:source` bind param. Read-path dedup `ORDER BY time, <source CASE:
  fyers_chain>fyers>upstox>else>, synced_at DESC` so greeks-bearing chain rows beat greeks-null live rows
  at a shared timestamp (single-source timestamps unaffected).

**Phase 1 — F1 chain candle builder [BUILT, verified: accumulator unit-tested]:**
- `market_data/chain_candle_builder.py` (NEW) — `ChainBarAccumulator` (pure, unit-tested 3m OHLC rollup:
  open=first, high/low, close=last LTP; per-bar volume = cumulative-volume delta; last OI/greeks/underlying
  carried; bucket flooring; shutdown flush) + `ChainCandleBuilder` service. Polls the FULL universe via
  `ATMWatchlistService._load_underlyings()` + `_to_fyers_symbol`, fetches the nearest-expiry band with
  `adapter.get_option_chain(sym, "")` (strikecount=12 ⇒ 12 ITM + ATM + 12 OTM, CE+PE, greeks app-side),
  rolls 3m bars, persists via `_persist_broker_candles(source="fyers_chain")` into `option_premium_candles`
  interval `3minute`. Tiered cadence: INDEX @60s (true OHLC), STOCK @180s (≈close/bar) ⇒ ~29.6k Fyers
  REST/day (~30% cap); calls self-stagger through the limiter (no manual sleeps).
- S1 consumer: NO refactor needed — the `analysis/s1_*.py` files are the off-prod research harness;
  live S1/watchlist reads go through `OptionHistoryService.load_candles(interval="3minute")`, which the
  builder now feeds for the full chain.
- Lifecycle: `core/config.py::CHAIN_CANDLE_BUILDER_ENABLED` (default **False**) + `main.py` lifespan
  start/stop. **Inert in prod until the flag is set + signed off + market-open verified.**

**MCX order-flow research finding (resolves §7 open question):** True MCX **5-level L2 depth IS available**
from both brokers — Fyers WS `DepthUpdate` (separate socket; `SymbolUpdate`+`DepthUpdate` can't share one
connection) with per-level `price/volume/ord` + `totalbuyqty/totalsellqty`, and a REST `/data/depth`
snapshot; Upstox V3 feed `full` mode carries 5 levels (`full_d30` = 30, Plus only). The platform already
subscribes Upstox in `"full"` mode but `_top_of_book` discards levels 1-4, and Fyers uses `SymbolUpdate`
(top-of-book only) — so book depth is on the wire today, just unparsed. CRITICAL CAVEAT: **neither broker
exposes per-trade aggressor-tagged prints**, so CVD / aggressive-buy-sell / toxicity are NOT exactly
computable — they must be tick-rule PROXIES (label them as such in `orderflow_snapshots`). REAL metrics:
top-of-book imbalance, depth imbalance (`tot_buy_qty/tot_sell_qty`, already in `market_ticks` mig-024),
multi-level book pressure (parse the 5-level ladder). PROXY metrics: trade intensity (Δvol/min), CVD proxy,
aggressive vol via tick rule. F3 depth source = Fyers `DepthUpdate` (richest: has `ord`); Upstox `full`
levels 1-4 = fallback (needs feed-read scope on the token). `base.Tick` has no multi-level ladder field →
schema add needed for L2 persist.

---

## 9. Live streaming UI — watchlist + positions (2026-06-07, BUILT, build-verified)

Audited end-to-end (frontend consumption + backend WS liveness + accuracy chain). Key truth: a "2s WS
push" is a HEARTBEAT cadence — `_stream_snapshot` re-serves whatever the payload_factory last built, so
"feels live" ≠ "is accurate". Real-time accuracy needs a per-tick overlay (`live_marks.overlay_*`) AND
the symbol on the broker WS.

**Built this session [verified: tsc 0 errors + py_compile + import/wiring smoke; NOT deployed — FE
image-baked, BE bind-mount]:**
- **Generic `/ws/strategy-snapshot` channel** (`api/websockets/ticks.py::ws_strategy_snapshot`, registered
  in `main.py`): `?desk=directional|gann|auction|fractal&symbol=&timeframe=` → dispatches to each desk's
  existing REST `live_snapshot` fn via `_stream_snapshot` (8s push). Frontend
  `createStrategySnapshotSocket` (`lib/websocket.ts`).
- **Watchlist/analytics now STREAM** on Directional/Gann/Auction: their `liveQuery` converted
  `useQuery` → `useLiveSnapshotQuery` + the new socket (8s push, instant reconnect, polling fallback,
  localStorage snapshot). For directional the universe watchlist = NIFTY/BANKNIFTY/SENSEX whose spot is
  WS-live → genuinely real-time spot.
- **NSE positions now STREAM** the `nse` slice off `/ws/positions-overview` (carries
  `overlay_nse_agent_status` per-tick marks → MORE accurate than the prior 30s poll); fallback to poll.
- (Earlier this session) Directional/Gann/Auction open positions already stream via
  `useStrategyPositionsStream` over `/ws/positions-overview`.

**Honest accuracy scorecard (today, post-build):** "feels live" = YES on every trading desk (watchlist +
positions push 2-8s). "Is accurate" = REAL for the 3 index spots (WS) + open NSE option-leg LTP/P&L once
WS-subscribed (≤45s after entry). STALE/snapshot: all greeks/IV/OI (2-5m), watchlist per-symbol LTP
beyond the 3 indices, commodity marks (~12s MCX REST floor, no MCX WS).

**To complete ACCURACY (deliberate, needs verification — not done):**
- (step 4) Emit `mark_source` (`tick|scan|rest_12s`) + `mark_age_s` per leg in `live_marks.overlay_live_marks`
  + a frontend freshness dot — converts the "all 2s = live" feel into an honest one. [backend+FE]
- (step 5) Fire `refresh_held_position_subscriptions` on FILL to kill the ~45s new-position blind window. [backend]
- (step 6) Widen the broker-WS subscription set to the directional/gann universe spot → real per-tick
  watchlist LTP beyond the 3 indices. [needs-feed-on]
- (step 7) Enable `CHAIN_CANDLE_BUILDER_ENABLED` (F1) — the ONLY path to live greeks/IV/OI on watchlist +
  positions. No frontend wire substitutes for this; it's a data-availability fact. [needs-feed-on, market-open verify]
