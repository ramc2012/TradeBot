# ADR-0043 — Unify the Substrate Beneath the Strategy Policies

**MarketContext · UnderlyingRef/VenueInstrumentRef/ContractRef · EvaluationResult · DataCapabilities + FeatureQuality · Hierarchical Shared Risk · Canonical Event Ledger · Selection Universe vs Subscription Plan**

- **Status:** PROPOSED — REVISION REQUIRED (rev 2, review amendments incorporated 2026-07-19)
- **Date:** 2026-07-19 (rev 2). Original draft: Sat 2026-07-18 night, market CLOSED, fully read-only.
- **Repo root:** `/Users/ramachandran/CLAUDE PROJECTS/Nomad Curie/TradeBot` (note the space in the path; all `file:line` citations below are under `<root>/backend/…` unless prefixed otherwise).
- **Supersedes/absorbs:** the three internal substrate maps compiled 2026-07-18 (Data+Universe, Signal+Risk+Ledger, Feature+DataQuality). This ADR is their single decision-grade synthesis.
- **Scope:** the SUBSTRATE only — instrument universe, market-data context, feature computation, evaluation contract, risk, execution/ledger, UI read model. The four strategy POLICIES (Auction, MP+OF, Convergence, Directional) stay separate; this ADR makes them plug into shared rails, it does not merge them.
- **Author's note to the owner:** every consolidation in here is grounded in a real `file:line` or a live DB count. Wherever a "refactor" would actually change trading behavior, that is called out explicitly and defaulted OFF — see §8 and §12.

---

## Revision history

| Rev | Date | Change |
|---|---|---|
| **v1** | 2026-07-18 | Original synthesis. Proposed `InstrumentRef` (single conflated identity), `MarketContext` keyed by `(instrument, as_of)`, a **scalar 5-tier capability ladder** (`REAL_L2 > REAL_L1_TRADES > REAL_TICKS > BAR_PROXY > UNAVAILABLE`), `SignalIntent` union (with the store refusing to build one below floor / returning `None`), a shared risk governor rejecting a global kill, a canonical ledger built by **promoting `agent_positions` + `paper_trade_book`** and dual-writing, a tiered universe, and a 7-step migration with "N-session" parity gates. |
| **v2** | 2026-07-19 | Senior architecture review APPROVED THE DIRECTION but marked it **REVISION REQUIRED** with 5 P0 + 3 P1 blocking amendments. All incorporated. Superseded designs purged. See the 8 amendments below. |

**Amendments addressed in v2:**

1. **P0-1 — Capability is a SET, not a monotonic ladder.** The scalar ladder is deleted. Replaced with `DataCapabilities.available` (a set) + a `FeatureQuality.derivation` grade (`OBSERVED | RECONSTRUCTED | MODELLED | BAR_INFERRED`). Sufficiency gates now match capability sets + derivations + coverage/age (§2.3, §6). Greeks are a DataCapability with their own FeatureQuality, never stamped "REAL" through an order-flow enum.
2. **P0-2 — Instrument identity split into THREE objects.** `InstrumentRef` is deleted. Replaced with `UnderlyingRef` / `VenueInstrumentRef` / `ContractRef` + a `ContractResolver.resolve(underlying, purpose, as_of)`. Front-month is an as-of resolution, not immutable identity. Four distinct price-increment kinds named (§2.1).
3. **P0-3 — `SignalIntent` replaced by `EvaluationResult`.** Multi-leg, state-carrying (`WATCHING|ARMED|ACTIONABLE|BLOCKED|EXITING`), bias≠leg preserved. The feature store never "refuses to build" / returns `None` — the **policy runner** validates requirements and emits a visible `BLOCKED` result (§2.4, §2.5).
4. **P0-4 — Do NOT promote existing tables into the canonical ledger.** A NEW idempotent event ledger (`strategy_accounts, order_intents, orders, fills, position_events, cash_events` + materialized `positions`/`account_balances`) is designed instead, with a transactional outbox and full reconciliation. Existing books stay authoritative until parity (§4).
5. **P0-5 — MarketContext key + feature-store ownership defined.** `(instrument, as_of)` was insufficient; replaced with an 8-field deterministic key and explicit per-plane ownership / single-writer / TTL / stampede / budget / fail-closed rules (§2.2).
6. **P1-6 — Unified UI read model added as an EARLY phase** (§5), before strategy cutover; it is the human validation surface for shadow parity.
7. **P1-7 — Universe cost broadened beyond REST; selection universe separated from the subscription plan; held/at-risk instruments pinned** (§6).
8. **P1-8 — Hierarchical risk controls** (global / broker-account / exchange / strategy / instrument / data-source-capability) with cross-lane exposure in the target architecture (§7).

**Resolved repository caution (was a review concern):** the review flagged that the tree might carry uncommitted concurrent edits. It does not — the working tree is CLEAN, all prior edits are committed at **HEAD `3dd91987`** ("frontend: desk LIVE badges tell the truth"). The only working-tree deltas are runtime paper-state JSON files written by the paper engine itself (`runtime/auction_intelligence/*.json`, `runtime/portfolio/daily_*.json`), never source. Migration step 1 (§8) is therefore already DONE.

---

## 1. Context and Decision

### 1.1 The situation today

There is **no shared substrate**. Every lane independently (a) picks its own universe, (b) assembles its own market-data context, (c) computes its own features from raw tables, (d) emits a differently-shaped signal, (e) runs its own risk governor, and (f) writes to its own ledger in its own storage paradigm. The convergence that exists is accidental, not architectural:

- **Two production Market-Profile engines** (`auction_intelligence/market_profile/engine.py:33` bar-TPO `MarketProfileEngine`, reused by Convergence/S2/commodity; and `market_data/market_profile.py:57` tick-TPO `MarketProfileBuilder`, used by S1/agent) recompute the same NIFTY 30-min TPO independently on their own cadences.
- **Four independent loaders of `underlying_spot_candles`**, each with its own bad-bar cleaner: auction `live.py:842` (+`:622`), convergence `service.py:387` (+`:39`), S2 `strategy2_mp_of.py:126` (+`:101`), runtime `market_intelligence_runtime.py:248`.
- **Price increments are literals in ≥3 places** (auction `0.5` in `SYMBOL_MAP`, directional `0.05` at `data.py:291`, convergence default `.05`) while the true sources are `fo_contract_catalog` (exchange order tick) and `commodity_contract_specs` (which deliberately keeps a *separate* profile-bucket tick — see §2.1).
- **Three storage paradigms for the ledger** (JSON files, `agent_positions`+`paper_trade_book` DB, and Directional's own `directional_paper_*` DB schema) with no cross-lane netting, exposure, or reconciliation possible — and no single one is fit to become canonical (§4).
- **Data-capability awareness exists in exactly one lane** — Convergence's `cvd_source ∈ {market_ticks, bar_proxy}` + the `real_tick_cvd` gate (`institutional_convergence/engine.py:326,334,355`). No other lane expresses source capability at all. Note this is already a *set-and-derivation* concept (a CVD value that is observed-from-ticks vs inferred-from-bars), NOT a rung on a total order — which is exactly why the v1 scalar ladder was wrong (P0-1).

We already own the *utilities* (`backend/market_data/`: `live_candle_store`, `quote_bus`, `source_policy`, `data_router`, `broker_circuit`, `atm_watchlist`, `market_profile`, `greeks_enrichment`; plus `brokers/rate_limiter.py`, `core/lane_registry.py`, `core/laneset.py`). What is missing is an object that composes them into a **versioned MarketContext** with declared data capabilities and per-feature quality/derivation, and a **canonical EvaluationResult + event ledger** the policies share.

### 1.2 The decision

Introduce a shared substrate beneath policies that stay separate, via feature-flagged, per-lane, reversible strangler-fig migration:

1. **Three instrument identity objects** — `UnderlyingRef` / `VenueInstrumentRef` / `ContractRef` + a `ContractResolver` (replacing `SYMBOL_MAP` and the ~6 ad-hoc symbol resolvers, and the single conflated `InstrumentRef` of v1). §2.1.
2. **`MarketContext`** — an immutable, **versioned** value object keyed by an **8-field deterministic snapshot key** (§2.2), assembled **compute-once** per `(venue-instrument, feature, watermark)` and handed to every policy that requested it, with explicit feature-store ownership per process plane.
3. **`DataCapabilities` (a set) + `FeatureQuality` (with a `derivation` grade)** — `available ⊆ {OHLCV_1M, TRADE_PRINTS, SIZED_BBO, DEPTH_L2, OPTION_CHAIN, GREEKS, OPEN_INTEREST}`; every feature carries `derivation ∈ {OBSERVED, RECONSTRUCTED, MODELLED, BAR_INFERRED}`, source, freshness, coverage, completeness, and `missing_reason`. §2.3.
4. **`EvaluationResult`** — one typed, multi-leg, state-carrying output every lane's `evaluate()` returns (replacing v1's lossy `SignalIntent`). §2.4.
5. **Hierarchical `RiskGovernor` (shared surface)** — kill/limit levels at global / broker-account / exchange / strategy / instrument / data-source-capability scopes, dispatching to per-policy exit managers and preserving the `SIGNAL_VALIDATION_UNCAPPED` bypass per gate. §7.
6. **A NEW canonical event ledger** — `strategy_accounts, order_intents, orders, fills, position_events, cash_events` + materialized `positions`/`account_balances`, populated by idempotent import workers behind a transactional outbox. Existing books stay authoritative until parity is proven; **no existing table is promoted in place**. §4.
7. **A unified UI read model** delivered early (§5), and a **selection universe distinct from the subscription plan** with held/at-risk instrument pinning (§6).

