# ADR-0043 — Unify the Substrate Beneath the Strategy Policies

**MarketContext · Canonical instrument identity (ExchangeRef / UnderlyingRef / Listing / ProviderAlias) · EvaluationResult with lifecycle identity · DataCapabilities + FeatureQuality · Scoped-predicate Shared Risk · Canonical Event Ledger (single causal chain) · Selection Universe vs Subscription Plan**

- **Status:** PROPOSED — REVISION REQUIRED (rev 3, third-review amendments incorporated 2026-07-19)
- **Date:** 2026-07-19 (rev 3). Rev 2: 2026-07-19. Original draft: Sat 2026-07-18 night, market CLOSED, fully read-only.
- **Approval state:** the ARCHITECTURE (shared substrate, separate policies) is ACCEPTED by review. Migration **steps 2–3 are NOT yet approved**; they unblock only on owner sign-off of rev 3. **No ledger, no capability enforcement, and no shared feature-store implementation may begin from rev 2** — rev 2's capability vocabulary, ledger event model, and lock design were materially wrong and are superseded below.
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
| **v3** | 2026-07-19 | **Third review: architecture ACCEPTED, 5 CRITICAL contract defects + 4 design gaps must land before steps 2–3 are approved.** All 9 incorporated. The largest correction is factual: rev 2 **misclassified quote snapshots as trade prints** — `market_ticks` has no trade id, no last-trade quantity and no broker aggressor side, so `TRADE_PRINTS` never existed as a capability in this system. Every claim built on it (the "84% satisfy {TRADE_PRINTS, SIZED_BBO}" coverage line, "Convergence CVD reconstructed from a real trade tape", "today's data can satisfy the scalp requirement") is **DELETED**, not softened. Also rewritten: the manage()-path degraded-data contract, EvaluationResult lifecycle identity, the ledger's causal chain, the leader lock, instrument identity, risk scoping, acceptance thresholds, and the UI contract gate. |

**Findings addressed in v3 (third review):**

| # | Severity | Finding | Sections rewritten |
|---|---|---|---|
| **C1** | CRITICAL | ADR misclassified **quote snapshots as trade prints**. Capability vocabulary replaced; every buy/sell-attribution feature regraded `MODELLED_FROM_QUOTES`; coverage recomputed; scalp restated as **UNSATISFIABLE today**. | §1.1, §1.2, §2.2, §2.3, §3.2, §6.4, §8, §9, §12, App. B |
| **C2** | CRITICAL | Fail-closed could **trap held positions** — a provider outage would block the exit logic needed during the outage. `manage()` gets its own capability requirements + a degraded-data state machine; risk-increasing vs risk-reducing vs emergency actions separated. | §2.2, §2.5 (new §2.5.2), §7, §8 |
| **C3** | CRITICAL | `EvaluationResult` had **no lifecycle identity or idempotency** — the ledger referenced an undefined `evaluation_result_id` and a state enum alone cannot stop repeated ACTIONABLE results reopening one setup. Identity block added. | §2.4 (new §2.4.1), §4.1, §4.2, §8 |
| **C4** | CRITICAL | Ledger had **competing event truths** (fills AND independently-written position/cash events) and an outbox claim that **cannot hold for JSON books**. Restructured to a single causal chain with deterministic projections + explicit JSON-import rules. | §3.3, §4.1, §4.3 (rewritten), §4.4, §8 |
| **C5** | CRITICAL | Redis leader lock was a bare `SET NX PX` — **unsafe for a 200–320s computation**; an expired lease lets a stale writer publish. Renewable fenced leases + token-checked publication required. | §2.2 (new §2.2.1) |
| **G6** | DESIGN GAP | Instrument identity **not yet canonical** — `venue` conflated data providers with execution venues; no immutable listing id; calendars treated as identity. | §2.1 (rewritten) |
| **G7** | DESIGN GAP | Risk scopes are **dimensions, not a hierarchy** — they overlap rather than nest. Replaced with scoped predicates + precedence/aggregation rules. | §7 (rewritten) |
| **G8** | DESIGN GAP | Acceptance thresholds **still subjective**; and blanket byte-parity **conflicts** with retiring the second MP engine. Measured baseline+target table; parity split into two tracks. | §11 (rewritten) |
| **G9** | DESIGN GAP | UI read model is **not a UI contract**. A separate UI contract is now a gate before step 4 — and it must ABSORB the already-shipped Phase 0 + Command workspace (`2addf247`, `79269e7c`). | §5 (rewritten), §11 |

**Amendments addressed in v2 (retained — the reviewer credited all of these):**

1. **P0-1 — Capability is a SET, not a monotonic ladder.** The scalar ladder is deleted. Replaced with `DataCapabilities.available` (a set) + a `FeatureQuality.derivation` grade. Sufficiency gates now match capability sets + derivations + coverage/age (§2.3, §6). Greeks are a DataCapability with their own FeatureQuality, never stamped "REAL" through an order-flow enum. *(v3: the set-not-ladder shape survives; the specific capability NAMES and derivation grades v2 chose were factually wrong and are replaced in §2.3 — see C1.)*
2. **P0-2 — Instrument identity split into THREE objects.** `InstrumentRef` is deleted; front-month is an as-of resolution, not immutable identity; four distinct price-increment kinds named (§2.1). *(v3: the split was necessary but insufficient — `VenueInstrumentRef` conflated data providers with execution venues, there was no immutable listing id, and calendars sat on the identity object. §2.1 is rewritten — see G6.)*
3. **P0-3 — `SignalIntent` replaced by `EvaluationResult`.** Multi-leg, state-carrying (`WATCHING|ARMED|ACTIONABLE|BLOCKED|EXITING`), bias≠leg preserved. The feature store never "refuses to build" / returns `None` — the **policy runner** validates requirements and emits a visible `BLOCKED` result (§2.4, §2.5).
4. **P0-4 — Do NOT promote existing tables into the canonical ledger.** A NEW idempotent event ledger is designed instead, with full reconciliation. Existing books stay authoritative until parity (§4). *(v3: the no-promotion rule survives. The v2 event MODEL did not — `position_events`/`cash_events` were presented as independently-written canonical streams alongside fills, and the "transactional outbox" claim does not hold for the JSON books. Both are corrected in §4 — see C4.)*
5. **P0-5 — MarketContext key + feature-store ownership defined.** `(instrument, as_of)` was insufficient; replaced with an 8-field deterministic key and explicit per-plane ownership / single-writer / TTL / stampede / budget / fail-closed rules (§2.2).
6. **P1-6 — Unified UI read model added as an EARLY phase** (§5), before strategy cutover; it is the human validation surface for shadow parity. *(v3: a list of read-model OBJECTS is not a UI contract; §5 is rewritten and gated — see G9.)*
7. **P1-7 — Universe cost broadened beyond REST; selection universe separated from the subscription plan; held/at-risk instruments pinned** (§6).
8. **P1-8 — Shared risk controls beyond a single global kill**, with cross-lane exposure in the target architecture (§7). *(v3: the six scopes v2 listed do not NEST — they overlap. §7 is rewritten as scoped predicates over dimensions — see G7.)*

**Repository state (updated for v3):** all prior substrate edits are committed. **HEAD is now `79269e7c`** ("ui phase 1: instrument-centric Command workspace"), on top of `2addf247` ("ui phase 0: one semantic truth contract"). Rev 2 recorded HEAD `3dd91987`; that is stale — **two UI phases have SHIPPED since**, which is why §5 must now absorb a live surface rather than design a greenfield one (G9). The working-tree deltas at the time of writing are (a) runtime paper-state JSON written by the paper engine itself (`runtime/auction_intelligence/*.json`, `runtime/portfolio/daily_*.json`), never source, and (b) a **concurrent frontend workstream** correcting the shipped order-flow grading to the C1 vocabulary (`frontend-v2/**` plus the backend modules that emit `of_source`/`cvd_source` labels). Neither is a substrate change and this ADR revision touched no code. Migration step 1 (§11) is therefore already DONE.

---

## 1. Context and Decision

### 1.1 The situation today

There is **no shared substrate**. Every lane independently (a) picks its own universe, (b) assembles its own market-data context, (c) computes its own features from raw tables, (d) emits a differently-shaped signal, (e) runs its own risk governor, and (f) writes to its own ledger in its own storage paradigm. The convergence that exists is accidental, not architectural:

- **Two production Market-Profile engines** (`auction_intelligence/market_profile/engine.py:33` bar-TPO `MarketProfileEngine`, reused by Convergence/S2/commodity; and `market_data/market_profile.py:57` tick-TPO `MarketProfileBuilder`, used by S1/agent) recompute the same NIFTY 30-min TPO independently on their own cadences.
- **Four independent loaders of `underlying_spot_candles`**, each with its own bad-bar cleaner: auction `live.py:842` (+`:622`), convergence `service.py:387` (+`:39`), S2 `strategy2_mp_of.py:126` (+`:101`), runtime `market_intelligence_runtime.py:248`.
- **Price increments are literals in ≥3 places** (auction `0.5` in `SYMBOL_MAP`, directional `0.05` at `data.py:291`, convergence default `.05`) while the true sources are `fo_contract_catalog` (exchange order tick) and `commodity_contract_specs` (which deliberately keeps a *separate* profile-bucket tick — see §2.1).
- **Three storage paradigms for the ledger** (JSON files, `agent_positions`+`paper_trade_book` DB, and Directional's own `directional_paper_*` DB schema) with no cross-lane netting, exposure, or reconciliation possible — and no single one is fit to become canonical (§4).
- **Data-capability awareness exists in exactly one lane** — Convergence's `cvd_source ∈ {market_ticks, bar_proxy}` + the `real_tick_cvd` gate (`institutional_convergence/engine.py:326,334,355`). No other lane expresses source capability at all. Note this is already a *set-and-derivation* concept (a CVD modelled from a dense quote/cumulative-volume stream vs one inferred from bars), NOT a rung on a total order — which is exactly why the v1 scalar ladder was wrong (P0-1). **Read `cvd_source == "market_ticks"` precisely: it means "from the quote-snapshot table", NOT "from a trade tape."** The name is a historical accident and rev 2 was misled by it (C1).

**The single most important factual correction in this ADR (C1) — we do not have a trade tape.** `market_ticks` is a **quote-snapshot** hypertable. Its full column set is `time, symbol, ltp, open, high, low, close, volume, oi, bid, ask, bid_qty, ask_qty` (`db/migrations/versions/001_initial_schema.py`, `CREATE TABLE market_ticks`). There is **no trade id, no last-traded quantity, and no broker-supplied aggressor side** — and `volume` is a **cumulative session counter**, not a per-trade size. Every "buy vs sell" number in this system is therefore *inferred*, never observed:
- `analytics/orderflow.py` says so in its own module docstring: Indian retail brokers don't push public trade prints, so true Lee-Ready CVD isn't available and these functions **approximate** it from OHLCV bars + L1 snapshots.
- `institutional_convergence/engine.py:33 build_footprint` reconstructs a footprint by differencing the **cumulative** `volume` between consecutive snapshots and guessing the side from where `ltp` sits relative to `bid`/`ask`/the previous `ltp` — a heuristic over quotes, not an executed print.

Consequence: any architecture that grades order flow as "observed" or "reconstructed from a real tape" is describing a system we do not have. §2.3 fixes the vocabulary.

We already own the *utilities* (`backend/market_data/`: `live_candle_store`, `quote_bus`, `source_policy`, `data_router`, `broker_circuit`, `atm_watchlist`, `market_profile`, `greeks_enrichment`; plus `brokers/rate_limiter.py`, `core/lane_registry.py`, `core/laneset.py`). What is missing is an object that composes them into a **versioned MarketContext** with declared data capabilities and per-feature quality/derivation, and a **canonical EvaluationResult + event ledger** the policies share.

### 1.2 The decision

Introduce a shared substrate beneath policies that stay separate, via feature-flagged, per-lane, reversible strangler-fig migration:

1. **Canonical instrument identity** — `ExchangeRef` (execution venue) / `UnderlyingRef` (economic thing) / `Listing` carrying an immutable `canonical_contract_id` / `ProviderAlias` (many time-boxed broker symbols per listing) / **versioned** session + expiry calendars and trading parameters, plus a `ContractResolver` returning a `ResolutionResult` — replacing `SYMBOL_MAP`, the ~6 ad-hoc symbol resolvers, and both v1's conflated `InstrumentRef` and v2's provider/venue-conflating `VenueInstrumentRef`. §2.1.
2. **`MarketContext`** — an immutable, **versioned** value object keyed by an **8-field deterministic snapshot key** (§2.2), assembled **compute-once** per `(canonical_contract_id, provider, feature, watermark)` and handed to every policy that requested it, with explicit feature-store ownership per process plane.
3. **`DataCapabilities` (a set) + `FeatureQuality` (with a `derivation` grade)** — `available ⊆ {QUOTE_UPDATES, CUMULATIVE_VOLUME, SIZED_BBO, OHLCV_1M, OPTION_CHAIN, GREEKS, OPEN_INTEREST}`, with `BROKER_AGGRESSOR_PRINTS` and `DEPTH_L2` declared as capabilities we **do not have** (structurally unavailable today); every feature carries `derivation ∈ {OBSERVED, MODELLED_FROM_QUOTES, MODELLED, BAR_INFERRED}`, source, freshness, coverage, completeness, and `missing_reason`. §2.3.
4. **`EvaluationResult`** — one typed, multi-leg, state-carrying output every lane's `evaluate()` returns (replacing v1's lossy `SignalIntent`). §2.4.
5. **A shared `RiskGovernor` of SCOPED PREDICATES** — rules selected over six independent, overlapping dimensions (account × strategy × venue × provider × instrument × capability) filtered by action-effect, with explicit precedence bands and aggregation rules, dispatching to per-policy exit managers and preserving the `SIGNAL_VALIDATION_UNCAPPED` bypass **per rule**. Band-0 safety actions are un-bypassable. §7.
6. **A NEW canonical event ledger with ONE causal chain** — `order_intents → order_events → fills`, with `positions`, cash and realised P&L as **deterministic projections** of the fill stream and marks as a separate valuation stream; `strategy_accounts` + an `evaluations` table keyed by the §2.4.1 identity fields. Populated by a transactional outbox for DB lanes and a generation/checkpoint importer for JSON lanes. Existing books stay authoritative until parity is proven; **no existing table is promoted in place**. §4.
7. **A unified UI read model** delivered early (§5), and a **selection universe distinct from the subscription plan** with held/at-risk instrument pinning (§6).

