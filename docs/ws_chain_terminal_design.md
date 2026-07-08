# WS-First Chain Terminal — Design (2026-07-07)

_Produced + adversarially verified via multi-agent workflow (wf_3ed4ee75-7de). Ground truth from the Upstox V3 protobuf schema + official docs, Fyers v3 KB, and repo code. See the verification appendix for what survived scrutiny._

# Terminal-Grade Live Chain Data Layer — Final Recommended Design

**TradeBot NSE F&O paper platform · WebSocket-first near-money core, bounded REST perimeter, flag-gated & reuse-first**

This document is the merged, implementation-ready design. It answers the three questions directly (Sections A–C), then specifies architecture, per-broker subscription/sharding math, ATM-watchlist derivation, storage, fail-safe, the terminal `/ws/quotes` contract, a phased migration, and risks/open-questions. Every capability is anchored to a verified ground-truth fact; every choice that depends on an **UNVERIFIED** broker fact is flagged inline as ⚠️ UNVERIFIED.

---

## A. Question 1 — Is the option chain available over WebSocket? (precise, per broker)

**Short answer: partially, and asymmetrically. Upstox streams a full option-chain payload including native greeks+IV+OI; Fyers streams price+OI but no greeks/IV (those stay app-side Black-Scholes). Neither offers a "chain" subscription primitive — you subscribe individual instrument keys and reassemble the chain client-side.**

### Upstox V3 market-data WebSocket — YES, native greeks + IV + OI on the wire

Proven authoritatively from the protobuf wire schema (`MarketDataFeedV3_pb2.py`): two feed shapes carry the chain fields.

- **`option_greeks` mode (the purpose-built LEAN chain mode)** → `FirstLevelWithGreeks` = `{ltpc, firstDepth (top-of-book), optionGreeks{delta,theta,gamma,vega,rho}, oi, iv, vtt(volume)}`.
- **`full` mode** → `MarketFullFeed` = everything in `option_greeks` **plus** `marketLevel` (5-level depth), `marketOHLC` (1m/30m/1d candles), `atp`, `tbq`, `tsq`.
- **`ltpc` mode** → `{ltp, ltt, ltq, cp}` only.
- **`full_d30` mode** → full-mode data at 30 depth levels, but a **hard 50-instrument-per-user ceiling** → unusable for chains.
- **Indices** → `IndexFullFeed = {ltpc, marketOHLC}` — no greeks/OI (an index has none; correct and expected).

Field precision:
- `iv` is a **sibling scalar on the feed, NOT inside `OptionGreeks`** — parse it separately.
- WS greeks include **`rho`**, which REST `/option/chain` does **not** return. The WS payload is strictly richer than REST for greeks.

Caps (official docs table):

| Mode | Individual cap / conn | Combined cap / conn (when mixing modes) |
|---|---|---|
| `ltpc` | 5000 | 2000 |
| **`option_greeks`** | **3000** | 2000 |
| `full` | 2000 | 1500 |
| `full_d30` | **50 (per-user hard ceiling)** | 1500 |

Connections per user: **2 (standard), 5 (Plus)**. To get the higher *individual* cap, keep a connection **single-mode**; mixing modes drops it to the *combined* cap.