Each policy implements the strategy interface: `requirements()` (capability sets + allowed derivations + coverage/age it needs) / `evaluate(context) -> EvaluationResult` / `manage(context, position, open_orders, original_thesis) -> list[ManagementAction]`. §2.5.

### 1.3 Why NOT one merged engine (the monolith we are deliberately rejecting)

The external review's own framing is "shared substrate, separate policies," and this is correct for concrete, load-bearing reasons — a single merged strategy engine would:

- **Erase four genuinely different edges.** The lanes are not variants of one strategy: Directional's edge is intraday mean-reversion + options positioning with OF *unavailable by design* (`directional_options/features.py`, `positioning_feed.py:58`); Convergence's edge is a BANKNIFTY-specific real-tick-CVD confirmation that hard-blocks on `real_tick_cvd`; Auction is a 200-320s MP/OF/regime CPU pipeline (`auction_intelligence/service.py:183-188`); MP+OF is a commodity-native tick evaluator ported to indices as a bar-inferred proxy. Merging them forces a single feature contract and a single risk doctrine onto four incompatible horizons and data-sufficiency profiles.
- **Collapse the direction semantics dangerously.** Three encodings exist today — `LONG/SHORT/FLAT` (Auction/Convergence bias), `CE/PE` (Directional option side), `BUY/SELL` (MP+OF). A monolith that treats a bearish *thesis*, a long-put *leg*, and a short-future *leg* as one bucket mis-signs P&L and netting (§8). `EvaluationResult` keeps thesis-bias and per-leg instrument+side+effect distinct precisely to prevent this (§2.4).
- **Re-introduce event-loop seizure.** The lanes run on a single core; S2 and Convergence hand-rolled bar-close-driven throttles/caches specifically to survive it (`strategy2_mp_of.py:604-614`, the 2026-07-13 degenerate-bar incident). One eager merged build per cycle re-creates the exact seizure those caches prevent.
- **Fail the doctrine test.** `SIGNAL_VALIDATION_UNCAPPED` (`core/config.py:229`) is an owner directive with a *per-lane* bypass pattern; a merged risk engine would flatten it (§7).

The substrate is where the reuse lives (instruments, bars, profiles, order-flow, chain/greeks, regime, ledger, risk gates). The **policies stay as pluggable `evaluate()` implementations**. This is the QuantConnect/Nautilus/Hummingbot/Freqtrade shape (§10), not a rewrite.

---

## 2. The Concrete Contracts (field-level, grounded in the union maps)

All contracts are additive in the first instance — every field maps to at least one existing lane so migration adapters are mechanical. `⊕` = a lane already carries it; `→` = derived at the adapter.

### 2.1 Instrument identity — THREE objects + a resolver (replaces `SYMBOL_MAP`, the ~6 resolvers, and v1's single conflated `InstrumentRef`)

v1 conflated root + spot + expiry + strike + future + lot + tick into one `InstrumentRef`. That is wrong: an underlying is stable identity; a venue instrument is a broker/exchange addressable key; a contract is an expiry/strike-bearing tradeable that **rolls**. Front-month is *not* immutable identity — it is a resolution made *as of* a decision time for a *purpose*. Split into three:

**`UnderlyingRef`** — the stable economic thing.

| Field | Consumed by | Current source (file) |
|---|---|---|
| `symbol` (canonical root: NIFTY, INFY, GOLD) | all | `fo_underlying_catalog` / `SYMBOL_MAP` / config |
| `kind` (INDEX/STOCK/COMMODITY) | all | `fo_underlying_catalog.kind`; commodity implicit |
| `sector_code` | convergence diversification | CBE payload |
| `market` (NSE/MCX) | all | catalog / commodity roots |
| `session` (RTH vs MCX 09:00–23:30) | all | hardcoded `live.py:69-72` / `_session_bounds:186` → move to catalog |
| `expiry_calendar` (weekday + `fo_expiry_catalog`) | S1, S2, directional, convergence | `analysis/instruments.py:18` + `fo_expiry_catalog` (3,095 rows) |

**`VenueInstrumentRef`** — an exact broker/exchange-addressable instrument on a venue.

| Field | Consumed by | Current source (file) |
|---|---|---|
| `venue` (UPSTOX/FYERS/exchange) | data_router, quote_bus | routing config |
| `instrument_key` / `spot_instrument_key` / `underlying_key` (Upstox) | S1, directional, convergence chain | `fo_underlying_catalog` (217 rows: 6 INDEX + 211 STOCK) |
| `fyers_symbol`, `app_symbol` | auction, convergence, `data_router` | `market_data/symbols.py` |

**`ContractRef`** — a specific tradeable (future OR option) with a lifecycle.

| Field | Consumed by | Current source (file) |
|---|---|---|
| `instrument_class` (FUTURE/OPTION) | risk/execution | schemas |
| `expiry`, `expiry_kind` (weekly/monthly) | S1, S2, directional, convergence | `fo_expiry_catalog` |
| `strike`, `option_type` (CE/PE) | directional, S1 | `atm_option_watchlist_snapshots` / chain |
| `lot_size` | all | `fo_underlying_catalog` (211/211) / `fo_contract_catalog` / commodity specs |
| `freeze_quantity`, `minimum_lot` | risk/execution | `fo_contract_catalog` (50,482 rows) |
| **`price_increment`** (the exchange ORDER tick) | execution, rounding | **`fo_contract_catalog`** / `commodity_contract_specs.mp_tick_size` |

**`ContractResolver.resolve(underlying: UnderlyingRef, purpose, as_of) -> ContractRef`** — one rollover-safe resolver replacing the 3 inline front-month builders (`live.py:192`, `service.py:253`, `data.index_futures_backfill`). `purpose ∈ {front_month_future, atm_option, weekly_expiry_target, monthly_expiry_target, …}`. The *same* `UnderlyingRef` resolves to *different* `ContractRef`s over time; the front-month symbol is an output of `resolve(…, front_month_future, as_of)`, **never a stored identity field**.

**Four distinct price-increment kinds — name them, never collapse to one canonical `tick_size`:**

| Increment | Meaning | Source | Consumer |
|---|---|---|---|
| **1. Exchange price increment** | the order tick the venue accepts (e.g. NSE 0.05, GOLD 1.0) | `fo_contract_catalog` / `commodity_contract_specs.mp_tick_size` | order rounding, stop/target snapping |
| **2. Market-profile bucket size** | the coarse TPO bucket that makes POC/VAH/VAL meaningful — deliberately ≫ the exchange tick on high-priced contracts | `commodity_contract_specs.mp_value_tick` / `mp_profile_tick()` (`:21-45`) | MP engines |
| **3. Strike interval** | the gap between listed option strikes | `analysis/instruments.py:30` `strike_step` | selector, walls, ATM tracker |
| **4. Display precision** | UI rounding for humans | UI layer | read model (§5) only |

**Proof this must stay separate (ground truth):** `commodity_contract_specs` already carries `mp_tick_size` (exchange, GOLD = 1.0) *and* `mp_value_tick` / `mp_profile_tick()` (the MP bucket) *with a docstring explaining why*: "The exchange tick … is far too fine for a value-area profile on high-priced contracts (GOLD ~143000 at tick 1.0 → thousands of one-rupee TPO levels → POC never concentrates)" (`commodity_contract_specs.py:21-45`). A single canonical `tick_size` erases (2) and silently breaks every commodity profile. (The same object also carries `roll_gap_frac`/`roll_gap_threshold()` — a contract-roll boundary marker — reinforcing that a ContractRef has a lifecycle, not a fixed identity.)

### 2.2 `MarketContext` — versioned, keyed by an 8-field deterministic snapshot key

v1 keyed on `(instrument, as_of)`. That is insufficient for a deterministic, replay-safe, split-process context. The **MarketContext snapshot key** is:

```
MarketContextKey:
  1. event_time_frontier      # newest event timestamp incorporated (tape/tick/bar), the read frontier
  2. completed_bar_watermark  # highest fully-closed bar per timeframe (never a forming bar → no lookahead)
  3. data_revision            # monotonically bumped on any late-arriving correction/backfill to inputs
  4. venue_contract           # VenueInstrumentRef (+ resolved ContractRef where applicable) — venue is part of identity
  5. session                  # session id/date + phase (pre-open / RTH / MCX-evening / post) from UnderlyingRef.session
  6. policy_horizon           # scalp | intraday | swing | positional — drives which watermarks/coverage are required
  7. feature_algo_version     # {feature_name: version} for every feature realized in this context
  8. input_snapshot_ids       # the FeatureQuality.input_snapshot_id set the features were computed from
```

`context_version = hash(all 8 fields)`; a `snapshot_id` pins the exact inputs a decision was computed against (referenced from `EvaluationResult.feature_snapshot_ids[]`). Lazily materialized per `(venue-instrument, feature, watermark)` — never one eager build (§8).