Each policy implements the strategy interface: `entry_requirements()` and a **separate** `management_requirements()` (capability sets + allowed derivations + coverage/age), `evaluate(context) -> EvaluationResult`, and `manage(context, position, open_orders, original_thesis) -> list[ManagementAction]`. The two requirement sets are deliberately different: **blocking an entry on missing data is safe; blocking an exit on missing data is not** (§2.5.2, C2). §2.5.

### 1.3 Why NOT one merged engine (the monolith we are deliberately rejecting)

The external review's own framing is "shared substrate, separate policies," and this is correct for concrete, load-bearing reasons — a single merged strategy engine would:

- **Erase four genuinely different edges.** The lanes are not variants of one strategy: Directional's edge is intraday mean-reversion + options positioning with OF *unavailable by design* (`directional_options/features.py`, `positioning_feed.py:58`); Convergence's edge is a BANKNIFTY-specific CVD confirmation that hard-blocks on `real_tick_cvd` (i.e. it demands the dense **quote-snapshot**-derived CVD and refuses the bar-inferred one — see C1: neither is a trade tape); Auction is a 200-320s MP/OF/regime CPU pipeline (`auction_intelligence/service.py:183-188`); MP+OF is a commodity-native tick evaluator ported to indices as a bar-inferred proxy. Merging them forces a single feature contract and a single risk doctrine onto four incompatible horizons and data-sufficiency profiles.
- **Collapse the direction semantics dangerously.** Three encodings exist today — `LONG/SHORT/FLAT` (Auction/Convergence bias), `CE/PE` (Directional option side), `BUY/SELL` (MP+OF). A monolith that treats a bearish *thesis*, a long-put *leg*, and a short-future *leg* as one bucket mis-signs P&L and netting (§8). `EvaluationResult` keeps thesis-bias and per-leg instrument+side+effect distinct precisely to prevent this (§2.4).
- **Re-introduce event-loop seizure.** The lanes run on a single core; S2 and Convergence hand-rolled bar-close-driven throttles/caches specifically to survive it (`strategy2_mp_of.py:604-614`, the 2026-07-13 degenerate-bar incident). One eager merged build per cycle re-creates the exact seizure those caches prevent.
- **Fail the doctrine test.** `SIGNAL_VALIDATION_UNCAPPED` (`core/config.py:229`) is an owner directive with a *per-lane* bypass pattern; a merged risk engine would flatten it (§7).

The substrate is where the reuse lives (instruments, bars, profiles, order-flow, chain/greeks, regime, ledger, risk gates). The **policies stay as pluggable `evaluate()` implementations**. This is the QuantConnect/Nautilus/Hummingbot/Freqtrade shape (§10), not a rewrite.

---

## 2. The Concrete Contracts (field-level, grounded in the union maps)

All contracts are additive in the first instance — every field maps to at least one existing lane so migration adapters are mechanical. `⊕` = a lane already carries it; `→` = derived at the adapter.

### 2.1 Instrument identity — canonical identity, provider aliases, and versioned reference data (G6; replaces `SYMBOL_MAP`, the ~6 resolvers, and v1's single conflated `InstrumentRef`)

v1 conflated root + spot + expiry + strike + future + lot + tick into one `InstrumentRef`. v2 split that into three objects — the right move, but **it did not go far enough** (G6). Two conflations survived in v2 and are removed here:

1. **`venue` conflated DATA PROVIDERS with EXECUTION VENUES.** v2's `VenueInstrumentRef.venue` took values `UPSTOX/FYERS/exchange` — mixing "who gives me the data / who routes my order" with "where the contract actually trades". These are orthogonal: NIFTY options trade on **NSE** and are reachable via **both** Upstox and Fyers; the same MCX contract is likewise multi-provider. Routing, budgets and circuit-breakers are per **provider**; margin, session halts, circuits, lot/freeze rules and settlement are per **exchange**. One field cannot carry both.
2. **`ContractRef` had no immutable identity**, and calendars were stored as identity fields on `UnderlyingRef`. A resolved front-month contract with no stable id cannot be joined across a metadata correction; and `session`/`expiry_calendar` are **reference data that changes** (exchange circulars, holiday updates, expiry-weekday migrations) — putting them on the identity object makes identity mutate when a holiday list is amended.

**The corrected model — five layers, cleanly separated.**

**(a) `ExchangeRef` — where it trades (execution venue), never a provider.**

| Field | Meaning | Source |
|---|---|---|
| `exchange` | NSE / BSE / MCX | catalog |
| `segment` | CASH / FO / CURRENCY / COMMODITY-FO | catalog |
| `clearing_corp` | settlement counterparty (matters for margin) | reference data |

**(b) `UnderlyingRef` — the stable economic thing (identity only, NO calendars).**

| Field | Consumed by | Current source (file) |
|---|---|---|
| `symbol` (canonical root: NIFTY, INFY, GOLD) | all | `fo_underlying_catalog` / `SYMBOL_MAP` / config |
| `kind` (INDEX/STOCK/COMMODITY) | all | `fo_underlying_catalog.kind`; commodity implicit |
| `sector_code` | convergence diversification | CBE payload |
| `exchange_ref` | all | catalog / commodity roots |

`session` and `expiry_calendar` are **REMOVED from `UnderlyingRef`** — see (e).

**(c) `Listing` + `canonical_contract_id` — the immutable tradeable identity.**

An economic instrument (e.g. "NIFTY 25000 CE expiring 2026-07-24") is distinct from its **listing** on an exchange. The listing carries the **immutable `canonical_contract_id`** — a system-owned surrogate key, minted once, never reused, never derived from a provider symbol (provider symbol formats change; see the option-candles defect where Fyers-format symbols missed an Upstox-keyed catalog).

| Field | Meaning |
|---|---|
| `canonical_contract_id` (PK) | immutable, system-minted, never re-used |
| `exchange_ref`, `underlying_ref` | (a) + (b) |
| `instrument_class` (FUTURE/OPTION/SPOT/INDEX) | — |
| `expiry`, `expiry_kind`, `strike`, `option_type` | contract economics |
| `listing_date`, `delisting_date` | the listing's own lifetime |

**(d) `ProviderAlias` — a MANY-to-one alias table, not an identity field.**

| Field | Meaning | Current source |
|---|---|---|
| `provider` (UPSTOX/FYERS/…) | data provider AND/OR broker — a **role**, not a venue | routing config |
| `provider_role` | `{MARKET_DATA, EXECUTION, BOTH}` | routing config |
| `provider_symbol` | e.g. Upstox `instrument_key`, Fyers `fyers_symbol`, `app_symbol` | `fo_underlying_catalog` (217 rows), `market_data/symbols.py` |
| `canonical_contract_id` | FK to (c) | — |
| `valid_from`, `valid_to` | aliases are **re-pointed** by providers; they are time-boxed | — |

One `canonical_contract_id` may have several aliases (Upstox key + Fyers symbol + app symbol) simultaneously. **`ContractRef` = `canonical_contract_id` + the alias set resolved for a given purpose.** Nothing downstream may key state on a provider symbol.

**(e) Calendars are VERSIONED REFERENCE DATA, not identity.**

`SessionCalendar` (RTH vs MCX 09:00–23:30 — today hardcoded at `live.py:69-72` / `_session_bounds:186`) and `ExpiryCalendar` (`analysis/instruments.py:18` + `fo_expiry_catalog`, 3,095 rows) become versioned tables with `metadata_version` + `effective_from`/`effective_to`. A decision made on 2026-07-10 is replayable against the calendar **as it was known then**, even after a holiday amendment. `MarketContextKey.data_revision` (§2.2) bumps when a calendar version changes.

**Per-contract trading parameters** (`lot_size`, `freeze_quantity`, `minimum_lot`, `price_increment`) are likewise **versioned reference data attached to the listing**, not identity: they carry `metadata_version` + effective dates. Source: `fo_contract_catalog` (50,482 rows) / `fo_underlying_catalog` (211/211 lot) / `commodity_contract_specs`.

**(f) The resolver returns a RESULT, not a bare ref.**

```
ContractResolver.resolve(underlying: UnderlyingRef,
                         purpose,            # front_month_future | atm_option | weekly_expiry_target | ...
                         as_of,
                         provider_role)      # MARKET_DATA vs EXECUTION may resolve to different aliases
  -> ResolutionResult:
       contract_ref: ContractRef | null       # canonical_contract_id + resolved aliases
       status: RESOLVED | AMBIGUOUS | NOT_LISTED | STALE_METADATA | NO_ALIAS_FOR_PROVIDER
       reason: str                            # human+machine readable WHY (never a silent null)
       source: str                            # which catalog/table answered
       metadata_version, metadata_as_of       # which reference-data version was used
       resolved_at
```

This replaces the 3 inline front-month builders (`live.py:192`, `service.py:253`, `data.index_futures_backfill`). The *same* `UnderlyingRef` resolves to *different* contracts over time; the front-month symbol is an **output** of `resolve(…, front_month_future, as_of)`, **never a stored identity field**. A failed resolution is an explicit `status` + `reason` — never `None`, never a fallback guess (the fail-closed stock-instrument-resolution pattern already proven in production).

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
  4. contract_and_provider    # canonical_contract_id (§2.1c) + the DATA-PROVIDER identity the values came from.
                              #   Provider is part of the key because two providers disagree on the same contract;
                              #   the EXECUTION venue is a property of the contract, not a separate key field (G6).
  5. session                  # session id/date + phase (pre-open / RTH / MCX-evening / post) from the SessionCalendar
                              #   VERSION in force (§2.1e) — not from an identity field
  6. policy_horizon           # scalp | intraday | swing | positional — drives which watermarks/coverage are required
  7. feature_algo_version     # {feature_name: version} for every feature realized in this context
  8. input_snapshot_ids       # the FeatureQuality.input_snapshot_id set the features were computed from
```

`context_version = hash(all 8 fields)`; a `snapshot_id` pins the exact inputs a decision was computed against (referenced from `EvaluationResult.feature_snapshot_ids[]`). Lazily materialized per `(canonical_contract_id, provider, feature, watermark)` — never one eager build (§8). `data_revision` also bumps on a reference-data (calendar / lot / freeze / price-increment) version change (§2.1e), so a metadata correction invalidates dependent contexts rather than silently changing meaning underneath a cached value.

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
  capabilities: DataCapabilities                   # see 2.3 — the SET available for this (canonical_contract_id, provider) at this key
  feature_quality: {feature_name: FeatureQuality}  # see 2.3 — per-feature derivation/coverage/freshness/missing_reason
```

**Feature-store ownership (decisive because of the process split — `laneset.py`).** The Phase-1 split makes ownership a hard architectural decision, not a detail: the **core plane** owns live ingestion (Fyers WS callback stream, `market_ticks`/spot writers, chain/greeks enrichment — `boots_core()`), while the **strategy plane** runs the own-loop agents and consumes Redis/PG (`boots_strategies()`). The ADR therefore DECIDES:

- **Who computes each feature.** Ingestion-coupled features (tick CVD/footprint from the live tape, L1/L2 book, greeks enrichment) are computed **only on the core plane** (single writer — it is the only plane with the WS callback). Pure-CPU derivations over already-persisted bars/profiles (MP TPO, regime, bar-inferred OF) may be computed on **either** plane but are **written by a single leader** (see next).
- **Single-writer / FENCED leader lease.** Every cached feature has exactly one writer identity `(plane, feature_key)`, elected by a **renewable, fenced Redis lease** — **NOT** a bare `SET NX PX` (C5, see §2.2.1). Non-leaders are read-only. This prevents the strategy plane and core plane both recomputing (and disagreeing on) the same NIFTY 30m TPO.
- **Redis TTL + eviction.** Per-feature TTL keyed to the feature's natural cadence (bar-close for MP, tape-cadence for OF, chain cadence for greeks); eviction is LRU within a declared memory budget; a stale-but-present value is served with `freshness=stale` (never silently as fresh).
- **Cache-stampede protection.** Recompute is guarded by the leader-lock + a short "compute-in-progress" sentinel so a cold key under N concurrent readers triggers ONE build, not N (the 200-320s auction block must never fan out).
- **Memory + serialization budgets.** Each feature declares a max serialized size; oversized payloads (the F-18 `app_runtime_state` blob lesson) are rejected at write, not discovered at read.
- **Behaviour after a provider failure — FAIL CLOSED for RISK-INCREASING actions ONLY.** If a required feature's provider errors or its cache is cold/stale past `maximum_age`, the policy runner emits a `BLOCKED` `EvaluationResult` (§2.4) for the **entry** path. It does **NOT** silently fall back to the lane's old in-process compute path for a risk-increasing decision — that is split-brain (two code paths, two answers, one book). A named degraded mode is permitted ONLY when the policy's `entry_requirements()` explicitly allows it (e.g. an MP_OF_BAR_PROXY policy that declares `allowed_derivations` includes `BAR_INFERRED`). Read-only surfaces (UI, shadow) may show the fallback, clearly labelled.

  **This rule does NOT extend to held positions (C2).** Rev 2 applied one fail-closed rule to everything, which — combined with "the shared governor never closes a position" — meant a provider outage could block the very exit logic needed *during* that outage, trapping live risk. The `manage()` path has its **own** requirements and its **own** degraded-data state machine: see §2.5.2. Summary of the corrected contract: **stale alpha data blocks OPENING; it must never block CLOSING.**

#### 2.2.1 Leader election must be a FENCED, RENEWABLE lease — `SET NX PX` alone is unsafe here (C5)