**⚠️ UNVERIFIED — Upstox plan-gating.** Official docs flag **only `full_d30`** as Plus-only, and the protobuf shows the index-vs-market split is per-*instrument-type* (not a plan flag). But community thread 14563 muddies whether `full`/`option_greeks` greeks are Standard-accessible (that user was on `full_d30`, which *is* Plus-only, so the evidence does not actually prove greeks are Plus-gated). The repo already reads non-zero `oi` from `marketFF` in `full` mode and real Upstox greeks have flowed into `option_chain_snapshots` — consistent with Standard working, but the account could simply be Plus. **This must be resolved empirically before trusting Upstox-native greeks (feasibility claim #1).** The design routes around it so it never blocks the critical path.

### Fyers v3 data WebSocket — OI YES, greeks/IV NO

- **`SymbolUpdate` (full) mode** carries: `ltp`, OHLC (`open/high/low_price`, `prev_close_price`), `vol_traded_today`, `oi`, **`pdoi` (previous-day OI)**, top-of-book (`bid/ask_price`, `bid/ask_size`), `tot_buy_qty`/`tot_sell_qty`, `avg_trade_price`, ckt bands, `ch/chp`.
- **No greeks, no IV anywhere in the schema (CONFIRMED).** Greeks are produced *only* on the REST chain path via `analytics.greeks.implied_volatility()` → `bs_greeks()` with `r=0.065`. This is an intentional app-side design, not a gap.
- **`Lite` mode** = LTP only.
- **Depth is a separate 5-level `DepthUpdate` frame** (`bid/ask_price1..5`, sizes, order counts, `tbq/tsq`); the 50-level ladder is the paid, NFO-only `FyersTbtSocket` (`FYERS_TBT_DEPTH_ENABLED`, default OFF). **The depth stream does NOT carry `oi` — `oi` is quote-stream only.**
- **Cap: 5,000 *unique symbols per API key*, deduped across connections** — duplicate subs of the same symbol across sockets are not double-counted. Opening more sockets does **not** raise the ceiling. **⚠️ UNVERIFIED — the max connection *count* per key is undocumented** (feasibility claim #14). A stale KB page still cites "200 symbols"; that is superseded — trust 5,000.

### Cadence (both brokers)

Event-driven near-real-time push, not a fixed poll. First frames = market-status then a snapshot, then live updates on change; idle keep-alive pings. **⚠️ UNVERIFIED — the greeks/IV recompute cadence is undocumented on both brokers** (feasibility claim #11). Treat greeks/IV as slow-moving — the existing `greeks_enrichment` daemon already relies on this (a ~120s snapshot cadence "comfortably covers" 1–30m bars). Do **not** build logic that assumes tick-synchronous greeks.

---

## B. Question 2 — Can the ATM CE/PE watchlist be DERIVED from streamed chains?

**Yes — the pick is a pure function of inputs that all stream, with three explicit caveats.**

`_select_liquid_atm_strikes` (`atm_watchlist.py:99-189`) needs exactly:

1. **Sorted strike ladder** — static intraday, seeded once from `fo_contract_catalog` / one cold-start chain snapshot; refreshed only on expiry roll or new-strike listing.
2. **Spot** — the index/equity tick, already on the tape.
3. **Per-strike, per-side `{volume, oi}`** — carried on every option tick (Fyers streams both; Upstox streams both + greeks).

With those, the pick reproduces **exactly** today's behaviour: CE = most-liquid strike at-or-**above** spot (±2-strike slop); PE = most-liquid at-or-**below**; a neighbour preferred only when its single-side volume ≥ **1.5×** the anchor's; liquidity = volume, fallback `oi/100`. The asymmetric result (`ce_atm_strike`/`pe_atm_strike`, `atm_strikes_asymmetric` flag) is unchanged. Greeks/IV for the picked legs are BS-recomputed from streamed `ltp`+spot (identical to today's REST path) or ingested Upstox-native for indices — **no greeks fidelity is lost by dropping the REST poll.**

**Why we must stream ATM±1, not just the 2 picked legs:** the 1.5× liquidity lift compares the anchor strike against its neighbour, so the neighbour's volume/oi must be on the tape.

**Three caveats (none blocking):**

- **C1 — `prev_oi` is not streamed** by either broker (only current `oi`). Recover it from a **once-daily 09:15 REST open snapshot** of the streamed band to preserve `oi_change`/`oi_change_pct` in `atm_option_watchlist_snapshots`. (Fyers also streams `pdoi` = *prior-session* OI, usable as a cross-check but not as today's intraday-open baseline.)
- **C2 — the 30-min MACD entry trigger and the live 15-min "cross" need BARS, not raw ticks.** Streamed ticks *can* build them (a new tick→bar aggregator) but do not provide them for free. This is in-scope work, not optional.
- **C3 — chain-wide analytics (PCR / max-pain / gamma-exposure) cannot be WS-derived** — they sum OI over *every* strike both sides, including deep-OTM wings the ATM±N band never streams. These keep a (slower) REST sweep permanently.

This makes the "roll" continuous and spot-driven, replacing today's static 09:05 lock that the 45s snapshot-sourced held-leg refresh only approximates.

---

## C. Question 3 — Terminal-like data-fetch design (analysis, tracking, performance)

The remainder of this document is the answer: a **WebSocket-first near-money core** (streamed ATM±N band + held/picked legs, driving the derived watchlist, live marks, and greeks-bearing premium bars) plus a **bounded REST perimeter** (deep-OTM chain-wide analytics, the wide stock universe, daily discovery, cold-start/gap-heal). One new component (`StreamingChainAssembler`) sits between the tick callback and the existing consumers; everything else reuses current tables, caches, flags, and the `/ws/quotes` tape.

---

## 1. Architecture overview

```
                         ┌────────────────────────── REST PERIMETER (kept, demoted to floor) ───────────────┐
                         │  daily instrument-master / expiry discovery                                       │
                         │  OptionChainService full-chain poll → option_chain_snapshots (PCR/max-pain/GEX)    │
                         │  MI premium top-up (gap-heal + non-streamed stocks + deep wings)                   │
                         │  cold-start history backfill (S1 ≥34 bars, directional 120-session lookback)       │
                         │  09:15 prev_oi open snapshot                                                       │
                         └───────────────────────────────────────────────────────────────────────────────────┘
                                                            ▲ fallback / reconciliation oracle
 Fyers WS  ─ SymbolUpdate (oi, pdoi, ltp, book) ─┐         │
 (5000 unique/key, workhorse + BS greeks)        │         │
                                                  ▼         │
 Upstox WS ─ option_greeks (δγθν ρ + iv + oi) ─▶  StreamingChainAssembler  ──┬─▶ Redis oc:{sym}:{exp} (60s TTL, live cache)
 (3000/conn, native-greeks overlay, indices)      (per (underlying,expiry)   ├─▶ atm_option_watchlist_snapshots (re-pick/side)
                                                    strike→{CE,PE} state,     ├─▶ option_premium_candles (tick→bar, greeks-stamped)
 Index/equity spot ticks ───────────────────────▶  re-runs the picker,       ├─▶ option_chain_snapshots (120s durable, band rows)
                                                    stamps greeks,            └─▶ quote_bus → /ws/quotes tape (greeks-extended)
                                                    aggregates bars)                    │
                                                                                        ▼
                             Consumers (unchanged shapes): S1 MACD · directional positioning · auction NTM-VolX/OF ·
                             live_marks overlay · directional paper marks · terminal QuoteGrid/Chain/Depth/badges
```

**Design principles (non-negotiable honesty):**

1. **WS is a subscription, not a poll** — a persistent Fyers/Upstox WS draws **zero** from the REST limiters (Fyers 190/min·95k/day, Upstox 1800/30min). Everything pushed onto WS is free of the REST budget.
2. **Greeks are already app-side and survive the migration.** Fyers never streams greeks (CONFIRMED); today's REST-chain greeks are already BS. Dropping the REST poll loses no greeks fidelity as long as we have streamed `ltp` + streamed spot — the exact BS inputs used today.
3. **Chain-wide analytics cannot be WS-derived** (caveat C3) — permanent REST sweep. Not a TODO, a structural limit.
4. **Reuse-first.** The only net-new moving part is `StreamingChainAssembler`. Every downstream is an existing table, cache key, tape, or flag. REST is demoted, never deleted — it is the fallback floor and the reconciliation oracle.

---

## 2. Data-source split — which broker streams what

The split assigns each broker the job it is uniquely good at and makes them mutually redundant.

| Data | Source of record | Rationale |
|---|---|---|
| Option **LTP / OI / volume / top-of-book** (streamed band, all underlyings) | **Fyers WS** (`SymbolUpdate`, reuse `option_subscription_manager`) | already wired to `data_router`, already dry-run-gated; 5,000-unique-per-key pool is the only cap large enough for the band; carries `oi` + `pdoi` natively |
| Option **greeks δ/γ/θ/ν (Fyers legs)** | **app-side BS** (`analytics.greeks`, r=0.065) | identical to today's REST path — zero fidelity loss; full control, no feed dependency |
| **Native greeks + IV + ρ (5 index chains)** | **Upstox WS** (`option_greeks`, separately gated) | native on the wire, adds `rho` + true broker IV (closes the greeks-null-since-06-23 gap for indices without the enrichment daemon); independent cross-check of BS numbers; redundancy if Fyers drops |
| **Index / equity spot** | existing index+equity tape (Fyers primary / Upstox) | drives the ATM roll; indices carry no OI/greeks (correct) |
| **`prev_oi`** | **09:15 REST open snapshot** (once/day) | not streamed by either broker |
| **Chain-wide PCR / max-pain / GEX** | **REST `OptionChainService`** (kept) | needs deep-OTM OI on every strike — never in the band |
| **~217-stock full chains / deep wings** | **REST** (bounded; optional Fyers ATM±1 stream later) | monthly stock chains are large; streaming wide blows the budget |
| **5-level depth (focused symbol)** | **Fyers `DepthUpdate`** (`/ws/depth`, ref-counted on focus) | depth stream carries no `oi`; TBT 50-level stays paid/OFF |
| **Cold-start / gap-heal / expiry discovery** | **REST** | historical, off the hot path |

**Greeks precedence & unit discipline.** For an index leg we may hold both Upstox-native and Fyers-BS greeks. Precedence: `upstox_native > bs`, extending the existing `option_premium_candles` source-precedence dedup (`fyers_chain > fyers > upstox > live_tick`). **Unit hazard preserved:** `option_chain_snapshots.iv` is **percent**, `option_premium_candles.iv` is **fraction** (the `/100` fix `greeks_enrichment` applies). Upstox-native `iv` unit is ⚠️ UNVERIFIED — **normalize at the parse boundary and store one convention per table** (feasibility claim #13).

**Why not make Upstox the universe streamer even though its greeks are "free"?** Three grounded reasons: (a) only **2 connections** on standard; (b) plan-gating unresolved (⚠️); (c) even at the verified 3,000/conn cap, the universe band needs multiple connections. Fyers has none of these ambiguities for the wide band. Upstox therefore serves where its native greeks add value on a small, cap-safe key-set: the 5 index chains.

> **Budget correction carried forward:** the low-confidence "~100 instruments/conn Upstox `full`" figure is **superseded** by the high-confidence protobuf/SDK number — `option_greeks` = **3,000/conn individual (2,000 combined)**. This flips Upstox from "REST-only, infeasible" to "viable for the near-money band in one connection."

---

## 3. WS connection & sharding plan — instrument-budget math

**Universe:** N = 224 underlyings (217 stocks + 7 indices; liquid streamed index set = 5). Per-underlying contracts (verified): ATM = 2, ATM±1 = 6, ATM±8 = 34 (17 strikes/side).

| Band | Per name | Indices only (×5) | Full universe (×224) | Fyers (5000 unique/**key**) | Upstox `option_greeks` (3000/conn, 2 std) |
|---|---|---|---|---|---|
| ATM only | 2 | 10 | 448 | 1 key (9%) | 1 conn (15%) |
| **ATM±1** | 6 | 30 | 1,344 | **1 key (27%)** | 1 conn (45%) |
| ATM±8 | 34 | 170 | 7,616 | **EXCEEDS 5000/key** ❌ | 3 conns → **Plus only** |
| **Hybrid: indices ±8 + stocks ±1** | — | — | **~1,472** | **1 key (~30%)** ✅ | 1 conn (49%) ✅ |

**Conflict resolution (important):** the caps-and-budget-math area computed "ATM±8 (7,616) → 2 Fyers conns" by treating 5,000 as *per-connection*. The authoritative fyers-ws finding overrides this: **5,000 is a per-API-key unique-symbol pool, deduped across connections** — a 2nd socket does not raise it. Therefore **full-universe ATM±8 is infeasible on a standard Fyers key**, and the design does **not** target it. Wings beyond the streamed band stay on REST.

### Recommended footprint (steady state)

**One Fyers connection (workhorse)** carries the whole streamed set:
- 5 indices × ATM±8 (34) = 170 option keys
- 217 stocks × ATM±1 (6) = 1,302 option keys
- ~222 spots (indices + equities, already subscribed) + held-position legs + S1 watchlist legs
- **≈ 1,472 option keys + spots ≈ 1,700 unique ≈ 34% of the 5,000 pool** — comfortable headroom for intraday roll churn and held-leg pinning.

**One Upstox connection (native-greeks overlay), `option_greeks` single-mode**, carries the **5 index chains' ATM±8 band** (170 keys, ~6% of 3,000) → native greeks/IV/ρ + cross-check + redundancy. The 2nd standard Upstox connection stays free for failover / deep-OTM REST-parallel capacity.

### Sharding rules

1. **Fyers:** shard only when unique symbols approach the **5,000 per-key** pool. The hybrid band is far under; no app-side chunking exists today (`client.subscribe(symbols=…)` single call) and none is needed until pushing past ~5,000 (which we explicitly reject for universe-wide ATM±8).
2. **Upstox:** keep each connection **single-mode** to get the 3,000 individual cap (not 2,000 combined). Put index-spot `ltpc` on a *separate* Upstox connection (or take spot from Fyers) so the chain connection stays pure `option_greeks`. **Never mix modes on the chain connection.**
3. **Sharding key = underlying** (all strikes of one underlying+expiry on the same socket) so the assembler for a chain never spans sockets — simplifies reconnect/rebuild.
4. **`full_d30` is banned** for chains (50/user hard ceiling). Depth stays Fyers 5-level, ref-counted per focus; TBT 50-level stays paid/OFF.
5. **Budget guard before every subscribe:** compute `remaining = cap − live_unique`; if a roll's adds would breach a *soft* cap (~80%), evict furthest-OTM non-held strikes first. Cap-exceed degrades gracefully, never a broker error.

**⚠️ UNVERIFIED gates (do not block the design):** Fyers max connection count per key (only 1 socket needed for the hybrid band, so off the critical path); Upstox real per-connection ceiling in `option_greeks` (verified as 3,000 in docs but confirm on the live account tier).

---

## 4. ATM-watchlist derivation + roll logic

The expensive operation today is *re-subscribing*; the cheap one is *re-picking*. Invert it: **subscribe the band once, roll the pick every tick.**

1. **Session open (09:05–09:15):** for each streamed underlying, pre-subscribe the **full ATM±N band** (indices ±8, stocks ±1) sized so intraday drift stays *inside* the pre-subscribed window. Seed the strike ladder + a one-shot REST chain snapshot for `prev_oi` and cold greeks warm-up.
2. **Every spot tick (debounced ~1–2s, or on strike-cross):** re-run `_select_liquid_atm_strikes` against the in-memory chain view → new `ce_atm_strike`/`pe_atm_strike`. Because all band strikes are already subscribed, **the roll is free** — no broker call. Strictly more responsive than today's 09:05 lock + 45s snapshot refresh; eliminates the "strikes locked even as spot moves" defect.
3. **Hysteresis (anti-churn):** only re-center the band when spot crosses the current ATM strike *midpoint* by a buffer (~0.25× strike step) **or** the pick flips for 2 consecutive evaluations. Prevents subscribe/unsubscribe thrash at strike boundaries.
4. **Lease, don't cut:** a strike leaving the window is retained with a **TTL lease** (~30–60s) before unsubscribe, so a whipsaw re-entry is free and mid-flight analysis keeps its feed. **Held legs are pinned indefinitely** while a position is open — this generalizes today's 45s `refresh_held_position_subscriptions` into an event-driven pin.
5. **Band-edge extension (rate-limited, the only broker touch):** when spot comes within ~2 strikes of the window boundary, widen the subscription on the drifting side via the existing `data_router.add/remove_subscriptions` path.
6. **Expiry rollover:** on weekly expiry change, re-enumerate `instrument_key`s from the daily instrument-master / `fo_contract_catalog` and rebuild the desired set (one REST touch).
7. **Persist the roll:** each re-pick writes `atm_option_watchlist_snapshots` (one row/side) exactly as today (`_persist_snapshot`), so `_strategy1_watchlist_legs` and held-leg refresh keep working unchanged. `oi_change`/`oi_change_pct` computed vs the **09:15 `prev_oi` snapshot** (caveat C1).

Desired-set per underlying: `desired = {held legs} ∪ {ATM±N band} ∪ {S1/S2 picked legs}`.

---

## 5. The one new component — `StreamingChainAssembler`

A thin service between `data_router`'s tick callback and the existing consumers.

- **State:** per `(underlying, expiry)` an in-memory dict `{strike → {CE:{ltp,oi,volume,bid,ask,iv,δ,γ,θ,ν,ρ?, last_tick_at}, PE:{…}}}`, updated on every option tick (grouped by strike/side from the trading symbol).
- **Greeks attach:** Fyers legs → BS recompute on ltp/spot change, throttled to ~1–3s (greeks are slow-moving, price is not). Upstox index legs → ingest native greeks+iv+ρ directly.
- **Four fan-outs:**
  1. **Redis `oc:{symbol}:{expiry}`** (reuse `OptionChainService`'s key + 60s TTL) written *from the stream* for streamed underlyings → `chain_strike_mark`, NTM-VolX (`option_chain_service.get_cached`), and directional marks read live streamed data with **zero consumer change**.
  2. **Watchlist re-pick** (§4) → `atm_option_watchlist_snapshots`.
  3. **Tick→bar aggregator** → `option_premium_candles` (1/3/15/30m OHLCV+oi, **greeks-stamped at write time**), source e.g. `ws_stream`.
  4. **`quote_bus` publish** of enriched option quotes → `/ws/quotes` tape (§7).
- **Cold start still REST:** S1's ≥34-bar 30m MACD warm-up and directional's 120-session lookback are REST-backfilled; the assembler serves only the live tail.

---

## 6. Storage & cache model — three tiers, all reused (feeder change, not a schema migration)

**Tier 1 — Redis tape (hot, ephemeral):**
- `quote_bus` keeps its 150ms coalesce → one frame/window, event-driven forward (`ws_quotes`, no snapshot timer). Extended with additive greeks short-keys (§7). Index frames unchanged.
- `oc:{symbol}:{expiry}` (60s TTL) now **written from the assembler** for streamed underlyings, from REST for deep wings / non-streamed stocks. Ephemeral by design; the live-read cache, not durable history.
- **`chain:{underlying}:{expiry}` assembly** = the streamed ATM±N band grouped by strike/side, feeding the optional `/ws/chain` UI channel. **Explicitly partial** — deep-OTM lives only in `oc:*`/REST; never present it as a full chain.

**Tier 2 — `option_chain_snapshots` (durable, 120s):** keep the `_persist_snapshot` contract (time, symbol, expiry, strike, option_type, ltp, oi, volume, iv, delta, gamma, theta, vega, bid, ask). **Feed it from the assembler for the streamed index band** (write every 120s from the WS view) and **merge deep-OTM strikes from the REST sweep** so PCR/max-pain/GEX stay whole. **IV stays percent here** — the assembler must write the same percent convention (×100 at the boundary when the source iv is a fraction) so `greeks_enrichment`'s `/100` and every downstream stay correct.

**Tier 3 — `option_premium_candles` (bar history, 3m/15m/30m):** add the assembler's **tick→bar writer** (`source='ws_stream'`), **greeks-bearing at write time** (Upstox-native for index legs, BS otherwise). This closes the "30m series greeks-null since 06-23" gap for streamed legs directly, reducing reliance on the indices-only `greeks_enrichment` backfill. **Slot into the source-precedence dedup deliberately** — recommend just below `fyers_chain`, above broker-backfill `fyers`/`upstox` — and document the choice, because the dedup ordering being correct is what keeps multi-broker duplicate rows honest. Keep `refresh_atm_premium_candles` and `chain_candle_builder` as **fallback/parallel writers** for non-streamed legs and gap-heal.

The only additive schema change is greek/iv/prev_oi slots on the `Tick` model (`base.py:155-174` today has no greek slots) and the `/ws/quotes` frame (§7).

---

## 7. Terminal UI data contract — extending `/ws/quotes`

Today `quote_bus._compact` ships short keys `s,p,o,h,l,pc,b,a,bz,az,v,oi,t,c` (150ms-coalesced, event-driven, rAF-drained, cell-isolated `useQuote`). The terminal renders OI/Bid/Ask columns but they populate only for symbols whose ticks carry them, and **watchlist-only option strikes are absent from the tape unless held/S2-picked**. WS-first fixes both by putting the ATM±N band on the tape and extending the frame **purely additively** (omit when absent; existing keys unchanged, so `useQuoteStore`/`useQuote`/`QuoteGrid` keep working).

**Additive short keys (option symbols only; indices keep `—`):**

| Key | Field | Source |
|---|---|---|
| `iv` | implied vol (fraction) | Upstox-native / BS |
| `dl` `gm` `th` `vg` | delta / gamma / theta / vega | Upstox-native / BS |
| `rh` | rho (nullable) | Upstox-native only |
| `poi` | prev_oi | 09:15 REST snapshot |
| `oic` | oi_change | derived (`oi − poi`) |
| `k` `ot` `ul` | strike / option_type / underlying | catalog |
| `gs` | greeks source (`n`ative \| `b`s) | honesty flag |
| `msrc` | mark source (`live_tick` \| `scan_guarded`) | honesty flag |

- `gs` lets the UI badge native-vs-computed greeks; `msrc` lets `LiveMarkBadge` distinguish a live print from a guarded/stale one.
- **Greeks get a separate, more generous staleness threshold** than price (greeks are slow-moving, cadence undocumented) so a slow greek update never false-flags a live leg as stale.
- Preserve the `·c` coalesced suffix; **also pass exchange `t`** so the UI can optionally age against exchange time, not just client-receipt `rxAt` (today's gap: a coalescer delay reads as "live").

**New opt-in `/ws/chain/{underlying}` channel:** a per-underlying multiplexed frame carrying the derived watchlist row —
`{underlying, spot, expiry, ce_atm_strike, pe_atm_strike, atm_strikes_asymmetric, ce:{…}, pe:{…}, extended_strikes:{CE:[…],PE:[…]}, lot_size, live_source}`
where `ce`/`pe` = `{strike, option_type, instrument_key, trading_symbol, ltp, prev_close, change, change_pct, oi, prev_oi, oi_change, oi_change_pct, volume, iv, delta, gamma, theta, vega, macd, macd_signal, macd_histogram, rsi}`. This is exactly the consumer contract already enumerated for the streamed-chain watchlist — the terminal, S1, and directional lanes read one frame. `macd/rsi` still come from `option_premium_candles` closes (built by the tick→bar aggregator), not the raw tick. The Chain-ladder view is a pure frontend composition over the same tape — no new socket needed. `/ws/depth` (5-level, ref-counted on focus) is unchanged — indices still show "No depth"; liquid option legs get a book when focused.

---

## 8. Feeding the consumers (analysis / tracking / performance)

| Consumer | Reads today | Under this design | Net change |
|---|---|---|---|
| **S1 · 30m ATM MACD** | `option_premium_candles` 30m closes (≥34 bars); marks off watchlist LTP | 30m bars from the tick→bar aggregator (greeks-bearing → IV filter no longer blind); live 15m cross resamples *streamed* LTP; marks via live tick | tick→30m aggregation (in scope); warm-up still REST-backfilled |
| **directional_options** | daily 30m OI→PCR/oi_build_bias; live IV from watchlist snap; marks from cached chain | native Upstox IV surface for indices (real `atm_iv` vs today's bisection); OI from streamed band; marks from streamed `oc:` cache | `prev_oi` daily seed; BANKNIFTY OI-build edge on fresher OI (data-quality upgrade, not a signal change) |
| **auction NTM-VolX + MP/OF** | near-money chain via `get_cached`; index order flow *fabricated* (bar-inference) | NTM band from streamed `chain:*`; **real sized ticks** (bid/ask_size, tot_buy/sell_qty) available if `AUCTION_OF_BOOK_SYMBOLS` maps an index→a streamed futures/ATM-option contract (≥4 sized ticks) | concrete unlock for real index order flow (separate wiring decision) |
| **Paper marks (S1/S2)** | `live_marks` overlay: `_tick_buffer→tick:{symbol}`, 30s max-age, 4.0× divergence guard | every held + ATM-band leg on the tape → `mark_source='live_tick'` for the whole tradable neighbourhood, not just open legs | register each streamed leg in `live_marks`; **keep the 4.0× cross-wiring guard** (Fyers hazard) |
| **directional paper marks** | `chain_strike_mark` via cached chain; freezes at entry on miss | stream-fed `oc:` cache → the "cache miss → frozen" Bug-B failure mode shrinks to genuine outages | — |
| **greeks_enrichment** | stamps `option_chain_snapshots` greeks onto null candles (indices only) | becomes **backfill-only** for gaps/non-streamed history; live greeks arrive on WS | demote once `ws_stream` bars carry greeks |
| **Performance** | realized_pnl reportedly *lifetime*, not today | per-tick marks + honest `msrc`/`gs` flags → clean inputs for intraday MTM / per-session attribution | **a per-session PnL attribution consumer is orthogonal** to this layer but now has clean inputs (open question #6) |

---

## 9. Fail-safe & fallback to REST (the safety spine)

REST is **demoted, never removed** — the floor and the reconciliation oracle. Degradation is per-symbol, never a global outage.

| Condition | Detection | Fallback |
|---|---|---|
| WS connection drops | socket close / no frames for T | resubscribe-on-reconnect (existing Fyers pattern); assembler marks affected chains stale; `OptionChainService` reverts *that underlying* to its 30s REST poll until healthy + snapshot replayed |
| Plan-gated / degraded Upstox feed | on subscribe, assert `FirstLevelWithGreeks`/`marketFF` (has oi/iv/greeks), not degraded `IndexFullFeed`/`ltpc` | fall to **Fyers OI/LTP + BS greeks** (zero greek loss); this is also how the ⚠️ Upstox plan-gating question resolves at runtime |
| Per-symbol staleness | per-strike `last_tick_at`; `get_live_mark` already rejects ticks >30s | mark strike `ws_stale`; consumer marks fall back to cached chain / 60s scan-cadence mark; surface via `LiveMarkBadge` |
| Cap pressure | manager tracks unique count vs pool | shed **outermost leased strikes first** (never held, never ATM±0/±1); priority `indices ±8 > held > S1 legs > stock ATM±1`; overflow stays on REST |
| Cross-wired tick (Fyers hazard) | `MAX_LIVE_DIVERGENCE_RATIO=4.0` (existing) | reject as `scan_guarded`; do not mark live — **must survive the marks-to-live_tick change**; Upstox-vs-Fyers divergence is an extra alarm |
| `prev_oi` missing | no 09:15 snapshot + no pdoi | report `oi_change` null rather than wrong |
| Chain-wide analytics | — | PCR/max-pain/GEX **always** read `option_chain_snapshots` fed by REST; a WS outage cannot blind them |

**Fail direction:** marks fail *closed* (stale → retain scan mark, never present stale as live); analysis fails *open* to REST. **Shadow-validate before every cutover** — run WS and REST in parallel for a session, compare (LTP, OI, greeks, resulting ATM pick, 30m bar closes) before flipping read precedence; divergence beyond a strike-step blocks promotion. Per the owner rule: none of this touches broker creds, no `docker compose down -v`, flag flips on the normal restart cadence **outside NSE hours (09:15–15:30)**.

---

## 10. Phased, flag-gated migration (build order)

Every phase is shadow-first, reconciled against REST, and reversible by flipping one flag. REST never stops running as the fallback spine, so rollback is instantaneous and lossless.

**Reused flags:** `OPTION_WS_SUBSCRIPTIONS_ENABLED` (existing dry-run gate on the subscribe path), `CHAIN_CANDLE_BUILDER_ENABLED`, `FYERS_TBT_DEPTH_ENABLED` (stays OFF).
**New flags:** `STREAM_CHAIN_ENABLED`, `STREAM_CHAIN_UNDERLYINGS` (starts = 5 indices), `STREAM_CHAIN_BAND` (`indices_pm8_stocks_pm1`), `STREAM_CHAIN_GREEKS_SOURCE` (`bs`|`upstox_native`), `STREAM_CHAIN_WATCHLIST_DERIVE`, `STREAM_CHAIN_WRITE_CANDLES`, `WS_CHAIN_FALLBACK_REST` (default on).

| Phase | Scope | Flag state | Exit gate (promote when…) |
|---|---|---|---|
| **P0 — Parse + model (no new transport)** | Extract Upstox `optionGreeks`+sibling `iv` in `build_tick` (already on the `full`-mode wire, discarded today); parse Fyers `pdoi`→prev_oi; add `iv/δ/γ/θ/ν/prev_oi` slots to `Tick`. **Run the empirical probes:** Upstox `option_greeks` on one live NSE_FO key (plan-gating + degraded-payload); Fyers oi/book confirmed on tick; observe oi/iv cadence. | all off (parse-only, inert until read) | greeks/iv/pdoi verified on live ticks; plan-gating answered; Fyers oi/book confirmed |
| **P1 — Assembler shadow (indices)** | `StreamingChainAssembler` for 5 indices ATM±8 (170 keys); assemble chain, derive ATM, compare to REST-derived ATM + REST greeks; log divergence. No consumer reads it. | `STREAM_CHAIN_ENABLED` (indices); `WATCHLIST_DERIVE=off`; `WRITE_CANDLES=off` | derived ATM matches REST within a strike-step ≥N sessions; BS-vs-Upstox greeks within tolerance |
| **P2 — Indices live** | Flip indices watchlist derivation + marks + `oc:` cache to streamed; extend `/ws/quotes` + `/ws/chain` for index legs; start greeks-bearing tick→bar candles for indices. REST runs parallel as fallback+oracle. | `WATCHLIST_DERIVE=on` (indices), `WRITE_CANDLES=on`, `GREEKS_SOURCE=upstox_native` if verified else `bs` | S1/directional/marks stable on streamed data ≥N sessions; PnL parity with REST book |
| **P3 — Widen to stocks ATM±1** | Add 217 stocks ATM±1 (→ ~1,472 total, one Fyers key ~30%); stocks stream price+oi + BS greeks; derive+roll stock watchlist. **Chain-wide analytics + deep wings stay REST.** | band = `indices_pm8_stocks_pm1` | pool stays <50% under drift; REST relief measured |
| **P4 — Retire steady-state REST poll** | `refresh_atm_premium_candles` + 434-priority top-up become **gap-heal/fallback only** for streamed underlyings; ~50k+ Fyers polls/day and ~5.5k Upstox chain calls/session recovered. Deep-wing GEX sweeps, daily discovery, cold-start backfill remain. | `WS_CHAIN_FALLBACK_REST` stays on | measured REST headroom; clean rollback drills passed |

**Rollback:** flip `STREAM_CHAIN_ENABLED` (or the per-scope derive flag) off → consumers revert to the REST-fed `oc:` cache and `atm_option_watchlist_snapshots` exactly as today.

---

## 11. Risks & open questions

**Hard limits this design accepts (does not fight):**
- **Full-universe ATM±8 (7,616) is out of reach** on a single standard Fyers key (exceeds the 5,000-unique-per-key pool; needs 3 Upstox/Plus conns). Target = hybrid ~1,472 band. Wings stay REST.
- **Chain-wide PCR / max-pain / GEX cannot be streamed** — structural, permanent REST sweep.
- **Stock deep chains stay REST** — budget blow-up if streamed wide.
- **`prev_oi` not streamed** — daily 09:15 snapshot; `oi_change` is vs-daily-open.
- **30m/15m MACD needs bars, not ticks** — aggregation is mandatory work.
- **Greeks/IV cadence undocumented** — treat as slow-moving; never present as tick-synchronous.
- **Fyers cross-wiring hazard** — the 4.0× divergence guard is the only defense; must survive marks-to-live_tick.

**Must-resolve-before-promotion (empirical, each gates only its phase):**
1. **⚠️ Upstox greeks on Standard tier?** Subscribe one NSE_FO key in `option_greeks`; confirm `FirstLevelWithGreeks` (oi/iv/greeks), not degraded `IndexFullFeed`/`ltpc`; record plan tier. Gates `GREEKS_SOURCE=upstox_native`; Fyers+BS path unaffected. (P0)
2. **⚠️ Upstox `iv` unit** (fraction vs percent) — measure one known contract; set the normalize-at-parse rule. (P0)
3. **⚠️ Does the Fyers option tick populate `oi`+bid/ask per frame** (vs only on-change / only via the 30s chain poll)? The picker liquidity-lift and QuoteGrid columns depend on it. Log live NFO ticks. (P0)
4. **⚠️ Greeks/IV inter-update latency** during market hours — decides if streamed greeks can drive intra-bar logic or only bar-close context. (P0)
5. **⚠️ Fyers max connection count per key** — only matters if we ever need >1 socket; the hybrid band fits one, so off the critical path. (before any P4 widening)
6. **Does a per-session (not lifetime) PnL attribution consumer of the marks exist?** If not, add one to realize the measurement payoff (orthogonal to this data layer).
7. **`greeks_enrichment` daemon live status** (gated-on but pending next restart, uncommitted per 2026-07-06 memory) — the assembler's write-time greeks supersede it for streamed legs, but reconcile coverage overlap; it still covers non-streamed history.
8. **Realized broker-call rate** from limiter snapshots vs the 54k/session *worst-case* ceiling — sharpens the true REST-relief number.

**Net effect:** the hybrid band (~1,472 keys, one Fyers connection at ~30% of the pool + one Upstox `option_greeks` overlay on the 5 index chains) removes the ~54k/session worst-case Fyers premium polls and ~5.5k/session Upstox chain calls for the tradable band, feeds analysis/tracking/performance from one live tape, rolls the ATM watchlist continuously with spot, and confines REST to cold-start, deep-wing chain-wide analytics, the stock deep universe, daily discovery, and a one-flip standby.


---

# Appendix — Adversarial verification outcomes

_Each feasibility claim the design relies on was independently re-verified against broker docs + code by a skeptic agent. Verdicts below; the ones that change the design are called out._


### 1. [CONFIRMED]

**Claim:** Upstox V3 WS in option_greeks mode delivers oi, iv, and delta/gamma/theta/vega/rho for a live NSE_FO option instrument_key on the account's CURRENT plan tier — i.e. the feed returns FirstLevelWithGreeks/MarketFullFeed, not a degraded IndexFullFeed/ltpc payload.


**Correction / note:** Confirmed, but two precision notes for the design: (a) option_greeks mode returns the FirstLevelWithGreeks payload (top-of-book + greeks/oi/iv), while MarketFullFeed is the separate 'full'-mode payload — both carry greeks/OI/IV, so use whichever; the claim's slash-conflation is harmless. (b) The docs make plan tier moot (option_greeks is ungated on all tiers), but WS greeks have not been empirically observed on THIS account because build_tick discards them today; to fully close the loop, subscribe one NSE_FO key in option_greeks mode on the live token and confirm a firstLevelWithGreeks payload with non-zero oi/iv/delta arrives (and that it is not full_d30, which WOULD require Plus).


### 2. [CONFIRMED]

**Claim:** Upstox option_greeks single-mode per-connection cap is 3000 instruments (2000 when mixing modes on one connection); full mode is 2000/1500; full_d30 has a hard 50-instrument per-user ceiling; connections are 2 (standard) / 5 (Plus).


### 3. [CONFIRMED]

**Claim:** Fyers v3 WS enforces 5000 unique symbols per API KEY, deduped across connections — opening additional sockets does NOT raise the ceiling, so 7616 (universe ATM±8) is infeasible on a standard key while the hybrid ~1472 band (~1700 with spots) fits one key at ~34%.


**Correction / note:** Minor, non-refuting caveat: the official KB does not state in exact words that "opening more connections cannot raise capacity" — it states the 5000 cap is per API key with cross-subscription dedup and recommends subscription management. That per-key framing logically forecloses raising the ceiling via extra sockets, but Fyers does not publish a hard max-connections-per-key number, and this specific inference was not empirically A/B-tested on the live account. Also, the specific "~1472" hybrid-band figure is design-specific and not independently in the ground truth (nearest ground-truth near-money band is ATM+/-1 = 1,344); this does not affect feasibility since anything in the 1,344-1,700 range is well under 5,000. To fully close the one open item, subscribe >5000 unique symbols split across two sockets on the live key and confirm the second socket's subscriptions are rejected/deduped.


### 4. [UNCERTAIN]

**Claim:** Fyers SymbolUpdate frames carry oi and pdoi per option tick during market hours (not only on OI change, and not only via the 30s REST chain poll).


**Correction / note:** Corrected, defensible statement: "The Fyers v3 SymbolUpdate schema DEFINES oi and pdoi fields for F&O instruments, and oi is delivered over the WebSocket (not only via the 30s REST chain poll) — the adapter reads it per tick. However, whether oi/pdoi is present on EVERY SymbolUpdate frame or only on frames where open interest changes is UNDOCUMENTED and unverified; do not assume guaranteed per-frame OI." To resolve: log live NFO option ticks during market hours (09:15-15:30 IST) and check whether the `oi` key is present on every SymbolUpdate frame or only on a subset, and whether `pdoi` is ever populated (the repo currently reads only `oi`). Authoritative field semantics live on the JS-rendered myapi.fyers.in/docsv3 SPA (not fetchable here) or via Fyers support.


### 5. [CONFIRMED]

**Claim:** Fyers WS carries NO greeks and NO iv in any message shape; greeks are producible only app-side via analytics.greeks (implied_volatility → bs_greeks, r=0.065) from streamed ltp+spot+strike+T, matching today's REST-chain greeks exactly.


**Correction / note:** Minor precision caveat on the word "exactly": the match is exact by construction (same BS routine, same r=0.065, same input set) only when fed identical ltp/spot/strike/T. In live operation, streamed ltp/spot are sampled at a different instant than a REST snapshot (and WS spot comes from the index tick vs the chain's underlying spot), and T advances between the two reads, so the numeric greeks will diverge slightly tick-to-tick. The METHOD is identical and no greeks feed is lost by streaming; live VALUES are reproducible-to-identical, not necessarily bit-identical. One residual uncertainty: the primary Fyers doc (myapi.fyers.in/docsv3, JS-rendered) can't be machine-fetched, so the "no greeks/iv on WS" rests on the decoded SDK interface + adapter code + sample code — three concordant sources, but not the rendered primary page.


### 6. [CONFIRMED]

**Claim:** _select_liquid_atm_strikes, run against a chain view assembled purely from streamed {spot, static strike ladder, per-strike per-side volume+oi}, reproduces the same CE/PE ATM pick as the REST-chain path — shadow-compare agrees within one strike step across >=N sessions.


**Correction / note:** Mechanism is confirmed; the empirical clause is still unmeasured. Run the shadow-compare with a shared spot source for both paths and report a match-RATE (not pass/fail), and set N; watch the three >1-strike tail cases (anchor-zero-liquidity max-over-window branch, early-session volume==0 oi/100 fallback, lift near-tie timing flips).


### 7. [REFUTED]

**Claim:** prev_oi is not streamed by either broker (only current oi); a once-daily 09:15 REST open snapshot is required to preserve oi_change/oi_change_pct, and Fyers pdoi is prior-session (not intraday-open).


**Correction / note:** Fyers DOES stream previous-day open interest on the v3 data WebSocket via the `pdoi` field ("Previous day open interest"), sibling to `oi` — verified in the extra-fyers MarketData interface and the ground-truth fyers-ws payload finding. Since this codebase computes oi_change = current_oi − prev_oi with prev_oi = previous-day OI (REST chain prev_oi, atm_watchlist.py:1551-1556; fyers.py get_option_chain opt.get("prev_oi")) — the standard NSE prior-session-close baseline, which equals pdoi — oi_change/oi_change_pct can be reconstructed entirely from the Fyers WS tick (oi + pdoi) with NO once-daily 09:15 REST snapshot. The current code just needs to extract pdoi in _handle_tick (it reads only `oi` today) and add a prev_oi slot to the Tick dataclass (base.py:164). The snapshot-required claim is correct ONLY for Upstox (V3 WS protobuf MarketFullFeed/FirstLevelWithGreeks carry oi+iv but no previous-OI field), and even there the proper baseline is the previous session's CLOSING OI, not an intraday 09:15-open OI snapshot. Fyers pdoi being "prior-session" is exactly what the metric needs, not a shortfall.


### 8. [UNCERTAIN]

**Claim:** Chain-wide PCR, max-pain, and gamma-exposure require deep-OTM OI on every strike both sides and CANNOT be reproduced from a streamed ATM±N band — they remain permanently REST-fed into option_chain_snapshots.


**Correction / note:** Split the claim into its true and false halves. TRUE: PCR/max-pain/GEX sum over the whole chain (both sides, deep-OTM included), so a NARROW streamed ATM±N band cannot reproduce them — you'd stream only the near-money slice. FALSE/overstated: "cannot be streamed" and "permanently REST-fed." Deep-OTM OI is on the WebSocket wire for both brokers (Upstox full/option_greeks 'oi'; Fyers SymbolUpdate 'oi'), so a FULL-CHAIN WS subscription (all strikes both sides) could feed these analytics — and for the 5 index underlyings that option_chain_snapshots actually covers, the full-chain strike count fits within Fyers' 5000/conn and Upstox's 3000-key option_greeks caps. REST remains the sensible choice for pragmatic reasons — one call returns the whole band, no client-side per-strike contract enumeration/rollover management, and it scales to the 217-stock universe that WS would blow up (~7.6k+ instruments for even an ±8 band) — but that is a design/convenience decision, not a hard technical limit. Correct statement: "Chain-wide PCR/max-pain/GEX need whole-chain OI and cannot come from a NARROW ATM±N stream; REST remains the practical feed (single-call convenience + stock-universe scale), though for indices a full-chain WS subscription is technically capable of supplying the same OI."