```
MarketContext(key: MarketContextKey):
  bars: {1m, 3m, 15m, 30m, daily}                 # ⊕ S1/S2(1m,3m), auction(30m), convergence(3m), directional(1m→resample); src underlying_spot_candles (+commodity runtime)
  sessions: {current, prior, history[~10]}         # ⊕ auction _group_rows_by_session, convergence _select_rule_sessions:356
  profiles:
    current {poc,vah,val,ib_high,ib_low,tpo}       # ⊕ auction/convergence/S2/commodity; built at the MP bucket tick (§2.1 kind 2), src recomputed in-proc (persisted twin market_profiles UNUSED for live decision)
    prior, composite_htf {weekly,monthly}          # ⊕ commodity HTF gate, auction
  order_flow:                                      # each feature carries its own FeatureQuality (see 2.3)
    cvd, book_pressure, footprint, ofi, toxicity   # ⊕ auction OrderFlowEngine, convergence footprint, commodity/S2
  options:
    atm_chain[strike,ltp,oi,iv,volume,source_broker]# ⊕ directional/S1; src atm_option_watchlist_snapshots (NO greeks columns)
    greeks[delta,gamma,theta,vega,iv,tte]          # ⊕ directional sizing; GREEKS is its own DataCapability (2.3), src option_premium_candles
    call_wall,put_wall,net_pressure,ntm_volx       # ⊕ auction/convergence; src OI on option_premium_candles / NTM VolX
    expiry_targets{weekly,monthly}, expiry_sufficiency{dte,listed?}  # ⊕ S2 select_s2_expiry_targets:196, directional DTE
  regime:
    india_vix                                      # ⊕ convergence _load_india_vix:340, directional, auction sizing; src SectorRotationTracker
    iv_percentile, vol_state                       # ⊕ directional IV sizing curve
    regime_label, sector_rotation, cbe{score,bias,quadrant}  # ⊕ convergence(universe)/directional(positioning); src cbe_scan_results
    positioning{oi_build, directional_bias}        # ⊕ directional (BANKNIFTY-specific), convergence; src directional_positioning_daily
  capabilities: DataCapabilities                   # see 2.3 — the SET available for this venue-instrument at this key
  feature_quality: {feature_name: FeatureQuality}  # see 2.3 — per-feature derivation/coverage/freshness/missing_reason
```

**Feature-store ownership (decisive because of the process split — `laneset.py`).** The Phase-1 split makes ownership a hard architectural decision, not a detail: the **core plane** owns live ingestion (Fyers WS callback stream, `market_ticks`/spot writers, chain/greeks enrichment — `boots_core()`), while the **strategy plane** runs the own-loop agents and consumes Redis/PG (`boots_strategies()`). The ADR therefore DECIDES:

- **Who computes each feature.** Ingestion-coupled features (tick CVD/footprint from the live tape, L1/L2 book, greeks enrichment) are computed **only on the core plane** (single writer — it is the only plane with the WS callback). Pure-CPU derivations over already-persisted bars/profiles (MP TPO, regime, bar-inferred OF) may be computed on **either** plane but are **written by a single leader** (see next).
- **Single-writer / leader-lock.** Every cached feature has exactly one writer identity `(plane, feature_key)`; a Redis leader-lock (`SET NX PX`) elects the writer; non-leaders read-only. This prevents the strategy plane and core plane both recomputing (and disagreeing on) the same NIFTY 30m TPO.
- **Redis TTL + eviction.** Per-feature TTL keyed to the feature's natural cadence (bar-close for MP, tape-cadence for OF, chain cadence for greeks); eviction is LRU within a declared memory budget; a stale-but-present value is served with `freshness=stale` (never silently as fresh).
- **Cache-stampede protection.** Recompute is guarded by the leader-lock + a short "compute-in-progress" sentinel so a cold key under N concurrent readers triggers ONE build, not N (the 200-320s auction block must never fan out).
- **Memory + serialization budgets.** Each feature declares a max serialized size; oversized payloads (the F-18 `app_runtime_state` blob lesson) are rejected at write, not discovered at read.
- **Behaviour after a provider failure — FAIL CLOSED for execution.** If a required feature's provider errors or its cache is cold/stale past `maximum_age`, the policy runner emits a `BLOCKED` `EvaluationResult` (§2.4). It does **NOT** silently fall back to the lane's old in-process compute path for an EXECUTION decision — that is split-brain (two code paths, two answers, one book). A named degraded mode is permitted ONLY when the policy's `requirements()` explicitly allows it (e.g. an MP_OF_BAR_PROXY policy that declares `allowed_derivations` includes `BAR_INFERRED`). Read-only surfaces (UI, shadow) may show the fallback, clearly labelled.

### 2.3 `DataCapabilities` (a SET) + `FeatureQuality` (with a derivation grade) — replaces the v1 scalar tier ladder

**The v1 monotone ladder (`REAL_L2 > REAL_L1_TRADES > REAL_TICKS > BAR_PROXY > UNAVAILABLE`) is DELETED.** It was wrong: depth updates, executed trade prints, and sized BBO are **orthogonal** data facts, not rungs on one total order. A venue can push sized BBO without a trade tape, or a trade tape without per-level depth; "L2 > L1 > ticks" implies a containment that does not exist.

**`DataCapabilities`** — what data actually exists for a venue-instrument at a MarketContext key:

```
DataCapabilities:
  available: set[Capability]   # subset of:
    OHLCV_1M        # completed 1-minute bars
    TRADE_PRINTS    # executed trades (aggressor tape)
    SIZED_BBO       # top-of-book bid/ask WITH sizes (L1)
    DEPTH_L2        # per-level book
    OPTION_CHAIN    # chain quotes/OI
    GREEKS          # delta/gamma/theta/vega/iv/tte  (a capability in its OWN right — never inferred from an OF enum)
    OPEN_INTEREST   # OI
```

**`FeatureQuality`** — attached to every feature the store realizes:

```
FeatureQuality:
  feature_name, timeframe
  derivation: enum { OBSERVED, RECONSTRUCTED, MODELLED, BAR_INFERRED }
    # OBSERVED     — read directly from the source of truth (broker tick, broker greeks snapshot)
    # RECONSTRUCTED— rebuilt from a lower-level observed stream (CVD signed from a real trade tape + book)
    # MODELLED     — computed by a model (greeks from a pricing model when the broker did not supply them)
    # BAR_INFERRED — inferred from bars because no finer stream exists (bar-proxy CVD/OF)
  source: str                       # market_ticks | tick_reconstruction_book | bar_inference | option_chain_snapshot | pricing_model | ...
  event_time                        # when the underlying market event occurred
  observed_time                     # when WE received it
  available_time                    # when it became readable in the store (ingest + write latency)
  freshness: { age_seconds, ok }    # ok = age<=maximum_age AND not frozen (data_quality_agent.py:131-147)
  coverage: float|null              # fraction of the required window actually covered (e.g. covered_bars/total_bars)
  completeness: float|null          # fraction of expected fields populated (e.g. chain strikes present, greeks non-null)
  frozen: bool                      # value-frozen detection (DataQualityAgent) — a stuck tape is not fresh
  input_snapshot_id, feature_version
  missing_reason: str|null          # WHY a required capability/derivation/coverage is not met
```

**Policy requirements become a predicate over the set + derivations + coverage/age, not a tier floor:**

```
Requirements(feature="order_flow.cvd",
             requires_all={TRADE_PRINTS, SIZED_BBO},   # capabilities that MUST be present
             allowed_derivations={OBSERVED, RECONSTRUCTED},  # BAR_INFERRED refused for this policy
             minimum_coverage=0.90,
             maximum_age=5s)
```

Convergence's real-tick-CVD gate is exactly this: `requires_all={TRADE_PRINTS}`, `allowed_derivations={OBSERVED, RECONSTRUCTED}` (its `bar_proxy` is `BAR_INFERRED`, refused). Greeks sizing is `requires_all={GREEKS}`, `allowed_derivations={OBSERVED}` for the broker-snapshot path or `{OBSERVED, MODELLED}` if a policy accepts model greeks — decided per policy, **never** by an order-flow enum.

**Mechanical mapping from what exists today (no new judgment needed):**

| Lane / feature | Today's label (file:line) | Capabilities present | Derivation stamped |
|---|---|---|---|
| Convergence CVD | `cvd_source=="market_ticks"` (`engine.py:326`) | TRADE_PRINTS (+SIZED_BBO where L1) | RECONSTRUCTED |
| Convergence CVD (fallback) | `cvd_source=="bar_proxy"` | OHLCV_1M only | BAR_INFERRED |
| Auction OF (book path) | `order_flow_source` `tick_reconstruction[_book]` (`live.py:1292`) | TRADE_PRINTS + SIZED_BBO | RECONSTRUCTED |
| Auction OF (bar path) | `bar_inference` (`live.py:1320`) | OHLCV_1M | BAR_INFERRED |
| Auction "depth ladder" | synthetic decay ladder anchored to `total_buy_qty` (`live.py:1462-1484`) | **NOT DEPTH_L2** (fabricated) — SIZED_BBO at most | MODELLED |
| Commodity OF | `of_source` from `tick_signed_volume_overrides` (`commodity_mp_signal.py:264,1112`) | TRADE_PRINTS where covered | RECONSTRUCTED (coverage<1 → BAR_INFERRED on the gap) |
| S2 OF | evaluator without ticks → `of_source="bar_inference"` (`strategy2_mp_of.py:632`) | OHLCV_1M | BAR_INFERRED |
| Directional OF | none (positioning is the proxy) | — | n/a (`missing_reason="of_unavailable_by_design"`) |
| Greeks (indices) | broker snapshot copy, IV %→fraction (`greeks_enrichment.py:100-159`) | GREEKS + OPTION_CHAIN | OBSERVED |
| Greeks (stocks) | no chain source (`greeks_enrichment.py:57-63` maps 5 indices only) | — | n/a (`missing_reason="no_chain_snapshot_for_underlying"`) |
| Option candle IV | 97% NULL live (§3.4 DB) | OPTION_CHAIN partial | n/a (`missing_reason="option_greeks_rest_null"`, completeness≈0.03) |

**DB reality (live, Sat 2026-07-18):** `market_ticks` last 2d = 152 symbols / 1.99M rows / **84% carry real L1 sizes** (`bid_qty>0 & ask_qty>0`) — so `TRADE_PRINTS` and `SIZED_BBO` are genuinely present for that cohort. The **only structurally-absent capability is `DEPTH_L2`** (no per-level book is persisted anywhere; the only real depth is `data_router.subscribe_depth:734`, ref-counted to auction book symbols, un-persisted). Note this is a *missing capability*, not a "top tier" — no policy that needs `TRADE_PRINTS`/`SIZED_BBO` is affected by `DEPTH_L2` being absent.