Rev 2 specified a bare `SET NX PX` leader lock. That is **provably unsafe for this workload**, because the workload is not short. `auction_intelligence/service.py:183` offloads `analyze()` — a pure-CPU block explicitly documented in-code as a **200–320s compute** — to a worker thread. Any lease shorter than the worst-case compute expires mid-computation; and a lease longer than that stalls recovery for minutes after a crash. Both ends are bad, and the failure is silent:

```
t0    worker A acquires lease (PX = 120s), begins a 300s MP/OF/regime build
t120  lease EXPIRES while A is still computing (A does not know)
t121  worker B acquires the same key legitimately, computes on FRESHER inputs, publishes v_B
t300  worker A finishes and publishes v_A  ← STALE output overwrites fresher output.
      Every downstream policy now decides on a 300-second-old profile that LOOKS current.
```

`SET NX PX` cannot prevent this: expiry is invisible to the holder, and the holder's write carries no proof it still owns the key. Required design:

1. **Renewable lease with a heartbeat.** The holder renews (extends the TTL) on a timer at ≤ 1/3 of the lease duration, *from a task independent of the compute* (the compute is in `asyncio.to_thread`; the renewer must not be blocked by it). Renewal is conditional on still owning the lease — a Lua compare-and-extend, never a blind `PEXPIRE`.
2. **Monotonic fencing tokens.** Every successful acquisition increments a monotonic per-key counter (`INCR fence:<feature_key>`) and the acquirer carries that token. Tokens are **strictly increasing across acquisitions**, so a later acquisition always outranks an earlier one, regardless of wall-clock or lease state.
3. **Token-checked, compare-and-set publication.** Publishing a feature value is an atomic Lua script: *publish only if the stored `fence_token` for this key is ≤ my token*; otherwise **abandon the result and log a fenced-write rejection**. Worker A above is rejected at t300 because B already stamped a higher token. This — not the lease — is what actually guarantees correctness; the lease is only an efficiency optimisation to avoid duplicate work.
4. **Atomic value + provenance publication.** The value, its `FeatureQuality` (§2.3), its `MarketContextKey`, and the `fence_token` are written in **one** atomic operation (single Lua script / single hash `HSET` of a serialized envelope). A reader must never be able to observe a new value against old provenance, or a value whose freshness/derivation stamp belongs to a different computation.
5. **Explicit crash recovery.** Lease loss is a first-class event: on renewal failure the holder **cancels or abandons** the in-flight compute rather than racing to publish; on restart a worker never assumes ownership. Orphaned "compute-in-progress" sentinels are TTL-bounded so a crashed leader cannot wedge a key permanently (the stampede guard must not become a deadlock).
6. **Required tests before any feature-store code ships.** (a) lease expiry mid-compute → the slow writer's publish is REJECTED, not applied; (b) two concurrent acquirers → strictly-increasing tokens, exactly one publish wins, and it is the higher token; (c) hard-kill the leader mid-compute → another worker acquires within the lease bound and no partial/half-provenance value is ever readable; (d) renewal starvation under event-loop lag (the renewer must survive the conditions that caused the 2026-07-13 seizure); (e) clock skew between planes does not affect ordering (tokens are monotonic counters, **not** timestamps).

**Applicability rule:** any feature whose worst-case compute can exceed 1/3 of its lease MUST use the fenced path. In practice that is all of them, so **fencing is mandatory for every feature writer**, not an opt-in for the auction block.

### 2.3 `DataCapabilities` (a SET) + `FeatureQuality` (with a derivation grade) — replaces the v1 scalar tier ladder

**The v1 monotone ladder (`REAL_L2 > REAL_L1_TRADES > REAL_TICKS > BAR_PROXY > UNAVAILABLE`) is DELETED.** It was wrong: depth updates, executed trade prints, and sized BBO are **orthogonal** data facts, not rungs on one total order. A venue can push sized BBO without a trade tape, or a trade tape without per-level depth; "L2 > L1 > ticks" implies a containment that does not exist.

**v2's capability NAMES were also wrong (C1) and are DELETED.** v2 listed `TRADE_PRINTS` as an available capability and mapped `market_ticks → TRADE_PRINTS`. **We have never had trade prints.** `market_ticks` is a quote-snapshot table (§1.1): `time, symbol, ltp, o/h/l/c, volume (CUMULATIVE), oi, bid, ask, bid_qty, ask_qty`. No trade id. No last-trade quantity. No broker aggressor side. The following v2 claims are **struck from this ADR in their entirety** and must not be carried into any implementation:

- ~~`market_ticks → TRADE_PRINTS`~~
- ~~"84% of instruments satisfy `{TRADE_PRINTS, SIZED_BBO}`"~~
- ~~"Convergence CVD is RECONSTRUCTED from a real trade tape"~~
- ~~"today's data can satisfy the proposed scalp requirement"~~

**`DataCapabilities`** — what data actually exists for a `(canonical_contract_id, provider)` at a MarketContext key:

```
DataCapabilities:
  available: set[Capability]   # subset of what we ACTUALLY have:
    QUOTE_UPDATES        # a stream of quote snapshots (ltp + bid/ask), event-timestamped
    CUMULATIVE_VOLUME    # session-cumulative traded volume on each snapshot (NOT per-trade size)
    SIZED_BBO            # top-of-book bid/ask WITH sizes (bid_qty/ask_qty > 0)
    OHLCV_1M             # completed 1-minute bars
    OPTION_CHAIN         # chain quotes / strikes
    GREEKS               # delta/gamma/theta/vega/iv/tte (a capability in its OWN right —
                         #   never inferred from an order-flow enum)
    OPEN_INTEREST        # OI

  # Declared, and DELIBERATELY ALWAYS ABSENT today — structurally unavailable, not merely missing:
    BROKER_AGGRESSOR_PRINTS  # executed trades with per-trade size and a BROKER-SUPPLIED aggressor side.
                             #   Indian retail broker feeds do not publish this (analytics/orderflow.py
                             #   module docstring). It is NOT persisted anywhere and cannot be back-derived.
    DEPTH_L2                 # per-level book. The only real depth is data_router.subscribe_depth:734,
                             #   ref-counted to auction book symbols and UN-PERSISTED.
```

Both absent capabilities are kept **named in the vocabulary on purpose**: a policy must be able to *declare* that it needs them, so the runner can emit an honest, permanent `BLOCKED` with `missing_reason="capability_structurally_unavailable"` instead of a lane quietly substituting a heuristic and calling it order flow.

**`FeatureQuality`** — attached to every feature the store realizes:

```
FeatureQuality:
  feature_name, timeframe
  derivation: enum { OBSERVED, MODELLED_FROM_QUOTES, MODELLED, BAR_INFERRED }
    # OBSERVED             — the value IS the datum the provider sent: ltp, bid, ask, bid_qty, ask_qty,
    #                        cumulative volume, OI, a broker greeks snapshot, a completed bar.
    # MODELLED_FROM_QUOTES — the INPUTS are observed quotes/cumulative volume, but the OUTPUT is an
    #                        ATTRIBUTION we never observed. Every buy/sell split in this system is here:
    #                        CVD, footprint buy/sell, aggression, absorption, delta, order-flow imbalance.
    #                        The quote stream is observed; the attribution is a heuristic (C1).
    # MODELLED             — computed by a model from observed inputs, with no attribution claim
    #                        (greeks from a pricing model; the synthetic depth ladder).
    # BAR_INFERRED         — inferred from OHLCV bars because no finer stream was available at all.
  # NOTE: v2's RECONSTRUCTED grade is DELETED. "Reconstructed" implied rebuilding a value from a
  # lower-level OBSERVED stream of the same kind (the Lee-Ready sense: signing real prints against a
  # real book). We have no print stream, so nothing in this system was ever RECONSTRUCTED. Anything
  # v2 graded RECONSTRUCTED is MODELLED_FROM_QUOTES.
  source: str                       # market_ticks_quotes | bar_inference | option_chain_snapshot | pricing_model | ...
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
             requires_all={QUOTE_UPDATES, CUMULATIVE_VOLUME, SIZED_BBO},  # what must be present
             allowed_derivations={MODELLED_FROM_QUOTES},   # BAR_INFERRED refused for this policy
             minimum_coverage=0.90,
             maximum_age=5s)
```

Convergence's `real_tick_cvd` gate is exactly this: `requires_all={QUOTE_UPDATES, CUMULATIVE_VOLUME}` (plus `SIZED_BBO` where the sign heuristic uses bid/ask), `allowed_derivations={MODELLED_FROM_QUOTES}` — its `bar_proxy` is `BAR_INFERRED` and is refused. **Note the honest reading: that gate does not distinguish "real" from "modelled" flow; it distinguishes flow modelled from a DENSE quote stream from flow inferred from SPARSE bars.** Both are attributions. It is still a worthwhile gate — quote-density materially changes the heuristic's error — but it is not, and never was, a real-tape gate. Greeks sizing is `requires_all={GREEKS}`, `allowed_derivations={OBSERVED}` for the broker-snapshot path or `{OBSERVED, MODELLED}` if a policy accepts model greeks — decided per policy, **never** by an order-flow enum.

**Mechanical mapping from what exists today (corrected — every buy/sell attribution regraded):**

| Lane / feature | Today's label (file:line) | Capabilities present | Derivation stamped (v3) | Was (v2) |
|---|---|---|---|---|
| Convergence CVD | `cvd_source=="market_ticks"` (`engine.py:326`) | QUOTE_UPDATES + CUMULATIVE_VOLUME (+SIZED_BBO where sizes present) | **MODELLED_FROM_QUOTES** | ~~RECONSTRUCTED~~ |
| Convergence CVD (fallback) | `cvd_source=="bar_proxy"` | OHLCV_1M only | BAR_INFERRED | BAR_INFERRED |
| Convergence footprint / buy-sell split | `engine.py:33 build_footprint` — diffs CUMULATIVE volume, guesses side from `ltp` vs `bid`/`ask`/prev `ltp` | QUOTE_UPDATES + CUMULATIVE_VOLUME + SIZED_BBO | **MODELLED_FROM_QUOTES** | ~~RECONSTRUCTED~~ |
| Auction OF (book path) | `order_flow_source` `tick_reconstruction[_book]` (`live.py:1292`) | QUOTE_UPDATES + SIZED_BBO + CUMULATIVE_VOLUME | **MODELLED_FROM_QUOTES** (the label "tick_reconstruction" is a misnomer — it reconstructs from quotes) | ~~RECONSTRUCTED~~ |
| Auction OF (bar path) | `bar_inference` (`live.py:1320`) | OHLCV_1M | BAR_INFERRED | BAR_INFERRED |
| Auction aggression / absorption / delta / OFI / toxicity | derived inside the OF engines | as their OF path | **MODELLED_FROM_QUOTES** (or BAR_INFERRED on the bar path) | ~~implied "real"~~ |
| Auction "depth ladder" | synthetic decay ladder anchored to `total_buy_qty` (`live.py:1462-1484`) | **NOT DEPTH_L2** (fabricated) — SIZED_BBO at most | MODELLED | MODELLED |
| Commodity OF | `of_source` from `tick_signed_volume_overrides` (`commodity_mp_signal.py:264,1112`) | QUOTE_UPDATES + CUMULATIVE_VOLUME where covered | **MODELLED_FROM_QUOTES** (coverage<1 → BAR_INFERRED on the gap) | ~~RECONSTRUCTED~~ |
| S2 OF | evaluator without ticks → `of_source="bar_inference"` (`strategy2_mp_of.py:632`) | OHLCV_1M | BAR_INFERRED | BAR_INFERRED |
| Directional OF | none (positioning is the proxy) | — | n/a (`missing_reason="of_unavailable_by_design"`) | n/a |
| Quote / L1 fields themselves (ltp, bid, ask, sizes, OI, cumulative volume) | `market_ticks` columns | QUOTE_UPDATES, SIZED_BBO, CUMULATIVE_VOLUME, OPEN_INTEREST | **OBSERVED** | — |
| Greeks (indices) | broker snapshot copy, IV %→fraction (`greeks_enrichment.py:100-159`) | GREEKS + OPTION_CHAIN | OBSERVED | OBSERVED |
| Greeks (stocks) | no chain source (`greeks_enrichment.py:57-63` maps 5 indices only) | — | n/a (`missing_reason="no_chain_snapshot_for_underlying"`) | n/a |
| Option candle IV | 97% NULL live (§3.4 DB) | OPTION_CHAIN partial | n/a (`missing_reason="option_greeks_rest_null"`, completeness≈0.03) | same |
| **Anything requiring a real tape** | — | **BROKER_AGGRESSOR_PRINTS ABSENT** | permanently BLOCKED, `missing_reason="capability_structurally_unavailable"` | ~~claimed available~~ |

**Coverage, recomputed honestly (live measurement, Sat 2026-07-18 — same query, corrected interpretation):**

| Capability | Coverage over the `market_ticks` 2-day cohort (152 symbols / 1.99M rows) |
|---|---|
| `QUOTE_UPDATES` | **100%** of the cohort — this is what the table IS |
| `CUMULATIVE_VOLUME` | **100%** of the cohort (session-cumulative counter, not per-trade size) |
| `SIZED_BBO` | **~84%** of rows carry `bid_qty>0 AND ask_qty>0`. **This 84% measures SIZED BBO ONLY.** v2 reported it as "84% satisfy `{TRADE_PRINTS, SIZED_BBO}`" — the trade-print half of that claim was never measured and was never true. |
| `OHLCV_1M` | present for the candle-covered universe (spot/premium candle tables) |
| `OPTION_CHAIN` / `GREEKS` | **5 symbols (indices) only** — stocks have no chain-greeks source (§3.4) |
| `OPEN_INTEREST` | present on quotes + chain where the provider supplies it |
| **`BROKER_AGGRESSOR_PRINTS`** | **0% — structurally absent.** Not persisted, not received, not derivable. |
| **`DEPTH_L2`** | **0% — structurally absent.** Only `data_router.subscribe_depth:734`, ref-counted and un-persisted. |

So there are **two** structurally-absent capabilities, not one. And the practically important consequence is the opposite of what v2 concluded: `DEPTH_L2`'s absence is survivable for most policies, but `BROKER_AGGRESSOR_PRINTS`'s absence means **no policy anywhere in this system can require observed order flow** — the honest ceiling for every OF-dependent lane is `MODELLED_FROM_QUOTES`.

### 2.4 `EvaluationResult` — the union output (replaces v1 `SignalIntent`)

v1's `SignalIntent` was lossy in four ways the review flagged: (a) BUY-future ≠ BUY-call collapsed into one `direction` enum; (b) `FLAT` is a lifecycle *state*, not a direction; (c) one thesis can imply *multiple legs* a single-instrument shape cannot carry; (d) a blocked/watching evaluation was forced to return `None`, hiding *why*. Replace with a state-carrying, multi-leg envelope:

```
EvaluationResult:
  # ── IDENTITY & LIFECYCLE (see 2.4.1 — added in v3, C3) ──────────────────────
  evaluation_id                   # PK. Unique per OBSERVATION (one policy, one instrument, one cycle).
  setup_id                        # STABLE across the whole thesis lifecycle. See 2.4.1.
  revision                        # 1,2,3… monotonic within a setup_id
  previous_evaluation_id          # the evaluation this one supersedes (null on revision 1)
  action_key                      # DETERMINISTIC idempotency key — see 2.4.1
  consumed_at, consumed_by        # set when an actor acts on this result (null until then)

  strategy_id                     # ⊕ all (registry key; agent_positions.strategy_key)
  strategy_version                # → pins per-lane policy version (A/B + reconciliation)
  as_of, feature_snapshot_ids[]   # → the MarketContext key + FeatureQuality snapshot ids this was computed against

  state: enum { WATCHING, ARMED, ACTIONABLE, BLOCKED, EXITING,
                EXPIRED, INVALIDATED, COMPLETED }        # last three are TERMINAL (v3)
    # WATCHING    — thesis forming, no action
    # ARMED       — conditions nearly met; pre-positioned
    # ACTIONABLE  — intents[] should be acted on now
    # BLOCKED     — a requirement failed; blockers[] says which (NEVER returned as None)
    # EXITING     — thesis invalidated while a position is open; manage()/exit path owns it
    # EXPIRED     — TERMINAL: thesis.validity.expires_at passed without action
    # INVALIDATED — TERMINAL: the setup's premise broke (level lost, regime flipped, contract rolled)
    # COMPLETED   — TERMINAL: the setup was acted on and is fully resolved (closed or filled-and-managed-out)

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

#### 2.4.1 Lifecycle identity and idempotency — a state enum is NOT enough (C3)

Rev 2 had two concrete holes. First, §4.1's `order_intents` table referenced an **`evaluation_result_id` that the contract never defined** — the ledger keyed off a field that did not exist. Second, and worse: **a state enum alone cannot prevent duplicate action.** A policy re-evaluates every cycle. If the setup conditions hold for six consecutive cycles, the policy emits six `ACTIONABLE` results describing *the same trade*. Nothing in rev 2 says they are the same trade, so a naive runner opens six positions. Convergence already had to solve this by hand with a consumed-setup dedup on `symbol:action:bar_time` (`paper.py:138`) — rev 3 lifts that into the contract instead of leaving each lane to reinvent it.

**The four identities, and what each is for:**

| Field | Cardinality | Changes when | Purpose |
|---|---|---|---|
| `evaluation_id` | one per policy-instrument-cycle | **every** evaluation | audit / provenance: "what did we think at 11:03:15" |
| `setup_id` | one per THESIS | only when a **new** thesis begins | the stable spine: WATCHING→ARMED→ACTIONABLE→EXITING→terminal all share it |
| `revision` | monotonic within `setup_id` | every re-evaluation of that setup | ordering + optimistic concurrency |
| `action_key` | one per **distinct intended action** | when the action itself changes | **the idempotency guarantee** |

**`setup_id` is the load-bearing addition.** It is minted when a policy first forms a thesis and **carried unchanged** through every subsequent state, including into `EXITING` and the terminal states. It survives: state transitions, re-evaluations, capability blips (an `ACTIONABLE` setup that goes `BLOCKED` for two cycles because a feature went stale, then returns, is the **same** setup — not a new one). It does **not** survive: a genuinely new thesis on the same instrument (new `setup_id`), or a terminal state (a `COMPLETED`/`INVALIDATED`/`EXPIRED` setup can never be revived — a later opportunity is a new `setup_id`).

**`action_key` — deterministic, content-addressed, the actual duplicate-prevention.**

```
action_key = hash(
    strategy_id,
    setup_id,
    intent_ordinal,                 # which leg of a multi-leg intent
    canonical_contract_id,          # §2.1c — NOT a provider symbol
    side, effect,
    quantized_target_quantity,      # quantized so float jitter cannot mint a new key
    action_epoch                    # see below
)
```

Properties this must have, stated as requirements on the implementation:
- **Deterministic** — the same intended action recomputed on a different worker, in a different process plane, after a restart, yields the identical key. No wall-clock, no uuid4, no dict-ordering dependence.
- **Stable across benign re-evaluation** — six consecutive `ACTIONABLE` results describing the same trade produce **one** `action_key`. The runner acts on the first and treats the rest as already-consumed.
- **Different when the action is genuinely different** — a size change beyond the quantization step, a different leg, a different side/effect, or a new `action_epoch` produce a new key and are allowed to act again.
- **`action_epoch`** is the explicit, policy-declared re-arm boundary (typically the bar-time of the triggering bar — exactly Convergence's `bar_time` component). It is what permits a *legitimate* second entry on the same setup at a later bar while forbidding same-bar duplicates. A policy that must never re-arm within a setup declares a constant epoch.

**How the ledger keys off these (ties into §4):**

| Ledger element | Key | Enforcement |
|---|---|---|
| `evaluations` (new table) | PK `evaluation_id`; index `(setup_id, revision)`; FK `previous_evaluation_id` | `UNIQUE(setup_id, revision)` — a duplicated revision is a bug, caught at write |
| `order_intents.evaluation_id` | FK → `evaluations.evaluation_id` | the rev-2 dangling reference is now defined |
| `order_intents.setup_id` | denormalized for lifecycle queries | — |
| `order_intents.action_key` | **`UNIQUE(account_id, action_key)`** | **this is the constraint that makes duplicate entry impossible at the DB layer**, not merely unlikely at the app layer. A retried/replayed/re-emitted intent is `INSERT … ON CONFLICT DO NOTHING`. |
| `consumed_at`, `consumed_by` | stamped on the `EvaluationResult` when an actor acts | records **who** consumed it (runner id / plane / operator) and **when** — so an evaluation that was seen-but-not-acted-on is distinguishable from one never delivered |

**Terminal-state rule.** Once a `setup_id` reaches `EXPIRED | INVALIDATED | COMPLETED`, the runner refuses to emit any further non-terminal result for it; the policy must mint a new `setup_id`. This is what actually stops "repeated ACTIONABLE evaluations reopening the same setup" — the state machine forbids the transition and the `UNIQUE(account_id, action_key)` constraint backstops it if the state machine is ever wrong.

### 2.5 The strategy interface — and the policy runner validates, NOT the store

```
class StrategyPolicy(Protocol):
    def entry_requirements(self) -> Requirements:
        # capability SETS + allowed derivations + minimum_coverage + maximum_age per feature (§2.3),
        # for RISK-INCREASING decisions. DECLARATION only — it does not fetch, gate, or decide.

    def management_requirements(self) -> ManagementRequirements:
        # SEPARATE and deliberately WEAKER — what manage() needs to act on an EXISTING position.
        # Split into three bands (§2.5.2): mark_requirements / thesis_requirements / emergency_requirements.

    def evaluate(self, context: MarketContext) -> EvaluationResult:
        # pure decision over a pre-built context. No IO, no raw-table fetches, no feature compute.
        # (auction analyze() is already pure CPU: service.py:61,183)
        # ALWAYS returns an EvaluationResult — WATCHING/ARMED/ACTIONABLE/BLOCKED/EXITING — never None.

    def manage(self, context: MarketContext, position: Position,
               open_orders: list[Order], original_thesis: Thesis) -> list[ManagementAction]:
        # LANE-OWNED exits stay here: Convergence CVD-reversal, Directional flip-confirmation,
        # Commodity cooldowns, Auction exit-confirmation cycles. Returns 0..N actions
        # (amend order, reduce, close leg, roll).
        # The shared governor never DECIDES a discretionary close — but see §2.5.2: it DOES own
        # emergency management, which is not the same thing and must not be blocked by stale alpha.
```

#### 2.5.1 Ownership of ENTRY validation (P0-3, retained)

The **feature store SUPPLIES data** — it never "refuses to build" a result and never returns `None`. The **policy runner** does the validation: it reads the policy's `entry_requirements()`, checks them against `MarketContext.capabilities` + `feature_quality`, and:

- if satisfied → calls `evaluate(context)` and passes the result through;
- if a required capability/derivation/coverage/age is not met → emits a `BLOCKED` `EvaluationResult` with `blockers[]` populated, **without** calling `evaluate()` (so `evaluate()` bodies never see insufficient data).

This generalises today's `real_tick_cvd` / `execution_ready` gates into one visible, uniform mechanism. Every place the v1/v2 text said "the store refuses to build a SignalIntent" / "returns `None`" is replaced by this runner-emits-`BLOCKED` rule.

#### 2.5.2 The `manage()` path — separate requirements and a degraded-data state machine (C2)

**The defect rev 2 shipped.** Rev 2 had exactly one fail-closed rule ("skip `evaluate()` on insufficient data") and one exit rule ("`manage()` owns exits; the shared governor never closes a position"). Put together, they are unsafe for **held** positions: if a provider outage degrades the data that `manage()` reads, the same gate that correctly refuses new entries also silences the exit logic — and the governor has been told it may not close. The system would sit in a degraded market, holding open risk, with its stop logic gated off by the outage that made the stop necessary. **Blocking an entry costs an opportunity; blocking an exit costs money.** They cannot share a contract.

**The corrected rule — classify by EFFECT ON RISK, not by code path.**

| Action class | Examples | Data contract |
|---|---|---|
| **RISK-INCREASING** | OPEN, ADD, roll into a larger position, widen a stop, increase leverage | **FAIL CLOSED.** Full `entry_requirements()` (or `thesis_requirements()` for adds) must be satisfied. Stale/absent required alpha data ⇒ BLOCKED. |
| **RISK-REDUCING** | CLOSE, REDUCE, tighten a stop, CANCEL a working order, hedge that strictly reduces net exposure | **PERMITTED under degradation.** Requires only a **fresh EXACT-CONTRACT quote** (§2.5.2 mark contract) — *not* the alpha features. Order cancellation requires no market data at all. |
| **EMERGENCY MANAGEMENT** | protective stop trigger, broker-native stop already resting at the venue, catastrophe liquidation, stale-mark escalation | **NEVER data-gated on alpha.** Runs on the mark band or, in the worst case, on no data at all (see the escalation ladder). |

**Three requirement bands, declared separately by each policy:**

```
ManagementRequirements:
  mark_requirements:      # to VALUE the position and to act on it at all
      requires_all={QUOTE_UPDATES}
      exact_contract=True          # the quote MUST be for the held canonical_contract_id itself —
                                   #   never an underlying proxy, never a stale chain snapshot,
                                   #   never a modelled premium. This is the ONE hard requirement.
      allowed_derivations={OBSERVED}
      maximum_age=<per-horizon; e.g. 15s intraday / 60s positional>
  thesis_requirements:    # to make a DISCRETIONARY, thesis-driven exit decision
      # the lane's real exit signals: Convergence CVD-reversal, Directional flip-confirmation,
      # Commodity cooldown, Auction exit-confirmation. May require MODELLED_FROM_QUOTES OF etc.
  emergency_requirements: # protective stops / catastrophe
      requires_all={}     # DELIBERATELY EMPTY — see the escalation ladder
```

**The degraded-data state machine for a HELD position.** `manage()` is always called (never skipped); the runner passes it a `data_state` and `manage()` is contractually limited to the actions that state permits:

```
      ┌──────────────────────────────────────────────────────────────────────────────┐
      │ HEALTHY                                                                      │
      │   mark fresh AND thesis features satisfy thesis_requirements                 │
      │   → ALL actions permitted (incl. ADD / risk-increasing)                      │
      └───────────────┬──────────────────────────────────────────────────────────────┘
                      │ thesis features stale/absent, mark still fresh
                      ▼
      ┌──────────────────────────────────────────────────────────────────────────────┐
      │ THESIS_DEGRADED                                                               │
      │   → RISK-INCREASING: BLOCKED. ADDs refused, stops may not be widened.         │
      │   → RISK-REDUCING:   PERMITTED (close/reduce/tighten/cancel on the mark).     │
      │   → protective stops: ACTIVE and evaluated on the mark.                       │
      │   → the position is flagged "managed without thesis" in the read model.       │
      └───────────────┬──────────────────────────────────────────────────────────────┘
                      │ exact-contract mark goes stale past mark maximum_age
                      ▼
      ┌──────────────────────────────────────────────────────────────────────────────┐
      │ MARK_STALE                                                                    │
      │   → RISK-INCREASING: BLOCKED (hard).                                          │
      │   → RISK-REDUCING:   PERMITTED, but ONLY as market/marketable orders —         │
      │       a limit price computed from a stale mark is a fiction. Cancels always OK.│
      │   → STALE-MARK ESCALATION starts: timer + operator alert + (if configured)     │
      │       a scheduled protective flatten. Unrealised P&L is rendered as UNKNOWN,   │
      │       never as the last-known number (the "measurement integrity" doctrine).   │
      └───────────────┬──────────────────────────────────────────────────────────────┘
                      │ escalation timer expires, or provider/session hard-fails
                      ▼
      ┌──────────────────────────────────────────────────────────────────────────────┐
      │ EMERGENCY                                                                     │
      │   → broker-native resting stops are the primary protection (they live at the   │
      │     VENUE and survive our outage entirely — this is why they are preferred      │
      │     over synthetic stops for anything held across a degradation window).       │
      │   → catastrophe liquidation is PERMITTED with NO alpha data and NO fresh mark.  │
      │   → operator notification is mandatory, not best-effort.                        │
      └──────────────────────────────────────────────────────────────────────────────┘