### 2.4 `EvaluationResult` — the union output (replaces v1 `SignalIntent`)

v1's `SignalIntent` was lossy in four ways the review flagged: (a) BUY-future ≠ BUY-call collapsed into one `direction` enum; (b) `FLAT` is a lifecycle *state*, not a direction; (c) one thesis can imply *multiple legs* a single-instrument shape cannot carry; (d) a blocked/watching evaluation was forced to return `None`, hiding *why*. Replace with a state-carrying, multi-leg envelope:

```
EvaluationResult:
  strategy_id                     # ⊕ all (registry key; agent_positions.strategy_key)
  strategy_version                # → pins per-lane policy version (A/B + reconciliation)
  as_of, feature_snapshot_ids[]   # → the MarketContext key + FeatureQuality snapshot ids this was computed against

  state: enum { WATCHING, ARMED, ACTIONABLE, BLOCKED, EXITING }
    # WATCHING   — thesis forming, no action
    # ARMED      — conditions nearly met; pre-positioned
    # ACTIONABLE — intents[] should be acted on now
    # BLOCKED    — a requirement failed; blockers[] says which (NEVER returned as None)
    # EXITING    — thesis invalidated; manage()/exit path owns it

  thesis:
    underlying: UnderlyingRef
    bias: enum { BULLISH, BEARISH, NEUTRAL }   # the VIEW — distinct from any leg's side
    confidence: float[0,1]                     # ⊕ Directional/S1/MP+OF/Auction; → Convergence: normalize score/100 but keep raw in evidence
    horizon: enum { scalp, intraday, swing, positional }
    validity: {as_of, expires_at}              # when the thesis goes stale

  intents: list[Intent]                        # ZERO..N legs — a bearish thesis may emit a long-put OR a short-future, or a spread
    Intent:
      instrument: ContractRef                  # the exact leg (§2.1) — future vs option is explicit here, not in a collapsed enum
      side: enum { BUY, SELL }
      effect: enum { OPEN, CLOSE, REDUCE, HEDGE }
      target_quantity | target_exposure        # sizing INTENT, not the sizing decision (that is risk's job)
      order_preferences: {type, limit_offset, tif, slippage_budget}

  evidence: list[Evidence]        # ⊕ ALL: rationale/reasons/selection_reason/signal_reason unioned; carries FeatureQuality refs
  blockers: list[Blocker]         # each: {requirement, observed, missing_reason} — populated iff state==BLOCKED
  conviction_extras: {}           # ⊕ Directional (p_up, jump_score, tail_probability); Convergence (score, quality A+/VALID); null elsewhere
  risk_hints: { iv_sizing_factor, risk_fraction, entry, stop, target1, target2, reward_risk }  # ⊕ per lane; advisory to risk
```

**Why this preserves the load-bearing distinctions (§8):** a BEARISH `thesis.bias` with an `Intent{instrument: <PE ContractRef>, side: BUY, effect: OPEN}` is a **long-premium put** — balance-sheet-long an option; a BEARISH thesis with `Intent{instrument: <FUT ContractRef>, side: SELL, effect: OPEN}` is a **short future** — balance-sheet-short. These net oppositely and P&L-sign oppositely; v1's single `direction` enum could not tell them apart. `state=BLOCKED` + `blockers[]` replaces the "store returns None" anti-pattern (P0-3): a blocked evaluation is *visible* in the read model (§5) with its reason.

### 2.5 The strategy interface — and the policy runner validates, NOT the store

```
class StrategyPolicy(Protocol):
    def requirements(self) -> Requirements:
        # capability SETS + allowed derivations + minimum_coverage + maximum_age per feature (§2.3).
        # This is DECLARATION only. It does not fetch, gate, or decide.

    def evaluate(self, context: MarketContext) -> EvaluationResult:
        # pure decision over a pre-built context. No IO, no raw-table fetches, no feature compute.
        # (auction analyze() is already pure CPU: service.py:61,183)
        # ALWAYS returns an EvaluationResult — WATCHING/ARMED/ACTIONABLE/BLOCKED/EXITING — never None.

    def manage(self, context: MarketContext, position: Position,
               open_orders: list[Order], original_thesis: Thesis) -> list[ManagementAction]:
        # LANE-OWNED exits stay here: Convergence CVD-reversal, Directional flip-confirmation,
        # Commodity cooldowns, Auction exit-confirmation cycles. Returns 0..N actions
        # (amend order, reduce, close leg, roll). The shared governor never closes a position.
```

**Ownership of validation (P0-3, critical).** The **feature store SUPPLIES data** — it never "refuses to build" a result and never returns `None`. The **policy runner** (the component that drives a policy each cycle) does the validation: it reads the policy's `requirements()`, checks them against the `MarketContext.capabilities` + `feature_quality`, and:

- if satisfied → calls `evaluate(context)` and passes the result through;
- if a required capability/derivation/coverage/age is not met → emits a `BLOCKED` `EvaluationResult` with `blockers[]` populated, **without** calling `evaluate()` (so `evaluate()` bodies never see insufficient data).

This generalises today's `real_tick_cvd` / `execution_ready` gates into one visible, uniform mechanism. Every place the v1 §1/§2 text said "the store refuses to build a SignalIntent" / "returns `None`" is replaced by this runner-emits-`BLOCKED` rule.

---

## 3. Current-State Duplication and Per-Lane Substrate (file:line)

### 3.1 Per-lane substrate table

| Lane | Universe (a) | Market-data assembly (b) | Instrument metadata (c) | Signal emit | Risk governor | Ledger |
|---|---|---|---|---|---|---|
| **Auction (index)** | static `live.py:80 SYMBOL_MAP` (5 idx + CRUDEOIL) | `live.py:266 build_live_analysis` → spot `:842`, OF `:1209/:1327` (+depth book), MP in-proc `MarketProfileEngine`; `service.py:61 analyze()` pure CPU | lot/tick **hardcoded in SYMBOL_MAP**, session `live.py:69` | `AnalysisBundle`/`AgentDecision`/`ExecutionInstruction` (`schemas.py:210,232,278`) | `risk/governor.py:11` | JSON `runtime/auction_intelligence/paper_positions.json` (`paper/book.py:73`) |
| **Auction (commodity)** | `commodity.py:132` roots GOLD,SILVERM,CRUDEOIL | `:180 _load_commodity_minute_rows` + shared OF helpers | `commodity_contract_specs.py` (8 MCX) | same as auction | same | 2nd JSON `PaperPositionBook` |
| **Convergence (NSE)** | CBE-selected `service.py:217` (~12 sym: `select_diversified_stocks:81` + indices `:75`) | **6 SQL/symbol** in a per-symbol loop (`service.py:384 _load_rule_inputs`: spot `:387`, ticks×3 `:401/:420/:424`, walls `:454`, catalog `:464`) ≈ 72 q/cycle; MP reuses auction engine `:480` | lot/tick `fo_contract_catalog:464`, future **inline** `service.py:253` | `engine.py:422 evaluate_rules()` → `dict` | `paper.py:200 _circuit_state` | JSON `runtime/institutional_convergence/paper.json` (`paper.py:24`) |
| **Convergence (MCX)** | 8 MCX roots `commodity.py:249` | front-month resolution | `commodity_contract_specs` | same | same | JSON `commodity_paper.json` |
| **MP+OF / S2** | none — per-underlying by S1 (`strategy_agent.py:3462`) | own SQL `strategy2_mp_of.py:126`, expiries `:263`, own MP `:402`, prior `:473` | expiry static `:47`, lot/tick from S1 rows | `:380 shape_result_for_s2()` → `dict` | inherits S1 | `agent_positions` + `paper_trade_book` (via S1) |
| **Commodity MP+OF** | static JSON `commodity_strategy.json` (`:163`) | `load_commodity_history_rows` (tick-first CVD/footprint) | `commodity_contract_specs.py` | `commodity_mp_signal.py:214/1015` → `dict` | `commodity_strategy_agent.py:1974 _entry_risk_block` | state file **+** `agent_positions`(commodity) **+** `paper_trade_book` |
| **Directional** | static 3 idx (`config.py:24`) + NIFTY-50 ∩ catalog (`data.py:306`), ranked/rotating/readiness-gated (`service.py:175-227`) — **the good pattern** | `data.py:138 load_live_spot_frame` + `:178 list_live_contract_snapshots`; **batched** readiness `:318` (2 grouped q for whole batch) | lot COALESCE both catalogs `:232`; **`price_increment` hardcoded `0.05` `:291`** | `signals.py:276 DirectionalSignal` + `ContractCandidate` | `risk.py:36 approve()` | **own DB schema** `directional_paper_positions`(266) + `directional_paper_journal`(14,492) (`paper.py:128`) |
| **S1 (30m ATM MACD)** | **entire catalog** (`window_calculator.py:262`, 6 idx + 211 stk) | own `MarketProfileBuilder`; owns the ATM watchlist scope | catalog | DB schema is the contract (`agent_positions`) | none (already uncapped); cash gate `portfolio.py:311` | `agent_positions`(78 open/65 closed) + state file `nse_strategy_state.json` |
| **MACD-Refined** | branch-restored lane | — | — | — | `macd_refined/paper.py:317` | JSON `runtime/macd_refined/signal_state.json` |

### 3.2 Concrete duplication (same data, N loaders)