```

Recovery is not automatic-and-silent: a transition back to HEALTHY requires the mark and thesis features to be fresh **and** the freshness to persist for a declared confirmation window, so a flapping provider cannot toggle a position between "protected" and "adding" every cycle.

**Consequences that must be honoured elsewhere in this ADR:**
- **The governor's "never closes a position" rule is scoped.** It means the shared governor never makes a *discretionary, thesis-driven* exit — that stays lane-owned. It explicitly **does** own protective-stop enforcement, stale-mark escalation and catastrophe liquidation, because those must work when the lane's data is gone. §7 states this as a scoped predicate on action-effect.
- **The universe pinning rule (§6.3) is what makes MARK_STALE rare**: a held instrument may never be demoted out of quote coverage. Pinning is now load-bearing for safety, not just tidiness.
- **`entry_requirements()` may be arbitrarily strict; `mark_requirements` may not.** A policy that declares mark requirements it cannot routinely satisfy is mis-specified and fails the step-2 contract review.
- **Required tests:** each state's permitted-action set is asserted directly; a provider outage during an open position must produce a permitted close in shadow; and a test must prove that no configuration of `entry_requirements()` can suppress a protective stop.

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
3. **Order-flow computation — 2 entirely separate stacks:** bar-CVD primitives `analytics/orderflow.py` (its module docstring states plainly that Indian retail brokers don't push public trade prints, so true Lee-Ready CVD is unavailable and these functions *approximate* it from OHLCV bars + L1 snapshots ⇒ `BAR_INFERRED` / `MODELLED_FROM_QUOTES`) used by Convergence/S2/commodity; vs the quote/L1 microstructure stack `auction_intelligence/order_flow/engine.py:18 OrderFlowEngine.compute:27` used by Auction/FMP. Different code, same intent — **and both are attributions, neither is an observed tape** (C1). The label `tick_reconstruction` in the auction path is a misnomer for "reconstructed from quote snapshots"; consolidation must not let that name imply a data source we do not have.
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

### 4.1 ONE causal chain, not competing event streams (C4)

**The defect rev 2 shipped.** Rev 2 listed `fills`, `position_events` and `cash_events` side by side as canonical event tables, with `position_events` described as "the OPEN→REDUCE→CLOSE lifecycle as EVENTS" and `cash_events` as "every capital movement" — each **independently written**. That is three writable truths for one economic reality. Fill truth, position truth and realised-P&L truth can then diverge, and nothing in the schema says which one wins. This is precisely the class of bug the ADR exists to eliminate (§3.3 documents it happening today: Directional sums lifetime P&L from a DB-wide payload sum, `agent_positions` carries per-row `realized_pnl`, Convergence sums a JSON array — three answers).

**The rule for v3: there is exactly ONE causal chain, and everything else is a deterministic projection of it.**

```
   EvaluationResult (§2.4.1)          ← what we THOUGHT (provenance, not causal to the book)
            │  evaluation_id / setup_id / action_key
            ▼
   order_intents                      ← what we DECIDED to do          [CAUSAL, level 1]
            │
            ▼
   order_events                       ← what happened TO THE ORDER     [CAUSAL, level 2]
            │   (PLACED / ACKED / AMENDED / PARTIAL / CANCELLED / REJECTED / EXPIRED)
            ▼
   fills                              ← what actually EXECUTED         [CAUSAL, level 3 — the ONLY
            │                            source of position and realised-P&L change]
            ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │  DETERMINISTIC PROJECTIONS — derived, rebuildable, never hand-written │
   │    positions           (qty, avg price, per (account_id, contract))  │
   │    realised_pnl        (per position / account / period)             │
   │    cash / balances     (reserved, free, equity)                      │
   └────────────────────────────────────────────────────────────────────┘

   marks                              ← VALUATION events, a SEPARATE stream (see below)
```

**Schema:**

```
strategy_accounts     # one funded book per policy-instance (replaces the implicit "each lane owns ₹1M")
  account_id (PK), strategy_id, strategy_version, market, base_currency,
  opening_capital, status, created_at

evaluations           # the EvaluationResult lifecycle record (§2.4.1)
  evaluation_id (PK), setup_id, revision, previous_evaluation_id, strategy_id, strategy_version,
  state, as_of, feature_snapshot_ids[], consumed_at, consumed_by
  UNIQUE (setup_id, revision)

order_intents         # what a policy WANTED (maps 1:1 to EvaluationResult.intents[])
  intent_id (PK), account_id,
  evaluation_id  FK -> evaluations.evaluation_id,     # rev 2's dangling reference, now DEFINED
  setup_id, action_key,
  canonical_contract_id, side, effect,
  target_quantity|target_exposure, order_preferences, as_of, feature_snapshot_ids[]
  UNIQUE (account_id, action_key)                     # duplicate-entry impossible at the DB layer (§2.4.1)

orders                # order IDENTITY only — current status is a PROJECTION of order_events
  order_id (PK), intent_id, account_id, canonical_contract_id, side, effect,
  order_type, requested_qty, limit_price, tif, placed_at
  # NOTE: no mutable `status` column written by hand. Status is projected from order_events.

order_events          # every state change of an order          [CAUSAL]
  order_event_id (PK), order_id, account_id, sequence,
  event_type(PLACED|ACKED|AMENDED|PARTIAL_FILL|FILLED|CANCELLED|REJECTED|EXPIRED),
  payload, exchange_time, received_time
  UNIQUE (order_id, sequence)

fills                 # individual executions        [CAUSAL — the ONLY position/P&L mover]
  fill_id (PK), order_id, order_event_id, account_id, canonical_contract_id,
  qty, price, fees, liquidity_flag, exchange_time, received_time

marks                 # VALUATION only — never causal, never touches realised P&L
  mark_id (PK), account_id, canonical_contract_id, mark_price, mark_source,
  feature_quality_ref, event_time, is_stale
  # Marks move UNREALISED value and drive the §2.5.2 mark-freshness state machine.
  # A mark can NEVER change quantity, average price, cash, or realised P&L.

-- PROJECTIONS (derived; fully rebuildable by replaying fills in order; never written directly):
positions             # (account_id, canonical_contract_id) -> qty, avg_price, realised_pnl_to_date
account_balances      # (account_id) -> cash, reserved, equity
position_events       # a DERIVED VIEW of the OPEN/ADD/REDUCE/CLOSE lifecycle, each row carrying
                      #   causing_fill_id (NOT NULL). It is a readable projection of the fill stream,
                      #   NOT an independently authoritative event source. If a projected
                      #   position_event cannot name the fill that caused it, that is a bug.
cash_events           # a DERIVED projection of capital movement (RESERVE/RELEASE/REALIZE/FEE),
                      #   each row carrying causing_fill_id or causing_intent_id (for reservations).
                      #   Reservations are caused by intents; realisations only by fills.
```

**Three invariants the implementation must enforce (and test):**
1. **Only fills move the book.** No process may write `positions`, `realised_pnl`, or a realisation row in `cash_events` except the projector, and the projector reads only `fills`. A "correction" is a **new compensating fill event**, never an UPDATE.
2. **Every projection row names its cause.** `position_events.causing_fill_id` and `cash_events.causing_fill_id|causing_intent_id` are NOT NULL. This is what makes the UI drill-down (§5, G9) portfolio → order → fill → source event mechanically possible rather than aspirational.
3. **Projections are rebuildable and periodically REBUILT.** A scheduled job re-derives `positions`/`balances` from the fill stream into a shadow table and diffs. A non-zero diff is a P0 alert, because it means something wrote a projection out of band.

Positions are keyed `(account_id, canonical_contract_id)` — so two strategies CAN hold the same contract in different accounts, the exact thing `agent_positions.symbol UNIQUE` forbids (§3.3).

**Deleted from rev 2:** the description of `position_events` and `cash_events` as independently-written canonical streams, and the `orders.status` mutable column. Both are superseded by the projection model above.

### 4.2 Idempotency key (every imported event)

Every row imported from a source lane carries a unique natural key:

```
(source_lane, source_event_id, event_type, event_sequence)   UNIQUE
```

Re-importing the same source event is a no-op (`INSERT … ON CONFLICT DO NOTHING`), so replays, retries, and dual-run overlaps cannot double-count — the precise hole `paper_trade_book` (no unique key, best-effort insert) has today.

### 4.3 Import: a transactional outbox for DB lanes — and something else entirely for the JSON lanes (C4)

**Rev 2 over-claimed.** It said a lane appends an outbox row "in the SAME local transaction as its book write", and then extended that to the JSON lanes via "a thin adapter". **That extension is impossible.** A filesystem write and a Postgres outbox INSERT cannot participate in one transaction. There is no atomic commit spanning `open()/write()/rename()` and `COMMIT`. Saying "thin adapter" does not create a two-phase commit. Four of the ledger's ten sources are JSON files (§3.3), so this is not an edge case — it is most of the migration.

**Track A — DB-backed lanes (`agent_positions`+`paper_trade_book`, `directional_paper_*`): a real transactional outbox.**
The lane's book write and its outbox INSERT go in the **same Postgres transaction** on the **same connection/pool**. A per-lane import worker drains the outbox and writes the canonical ledger idempotently (§4.2). This is genuinely atomic and is the pattern rev 2 described. Requirement: the outbox row must be written on the *same pool* as the book write — Directional's separate txn path (`directional_options/paper.py:128`) means Directional needs its **own** outbox table in its **own** schema, not a shared one.

**Track B — JSON-file lanes (Auction NSE, Auction commodity, Convergence NSE, Convergence MCX, MACD-Refined, FMP): NOT an outbox. A durable file-state importer with explicit rules.**

Since atomicity across the boundary is unavailable, correctness is achieved by making the file a **replayable, verifiable source** and the importer **idempotent and resumable**:

1. **Atomic file replacement, always.** Every book mutation writes a temp file in the same directory, `fsync`s it, then `rename()`s over the target (POSIX atomic within a filesystem), then `fsync`s the directory. A reader must never observe a partially-written book. Any lane still doing an in-place truncating write is fixed *before* it is imported — a torn JSON book is unrecoverable, not merely late.
2. **Monotonic content generation + content hash.** Each book carries `generation` (monotonic, incremented on every mutation) and `content_hash` (of the canonicalised payload). The importer records the last `(generation, content_hash)` it successfully imported per file. Re-reading an unchanged file is a cheap no-op; a `generation` that goes backwards is a **restore-from-stale-state alarm**, not something to import silently.
3. **Per-event stable ids inside the file.** Every position/order/trade entry carries a lane-assigned stable `source_event_id` written *by the lane at mutation time* (not derived by the importer from array position — array indices shift). Without this, the §4.2 idempotency key cannot be formed. Lanes that do not have such ids today get them added as a **write-only, behaviour-neutral** change in migration step 7.
4. **Durable checkpointing.** The importer's checkpoint (`file path, generation, content_hash, last imported source_event_id, imported_at`) is committed in the **same Postgres transaction** as the canonical rows it derived from that generation. So the checkpoint and the imported events are atomic *with each other* — which is achievable — even though neither is atomic with the file write.
5. **Explicit replay rules.**
   - Re-importing a generation already checkpointed: **no-op** (idempotency key §4.2 + checkpoint).
   - Crash between file write and import: the next poll observes a higher `generation` and imports it. **Nothing is lost, only delayed** — this is the accepted eventual-consistency window, and it is bounded and monitored.
   - Crash mid-import: the transaction rolls back including the checkpoint; the generation is re-imported from the start; `ON CONFLICT DO NOTHING` absorbs any partial prior work.
   - **Missed generations.** A file can advance 41 → 44 while the importer is down; generations 42 and 43 are gone forever because the file holds only current state. The importer therefore **detects the gap** (`generation` jumped by >1) and marks the interval `RECONSTRUCTED_FROM_SNAPSHOT` rather than pretending it saw every event. Reconciliation (§4.4) treats a gap-marked interval as **failing** the acceptance window — it does not silently pass.
   - **File deleted / reset to empty:** never interpreted as "everything closed". It raises and halts that lane's import.
6. **The file remains authoritative for that lane throughout.** Track B is an *observer*. A lane's read path flips to canonical only after §4.4 reconciliation passes with **zero** gap-marked intervals in the window.

**Consequence for sequencing:** Track A lanes are cheap; Track B lanes need the atomic-write + generation + stable-id groundwork first. Migration step 7 (§11) is ordered accordingly.

### 4.4 Reconciliation (full, not just equity)

Before any lane's READ path flips to the canonical ledger, reconciliation must match on ALL of:

- **quantity** per `(account, contract)`;
- **average price** per position;
- **partial fills** (fill count + fill quantities, not just net);
- **fees**;
- **timestamps** (entry/exit/event times within tolerance);
- **reserved capital** per account;
- **realised P&L** (projected from the **fill stream** — the ONE authoritative source (§4.1); never a naive UNION that double-counts a lane writing both a position row and a trade-book row, the Commodity hazard §8);
- **state** (open/closed/partial matches the source book's view);
- **projection self-consistency** — the scheduled rebuild-from-fills diff (§4.1 invariant 3) is zero for the whole window;
- **import completeness** — for Track B lanes, **zero gap-marked generation intervals** in the window (§4.3 rule 5).

The `agent_audit_events` bus (620,560 rows, already cross-lane) is the reconciliation event log. Existing books stay **authoritative** until a lane clears reconciliation for the acceptance window (§11 thresholds).

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
                           #   applicable risk rules + winning verdict/rule_id (§7), reconciliation status vs legacy book
Detail endpoints           # profile / footprint / trade history / order+fill lifecycle drill-downs
```

**Workspaces it powers (the owner's layout):** Command (portfolio/risk/reconciliation health), Structure (profiles/levels), Flow (order flow + FeatureQuality — every buy/sell number labelled `MODELLED_FROM_QUOTES`, per C1), Strategies (every policy's `EvaluationResult` incl. BLOCKED), Risk (scoped rules §7), Research (replay/shadow diffs). Shipping this BEFORE cutover means shadow parity (§11) is inspected by a human on the same surface the strategies will later drive.