1. **`underlying_spot_candles` — 4 loaders, 4 bad-bar cleaners** (auction `live.py:842`+`:622`; convergence `service.py:387`+`:39`; S2 `strategy2_mp_of.py:126`+`:101`; runtime `market_intelligence_runtime.py:248`).
2. **`market_ticks` order flow — ≥3 loaders, each re-deriving the near-median tape filter** (auction `live.py:1327`+`_filter_tick_rows_near_median:137`; convergence 3 queries `service.py:401/420/424`; commodity agent).
3. **Order-flow computation — 2 entirely separate stacks:** bar-CVD primitives `analytics/orderflow.py` (its docstring `:1-17` states Indian brokers don't push aggressor-tagged prints — bar/L1 fallback ⇒ `BAR_INFERRED`/`RECONSTRUCTED`) used by Convergence/S2/commodity; vs L1/L2 microstructure `auction_intelligence/order_flow/engine.py:18 OrderFlowEngine.compute:27` used by Auction/FMP. Different code, same intent.
4. **Market Profile — 2 production engines + 5 research copies** (§1.1); persisted `market_profiles` (291 rows / 4 symbols / 1 timeframe) read by **no** live decision.
5. **Regime — 3 separate notions** (directional `regime.py:23`; auction `regime/engine.py`; HMM `analytics/regime_hmm.py` research-only) + a 4th commodity string.
6. **Instrument metadata resolved ≥4 ways; the exchange price increment is a literal in ≥3 places** (§1.1) — and conflated with the MP bucket tick nowhere except commodity, which correctly separates them (§2.1).
7. **Futures front-month symbol built ≥3 ways** (`live.py:192`, `service.py:253` inline, `data.index_futures_backfill`) — all replaced by `ContractResolver.resolve(…, front_month_future, as_of)`.
8. **Per-symbol recompute is uncached across lanes** — auction rebuilds MP+OF+regime+3 agents per symbol as a 200-320s CPU block (`service.py:183-188`); convergence rebuilds the same 30-min TPO per symbol (`engine.py:285`); S2 rebuilds per underlying behind a hand-rolled throttle (`strategy2_mp_of.py:551-617`); S1 computes the same NIFTY TPO in a *second* engine.

### 3.3 The ledger's N truths (verified live) — none is fit to be promoted in place

| # | Owner | Storage | Rows (live) |
|---|---|---|---|
| 1–2 | Auction NSE / Commodity | 2× JSON `PaperPositionBook` (`paper/book.py:73`) | open/closed arrays |
| 3–4 | Convergence NSE / MCX | 2× JSON (`paper.py:24`; incl. `order_log`) | — |
| 5 | Commodity MP+OF | state file **+** `agent_positions`(commodity) **+** `paper_trade_book` | 128 closed in trade book |
| 6 | S1 | state file **+** `agent_positions`(NSE) **+** `runtime/portfolio/daily_*.json` | 78 open / 65 closed |
| 7 | Directional | **own DB schema** `directional_paper_positions` + `_journal` + `_option_trades` (`paper.py:128`) | 266 / 14,492 |
| 8 | MACD-Refined | JSON `runtime/macd_refined/signal_state.json` | — |
| 9 | FMP (parked) | JSON `runtime/fractal_market_profile/paper_positions.json` | — |
| 10 | Legacy live_engine | DB `orders`/`positions`/`paper_sessions` | 261 / 144 |
| — | Shared audit bus | DB `agent_audit_events` (already cross-lane) | 620,560 |

**Why NO existing table can be promoted to canonical (P0-4, ground-truthed):**

- **`agent_positions.symbol` is globally `TEXT NOT NULL UNIQUE`** (`db/migrations/versions/011_agent_audit_tables.py:69`). That means **two strategies can never hold a row for the same contract at the same time** — the moment MP+OF and Directional both want to be long the same option, one lane's insert fails the unique constraint. A canonical multi-lane ledger MUST key positions by `(strategy_account, contract)`, which this table's schema forbids. It cannot be the shared positions table.
- **`paper_trade_book` stores CLOSED trades, not an order/fill event stream** (`core/runtime_state.py:145-176`): every row carries both `entry_price` AND `exit_price` — it is a booked round-trip, not the OPEN→PARTIAL→FILL→CLOSE lifecycle a reconcilable ledger needs. It has **`BIGSERIAL PRIMARY KEY` and NO idempotency/unique constraint** on any source-event key, so re-importing or replaying double-inserts silently. `record_paper_trade` is explicitly **best-effort**: "returns False (and logs) on failure — never raises into the trading path" (`:200-201`) — a canonical ledger write that can silently no-op while trading continues is a correctness hole.
- **No atomic cross-writer transaction is possible.** `paper_trade_book` writes on the shared `core.runtime_state` pool (its own txn), while Directional writes its `directional_paper_*` schema on a **separate DB txn/path** (`directional_options/paper.py:128`), and JSON lanes write files. There is no single transaction that can atomically dual-write "old book + canonical", so a promotion-in-place cannot guarantee both sides agree.

Conclusion: build a **new** event ledger (§4) and *import* into it idempotently; keep every existing book authoritative until parity.

### 3.4 Greeks / options-data reality (DB-grounded, the hard blockers)

- `option_premium_candles` last 3d: upstox **97.4% iv-NULL**, fyers 98.9% NULL, **0 `fyers_chain` rows** (the greeks-bearing builder is effectively not running). Chain builder is flag-off: `CHAIN_CANDLE_BUILDER_ENABLED` default False (`chain_candle_builder.py:275`).
- `option_chain_snapshots` last 2d: 323,844 rows / 189,461 iv-set / **only 5 symbols (indices)** → stocks have **no chain-greeks source** (`GREEKS` capability absent for stocks); `greeks_enrichment` maps 5 indices only (`greeks_enrichment.py:57-63`).
- Live greeks completeness is **indices-only via a snapshot-copy stopgap** (`derivation=OBSERVED`, completeness≈1 for 5 indices; capability ABSENT elsewhere). The greeks live in `option_premium_candles`; the quotes live in `atm_option_watchlist_snapshots` (which has **no greeks columns**) — the split the MarketContext options block must unify.
- `index_futures_candles` = **0 rows** → positional index-futures MP is a hard NO-GO until a writer exists.

### 3.5 The shared REST token bucket (already built)

`brokers/rate_limiter.py`: **Fyers 10/s · 200/min · 100k/day; Upstox 50/s · 2000/30min**, one shared budget per token. Quota classes **CRITICAL (40% reserved) / STANDARD (≥35%) / BULK (≤25%)**. The 2026-07-17 collapse (chain-poll storm burned ~83% of the Upstox budget → S1 starved) proves classification-without-enforcement fails: **tier membership must be enforced at the `broker_class` contextvar boundary** or tiers leak.

---

## 4. Canonical Event Ledger (NEW schema — no existing table promoted)

Per P0-4 and §3.3, the canonical ledger is a **new, append-only, idempotent event store** that the existing books IMPORT into. It is authoritative for nothing until it proves parity; existing books keep trading.

### 4.1 Schema

```
strategy_accounts     # one funded book per policy-instance (replaces the implicit "each lane owns ₹1M")
  account_id (PK), strategy_id, strategy_version, market, base_currency,
  opening_capital, status, created_at

order_intents         # what a policy WANTED (maps 1:1 to EvaluationResult.intents[])
  intent_id (PK), account_id, evaluation_result_id, contract_ref, side, effect,
  target_quantity|target_exposure, order_preferences, as_of, feature_snapshot_ids[]

orders                # a placed order (paper or, later, live)
  order_id (PK), intent_id, account_id, contract_ref, side, effect,
  order_type, qty, limit_price, tif, status(NEW|PARTIAL|FILLED|CANCELLED|REJECTED),
  placed_at, updated_at

fills                 # individual executions against an order (supports PARTIAL fills)
  fill_id (PK), order_id, account_id, contract_ref, qty, price, fees, liquidity_flag,
  exchange_time, received_time

position_events       # the OPEN→REDUCE→CLOSE lifecycle as EVENTS (not current-state)
  position_event_id (PK), account_id, contract_ref, event_type(OPEN|ADD|REDUCE|CLOSE|MARK|ROLL),
  qty_delta, price, realized_pnl_delta, unrealized_mark, event_time

cash_events           # every capital movement (reserve/release/realize/fee/funding)
  cash_event_id (PK), account_id, event_type(RESERVE|RELEASE|REALIZE|FEE|FUNDING),
  amount, balance_after, event_time

-- MATERIALIZED (derived, rebuildable from the events above):
positions             # current state per (account_id, contract_ref)  ← NOT globally-unique-by-symbol
account_balances      # current cash/reserved/equity per account_id
```

Positions are keyed `(account_id, contract_ref)` — so two strategies CAN hold the same contract in different accounts, the exact thing `agent_positions.symbol UNIQUE` forbids (§3.3).

### 4.2 Idempotency key (every imported event)

Every row imported from a source lane carries a unique natural key:

```
(source_lane, source_event_id, event_type, event_sequence)   UNIQUE
```

Re-importing the same source event is a no-op (`INSERT … ON CONFLICT DO NOTHING`), so replays, retries, and dual-run overlaps cannot double-count — the precise hole `paper_trade_book` (no unique key, best-effort insert) has today.

### 4.3 Transactional outbox / import workers

Each lane keeps writing its own book exactly as today. In the SAME local transaction as its book write, it appends an **outbox row** (source-lane-local table) describing the event. A per-lane **import worker** reads the outbox and writes the canonical event ledger idempotently (§4.2). This gives durable dual-write WITHOUT the impossible cross-txn atomicity of §3.3: the lane's own txn stays atomic; the canonical write is eventually-consistent and idempotent. A lane that has no outbox table yet (JSON lanes) gets a thin adapter that emits outbox rows on each book mutation.

### 4.4 Reconciliation (full, not just equity)

Before any lane's READ path flips to the canonical ledger, reconciliation must match on ALL of:

- **quantity** per `(account, contract)`;
- **average price** per position;
- **partial fills** (fill count + fill quantities, not just net);
- **fees**;
- **timestamps** (entry/exit/event times within tolerance);
- **reserved capital** per account;
- **realised P&L** (from `position_events` realized deltas, ONE authoritative source — never a naive UNION that double-counts a lane writing both a position row and a trade-book row, the Commodity hazard §8);
- **state** (open/closed/partial matches the source book's view).

The `agent_audit_events` bus (620,560 rows, already cross-lane) is the reconciliation event log. Existing books stay **authoritative** until a lane clears reconciliation for the acceptance window (§8 thresholds).

---

## 5. Unified UI Read Model (early phase — the human validation surface)

The `/api/system/lanes` telemetry (`lane_registry.py`, `_BOOK_PROBES:878`) reports per-lane book equity/breach but is **not** a workspace read model. Deliver a read-only read model EARLY (migration step 4, §8) — before any strategy cutover — because it is the best human surface for validating shadow parity, and it satisfies the owner's "one UI over the split" requirement.

**Read-model objects (all read-only, computed from the substrate + event ledger):**

```
UniverseRow                # per underlying: tier, subscription state, rank, held?, at-risk?, capabilities present
InstrumentWorkspaceSnapshot# one instrument's full picture: bars, current+prior profile, OF w/ FeatureQuality,
                           #   chain/greeks w/ completeness, regime, resolved front-month ContractRef
EvaluationResult[]         # the CURRENT EvaluationResult for EVERY policy on this instrument — including
                           #   WATCHING/ARMED/BLOCKED (blockers[] shown), so "why didn't it trade" is visible
FeatureProvenance          # per feature: derivation, source, event/observed/available times, coverage, completeness, freshness
PortfolioRiskSummary       # per strategy_account + aggregate: exposure, reserved capital, realized/unrealized,
                           #   hierarchical risk state (§7), reconciliation status vs legacy book
Detail endpoints           # profile / footprint / trade history / order+fill lifecycle drill-downs
```

**Workspaces it powers (the owner's layout):** Command (portfolio/risk/reconciliation health), Structure (profiles/levels), Flow (OF + FeatureQuality), Strategies (every policy's `EvaluationResult` incl. BLOCKED), Risk (hierarchical limits §7), Research (replay/shadow diffs). Shipping this BEFORE cutover means shadow parity (§8) is inspected by a human on the same surface the strategies will later drive.

---

## 6. Universe: selection vs subscription, full cost, and pinning

### 6.1 Cost is NOT just REST

v1 called broad WS tape "cheap." Too narrow. The **total acquisition cost** of a universe is the sum of:

- **broker subscription limits** (WS symbol caps per token);
- **network bandwidth** (tick fan-in);
- **Redis fan-out** (the coalesced tick fan-out already tuned — 150ms, `/pools`);
- **DB write volume** (`market_ticks`/candle/chain writes);
- **feature CPU** (the 200-320s auction block; every MP/OF/greeks build);
- **cache memory** (feature-store budgets §2.2);
- **UI update/render** (read-model refresh §5).

REST is one term, not the term. A name can be WS-cheap but feature-CPU-expensive (a full MP+OF+regime rebuild).

### 6.2 Selection universe ≠ subscription plan

Separate two things v1 merged:

- **Selection universe** — the set of *underlyings* a policy is interested in (ranked/rotating/readiness-gated, e.g. directional's ~50, `service.py:175-227`).
- **Subscription plan** — the concrete set of *venue instruments* to stream/poll. One selected underlying **expands** to: its spot, its front-month future (`ContractResolver`), and *dozens* of option contracts (ATM ± strikes × 2 types × expiries). The subscription plan is the expansion, and it is what actually consumes the budgets in §6.1.

The tier map (A microstructure / B intraday ~50 / C broad ~211 / D on-demand) applies to the **subscription plan**, enforced at the `rate_limiter.py` `broker_class` contextvar (the collapse proves membership-without-contextvar-enforcement leaks).

### 6.3 Pinning rule (never demote an at-risk instrument)

Promotion/demotion by rank is allowed ONLY after PINNING, at full quote + risk-marking coverage, every instrument that is:

- **held** in any `strategy_account` position;
- under an **open order** or working intent;
- part of an **active setup** (ARMED/ACTIONABLE EvaluationResult);
- **required to risk-mark** a held position (e.g. the underlying/future needed to mark an option).

A held instrument must **NEVER** be demoted out of quote/risk coverage because its rank fell — losing its mark would blind risk to a live position. Pinning overrides rank; rank only reorders the *unpinned* remainder.

### 6.4 Sufficiency-gate matrix (horizon × required capability sets + derivations)

Restated on the §2.3 model — capability SETS + allowed derivations + coverage/age, NOT tiers:

| Horizon | Order flow | Bars / profiles | Options / greeks | Regime / VIX | Sessions / rollover |
|---|---|---|---|---|---|
| **Scalp** | `requires_all={TRADE_PRINTS, SIZED_BBO}`, `allowed_derivations={OBSERVED, RECONSTRUCTED}`, coverage≥0.9, age≤5s (BAR_INFERRED refused) | completed 3m + current profile | `{GREEKS}` OBSERVED for premium sizing | fresh regime | intraday only |
| **Intraday** (S2, auction swing-lite, directional intraday) | `requires_all={TRADE_PRINTS}` OBSERVED/RECONSTRUCTED (BAR_INFERRED only in an explicitly-named degraded policy) | completed 3m + 15m + current/prior profiles + quote | `{OPTION_CHAIN, OPEN_INTEREST}` + walls + IV (fraction-unit) | regime + VIX available | current + prior session |
| **Swing** | `{TRADE_PRINTS}` preferred; BAR_INFERRED acceptable if the policy declares it | 15m/30m profiles + composite | `{GREEKS, OPEN_INTEREST}` + walls; DTE≥7 expiry-sufficiency | longer-window regime | current + prior + composite |
| **Positional** (directional positional, CBE) | OF not required (positioning replaces it) | daily + composite profiles | `{GREEKS}` daily / IV percentile (low-IV favors long premium), positioning (OI) | long regime + India-VIX percentile | rollover-safe futures (ContractResolver), multi-session |

Grounded in today's honest gates: convergence's scalp/intraday `real_tick_cvd` (`engine.py:355`), the IV-percentile-conditions-size finding (memory: low IV favors long premium), and the `dte<7 skip` (memory: option-candle coverage). **Hard blockers:** `index_futures_candles` empty (positional index-futures NO-GO) and the greeks-vs-watchlist split — both must be resolved before the positional/options floors can be enforced (step 10, §8).

---

## 7. Hierarchical Risk Controls (replaces v1's flat "reject a global kill")

v1 correctly rejected a *single global* kill (one lane's breach must not halt healthy lanes) but stopped there. The professional answer is a **hierarchy of kill/limit scopes**, each independently armable:

```
Risk scope hierarchy (outer halts everything under it; inner is narrowest):
  1. GLOBAL                     # operator/catastrophe only — manual, not an automatic breach fan-out
  2. BROKER_ACCOUNT             # per broker token (Upstox / Fyers) — margin, connectivity, budget starvation
  3. EXCHANGE                   # per venue (NSE / MCX) — session halts, circuit, feed loss
  4. STRATEGY                   # per strategy_account — the current per-lane governors live here
  5. INSTRUMENT                 # per contract — position/loss caps, freeze-qty, single-name exposure
  6. DATA_SOURCE_CAPABILITY     # kill signals that DEPEND on a lost capability/derivation
                                #   (e.g. if TRADE_PRINTS goes frozen, halt policies requiring OBSERVED OF —
                                #    do not let them silently degrade to BAR_INFERRED and keep trading)
```

**Doctrine preserved (§8):** the `SIGNAL_VALIDATION_UNCAPPED` bypass stays a **per-gate** property at the STRATEGY scope; the Commodity catastrophe kill-switch backstop (`commodity_strategy_agent.py:3357`, which the flag does NOT disable) is a STRATEGY/INSTRUMENT-scope gate; a GLOBAL fire is operator-only and never an automatic single-lane-breach fan-out. Exits stay lane-owned (`manage()`).

**Cross-lane exposure is in the TARGET architecture** — the DATA_SOURCE_CAPABILITY and aggregate STRATEGY layers roll up to a portfolio exposure view. Per the refined owner decision (§12), it **ships as aggregation/alerting FIRST** (observation-only), entry-blocking later and owner-gated. Without this layer in the target, "shared risk" is just N independent ₹1M books behind one interface — which is explicitly NOT the goal.

---

## 8. What BREAKS if Consolidated Naively (load-bearing)

**Evaluation union**
- **Direction-encoding collapse.** A short future and a long put both profit when spot falls but are opposite balance-sheet positions; a single `direction` enum mis-nets. `EvaluationResult` keeps `thesis.bias` (the view) distinct from each `Intent`'s `instrument(ContractRef) + side + effect` (the leg) — a bearish thesis expressed as `BUY <PE>` (long premium) vs `SELL <FUT>` (short future) never collapses (§2.4).
- **Confidence-scale merge.** Convergence emits `score` 0-100 + `quality` A+/VALID (`engine.py:421,431`), not `[0,1]`. A min-confidence gate assuming `[0,1]` (Auction `min_model_confidence=0.55`) passes every Convergence signal or rejects all. Normalize `thesis.confidence` to `[0,1]` but keep the raw in `conviction_extras`; never collapse.
- **Setup-dedup / lifecycle loss.** Convergence's consumed-setup dedup depends on a stable setup identity (`symbol:action:bar_time`, `paper.py:138`). The `EvaluationResult.state` machine (`WATCHING→ARMED→ACTIONABLE→EXITING`) carries lifecycle so a fresh evaluation each cycle does not re-open the same setup (the churn flip-confirmation was built to stop).
- **BLOCKED must be visible, not `None`.** A policy that can't act because a capability/coverage/age failed returns `state=BLOCKED` with `blockers[]` — the read model (§5) shows why. The v1 "store returns `None`" hid this.

**Risk governor**
- **Bypass flattening** (§7) and **kill-switch fan-out** — a global automatic kill firing on one lane's breach halts healthy lanes; the hierarchy (§7) scopes kills instead.
- **Cross-lane exposure changes behavior.** Every lane sized against its OWN ₹1M `strategy_account`. A shared exposure cap suppresses entries that pass today — new alpha-affecting policy, shipped observation-first/opt-in/measured (§7, §12), never a side effect of the merge.

**Ledger**
- **Two realized-pnl truths** — Directional sums lifetime from a DB-wide payload sum (`paper.py:875-892`); `agent_positions` has per-row `realized_pnl`; Convergence sums a JSON array. A naive UNION double-counts a lane that writes both a position row and a trade-book row (Commodity). The event ledger's `position_events` realized deltas are the ONE authoritative source (§4.4).
- **Capital-semantics drift** — S1's `reconcile_available_capital` rebuilds cash from realized+reserved (`portfolio.py:311`, its own warning at `:106`). Unscoped in a shared store it corrupts S1's cash. Every capital computation is `strategy_account`-scoped (`cash_events.account_id`).
- **`agent_positions.symbol UNIQUE` blocks multi-lane ownership** (§3.3) — the reason positions are keyed `(account_id, contract_ref)` in the new schema.
- **`paper_trade_book` has no idempotency + best-effort insert** (§3.3) — the reason the new ledger uses the `(source_lane, source_event_id, event_type, event_sequence)` unique key and an outbox.
- **Journal volume** — 14,492 Directional journal rows vs hundreds in JSON; keep per-lane retention as store policy (`ORDER_LOG_LIMIT=2000`, `paper.py:16`) so imports don't set an unbounded default.

**MarketContext / substrate**
- **Context is not free to share.** Lanes compute at different cadences/scopes (S2 throttles MP to 90s `:89`; Convergence adaptive 45-90s `:101`; each caches its own MP). One eager build re-introduces the 2026-07-13 event-loop seizure. MarketContext is **lazily materialized per (venue-instrument, feature, watermark)** and reuses existing caches, under the single-writer/leader-lock + stampede guard (§2.2).
- **Split-brain on provider failure.** If the store is unavailable for an EXECUTION decision, the runner FAILS CLOSED (emits BLOCKED) — it does NOT silently drop back to the lane's old in-process compute, which would be two answers over one book (§2.2). Read-only surfaces may show the labelled fallback.
- **Capability must fail closed but roll out advisory-first.** Only Convergence expresses capability today; S1/Directional trade on bars. Making requirements blocking on day one **newly blocks working lanes** — introduce them as advisory metadata (step 5/6), enforce as blocking only in step 10, measured (§8).

---

## 9. Non-Goals (what this ADR does NOT do)

- **Does not merge the four policies** into one engine (§1.3). `evaluate()` bodies stay separate and lane-owned.
- **Does not change any lane's trading edge, entry logic, or exit logic** in steps 1–9. Behavior-changing gates (sufficiency, exposure) are step 10 only, opt-in and measured.
- **Does not enable cross-lane exposure netting as part of the refactor.** It becomes *possible* (§7) but ships observation-only pending owner sign-off.
- **Does not build a `DEPTH_L2` feed.** No per-level book is ingested; every depth ladder today is synthetic/`MODELLED` (`live.py:1462-1484`). L2 ingestion is a data decision, not a refactor (owner, §12).
- **Does not fix the option-candle REST-only defect or fill stock greeks as a precondition** — sequenced into step 10 because the options floors depend on it; the substrate ships without waiting on it.
- **Does not touch live-money paths** — all lanes are paper; legacy `orders/positions/paper_sessions` are BRIDGED, retired only after the event ledger is authoritative and no live reader exists (§12).
- **Does not build the index-futures MP sleeve** (`index_futures_candles` empty — data NO-GO).
- **Does not add a second Market-Profile or order-flow algorithm** — it consolidates onto one of each (retiring `MarketProfileBuilder`), it does not invent a third.
- **Does not promote any existing table into the canonical ledger** (§3.3, §4).

---

## 10. How this maps to the reference architectures

| Reference | Their construct | This ADR's mapping |
|---|---|---|
| **QuantConnect** | Universe → multiple Alphas → Portfolio Construction → Risk → Execution | Selection universe + subscription plan (§6) → policy `evaluate()` alphas emitting `EvaluationResult` → opt-in Portfolio Construction/netting (step 10) → hierarchical `RiskGovernor` (§7) → canonical event ledger (§4) |
| **NautilusTrader** | Central instruments / data / cache / risk / execution + **reconciliation** | `UnderlyingRef/VenueInstrumentRef/ContractRef` + `ContractResolver` (instruments) + `MarketContext`/Feature Store (data + cache) + hierarchical `RiskGovernor` + event ledger; the **full-field reconciliation gate** (§4.4) before any read-path flip is Nautilus's reconciliation pattern directly |
| **Hummingbot** | Market-data controllers vs position-owning executors | Feature Store providers = data controllers (compute-once, no positions, single-writer); policy `manage()` + lane exits = position-owning executors — the exact split this ADR keeps (§2.5, §7) |
| **Freqtrade** | Short refreshed informative universe + tiered subscription | Tier-B ranked/rotating/readiness-gated ~50 (`service.py:175-227`) = the refreshed informative *selection* universe; A/B/C/D quota-class = the *subscription plan* (§6), enforced at the shared REST token bucket |

---

## 11. Migration — the 10 steps (replaces v1's 7-step sequence)

Ordered so contracts + read models (low risk, reversible) precede shared compute (medium) precede ledger/risk/policy cutover (high). **Every step is flag-gated and reversible; steps 1–9 change no live behavior — all lanes are paper.** Standing rules honored: no runs 09:15–15:30 IST without owner OK; never `down -v`; never reset broker creds.

The **golden-replay parity gate** (defined once, applied per step): capture exact inputs+outputs of each existing builder/loader for a set of replay sessions (reuse existing serialization: `MarketProfileResult`, `OrderFlowSnapshot`, convergence result dict, `agent_positions` rows); run the new path over identical inputs offline (deterministic — freezegun clock + single-thread OpenBLAS warm-up per the conftest fix); assert **byte-identical** for integer/letter fields and a fixed ε for floats. Then **shadow-live** before flipping any read path. Parity is per-feature and per-lane; a failed diff blocks only that seam. `agent_audit_events` is the reconciliation event log.

1. **Finish + reconcile the current concurrent edits.** *DONE* — working tree is clean, HEAD `3dd91987`; the only deltas are runtime paper-state JSON (§Revision history). No source-merge collision to resolve.
2. **Approve the corrected CONTRACTS only** — `UnderlyingRef/VenueInstrumentRef/ContractRef` + `ContractResolver`, the `DataCapabilities` set + `FeatureQuality`, the `EvaluationResult` envelope, and the event-ledger schema — as **pure types**, no wiring. (Owner sign-off gate on the contracts before any adapter is built.)
3. **Canonical instrument resolution.** One resolver backed by `fo_underlying_catalog` + `fo_contract_catalog` + `analysis/instruments.py` + `market_data/symbols.py` + `commodity_contract_specs.py`. Closes the exchange-price-increment gap (auction `0.5`, directional `0.05`, convergence `.05` → `fo_contract_catalog`) WITHOUT collapsing the MP bucket tick (§2.1). One `ContractResolver` replaces the 3 front-month builders. Per-lane behind `INSTRUMENT_REF_<LANE>`.
4. **Dual-emit read-only `EvaluationResult` adapters + build the unified UI read model (§5).** Every lane emits an `EvaluationResult` record ALONGSIDE its native shape; the read model renders all policies' results (incl. BLOCKED) + feature provenance + portfolio/risk. Extend `_BOOK_PROBES` (`lane_registry.py:878`) to all lanes. This is the human validation surface for every later shadow step.
5. **Batched data loaders + snapshot watermarks.** Consolidate the 4 spot loaders / 3 tape loaders into batched shared loaders that stamp the 8-field MarketContext key (`event_time_frontier`, `completed_bar_watermark`, `data_revision`, …). Read-only; no lane reads from them yet.
6. **Feature providers — OFFLINE / sampled-shadow ONLY.** Wrap `MarketProfileEngine`, `analytics/orderflow`, `OrderFlowEngine`, greeks provider as single-writer `FeatureProvider`s (§2.2 ownership). Run them **offline over replay** and on a **sampled** live subset — do NOT double the 200-320s live compute by running a full second pipeline in-process. Prove byte-identical MP/OF/greeks vs the live builders; prove de-dup (one NIFTY 30m TPO shared) with loop-lag flat.
7. **Build the idempotent event ledger + import/reconcile lane by lane (§4).** Stand up the new schema + outbox + import workers. Reconcile (§4.4 — full fields, not just equity) lane by lane, lowest-risk first: Commodity → S1 → Directional → Convergence → Auction → MACD-Refined. Existing books stay authoritative; flip a lane's READ path only after it clears the acceptance window.
8. **Shared risk in OBSERVATION / assert-equivalent mode (§7).** The hierarchical governor computes verdicts in shadow (the lane's own governor still decides); cross-lane exposure is aggregation/alerting only. Prove per-lane allow/block/size-multiplier identical to today's governors (including the exact bypass sets) before anything becomes authoritative.
9. **Cut policies over individually on MEASURED parity.** Point each `evaluate()` at the store-built `MarketContext`, cutover order S2 → Convergence+Commodity → S1 (retire `MarketProfileBuilder` + collapse the 3 regime notions behind `feature_version` variants) → Auction last. Per-lane read-path flag reverts to the lane's own compute. Still behavior-preserving (advisory capability metadata).
10. **Enable NEW sufficiency/exposure behavior ONLY via separate owner-approved flags.** Turn capability sufficiency gates from advisory to blocking; enable cross-lane exposure entry-blocking; independently fix the option-candle REST-only defect / `CHAIN_CANDLE_BUILDER_ENABLED` so options floors are enforceable beyond the 5 indices. Each is a distinct flag, measured A/B, owner-gated. **This is the only step that changes trading behavior.**

### Explicit acceptance thresholds (replace every vague "N sessions")

Each step's flip requires meeting ALL of its named thresholds — no "a few sessions looked fine":

| Threshold | Definition (per lane/seam) |
|---|---|
| **Minimum sessions** | ≥ 10 distinct trading sessions of clean shadow (contracts/instrument/read-model steps); ≥ 20 for ledger read-path flip and any behavior-changing gate. |
| **# evaluations / signals** | ≥ 500 shadow `EvaluationResult`s per policy, of which ≥ 50 `ACTIONABLE`, before that policy's cutover (so parity isn't measured on near-zero action). |
| **Max decision divergence** | 0 divergences on integer/enum/letter fields (side, effect, state, TPO letters, POC/VAH/VAL bucket); float fields within ε (price ≤ 1 exchange tick, confidence ≤ 0.01). Any divergence blocks the flip and is triaged as a correctness finding. |
| **Latency** | new path per-instrument compute ≤ the lane's current budget (no regression to the 200-320s auction block); event-loop lag telemetry flat vs baseline. |
| **Cache-hit rate** | ≥ 0.90 on shared features (proves de-dup is real, not a second pipeline). |
| **Query count** | shared loaders reduce per-cycle DB queries vs baseline (e.g. convergence's ~72 q/cycle materially down); no per-symbol N+1 reintroduced. |
| **Reconciliation tolerance** | ledger: quantity/fills/state EXACT; realized P&L + reserved capital to the paisa; timestamps within 1s; for the full acceptance window before read-path flip. |

---

## 12. Open Owner-Decisions and Sign-off Checkpoints (refined)

Decisions the owner must make; each is a **sequencing checkpoint** where the migration pauses for sign-off.

1. **S2 sufficiency — named degraded policy, never a silent MACD.** S2 is `BAR_INFERRED` today (`of_source="bar_inference"`). If S2 should keep trading at intraday horizon without real ticks, it does so as a **separately-NAMED `MP_OF_BAR_PROXY` policy** whose `requirements()` explicitly declares `allowed_derivations ⊇ {BAR_INFERRED}` — it must **never silently become MACD** or masquerade as an OBSERVED-OF policy. Otherwise it blocks (honest). — *Checkpoint before step 9 S2 cutover.*
2. **Scalp — only satisfiable-capability strategies, not "L2-capable".** Enable scalp only for strategies whose declared `{TRADE_PRINTS, SIZED_BBO}` requirements are satisfiable on today's data (the 84% sized-L1 cohort). Do NOT label them "L2-capable" — `DEPTH_L2` is structurally absent. — *Checkpoint before enabling scalp sufficiency gates.*
3. **Second broker token — decide from MEASURED capacity, not this ADR.** Whether A-tier persistence + broad-C batched REST needs a 2nd token is a question for step-8 budget telemetry, not a design-time assertion. The ADR does not pre-decide it. — *Checkpoint after step 8 measurement if telemetry shows starvation.*
4. **Ledger — BRIDGE, don't retire.** Bridge the existing books (JSON, `agent_positions`+`paper_trade_book`, `directional_paper_*`) into the event ledger via outbox/import; do NOT retire any of them until the new event ledger is proven authoritative on that lane. Legacy `orders/positions/paper_sessions` retired last, only after confirming no live reader. — *Checkpoint before any lane's read-path flip and before legacy retirement.*
5. **Cross-lane exposure — aggregation/alerts first, entry-blocking later.** Ship the portfolio exposure roll-up as observation-only (§7); enable entry-blocking only after measurement and explicit owner sign-off. — *Checkpoint before turning any exposure gate to blocking (step 10).*
6. **`CHAIN_CANDLE_BUILDER_ENABLED` vs the enrichment stopgap.** Turn on the real-greeks chain builder in the live budget (fixes stock greeks / the options floors) or keep the index-only snapshot-copy stopgap? — *Checkpoint before step 10 options-floor enforcement.*
7. **`DEPTH_L2` ingestion.** Ingest a real per-level depth feed to make the `DEPTH_L2` capability present, or accept that scalp/L2-dependent gates stay unsatisfiable? — *Owner call, independent of the migration.*
8. **"MP+OF" is TWO policy IDs sharing a library.** MP+OF is NOT one policy — it is **two policy IDs** (an index long-premium policy and a commodity-futures policy) that SHARE a feature/trigger library (the `commodity_mp_signal` evaluator) but own **different books/accounts**. Register them as two `strategy_id`s over one shared library. — *Checkpoint before step 9 MP+OF cutover.*
9. **Stock Greeks stay unavailable until an SLO-meeting feed exists.** Do not synthesize or partially-fill stock greeks to satisfy a floor; the `GREEKS` capability stays ABSENT for stocks until a source-backed feed meets completeness + budget SLOs. — *Checkpoint before extending any greeks-dependent gate beyond the 5 indices.*

---

## Appendix A — Key files (absolute)

- Spine: `/Users/ramachandran/CLAUDE PROJECTS/Nomad Curie/TradeBot/backend/core/lane_registry.py`, `…/backend/core/laneset.py` (core vs strategy plane — feature-store ownership §2.2)
- Standing flag: `…/backend/core/config.py:229`
- Substrate seed: `…/backend/market_data/market_intelligence_runtime.py`, `…/backend/market_data/atm_watchlist.py`, `…/backend/market_data/fo_universe_bootstrap.py`, `…/backend/agent/window_calculator.py:245`
- Feature engines: `…/backend/auction_intelligence/market_profile/engine.py`, `…/backend/market_data/market_profile.py`, `…/backend/analytics/orderflow.py`, `…/backend/auction_intelligence/order_flow/engine.py`
- Per-lane loaders/emitters: `…/backend/auction_intelligence/live.py` (`:594-960`, `:1209-1524`, `:1657`), `…/backend/auction_intelligence/service.py:183`, `…/backend/institutional_convergence/service.py:384`, `…/backend/institutional_convergence/engine.py:322-358`, `…/backend/paper_engine/strategy2_mp_of.py:126-639`, `…/backend/paper_engine/commodity_mp_signal.py:264-1113`, `…/backend/directional_options/data.py:138-401`, `…/backend/directional_options/signals.py:276`, `…/backend/paper_engine/strategy_agent.py`
- Metadata: `…/backend/analysis/instruments.py`, `…/backend/market_data/symbols.py`, `…/backend/market_data/commodity_contract_specs.py` (`:21-45` — the mp_tick_size vs mp_value_tick separation, §2.1)
- DataQuality / source: `…/backend/market_data/data_quality_agent.py`, `…/backend/market_data/source_policy.py`, `…/backend/market_data/greeks_enrichment.py`, `…/backend/market_data/chain_candle_builder.py`
- Risk: `…/backend/auction_intelligence/risk/governor.py`, `…/backend/directional_options/risk.py`, `…/backend/institutional_convergence/paper.py:200`, `…/backend/paper_engine/commodity_strategy_agent.py:1974,3357`, `…/backend/paper_engine/portfolio.py:311`
- Ledger (existing, to be BRIDGED not promoted): DB `agent_positions` (`symbol UNIQUE` at `db/migrations/versions/011_agent_audit_tables.py:69`) + `paper_trade_book` (`core/runtime_state.py:145-201`, best-effort insert, no idempotency key) + Directional `directional_paper_*` (`directional_options/paper.py:128`); audit `agent_audit_events`
- Budget: `…/backend/brokers/rate_limiter.py`

## Appendix B — DB ground-truth (live, Sat 2026-07-18)

- `fo_underlying_catalog`: 217 rows (6 INDEX + 211 STOCK, all keyed, 211/211 lot_size, no price increment). `fo_contract_catalog`: 50,482 (exchange price increment / freeze / min-lot). `fo_expiry_catalog`: 3,095.
- `market_ticks` last 2d: 152 symbols / 1.99M rows / **84% real L1 sizes** (`TRADE_PRINTS` + `SIZED_BBO` present). 9,990 distinct option symbols on WS over 3d. `DEPTH_L2` structurally absent (un-persisted).
- `option_premium_candles` last 3d: upstox **97.4% iv-NULL**, fyers 98.9% NULL, **0 `fyers_chain` rows**.
- `option_chain_snapshots` last 2d: 323,844 rows / 189,461 iv-set / **5 symbols (indices only)** — `GREEKS` present indices-only.
- `market_profiles`: 291 rows / 4 symbols / 1 timeframe since 2026-02-12 (unread by live decision). `index_futures_candles`: **0 rows**.
- Ledger: `agent_positions` 78 open/65 closed (`symbol UNIQUE`); `paper_trade_book` 128 closed (no idempotency key); `directional_paper_positions` 266 / `_journal` 14,492; legacy `orders/positions` 261/144; `agent_audit_events` 620,560.
- Budget: Fyers 10/s·200/min·100k/day; Upstox 50/s·2000/30min. Quota CRITICAL 40% / STANDARD ≥35% / BULK ≤25%.