### 5.1 A list of read-model OBJECTS is not a UI CONTRACT — and one is now a GATE (G9)

Everything above says *what data* the UI can read. It says nothing about *how the UI stays correct while the data moves*, which is where trading UIs actually fail. **A separate UI contract document must be written and APPROVED before migration step 4 begins.** Step 4 is blocked on it.

**Factual note that shapes this — the UI is not greenfield.** Two phases have **already SHIPPED** on the existing endpoints: `2addf247` ("ui phase 0: one semantic truth contract + metric honesty across every desk") and `79269e7c` ("ui phase 1: instrument-centric Command workspace", `/strategies/market-structure`). HEAD is `79269e7c`. The UI contract must therefore be written to **ABSORB and SUPERSEDE that shipped surface** — reconciling its semantic-truth vocabulary and its Command workspace with the substrate read model — **not** to pretend a clean slate exists. A contract that ignores the shipped surface would fork the UI into two truth vocabularies, which is the exact failure phase 0 was built to end. (A concurrent workstream is separately correcting the shipped order-flow grading to match C1; the UI contract must land on the corrected vocabulary.)

**The UI contract must specify, at minimum:**

1. **One atomic `as_of` / `context_version` envelope across EVERY panel.** The whole workspace renders one coherent instant. A price panel from t and a risk panel from t−9s displayed together is a lie the operator cannot detect. Every response carries the envelope; the shell displays it; panels that cannot honour the current envelope render as *superseded*, not as stale-but-confident.
2. **Snapshot + delta streaming, with a defined RESYNCHRONISATION protocol.** Initial snapshot → sequenced deltas → explicit resync trigger (sequence gap, reconnect, envelope jump, server-initiated invalidation). Resync must be per-panel where possible, not a full page reload.
3. **Sequence gaps, backpressure, and stale-state semantics.** Every stream is sequenced; a detected gap forces resync rather than silent interpolation. Backpressure policy is explicit per stream (coalesce quotes — the 150ms coalesced tick fan-out already exists; **never** coalesce order/fill events, which must be lossless). Stale state has a defined visual contract, and — per §2.5.2 — a stale mark renders unrealised P&L as **UNKNOWN**, never as the last-known number.
4. **Linked instrument / time / crosshair state.** Selecting an instrument, a time, or a crosshair position propagates across panels with defined scope, defined precedence when two panels disagree, and URL-encodable state so a view is shareable and reproducible.
5. **Virtualized universe and blotter.** The universe is 217 underlyings expanding to thousands of subscription-plan instruments (§6.2), and the Directional journal alone is 14,492 rows. Row virtualization, server-side paging/filtering/sorting, and a bounded client working set are contract requirements, not optimizations.
6. **Discrepancy DRILL-DOWN: portfolio → position → order → fill → source event.** Every displayed number must be traceable to the causal event that produced it (§4.1 invariant 2 exists to make this possible). When canonical and legacy books disagree during reconciliation, the operator must be able to reach the differing event in a bounded number of clicks. This is the single most valuable operator feature during the ledger migration.
7. **Alerts, acknowledgement, keyboard workflows, saved layouts.** Alert taxonomy + severity + routing; explicit acknowledge/snooze/escalate with an audit trail (a stale-mark escalation, §2.5.2, is an alert with a mandatory ack); keyboard-first navigation for the workflows an operator repeats; named saved layouts that survive deploys.
8. **Frontend render + update budgets.** Measured, not aspirational: first meaningful paint, panel update p95 under a defined message rate, sustained-stream frame budget, memory ceiling for a long-lived session (a desk stays open all day — a slow leak is a crash at 14:45). Budgets are stated in the same measured form as §11's backend table.
9. **Accessibility conventions.** Keyboard reachability for every action, focus management, contrast that survives the red/green semantics of a P&L display, colour never the sole carrier of meaning (an operator with a colour-vision deficiency must be able to read direction and breach state), and screen-reader labelling for live-updating regions.

**Gate:** step 4 (§11) may not start until this document exists, covers all nine items, explicitly maps the shipped `2addf247`/`79269e7c` surface into it, and is owner-approved.

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

Restated on the corrected §2.3 model — capability SETS + allowed derivations + coverage/age, NOT tiers, and **NOT trade prints** (C1):

| Horizon | Order flow | Bars / profiles | Options / greeks | Regime / VIX | Sessions / rollover | Satisfiable today? |
|---|---|---|---|---|---|---|
| **Scalp** | `requires_all={BROKER_AGGRESSOR_PRINTS}` and/or `{DEPTH_L2}`, coverage≥0.9, age≤5s | completed 3m + current profile | `{GREEKS}` OBSERVED for premium sizing | fresh regime | intraday only | **NO — UNSATISFIABLE.** Both required capabilities are structurally absent (§2.3). Permanently BLOCKED. |
| **Intraday** (S2, auction swing-lite, directional intraday) | `requires_all={QUOTE_UPDATES, CUMULATIVE_VOLUME, SIZED_BBO}`, `allowed_derivations={MODELLED_FROM_QUOTES}` (BAR_INFERRED only in an explicitly-named degraded policy) | completed 3m + 15m + current/prior profiles + quote | `{OPTION_CHAIN, OPEN_INTEREST}` + walls + IV (fraction-unit) | regime + VIX available | current + prior session | **YES** for the ~84% SIZED_BBO cohort; greeks-dependent variants **indices only** |
| **Swing** | `{QUOTE_UPDATES, CUMULATIVE_VOLUME}` preferred (`MODELLED_FROM_QUOTES`); BAR_INFERRED acceptable if the policy declares it | 15m/30m profiles + composite | `{GREEKS, OPEN_INTEREST}` + walls; DTE≥7 expiry-sufficiency | longer-window regime | current + prior + composite | **YES**, greeks-dependent parts indices-only |
| **Positional** (directional positional, CBE) | OF not required (positioning replaces it) | daily + composite profiles | `{GREEKS}` daily / IV percentile (low-IV favors long premium), positioning (OI) | long regime + India-VIX percentile | rollover-safe futures (ContractResolver), multi-session | **PARTIAL** — index-futures MP is a hard NO-GO (`index_futures_candles` = 0 rows) |

**Scalp is UNSATISFIABLE on today's data, and this ADR states that as a conclusion, not a caveat (C1).** Rev 2 claimed the 84% cohort satisfied the scalp requirement; it did not, because that 84% measured **sized BBO only** and the scalp requirement's real content is aggressor attribution and/or book depth — neither of which we receive. A genuine scalp horizon needs `BROKER_AGGRESSOR_PRINTS` and/or `DEPTH_L2`; acquiring either is a **data-feed decision for the owner** (§12), not something the substrate refactor can unlock. Until then, any policy declaring the scalp horizon is permanently `BLOCKED` with `missing_reason="capability_structurally_unavailable"` — which is the honest outcome, and is exactly why the two absent capabilities are named in the vocabulary rather than omitted.

**What the intraday row honestly is.** Convergence's `real_tick_cvd` gate (`engine.py:355`) does real work — dense quote snapshots make the buy/sell heuristic far less wrong than bar inference — but it separates *dense-quote-modelled* flow from *bar-inferred* flow. It does not, and cannot, deliver observed flow. Every intraday policy that leans on order flow must be sized and evaluated with that error in mind.

Also grounded in today's honest gates: the IV-percentile-conditions-size finding (low IV favours long premium) and the `dte<7 skip`. **Hard blockers:** `index_futures_candles` empty (positional index-futures NO-GO) and the greeks-vs-watchlist split — both must be resolved before the positional/options floors can be enforced (step 10, §11).

---

## 7. Risk as SCOPED PREDICATES over dimensions — not a hierarchy (G7)

v1 correctly rejected a *single global* kill (one lane's breach must not halt healthy lanes). v2 replaced it with a six-level **hierarchy** — and that shape is wrong (G7). A hierarchy asserts containment: each level nests inside the one above. These levels **do not nest, they overlap**:

- A **strategy** trades across **exchanges** (Auction runs NSE indices *and* MCX commodities), so STRATEGY is not inside EXCHANGE — nor the reverse.
- An **exchange** is reachable via **multiple providers** (NSE via both Upstox and Fyers), so EXCHANGE is not inside BROKER_ACCOUNT.
- A **broker token is not necessarily a funded account.** Upstox/Fyers tokens are *data + routing credentials* whose limits (`brokers/rate_limiter.py`: Fyers 10/s·200/min·100k/day; Upstox 50/s·2000/30min) are **rate budgets**, while `strategy_accounts` (§4.1) hold *capital*. Rev 2's "BROKER_ACCOUNT" silently merged a rate-limit domain with a capital domain — two different resources with two different breach meanings.
- **Capability** (§2.3) is a property of a *data feed for a contract*, not a container of anything.

Modelling overlapping concerns as a containment tree forces false parent-child relationships and leaves the real question — *what happens when two rules both apply and disagree?* — unanswered.

### 7.1 The model: rules are predicates over independent dimensions

A risk rule is a **scoped predicate** with an explicit selector over six independent dimensions, an action-effect filter, and a declared precedence:

```
RiskRule:
  rule_id, enabled, owner, description
  scope:                                   # each dimension: a value, a set, or ANY (wildcard)
    account:      strategy_account | set | ANY     # capital domain (§4.1)
    strategy:     strategy_id | set | ANY          # policy identity (may span exchanges)
    venue:        exchange/segment | set | ANY     # EXECUTION venue (§2.1a) — NOT a provider
    provider:     data/execution provider | set | ANY   # rate-budget + connectivity domain
    instrument:   canonical_contract_id | underlying | kind | set | ANY
    capability:   capability/derivation predicate | ANY # e.g. "SIZED_BBO frozen", "mark stale"
  action_effect:  set ⊆ {OPEN, ADD, REDUCE, CLOSE, HEDGE, CANCEL, AMEND}   # §2.5.2 classification
  predicate:      a measurable condition (loss, exposure, drawdown, count, freshness, budget burn)
  verdict:        ALLOW | BLOCK | SIZE_MULTIPLIER(x) | REQUIRE_APPROVAL
  precedence:     integer band (see 7.2)
  bypassable_by:  set of flags (e.g. SIGNAL_VALIDATION_UNCAPPED) — per-rule, never global
```

A rule **applies** to a proposed action iff every dimension in its scope matches (wildcards always match) **and** the action's effect is in `action_effect`. Dimensions are ANDed within a rule; independent rules compose per §7.2. This expresses everything the v2 hierarchy expressed, plus the things it could not — e.g. *"block OPEN for strategy S on MCX **via provider Fyers only** when the Fyers budget is starved"*, which has no place in a containment tree.

### 7.2 Precedence and aggregation — stated explicitly, because overlap is the normal case

**Precedence bands** (lower band wins outright; within a band, aggregation rules apply):

| Band | Class | Examples | Note |
|---|---|---|---|
| 0 | **Safety floor** | protective stops, catastrophe liquidation, stale-mark escalation (§2.5.2) | **Cannot be blocked or bypassed by ANY rule or flag.** No configuration may suppress these. |
| 1 | **Operator manual** | operator halt/flatten, manual kill | Human intent outranks automation. Never fires automatically from a single lane's breach. |
| 2 | **Regulatory / venue hard limits** | freeze quantity, lot multiples, venue circuit/session halt | Physically un-exceedable; not a policy choice. |
| 3 | **Capital / margin** | account capital, reserved cash, margin sufficiency | Per `strategy_account`. |
| 4 | **Exposure limits** | per-instrument, per-underlying, per-venue, aggregate cross-lane | Cross-lane roll-up ships observation-only first (§12). |
| 5 | **Data-capability gates** | required capability absent/frozen/stale for the action's effect class | Applies to risk-INCREASING effects only (§2.5.2). |
| 6 | **Discretionary / tuning** | per-lane sizing multipliers, cooldowns, churn dampers | Where today's per-lane governors mostly live. |

**Aggregation rules (deterministic, no ambiguity permitted):**
1. **Any applicable `BLOCK` blocks** — verdicts are not voted on. One block is a block.
2. **`SIZE_MULTIPLIER`s compose multiplicatively**, then the result is clamped by the **most restrictive** absolute cap that applies. Multipliers never compose to >1.0.
3. **`REQUIRE_APPROVAL` dominates `ALLOW`** and is dominated by `BLOCK`.
4. **Lower precedence band wins on conflict.** A band-3 capital block is not overridable by a band-6 lane multiplier.
5. **Band 0 is absolute.** A band-0 safety action executes even when every other rule would block — a stop-loss must never be blocked by an exposure cap, a capability gate, or a stale feed. This is the §2.5.2 contract expressed as risk precedence.
6. **The applicable-rule set and the winning verdict are both RECORDED** on every decision, so the read model can answer "why was this blocked" with the specific `rule_id` (§5.1 item 6) rather than a generic denial.
7. **Bypass is per-rule.** `SIGNAL_VALIDATION_UNCAPPED` (`core/config.py:229`) lists the rules it bypasses; it can never bypass band 0–2, and it is never a global off-switch.

### 7.3 Doctrine preserved

The `SIGNAL_VALIDATION_UNCAPPED` bypass stays a **per-gate/per-rule** property (band 6 and parts of bands 3–4), exactly as today. The Commodity catastrophe kill-switch backstop (`commodity_strategy_agent.py:3357`) — which the flag deliberately does **not** disable — is a **band-0** rule, and this model explains why it is un-bypassable rather than leaving it an exception. A global halt is band 1, operator-only, and never an automatic single-lane-breach fan-out.

**Exits stay lane-owned** — with the §2.5.2 scoping: the shared governor never makes a *discretionary, thesis-driven* exit (that is `manage()`), but it **does** own band-0 safety actions, which must fire when the lane's data is degraded or gone.

**Cross-lane exposure is in the TARGET architecture** — band-4 aggregate rules roll up to a portfolio exposure view. Per the refined owner decision (§12), it **ships as aggregation/alerting FIRST** (observation-only), entry-blocking later and owner-gated. Without this layer in the target, "shared risk" is just N independent ₹1M books behind one interface — which is explicitly NOT the goal.

---

## 8. What BREAKS if Consolidated Naively (load-bearing)

**Evaluation union**
- **Direction-encoding collapse.** A short future and a long put both profit when spot falls but are opposite balance-sheet positions; a single `direction` enum mis-nets. `EvaluationResult` keeps `thesis.bias` (the view) distinct from each `Intent`'s `instrument(ContractRef) + side + effect` (the leg) — a bearish thesis expressed as `BUY <PE>` (long premium) vs `SELL <FUT>` (short future) never collapses (§2.4).
- **Confidence-scale merge.** Convergence emits `score` 0-100 + `quality` A+/VALID (`engine.py:421,431`), not `[0,1]`. A min-confidence gate assuming `[0,1]` (Auction `min_model_confidence=0.55`) passes every Convergence signal or rejects all. Normalize `thesis.confidence` to `[0,1]` but keep the raw in `conviction_extras`; never collapse.
- **Setup-dedup / lifecycle loss.** Convergence's consumed-setup dedup depends on a stable setup identity (`symbol:action:bar_time`, `paper.py:138`). **A state enum alone does not solve this** (C3): six consecutive `ACTIONABLE` results describing one trade are six results. The fix is the §2.4.1 identity block — a stable `setup_id`, a deterministic `action_key` with `UNIQUE(account_id, action_key)` at the DB layer, and terminal states (`EXPIRED|INVALIDATED|COMPLETED`) a setup can never leave. That is what stops re-opening the same setup (the churn flip-confirmation was built to stop).
- **BLOCKED must be visible, not `None`.** A policy that can't act because a capability/coverage/age failed returns `state=BLOCKED` with `blockers[]` — the read model (§5) shows why. The v1 "store returns `None`" hid this.

**Risk governor**
- **Bypass flattening** (§7) and **kill-switch fan-out** — a global automatic kill firing on one lane's breach halts healthy lanes. The scoped-predicate model (§7) scopes kills instead: a breach fires only the rules whose `account`/`strategy`/`venue`/`provider`/`instrument`/`capability` selectors actually match, and a global halt is band-1 **operator-only**, never an automatic single-lane fan-out.
- **Cross-lane exposure changes behavior.** Every lane sized against its OWN ₹1M `strategy_account`. A shared exposure cap suppresses entries that pass today — new alpha-affecting policy, shipped observation-first/opt-in/measured (§7, §12), never a side effect of the merge.

**Ledger**
- **Two realized-pnl truths** — Directional sums lifetime from a DB-wide payload sum (`paper.py:875-892`); `agent_positions` has per-row `realized_pnl`; Convergence sums a JSON array. A naive UNION double-counts a lane that writes both a position row and a trade-book row (Commodity). **The FILL STREAM is the ONE authoritative source; realised P&L, positions and cash are deterministic PROJECTIONS of it** (§4.1, C4). Rev 2's answer — "`position_events` realized deltas" — was itself a competing truth, because rev 2 wrote `position_events` independently of fills; that is now a derived view whose every row must name its `causing_fill_id`.
- **Capital-semantics drift** — S1's `reconcile_available_capital` rebuilds cash from realized+reserved (`portfolio.py:311`, its own warning at `:106`). Unscoped in a shared store it corrupts S1's cash. Every capital computation is `strategy_account`-scoped (`cash_events.account_id`).
- **`agent_positions.symbol UNIQUE` blocks multi-lane ownership** (§3.3) — the reason positions are keyed `(account_id, canonical_contract_id)` in the new schema (§4.1).
- **`paper_trade_book` has no idempotency + best-effort insert** (§3.3) — the reason the new ledger uses the `(source_lane, source_event_id, event_type, event_sequence)` unique key, a real outbox for the DB lanes, and a generation/hash/checkpoint importer for the JSON lanes where an outbox is **impossible** (§4.3, C4).
- **Journal volume** — 14,492 Directional journal rows vs hundreds in JSON; keep per-lane retention as store policy (`ORDER_LOG_LIMIT=2000`, `paper.py:16`) so imports don't set an unbounded default.

**MarketContext / substrate**
- **Context is not free to share.** Lanes compute at different cadences/scopes (S2 throttles MP to 90s `:89`; Convergence adaptive 45-90s `:101`; each caches its own MP). One eager build re-introduces the 2026-07-13 event-loop seizure. MarketContext is **lazily materialized per `(canonical_contract_id, provider, feature, watermark)`** and reuses existing caches, under the single-writer/leader-lock + stampede guard (§2.2).
- **Split-brain on provider failure.** If the store is unavailable for a RISK-INCREASING decision, the runner FAILS CLOSED (emits BLOCKED) — it does NOT silently drop back to the lane's old in-process compute, which would be two answers over one book (§2.2). Read-only surfaces may show the labelled fallback.
- **Fail-closed must not trap held positions** (C2). The rule above is scoped to risk-increasing actions. Applying it uniformly — as rev 2 did — means a provider outage gates off the exit logic needed *during* the outage while the governor is simultaneously told it "never closes a position". The `manage()` path has its own weaker requirements and a degraded-data state machine (§2.5.2), and band-0 safety actions are un-blockable by any rule (§7.2).
- **A stale writer can publish over a fresh one** (C5). A 200–320s compute under a bare `SET NX PX` lease republishes stale output after the lease expires and a second worker has already published. Fenced, renewable leases with token-checked compare-and-set publication (§2.2.1) are mandatory for every feature writer.
- **Capability must fail closed but roll out advisory-first.** Only Convergence expresses capability today; S1/Directional trade on bars. Making requirements blocking on day one **newly blocks working lanes** — introduce them as advisory metadata (step 5/6), enforce as blocking only in step 10, measured (§11).
- **Renaming a data source does not create one** (C1). Consolidating the two order-flow stacks must not let the label `tick_reconstruction` imply an aggressor tape. Every consolidated buy/sell number is stamped `MODELLED_FROM_QUOTES`, in the ledger, in the read model, and in any research output — otherwise the refactor launders a heuristic into an apparent observation, and every downstream sizing/confidence decision inherits a false confidence.

---

## 9. Non-Goals (what this ADR does NOT do)

- **Does not merge the four policies** into one engine (§1.3). `evaluate()` bodies stay separate and lane-owned.
- **Does not change any lane's trading edge, entry logic, or exit logic** in steps 1–9. Behavior-changing gates (sufficiency, exposure) are step 10 only, opt-in and measured.
- **Does not enable cross-lane exposure netting as part of the refactor.** It becomes *possible* (§7) but ships observation-only pending owner sign-off.
- **Does not build a `DEPTH_L2` feed, and does not acquire `BROKER_AGGRESSOR_PRINTS`.** No per-level book is ingested; every depth ladder today is synthetic/`MODELLED` (`live.py:1462-1484`). No aggressor-tagged trade tape is received at all (C1). Acquiring either is a **data-feed decision for the owner** (§12), not a refactor — and until one is made, the scalp horizon stays unsatisfiable (§6.4) and the ceiling for all order flow stays `MODELLED_FROM_QUOTES`.
- **Does not improve the accuracy of order-flow attribution.** It labels it honestly. Whether a better estimator exists on quote data is a research question, explicitly out of scope here.
- **Does not fix the option-candle REST-only defect or fill stock greeks as a precondition** — sequenced into step 10 because the options floors depend on it; the substrate ships without waiting on it.
- **Does not touch live-money paths** — all lanes are paper; legacy `orders/positions/paper_sessions` are BRIDGED, retired only after the event ledger is authoritative and no live reader exists (§12).
- **Does not build the index-futures MP sleeve** (`index_futures_candles` empty — data NO-GO).
- **Does not add a second Market-Profile or order-flow algorithm** — it consolidates onto one of each (retiring `MarketProfileBuilder`), it does not invent a third. Note that retiring an engine is an **algorithm replacement**, not a refactor, and is therefore governed by the semantic-fixture parity track, not byte-parity (§11, G8).
- **Does not promote any existing table into the canonical ledger** (§3.3, §4).

---

## 10. How this maps to the reference architectures

| Reference | Their construct | This ADR's mapping |
|---|---|---|
| **QuantConnect** | Universe → multiple Alphas → Portfolio Construction → Risk → Execution | Selection universe + subscription plan (§6) → policy `evaluate()` alphas emitting `EvaluationResult` → opt-in Portfolio Construction/netting (step 10) → scoped-predicate `RiskGovernor` (§7) → canonical event ledger (§4) |
| **NautilusTrader** | Central instruments / data / cache / risk / execution + **reconciliation** | canonical instrument identity + `ProviderAlias` + `ContractResolver` (instruments — Nautilus likewise separates venue from data client) + `MarketContext`/Feature Store (data + cache) + scoped-predicate `RiskGovernor` + an order-events/fills event ledger with derived positions (Nautilus's own model: fills are causal, positions are derived); the **full-field reconciliation gate** (§4.4) before any read-path flip is Nautilus's reconciliation pattern directly |
| **Hummingbot** | Market-data controllers vs position-owning executors | Feature Store providers = data controllers (compute-once, no positions, single-writer); policy `manage()` + lane exits = position-owning executors — the exact split this ADR keeps (§2.5, §7) |
| **Freqtrade** | Short refreshed informative universe + tiered subscription | Tier-B ranked/rotating/readiness-gated ~50 (`service.py:175-227`) = the refreshed informative *selection* universe; A/B/C/D quota-class = the *subscription plan* (§6), enforced at the shared REST token bucket |

---

## 11. Migration — the 10 steps (replaces v1's 7-step sequence)

Ordered so contracts + read models (low risk, reversible) precede shared compute (medium) precede ledger/risk/policy cutover (high). **Every step is flag-gated and reversible; steps 1–9 change no live behavior — all lanes are paper.** Standing rules honored: no runs 09:15–15:30 IST without owner OK; never `down -v`; never reset broker creds.

### The parity gate has TWO TRACKS — blanket byte-parity is self-contradictory (G8)

Rev 2 demanded **byte-identical** parity for every seam while *also* committing to retire `MarketProfileBuilder` and collapse three regime notions into one (§9, step 9). Those cannot both hold: two independently-written MP engines (bar-TPO `MarketProfileEngine` vs tick-TPO `MarketProfileBuilder`) consume different inputs at different bucket sizes and **will** produce different POC/VAH/VAL on some sessions. Under a blanket byte-parity rule, retiring one is permanently blocked; in practice the rule would be quietly waived, which is worse — an unreviewed behaviour change shipped under a parity banner.

**Track (a) — REFACTOR PARITY: byte-identical. Applies when the algorithm is unchanged.**
Moving a computation behind a shared loader/provider, batching queries, relocating a feature to another plane, changing serialization. Capture exact inputs+outputs of each existing builder/loader for a set of replay sessions (reuse existing serialization: `MarketProfileResult`, `OrderFlowSnapshot`, convergence result dict, `agent_positions` rows); run the new path over identical inputs offline (deterministic — freezegun clock + single-thread OpenBLAS warm-up per the conftest fix); assert **byte-identical** on integer/enum/letter fields and a fixed ε on floats. **Any diff is a bug, full stop.** Then shadow-live before flipping any read path.

**Track (b) — ALGORITHM REPLACEMENT: golden semantic fixtures + separately-approved behavioural divergence.**
Applies whenever one implementation replaces a *different* implementation: retiring `MarketProfileBuilder`, collapsing the three regime notions, unifying the two order-flow stacks. Byte-parity is not the gate. Instead:

1. **Golden semantic fixtures** — a curated, version-controlled corpus of sessions chosen to cover the cases that matter (trend day, balanced day, double-distribution, gap-open, low-liquidity, half-session, expiry day, degenerate/thin bars per the 2026-07-13 incident, roll boundary). Each fixture asserts **semantic properties**, not bytes: value area contains the POC; VAH ≥ POC ≥ VAL; TPO letter count matches session minutes; value-area volume fraction within tolerance; the regime label is stable under an ε perturbation of inputs.
2. **A divergence report, produced BEFORE the flip.** For every fixture and every shadow session: where the old and new implementations disagree, by how much, and — for each material class of disagreement — *why*, with the case shown.
3. **Separately approved behavioural divergence.** The owner approves the divergence report **as a behaviour change**, on its own, distinct from approving the refactor. Divergence that no one can explain blocks the flip regardless of magnitude — an unexplained diff means we do not understand at least one of the two implementations.
4. **Explicit downstream-impact analysis.** Every consumer of the changed output is enumerated with the expected effect (which gates could flip, which sizes could move). A "cosmetic" POC shift that moves a value-area entry trigger is not cosmetic.
5. **Reversibility.** The retired implementation stays behind a flag for the full acceptance window and is deleted only after it closes cleanly.

**Classification is declared per seam in advance**, in the step's plan, and reviewed — a seam may not be re-labelled from (b) to (a) after a diff appears. Parity is per-feature and per-lane; a failed diff blocks only that seam. `agent_audit_events` is the reconciliation event log.

1. **Finish + reconcile the current concurrent edits.** *DONE* — HEAD `79269e7c` (see §Revision history; the rev-2 figure `3dd91987` is stale). No source-merge collision to resolve.
2. **Approve the corrected CONTRACTS only** — the §2.1 identity layers (`ExchangeRef` / `UnderlyingRef` / `Listing`+`canonical_contract_id` / `ProviderAlias` / versioned calendars) + `ContractResolver` returning a `ResolutionResult`; the corrected `DataCapabilities` vocabulary + `FeatureQuality` derivations (§2.3); the `EvaluationResult` envelope **including the §2.4.1 lifecycle-identity block**; the split `entry_requirements()` / `management_requirements()` (§2.5); and the §4.1 causal-chain ledger schema — as **pure types**, no wiring. **NOT YET APPROVED — this step is blocked on owner sign-off of rev 3, and nothing may be implemented from rev 2's superseded contracts.**
3. **Canonical instrument resolution.** One resolver backed by `fo_underlying_catalog` + `fo_contract_catalog` + `analysis/instruments.py` + `market_data/symbols.py` + `commodity_contract_specs.py`, minting `canonical_contract_id`s and populating the `ProviderAlias` table (§2.1). Closes the exchange-price-increment gap (auction `0.5`, directional `0.05`, convergence `.05` → `fo_contract_catalog`) WITHOUT collapsing the MP bucket tick. Calendars and trading parameters land as versioned reference data. One `ContractResolver` replaces the 3 front-month builders. Per-lane behind `INSTRUMENT_REF_<LANE>`. **Blocked on step 2.**
4. **Dual-emit read-only `EvaluationResult` adapters + build the unified UI read model (§5).** Every lane emits an `EvaluationResult` record (with §2.4.1 identity fields) ALONGSIDE its native shape; the read model renders all policies' results (incl. BLOCKED) + feature provenance + portfolio/risk. Extend `_BOOK_PROBES` (`lane_registry.py:878`) to all lanes. This is the human validation surface for every later shadow step. **GATE: may not start until the separate UI CONTRACT (§5.1) is written, covers all nine required areas, explicitly absorbs the already-shipped Phase-0 + Command surface (`2addf247`, `79269e7c`), and is owner-approved.**
5. **Batched data loaders + snapshot watermarks.** Consolidate the 4 spot loaders / 3 tape loaders into batched shared loaders that stamp the 8-field MarketContext key (`event_time_frontier`, `completed_bar_watermark`, `data_revision`, …). Read-only; no lane reads from them yet.
6. **Feature providers — OFFLINE / sampled-shadow ONLY.** Wrap `MarketProfileEngine`, `analytics/orderflow`, `OrderFlowEngine`, greeks provider as single-writer `FeatureProvider`s (§2.2 ownership) behind **fenced leases (§2.2.1) — the fencing tests are a precondition of this step, not a follow-up**. Run them **offline over replay** and on a **sampled** live subset — do NOT double the 200-320s live compute by running a full second pipeline in-process. Track (a) byte-parity for the wrapping itself; Track (b) semantic fixtures for anything that replaces an algorithm. Prove de-dup (one NIFTY 30m TPO shared) against the measured loop-lag target.
7. **Build the idempotent event ledger + import/reconcile lane by lane (§4).** Stand up the schema (§4.1 causal chain + projections). **Track A first** (DB lanes with a real transactional outbox): S1/Commodity via `agent_positions`+`paper_trade_book`, then Directional in its own schema/pool. **Track B second** (JSON lanes) — and only after their atomic-write + `generation`/`content_hash` + stable `source_event_id` groundwork lands (§4.3), since without it the importer cannot be idempotent. Reconcile (§4.4 — full fields, projection self-consistency, zero gap-marked intervals) lane by lane. Existing books stay authoritative; flip a lane's READ path only after it clears the acceptance window.
8. **Shared risk in OBSERVATION / assert-equivalent mode (§7).** The scoped-predicate rule engine computes verdicts in shadow (the lane's own governor still decides); cross-lane exposure is aggregation/alerting only. Prove per-lane allow/block/size-multiplier identical to today's governors (including the exact bypass sets and the band-0 un-bypassable set) before anything becomes authoritative. Every shadow verdict records its applicable-rule set and winning `rule_id` (§7.2 rule 6).
9. **Cut policies over individually on MEASURED parity.** Point each `evaluate()` at the store-built `MarketContext`, cutover order S2 → Convergence+Commodity → S1 (retire `MarketProfileBuilder` + collapse the 3 regime notions behind `feature_version` variants) → Auction last. Per-lane read-path flag reverts to the lane's own compute. Still behavior-preserving (advisory capability metadata).
10. **Enable NEW sufficiency/exposure behavior ONLY via separate owner-approved flags.** Turn capability sufficiency gates from advisory to blocking; enable cross-lane exposure entry-blocking; independently fix the option-candle REST-only defect / `CHAIN_CANDLE_BUILDER_ENABLED` so options floors are enforceable beyond the 5 indices. Each is a distinct flag, measured A/B, owner-gated. **This is the only step that changes trading behavior.**

### Explicit acceptance thresholds — MEASURED BASELINE → TARGET (G8)

Rev 2's latency, loop-lag and query rows were **not gates**: "≤ the lane's current budget", "flat vs baseline" and "materially down" cannot be evaluated, so any result could be argued into passing. Rev 3 requires that **every performance row is filled in with a measured baseline BEFORE the step begins**, and the target is expressed as an explicit relation to that recorded number. A step whose baseline row is empty **cannot start** — capturing the baseline is part of the step.

| Threshold | Baseline (captured before the step) | Target / gate |
|---|---|---|
| **Minimum sessions** | — | ≥ 10 distinct trading sessions of clean shadow (contracts/instrument/read-model steps); ≥ 20 for ledger read-path flip and any behaviour-changing gate. |
| **# evaluations / signals** | — | ≥ 500 shadow `EvaluationResult`s per policy, of which ≥ 50 `ACTIONABLE`, before that policy's cutover (so parity isn't measured on near-zero action). |
| **Decision divergence — Track (a)** | — | **0** divergences on integer/enum/letter fields (side, effect, state, TPO letters, POC/VAH/VAL bucket); floats within ε (price ≤ 1 exchange tick, confidence ≤ 0.01). Any divergence blocks the flip and is triaged as a correctness finding. |
| **Decision divergence — Track (b)** | — | All golden semantic fixtures pass; a divergence report exists; **every** material divergence class is explained; owner has approved the divergence **as a behaviour change**. Unexplained divergence blocks regardless of size. |
| **Per-instrument compute latency** | record **p50 / p95 / p99** ms per feature per instrument on the current path, over ≥ 3 sessions | new path p50 ≤ baseline p50, **p95 ≤ baseline p95, p99 ≤ 1.10 × baseline p99**. The auction block's p99 is recorded in seconds and may not regress at all. |
| **Cold vs warm cache** | record both separately: cold-key build time and warm-read time | warm read **p95 ≤ 50 ms**; cold build p99 ≤ baseline p99 of the equivalent current build; **cold-start storm** (all keys cold after a restart) completes within the lane's cycle budget — measured, not assumed. |
| **Cache-hit rate** | current effective reuse (largely 0 — each lane recomputes) | ≥ 0.90 steady-state on shared features (proves de-dup is real, not a second pipeline), measured over a full session excluding the first 10 minutes. |
| **CPU** | record per-plane CPU% (mean + p95) over ≥ 3 sessions | total across planes ≤ **1.0 ×** baseline for a de-dup step (the point is to remove duplicate work); ≤ **1.15 ×** for a step that adds a genuinely new capability, with the increase attributed. |
| **RSS / memory** | record per-process RSS at open, midday, and close (the close figure captures leaks) | close-of-session RSS ≤ **1.10 ×** baseline; **no positive-slope trend** across a session (the F-18 `app_runtime_state` lesson); PG worker stays inside the 3 GB cgroup with the measured headroom recorded. |
| **Event-loop lag** | record **p50 / p95 / max** loop lag per plane over ≥ 3 sessions | p95 ≤ baseline p95; **max ≤ 250 ms** and never above the watchdog threshold (the 2026-07-13 seizure is the reference failure); zero watchdog kills in the window. |
| **Query count** | record exact per-cycle query counts per lane (convergence ≈ 72 q/cycle is the known figure) | **≥ 50% reduction** on any lane whose loaders were consolidated; **zero** new per-symbol N+1 patterns (asserted by a query-count test, not by inspection); DB statement-timeout breaches = 0. |
| **Payload / serialization size** | record p95 and max serialized size per cached feature and per API payload | each feature within its declared budget (§2.2); **max payload ≤ 2 × p95** (no unbounded blob); oversized writes rejected at write time, and the rejection count is 0 in a clean window. |
| **Broker budget** | record per-provider request counts + rejection/429 counts per session | no increase in per-provider request count for a refactor step; quota-class breaches = 0; the 2026-07-17 starvation signature (one class burning >50% of a budget) absent. |
| **Reconciliation tolerance** | — | ledger: quantity/fills/state EXACT; realised P&L + reserved capital to the paisa; timestamps within 1 s; projection-rebuild diff **= 0**; Track B gap-marked intervals **= 0**; for the full acceptance window before read-path flip. |
| **Frontend budgets** | per the UI contract (§5.1 item 8) | defined and approved in the UI contract; step 4 is gated on them. |

**Measurement hygiene (required, else the numbers are theatre):** baselines are captured on the same host under comparable market conditions (a quiet session baseline may not be compared against an expiry-day candidate); every figure names its window and sample count; and the comparison is recorded in the step's plan **before** the candidate runs, so the bar cannot be moved after seeing the result.

---

## 12. Open Owner-Decisions and Sign-off Checkpoints (refined)

Decisions the owner must make; each is a **sequencing checkpoint** where the migration pauses for sign-off.

1. **S2 sufficiency — named degraded policy, never a silent MACD.** S2 is `BAR_INFERRED` today (`of_source="bar_inference"`). If S2 should keep trading at intraday horizon without a dense quote stream, it does so as a **separately-NAMED `MP_OF_BAR_PROXY` policy** whose `entry_requirements()` explicitly declares `allowed_derivations ⊇ {BAR_INFERRED}` — it must **never silently become MACD** or masquerade as a quote-modelled-OF policy. (Note per C1: the honest contrast is BAR_INFERRED vs MODELLED_FROM_QUOTES — "observed order flow" is not an option for any lane.) Otherwise it blocks (honest). — *Checkpoint before step 9 S2 cutover.*
2. **Scalp — UNSATISFIABLE today; the only real question is whether to BUY the data (C1).** Rev 2 said scalp was satisfiable on "the 84% sized-L1 cohort". **It is not.** That 84% measured sized BBO only; a scalp horizon needs aggressor attribution and/or book depth, and **both `BROKER_AGGRESSOR_PRINTS` and `DEPTH_L2` are structurally absent** (§2.3, §6.4). So the owner decision is not "which strategies to enable" — it is: **do we acquire a feed that carries per-trade size + aggressor side and/or per-level depth, or do we accept that no scalp-horizon policy can ever be enabled here?** Until that is answered, scalp policies stay permanently BLOCKED with an honest `missing_reason`. — *Owner call; blocks any scalp sufficiency gate indefinitely.*
3. **Second broker token — decide from MEASURED capacity, not this ADR.** Whether A-tier persistence + broad-C batched REST needs a 2nd token is a question for step-8 budget telemetry, not a design-time assertion. The ADR does not pre-decide it. — *Checkpoint after step 8 measurement if telemetry shows starvation.*
4. **Ledger — BRIDGE, don't retire.** Bridge the existing books into the event ledger: **Track A outbox** for the DB lanes (`agent_positions`+`paper_trade_book`, `directional_paper_*`), **Track B generation/checkpoint import** for the JSON lanes, where a shared transaction is impossible (§4.3, C4). Do NOT retire any book until the new ledger is proven authoritative on that lane. Legacy `orders/positions/paper_sessions` retired last, only after confirming no live reader. — *Checkpoint before any lane's read-path flip and before legacy retirement.*
5. **Cross-lane exposure — aggregation/alerts first, entry-blocking later.** Ship the portfolio exposure roll-up as observation-only (§7); enable entry-blocking only after measurement and explicit owner sign-off. — *Checkpoint before turning any exposure gate to blocking (step 10).*
6. **`CHAIN_CANDLE_BUILDER_ENABLED` vs the enrichment stopgap.** Turn on the real-greeks chain builder in the live budget (fixes stock greeks / the options floors) or keep the index-only snapshot-copy stopgap? — *Checkpoint before step 10 options-floor enforcement.*
7. **`DEPTH_L2` and `BROKER_AGGRESSOR_PRINTS` ingestion — the two structurally-absent capabilities.** Ingest and persist a real per-level depth feed, and/or source a feed carrying per-trade size + aggressor side, to make those capabilities present? Absent both, **every order-flow number in this system remains `MODELLED_FROM_QUOTES` forever** and no observed-flow requirement is satisfiable anywhere (§2.3). This is the highest-leverage data decision open. — *Owner call, independent of the migration; tied to decision 2.*
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
- `market_ticks` **is a QUOTE-SNAPSHOT table** (`db/migrations/versions/001_initial_schema.py`): `time, symbol, ltp, o/h/l/c, volume (CUMULATIVE), oi, bid, ask, bid_qty, ask_qty`. **No trade id, no last-trade quantity, no broker aggressor side.** Last 2d: 152 symbols / 1.99M rows; **~84% of rows carry `bid_qty>0 AND ask_qty>0` ⇒ that figure measures `SIZED_BBO` coverage ONLY** (rev 2 misreported it as trade-print coverage — C1). `QUOTE_UPDATES` + `CUMULATIVE_VOLUME` = 100% of the cohort. 9,990 distinct option symbols on WS over 3d. **`BROKER_AGGRESSOR_PRINTS` structurally absent** (never received — `analytics/orderflow.py` module docstring). **`DEPTH_L2` structurally absent** (only `data_router.subscribe_depth:734`, ref-counted, un-persisted).
- `option_premium_candles` last 3d: upstox **97.4% iv-NULL**, fyers 98.9% NULL, **0 `fyers_chain` rows**.
- `option_chain_snapshots` last 2d: 323,844 rows / 189,461 iv-set / **5 symbols (indices only)** — `GREEKS` present indices-only.
- `market_profiles`: 291 rows / 4 symbols / 1 timeframe since 2026-02-12 (unread by live decision). `index_futures_candles`: **0 rows**.
- Ledger: `agent_positions` 78 open/65 closed (`symbol UNIQUE`); `paper_trade_book` 128 closed (no idempotency key); `directional_paper_positions` 266 / `_journal` 14,492; legacy `orders/positions` 261/144; `agent_audit_events` 620,560.
- Budget: Fyers 10/s·200/min·100k/day; Upstox 50/s·2000/30min. Quota CRITICAL 40% / STANDARD ≥35% / BULK ≤25%.
