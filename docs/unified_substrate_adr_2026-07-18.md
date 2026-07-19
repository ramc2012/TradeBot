# ADR-0043 — Unify the Substrate Beneath the Strategy Policies

**MarketContext · InstrumentRef · SignalIntent · Capability-Tier DataQuality · Shared Risk · Canonical Ledger · Tiered Universe**

- **Status:** PROPOSED — PLAN/ADR FIRST (owner directive). Zero code changed producing this; drafted Sat 2026-07-18 night, market CLOSED, fully read-only.
- **Date:** 2026-07-19
- **Repo root:** `/Users/ramachandran/CLAUDE PROJECTS/Nomad Curie/TradeBot` (note the space in the path; all `file:line` citations below are under `<root>/backend/…` unless prefixed otherwise).
- **Supersedes/absorbs:** the three internal substrate maps compiled 2026-07-18 (Data+Universe, Signal+Risk+Ledger, Feature+DataQuality). This ADR is their single decision-grade synthesis.
- **Scope:** the SUBSTRATE only — instrument universe, market-data context, feature computation, signal contract, risk, execution/ledger, UI state. The four strategy POLICIES (Auction, MP+OF, Convergence, Directional) stay separate; this ADR makes them plug into shared rails, it does not merge them.
- **Author's note to the owner:** every consolidation in here is grounded in a real `file:line` or a live DB count. Wherever a "refactor" would actually change trading behavior, that is called out explicitly and defaulted OFF — see §7 and §11.

---

## 1. Context and Decision

### 1.1 The situation today

There is **no shared substrate**. Every lane independently (a) picks its own universe, (b) assembles its own market-data context, (c) computes its own features from raw tables, (d) emits a differently-shaped signal, (e) runs its own risk governor, and (f) writes to its own ledger in its own storage paradigm. The convergence that exists is accidental, not architectural:

- **Two production Market-Profile engines** (`auction_intelligence/market_profile/engine.py:33` bar-TPO `MarketProfileEngine`, reused by Convergence/S2/commodity; and `market_data/market_profile.py:57` tick-TPO `MarketProfileBuilder`, used by S1/agent) recompute the same NIFTY 30-min TPO independently on their own cadences.
- **Four independent loaders of `underlying_spot_candles`**, each with its own bad-bar cleaner: auction `live.py:842` (+`:622`), convergence `service.py:387` (+`:39`), S2 `strategy2_mp_of.py:126` (+`:101`), runtime `market_intelligence_runtime.py:248`.
- **`tick_size` is a literal in ≥3 places** (auction `0.5` in `SYMBOL_MAP`, directional `0.05` at `data.py:291`, convergence default `.05`) while the only true source is `fo_contract_catalog`.
- **Three storage paradigms for the ledger** (JSON files, `agent_positions`+`paper_trade_book` DB, and Directional's own `directional_paper_*` DB schema) with no cross-lane netting, exposure, or reconciliation possible.
- The **capability-tier concept already exists in exactly one lane** — Convergence's `cvd_source ∈ {market_ticks, bar_proxy}` + the `real_tick_cvd` gate (`institutional_convergence/engine.py:326,334,355`). No other lane expresses source capability at all.

We already own the *utilities* (`backend/market_data/`: `live_candle_store`, `quote_bus`, `source_policy`, `data_router`, `broker_circuit`, `atm_watchlist`, `market_profile`, `greeks_enrichment`; plus `brokers/rate_limiter.py`, `core/lane_registry.py`, `core/laneset.py`). What is missing is an object that composes them into a **versioned MarketContext keyed by `(instrument, as_of)`** with declared capability tiers and data-quality reasons, and a **canonical SignalIntent / ledger** the policies share.

### 1.2 The decision

Introduce a shared substrate beneath policies that stay separate, via feature-flagged, per-lane, reversible strangler-fig migration:

1. **`InstrumentRef`** — one canonical instrument identity, replacing `SYMBOL_MAP` and the ~6 ad-hoc symbol resolvers.
2. **`MarketContext`** — an immutable, **versioned** value object keyed by `(instrument, as_of)`, assembled **compute-once** per `(instrument, tier, cadence)` and handed to every policy that requested it.
3. **`DataQuality` + a 5-tier capability ladder** (`REAL_L2 > REAL_L1_TRADES > REAL_TICKS > BAR_PROXY > UNAVAILABLE`), attached per-feature with source, freshness, coverage, and `missing_reason`.
4. **`SignalIntent`** — one typed union every lane's `evaluate()` returns (superset of today's five shapes).
5. **`RiskGovernor` (shared surface)** — one ordered entry-gate pipeline that dispatches to per-policy exit managers and preserves the `SIGNAL_VALIDATION_UNCAPPED` bypass per gate.
6. **`OrderLedger` (canonical)** — one orders/positions/reconciliation store, built by promoting the existing `agent_positions` + `paper_trade_book` DB tables to carry all lanes.

Each policy implements the strategy interface: `requirements()` (which context tiers/features it needs) / `evaluate(context) -> SignalIntent` / `manage(intent, position) -> ExitAction`.

### 1.3 Why NOT one merged engine (the monolith we are deliberately rejecting)

The external review's own framing is "shared substrate, separate policies," and this is correct for concrete, load-bearing reasons — a single merged strategy engine would:

- **Erase four genuinely different edges.** The lanes are not variants of one strategy: Directional's edge is intraday mean-reversion + options positioning with OF *unavailable by design* (`directional_options/features.py`, `positioning_feed.py:58`); Convergence's edge is a BANKNIFTY-specific real-tick-CVD confirmation that hard-blocks on `real_tick_cvd`; Auction is a 200-320s MP/OF/regime CPU pipeline (`auction_intelligence/service.py:183-188`); MP+OF is a commodity-native tick evaluator ported to indices as bar-proxy. Merging them forces a single feature contract and a single risk doctrine onto four incompatible horizons and data-sufficiency profiles.
- **Collapse the direction semantics dangerously.** Three encodings exist today — `LONG/SHORT/FLAT` (Auction/Convergence bias), `CE/PE` (Directional option side), `BUY/SELL` (MP+OF). A monolith that treats `SHORT` (short future) and `PE` (long-premium put) as one bucket mis-signs P&L and netting (§7).
- **Re-introduce event-loop seizure.** The lanes run on a single core; S2 and Convergence hand-rolled bar-close-driven throttles/caches specifically to survive it (`strategy2_mp_of.py:604-614`, the 2026-07-13 degenerate-bar incident). One eager merged build per cycle re-creates the exact seizure those caches prevent.
- **Fail the doctrine test.** `SIGNAL_VALIDATION_UNCAPPED` (`core/config.py:229`) is an owner directive with a *per-lane* bypass pattern; a merged risk engine would flatten it (§6).

The substrate is where the reuse lives (instruments, bars, profiles, order-flow tiers, chain/greeks, regime, ledger, risk gates). The **policies stay as pluggable `evaluate()` implementations**. This is the QuantConnect/Nautilus/Hummingbot/Freqtrade shape (§9), not a rewrite.

---

## 2. The Concrete Contracts (field-level, grounded in the union maps)

All contracts are additive in the first instance — every field maps to at least one existing lane so migration adapters are mechanical. `⊕` = a lane already carries it; `→` = derived at the adapter.

### 2.1 `InstrumentRef` — canonical identity (replaces `SYMBOL_MAP` + the ~6 resolvers)

| Field | Consumed by | Current source (file) |
|---|---|---|
| `symbol` (canonical root: NIFTY, INFY, GOLD) | all | `fo_underlying_catalog` / `auction_intelligence/live.py:80 SYMBOL_MAP` / config |
| `kind` (INDEX/STOCK/COMMODITY) | all | `fo_underlying_catalog.kind`; commodity implicit |
| `spot_instrument_key`, `underlying_key` (Upstox) | S1, directional, convergence chain | `fo_underlying_catalog` (217 rows: 6 INDEX + 211 STOCK) |
| `fyers_symbol`, `app_symbol` | auction, convergence, `data_router` | `market_data/symbols.py` |
| `lot_size` | all | `fo_underlying_catalog` (211/211 carry it) / `fo_contract_catalog` / SYMBOL_MAP / commodity specs |
| `tick_size` | auction, convergence, directional, MP engines | **`fo_contract_catalog` only** — hardcoded everywhere else; THE gap to close |
| `minimum_lot`, `freeze_quantity` | risk/execution (auction, directional) | `fo_contract_catalog` (50,482 rows) |
| `strike_step` | selector, walls | `analysis/instruments.py:30` |
| `expiry_weekday` / expiry calendar | S1, S2, directional, convergence | `analysis/instruments.py:18` + `fo_expiry_catalog` (3,095 rows) |
| `session_bounds` (RTH vs MCX 09:00–23:30) | all | hardcoded `live.py:69-72` / `_session_bounds:186` |
| `front_month_future_symbol` (rollover-safe) | auction, convergence, commodity | 3 builders: `live.py:192`, `service.py:253` (inline), `data.index_futures_backfill` |
| `sector_code` | convergence diversification | CBE payload |

### 2.2 `MarketContext` — versioned, keyed by `(instrument, as_of)`

An immutable value object. **Versioning:** `context_version = hash(instrument_id, as_of_bucket, {feature_version…})`; a `snapshot_id` pins the exact inputs a signal was computed against (referenced from `SignalIntent`). Lazily materialized per `(instrument, tier, requested-feature)` — never one eager build (§7).

```
MarketContext(instrument: InstrumentRef, as_of, context_version, snapshot_id):
  bars: {1m, 3m, 15m, 30m, daily}                 # ⊕ S1/S2(1m,3m), auction(30m), convergence(3m), directional(1m→resample); src underlying_spot_candles (+commodity runtime)
  sessions: {current, prior, history[~10]}         # ⊕ auction _group_rows_by_session, convergence _select_rule_sessions:356
  profiles:
    current {poc,vah,val,ib_high,ib_low,tpo}       # ⊕ auction/convergence/S2/commodity; src recomputed in-proc (persisted twin market_profiles UNUSED for live decision)
    prior, composite_htf {weekly,monthly}          # ⊕ commodity HTF gate, auction
  order_flow:                                      # carries SOURCE + FRESHNESS + CAPABILITY TIER (see 2.3)
    cvd, book_pressure, footprint, ofi, toxicity   # ⊕ auction OrderFlowEngine, convergence footprint, commodity/S2
    capability_tier, of_source, is_stale, coverage
  options:
    atm_chain[strike,ltp,oi,iv,volume,source_broker]# ⊕ directional/S1; src atm_option_watchlist_snapshots (NO greeks columns)
    greeks[delta,gamma,theta,vega,iv,tte]          # ⊕ directional sizing; src option_premium_candles (the split to unify)
    call_wall,put_wall,net_pressure,ntm_volx       # ⊕ auction/convergence; src OI on option_premium_candles / NTM VolX
    expiry_targets{weekly,monthly}, expiry_sufficiency{dte,listed?}  # ⊕ S2 select_s2_expiry_targets:196, directional DTE
  regime:
    india_vix                                      # ⊕ convergence _load_india_vix:340, directional, auction sizing; src SectorRotationTracker
    iv_percentile, vol_state                       # ⊕ directional IV sizing curve
    regime_label, sector_rotation, cbe{score,bias,quadrant}  # ⊕ convergence(universe)/directional(positioning); src cbe_scan_results
    positioning{oi_build, directional_bias}        # ⊕ directional (BANKNIFTY-specific), convergence; src directional_positioning_daily
  data_quality: DataQuality                        # see 2.3 — spot_fresh, watchlist_fresh, missing_feature_reasons[]
```

### 2.3 `DataQuality` + the 5 capability tiers

The **tier ladder is monotone** — a consumer that accepts tier N accepts every richer tier:

`REAL_L2 > REAL_L1_TRADES > REAL_TICKS > BAR_PROXY > UNAVAILABLE`

Every feature in the store declares a `FeatureQuality`; the provider that already knows the truth stamps the tier (it is **not** guessed centrally):

```
FeatureQuality:
  feature_name, timeframe
  capability_tier: enum(5)          # what the VALUE actually is
  source: str                       # market_ticks | tick_reconstruction_book | bar_inference | option_chain_snapshot | ...
  as_of, observed_at, age_seconds   # from market_data/data_quality_agent.py:275 assess_observation
  freshness_ok: bool                # age<=budget AND not frozen (data_quality_agent.py:131-147)
  coverage: {covered_bars,total_bars}|null   # e.g. commodity of_tick_covered_bars
  input_snapshot_id, feature_version
  missing_reason: str|null          # WHY it is UNAVAILABLE / degraded
```

**Mechanical tier mapping from what exists today (no new judgment needed):**

| Lane / feature | Today's label (file:line) | Tier stamped |
|---|---|---|
| Convergence CVD | `cvd_source=="market_ticks"` / `"bar_proxy"` (`engine.py:326`) | REAL_TICKS / BAR_PROXY |
| Auction OF | `order_flow_source` `tick_reconstruction[_book]` (`live.py:1292`) vs `bar_inference` (`live.py:1320`) | REAL_L1_TRADES (book path) / REAL_TICKS / BAR_PROXY |
| Auction depth ladder | synthetic decay ladder anchored to `total_buy_qty` (`live.py:1462-1484`) | **≤ REAL_L1 — never REAL_L2** (ladder is fabricated) |
| Commodity OF | `of_source` from `tick_signed_volume_overrides` (`commodity_mp_signal.py:264,1112`) | REAL_TICKS where covered / BAR_PROXY |
| S2 OF | calls evaluator without ticks → `of_source="bar_inference"` (`strategy2_mp_of.py:632`) | BAR_PROXY |
| Directional OF | none (positioning is the proxy) | UNAVAILABLE (by design) |
| Greeks (indices) | broker snapshot copy, IV %→fraction (`greeks_enrichment.py:100-159`) | REAL |
| Greeks (stocks) | no chain source (`greeks_enrichment.py:57-63` maps 5 indices only) | UNAVAILABLE, `missing_reason="no_chain_snapshot_for_underlying"` |
| Option candle IV | 97% NULL live (see §3.4 DB) | `missing_reason="option_greeks_rest_null"` |

**DB reality (live, Sat 2026-07-18):** `market_ticks` last 2d = 152 symbols / 1.99M rows / **84% carry real L1 sizes** (`bid_qty>0 & ask_qty>0`) — the REAL_TICKS/REAL_L1 path has genuine data. The **only structurally-missing tier is REAL_L2** (no per-level book is ingested anywhere; the only real depth is `data_router.subscribe_depth:734`, ref-counted to auction book symbols, un-persisted).

### 2.4 `SignalIntent` — the union output

Frozen dataclass. Lossy-collapse hazards preserved explicitly (see §7).

```
SignalIntent:
  # identity / provenance
  strategy_key            # ⊕ all (registry key; agent_positions.strategy_key)
  strategy_version        # → new; pins per-lane policy version (enables A/B + reconciliation)
  signal_id               # ⊕ S1/Commodity (uuid); → mint for others
  setup_id                # ⊕ Convergence symbol:action:bar_time (paper.py:138); → derive for others
  as_of, snapshot_id      # → MarketContext version this was computed against
  sleeve                  # ⊕ Directional; → "intraday"/"positional" for others
  horizon                 # ⊕ Directional (bars/hours); → session-scoped for intraday lanes
  # instrument
  instrument: InstrumentRef (+ expiry, expiry_kind, strike, option_type, trading_symbol, instrument_key, lot_size, tick_size)
                          # ⊕ Auction ExecutionInstruction (schemas.py:244-263), Directional ContractCandidate (schemas.py:109),
                          #   S1 agent_positions cols; Convergence=underlying/futures only; MP+OF=side+expiry_tracks
  # decision
  direction               # NORMALIZED enum {LONG_PREMIUM_CALL, LONG_PREMIUM_PUT, LONG_FUT, SHORT_FUT, FLAT}
                          #   adapter: CE→CALL, PE→PUT, BUY→CALL, SELL→PUT, LONG/SHORT(index)→FUT.
                          #   ** do NOT keep raw CE/PE vs LONG/SHORT — that ambiguity is a real netting hazard (§7) **
  confidence              # ⊕ Directional/S1/MP+OF/Auction [0,1]; → Convergence: normalize score/100 but RECORD both
  conviction_extras{}     # ⊕ Directional (p_up, jump_score, tail_probability, model_uncertainty); Convergence (score, quality A+/VALID); null elsewhere
  # levels
  entry, stop, target1, target2   # ⊕ Auction, Convergence (engine.py:447), Directional; MP+OF: stop_hint only (target2=null)
  reward_risk             # ⊕ Convergence; → compute elsewhere
  expiry_time             # ⊕ Directional (expiry guard); → EOD-squareoff for intraday
  # sizing INTENT (not the sizing decision — that is risk's job)
  iv_sizing_factor        # ⊕ Directional (schemas.py:106); default 1.0
  risk_fraction           # ⊕ Convergence/Auction (sleeve_fraction); default from config
  # evidence / quality (the most consistent field family across lanes)
  data_capability_tier    # ⊕ Convergence (cvd_source); → derive for all from MarketContext
  readiness_gates{}       # ⊕ Convergence/MP+OF; → surface per lane
  evidence[]              # ⊕ ALL: rationale/reasons/blocked_reasons/rejection_reasons/selection_reason/signal_reason unioned
  data_quality, missing_features[]   # → from MarketContext
```

### 2.5 The strategy interface

```
class StrategyPolicy(Protocol):
    def requirements(self) -> Requirements:
        # declares per-feature minimum capability tier + which MarketContext fields it needs.
        # The store REFUSES to build a SignalIntent when a required feature is below floor,
        # attaching missing_reason (this generalises today's real_tick_cvd / execution_ready gates).

    def evaluate(self, context: MarketContext) -> SignalIntent | None:
        # pure decision over a pre-built context. No IO, no raw-table fetches, no feature compute.
        # (auction analyze() is already pure CPU: service.py:61,183)

    def manage(self, intent: SignalIntent, position: Position) -> ExitAction:
        # LANE-OWNED exits stay here unchanged: Convergence CVD-reversal, Directional flip-confirmation,
        # Commodity cooldowns, Auction exit-confirmation cycles. The shared governor never closes a position.
```

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
| **Directional** | static 3 idx (`config.py:24`) + NIFTY-50 ∩ catalog (`data.py:306`), ranked/rotating/readiness-gated (`service.py:175-227`) — **the good pattern** | `data.py:138 load_live_spot_frame` + `:178 list_live_contract_snapshots`; **batched** readiness `:318` (2 grouped q for whole batch) | lot COALESCE both catalogs `:232`; **`tick_size` hardcoded `0.05` `:291`** | `signals.py:276 DirectionalSignal` + `ContractCandidate` | `risk.py:36 approve()` | **own DB schema** `directional_paper_positions`(266) + `directional_paper_journal`(14,492) (`paper.py:128`) |
| **S1 (30m ATM MACD)** | **entire catalog** (`window_calculator.py:262`, 6 idx + 211 stk) | own `MarketProfileBuilder`; owns the ATM watchlist scope | catalog | DB schema is the contract (`agent_positions`) | none (already uncapped); cash gate `portfolio.py:311` | `agent_positions`(78 open/65 closed) + state file `nse_strategy_state.json` |
| **MACD-Refined** | branch-restored lane | — | — | — | `macd_refined/paper.py:317` | JSON `runtime/macd_refined/signal_state.json` |

### 3.2 Concrete duplication (same data, N loaders)

1. **`underlying_spot_candles` — 4 loaders, 4 bad-bar cleaners** (auction `live.py:842`+`:622`; convergence `service.py:387`+`:39`; S2 `strategy2_mp_of.py:126`+`:101`; runtime `market_intelligence_runtime.py:248`).
2. **`market_ticks` order flow — ≥3 loaders, each re-deriving the near-median tape filter** (auction `live.py:1327`+`_filter_tick_rows_near_median:137`; convergence 3 queries `service.py:401/420/424`; commodity agent).
3. **Order-flow computation — 2 entirely separate stacks:** bar-CVD primitives `analytics/orderflow.py` (its docstring `:1-17` states Indian brokers don't push aggressor-tagged prints — bar/L1 fallback) used by Convergence/S2/commodity; vs L1/L2 microstructure `auction_intelligence/order_flow/engine.py:18 OrderFlowEngine.compute:27` used by Auction/FMP. Different code, same intent.
4. **Market Profile — 2 production engines + 5 research copies** (§1.1); persisted `market_profiles` (291 rows / 4 symbols / 1 timeframe) read by **no** live decision.
5. **Regime — 3 separate notions** (directional `regime.py:23`; auction `regime/engine.py`; HMM `analytics/regime_hmm.py` research-only) + a 4th commodity string.
6. **Instrument metadata resolved ≥4 ways; `tick_size` literal in ≥3 places** (§1.1).
7. **Futures front-month symbol built ≥3 ways** (`live.py:192`, `service.py:253` inline, `data.index_futures_backfill`).
8. **Per-symbol recompute is uncached across lanes** — auction rebuilds MP+OF+regime+3 agents per symbol as a 200-320s CPU block (`service.py:183-188`); convergence rebuilds the same 30-min TPO per symbol (`engine.py:285`); S2 rebuilds per underlying behind a hand-rolled throttle (`strategy2_mp_of.py:551-617`); S1 computes the same NIFTY TPO in a *second* engine.

### 3.3 The ledger's N truths (verified live)

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

**The canonical anchor already half-exists:** `agent_positions` (`signal_id, session_id, market, strategy_key, symbol, underlying, expiry, strike, option_type, qty, entry/current/peak_price, realized/unrealized_pnl, entry_iv_pct, spot_setup, regime, signal_reason, phase, status…`) + `paper_trade_book` (`market, strategy_key, session_id, action, qty, lots, entry/exit_price, pnl, setup_type, regime, exit_reason, signal_id`). Already `(market, strategy_key, signal_id)`-keyed with provenance — a `SignalIntent`-shaped ledger two lanes already use. `core/lane_registry.py` already enumerates every lane's `paper_book_source` as free text and has a `risk_book_source` probe hook (`_BOOK_PROBES:878`, only `macd_refined_paper` wired).

### 3.4 Greeks / options-data reality (DB-grounded, the hard blockers)

- `option_premium_candles` last 3d: upstox **97.4% iv-NULL**, fyers 98.9% NULL, **0 `fyers_chain` rows** (the greeks-bearing builder is effectively not running). Chain builder is flag-off: `CHAIN_CANDLE_BUILDER_ENABLED` default False (`chain_candle_builder.py:275`).
- `option_chain_snapshots` last 2d: 323,844 rows / 189,461 iv-set / **only 5 symbols (indices)** → stocks have **no chain-greeks source**; `greeks_enrichment` maps 5 indices only (`greeks_enrichment.py:57-63`).
- Live greeks completeness is **indices-only via a snapshot-copy stopgap**. The greeks live in `option_premium_candles`; the quotes live in `atm_option_watchlist_snapshots` (which has **no greeks columns**) — the split the MarketContext options block must unify.
- `index_futures_candles` = **0 rows** → positional index-futures MP is a hard NO-GO until a writer exists.

### 3.5 The shared REST token bucket (already built)

`brokers/rate_limiter.py`: **Fyers 10/s · 200/min · 100k/day; Upstox 50/s · 2000/30min**, one shared budget per token. Quota classes **CRITICAL (40% reserved) / STANDARD (≥35%) / BULK (≤25%)**. The 2026-07-17 collapse (chain-poll storm burned ~83% of the Upstox budget → S1 starved) proves classification-without-enforcement fails: **tier membership must be enforced at the `broker_class` contextvar boundary** or tiers leak.

---

## 4. Risk-Ordered Migration — the 7 steps

Ordered so contracts + observability (low risk, reversible) precede shared compute (medium) precede policy/ledger/risk cutover (high). **Every step is flag-gated and reversible; no step changes live behavior — all lanes are paper today.** Standing rules honored: no runs 09:15–15:30 IST without owner OK; never `down -v`; never reset broker creds.

The **golden-replay parity gate** (defined once, applied per step): capture the exact inputs+outputs of each existing builder/loader for a set of replay sessions (reuse existing serialization: `MarketProfileResult`, `OrderFlowSnapshot`, convergence result dict, `agent_positions` rows); run the new path over identical inputs offline (deterministic — freezegun clock + single-thread OpenBLAS warm-up per the conftest fix); assert **byte-identical** for integer/letter fields and a fixed ε for floats. Then **shadow-live** for N sessions before flipping any read path. Parity is per-feature and per-lane, so a failed diff blocks only that seam. `agent_audit_events` is the reconciliation event log.

---

### Step 1 — Contracts + Observability (types only, zero behavior change)

- **Deliverable:** `InstrumentRef`, `MarketContext`, `SignalIntent`, `FeatureQuality`, the 5-tier enum, and the `requirements()/evaluate()/manage()` protocol as **pure types**. A translation layer that *reads* today's `of_source`/`cvd_source`/`execution_ready`/`DataQualityAgent` into `FeatureQuality`. Every lane emits a `SignalIntent` record **alongside** its native shape. Extend `_BOOK_PROBES` (`lane_registry.py:878`) to **all** lanes so `/api/system/lanes` reports real book equity/breach for all lanes, not just `macd_refined_paper`.
- **Touches:** new `substrate/` package; a shim per lane; the lane registry. No existing decision path changed.
- **Effort:** M. **Reversibility:** trivial (delete the shim; records are write-only). **Flag-gated / behavior-unchanged:** yes / yes.
- **Parity gate:** tier-label parity — the emitted `capability_tier`/`of_source` must equal each lane's current `of_source`/`cvd_source` string on every replayed row. Suite green.

### Step 2 — `InstrumentRef` + canonical instrument resolution

- **Deliverable:** one resolver backed by `fo_underlying_catalog` + `fo_contract_catalog` + `analysis/instruments.py` + `market_data/symbols.py` + `commodity_contract_specs.py`, exposing `InstrumentRef`. **Closes the `tick_size` gap** (auction `0.5`, directional `0.05`, convergence `.05` → all read `fo_contract_catalog`). One rollover-safe `front_month_future_symbol` builder replaces the 3.
- **Touches:** the metadata read sites (`live.py:80`, `service.py:253/464`, `data.py:232/291`, `strategy_agent`). Lanes adopt it one at a time behind `INSTRUMENT_REF_<LANE>`.
- **Effort:** M. **Reversibility:** per-lane flag flip back to the literal. **Flag-gated / behavior-unchanged:** yes / **must be proven** — a wrong tick/lot changes sizing.
- **Parity gate:** for every `(lane, symbol)`, assert the resolved lot/tick/session/future-symbol equals what the lane computed before, on N replay sessions. Any mismatch (e.g. auction's hardcoded NIFTY lot 65 vs catalog) is triaged as a **data-correctness finding**, not silently adopted.

### Step 3 — Feature Store as pass-through adapters (compute-once MarketContext)

- **Deliverable:** wrap `MarketProfileEngine`, `analytics/orderflow`, `OrderFlowEngine`, and the greeks provider as `FeatureProvider`s keyed by `feature_key=(instrument, timeframe, feature_name, as_of_bucket, feature_version, input_snapshot_id)`. Two-tier cache (process-local dict like `_S2_TODAY_PROFILE_CACHE`, backed by Redis like `MarketProfileBuilder.store_profile:235`). **Bar-close-driven invalidation** (reuse S2's rule `strategy2_mp_of.py:604-614`), not time-driven. Every build stays behind `asyncio.to_thread` (the CPU bulkhead auction/S1 already use; OpenBLAS-deadlock fix still applies). **No math changes** — adapters only. Policies still call their old paths; the store runs in shadow.
- **Touches:** new `FeatureProvider`s wrapping existing builders; no lane reads from the store yet.
- **Effort:** L. **Reversibility:** store is shadow-only; delete to revert. **Flag-gated / behavior-unchanged:** yes / yes.
- **Parity gate:** golden-replay **byte-identical** for MP (TPO letters, POC/VAH/VAL, poor-high/low), OF/CVD series, and greeks vs the live builders, across all four lanes; store measurably de-dupes (one NIFTY 30m TPO build shared) with **no event-loop regression** (loop-lag telemetry flat).

### Step 4 — DataQuality/capability-tier + Tiered Universe subscription (live as a READ)

- **Deliverable:** re-express each lane's existing gate as a **read** against `FeatureQuality` + the §5 sufficiency matrix (advisory first). Introduce the A/B/C/D tier map as a **subscription policy** over `atm_watchlist`/`quote_bus`, enforced at the `rate_limiter.py` `broker_class` contextvar boundary (§5). Tiers are **advisory metadata** on `SignalIntent.data_capability_tier` before any sufficiency gate becomes blocking.
- **Touches:** the gate read sites (`engine.py:355 real_tick_cvd`, `live.py:1657 _build_live_data_status`, convergence `readiness_gates`); `rate_limiter` quota-class assignment per tier.
- **Effort:** M. **Reversibility:** advisory-only; flipping a gate to blocking is a separate later flag. **Flag-gated / behavior-unchanged:** yes / yes (as a read).
- **Parity gate:** for N sessions, assert the tier-read reproduces each lane's current accept/reject **row-for-row**; assert budget telemetry shows tier membership actually enforced (no BULK work in CRITICAL, per the 2026-07-17 collapse post-mortem).

### Step 5 — Canonical Ledger (promote `agent_positions` + `paper_trade_book`, dual-write)

- **Deliverable:** `LedgerWriter` protocol (`open/partial_close/close/mark/summary`) backed by the promoted DB pair + the few added columns (`strategy_version, setup_id, sleeve, stop, target1, target2, data_capability_tier, evidence` JSONB, per-lane retention). Each lane's existing book becomes a thin adapter that **dual-writes** behind `LEDGER_CANONICAL_<LANE>`. Replace `LaneSpec.paper_book_source` free text with a typed `ledger_handle`. **Migration order (lowest-risk first):** Commodity (already DB dual-writing) → S1 (already in `agent_positions`) → Directional (DB→DB schema map; richest, best-tested, 14,492-row journal to reconcile) → Convergence JSON→DB → Auction JSON→DB → MACD-Refined. FMP stays parked. Legacy `orders/positions/paper_sessions` retired last, only after confirming no live reader.
- **Touches:** all book classes; `lane_registry`; the `/api/system/lanes` UI (pointed at canonical **last**).
- **Effort:** L. **Reversibility:** dual-write means the JSON/legacy book stays authoritative until read-path flip; revert = flip read path back. **Flag-gated / behavior-unchanged:** yes / yes (dual-write is additive).
- **Parity gate:** **reconciliation gate (Nautilus pattern)** — canonical vs legacy book equity/open-count/realized must match **to the paisa for N sessions** before flipping that lane's read path. Guardrails: one authoritative realized source = trade-book rows (positions are current-state only) to avoid the Commodity double-count; every capital computation stays `strategy_key`-scoped so S1's `reconcile_available_capital:311` cannot erase another lane's cash; keep per-lane `order_log` retention (`ORDER_LOG_LIMIT=2000`, `paper.py:16`) so the 14,492-row journal doesn't set an unbounded default.

### Step 6 — Shared Risk-Governor gate pipeline (per-policy exits + UNCAPPED bypass preserved)

- **Deliverable:** replace the five bespoke `if not settings.SIGNAL_VALIDATION_UNCAPPED` branches with a **declarative gate registry** — each cap is a gate tagged `{scope: infra|capital|discipline|exit, bypassed_by_validation: bool, mode: paper|live}`. The uncapped bypass becomes a property of `scope=capital` gates. **Exit managers stay per-policy** — the governor gates *entries* and returns a size multiplier; `manage()` remains lane-owned. **Kill-switch stays a per-policy capability** (Auction/Commodity kill-switches are live-only/operator/catastrophe-only; a global fire would halt healthy lanes). Cross-lane exposure is **NEW capability added as an opt-in gate, default OFF** (§7).
- **Touches:** `auction_intelligence/risk/governor.py:11`, `directional_options/risk.py:36`, `institutional_convergence/paper.py:200`, `commodity_strategy_agent.py:1974`, `portfolio.py:311` — each wrapped as gates.
- **Effort:** L. **Reversibility:** governor runs in **assert-equivalent shadow** first (compute the gate verdict, still let the lane's own governor decide) before it becomes authoritative. **Flag-gated / behavior-unchanged:** yes / **must be byte-equivalent first**.
- **Parity gate:** on replayed sessions, the gate pipeline's allow/block/size-multiplier must be **identical per lane** to today's governor (including the exact bypass set: Auction keeps close-buffer + max-concurrent even uncapped; Commodity keeps its catastrophe kill-switch backstop `commodity_strategy_agent.py:3357` which the flag does NOT disable; Convergence reports circuit state but suppresses the lock when uncapped `paper.py:130`). Only after identical for N sessions may any **new** gate be unlocked.

### Step 7 — Policy cutover + Sufficiency gates + Portfolio construction

- **Deliverable:** point each policy's `evaluate()` at the store-built `MarketContext` (cutover order: **S2 first** — lowest risk, BAR_PROXY, single evaluator, already throttled; then **Convergence + Commodity** together — shared tick-CVD provider; then **S1** — retire the 2nd MP engine `MarketProfileBuilder` behind a version flag + collapse the 3 regime notions into one provider whose params are `feature_version` variants; then **Auction last** — highest blast radius, the 200-320s pipeline). Turn per-horizon **sufficiency gates** from advisory to blocking (§5). Ship **opt-in cross-lane exposure/netting** and portfolio construction — each OFF, measured, owner-gated on. Independently, fix the option-candle REST-only defect / enable `CHAIN_CANDLE_BUILDER_ENABLED` so the options sufficiency floors become enforceable beyond the 5 indices.
- **Touches:** every lane's `evaluate()` read path; retirement of `MarketProfileBuilder`, the duplicate loaders, and the merged regime; the greeks feed.
- **Effort:** L. **Reversibility:** per-lane read-path flag back to the lane's own compute; sufficiency gates and exposure netting each independently revertible. **Flag-gated / behavior-unchanged:** yes / **NO — this step changes behavior** (sufficiency gates newly block BAR_PROXY signals; exposure netting suppresses entries). Hence it is last, opt-in, and measured.
- **Parity gate:** shadow-live parity on each lane's `SignalIntent` for N sessions **before** cutover; then measured A/B (gates OFF vs ON) with owner sign-off before enabling any behavior-changing gate.

---

## 5. Tiered Universe (A/B/C/D) — grounded in catalog + budget

**Tiering axis = REST-derived-data cost, not tick breadth.** The WS tape is already broad (9,990 symbols / 3 days) and cheap; the scarce resource is REST (Upstox ~66/min after the CRITICAL reservation; Fyers 200/min). The 2026-07-17 collapse was precisely a tier-leak — broad names pulled into the fast REST loop.

| Tier | Membership (grounded) | Data | Cost | Quota class | Status |
|---|---|---|---|---|---|
| **A — microstructure** | 6 indices + 8 MCX + ~20-30 liquid F&O (the 152 sized-L1 symbols in `market_ticks` + auction book symbols) | real ticks / L1 (+L2 on-demand via `data_router.subscribe_depth:734`) | WS only | CRITICAL | Feasible now. **Gap:** L2 depth un-persisted, ref-counted to auction OF — A needs it cache-exposed for convergence/directional too |
| **B — intraday ~50 ranked** | directional NIFTY-50 ∩ catalog (already ranked/rotating/readiness-gated `service.py:175-227`) | bars + quotes + walls + selective tick CVD | ATM watchlist writer in CRITICAL 40% reserve | CRITICAL/STANDARD | Feasible now — "promote directional's stock path to the shared substrate" |
| **C — broad research ~211** | full `fo_contract_catalog` scan (S1 via `window_calculator:262`) | batched bars/profiles | **must be BULK ≤25% + round-robin** | BULK | Feasible with the quota discipline already built; `stock_readiness`'s batched 2-query pattern (`data.py:318`) is the template |
| **D — on-demand** | full chain/greeks/footprint/depth per request (FMP, deep-dive) | demand-driven | triggered by a policy `requirements()` asking for D, not a standing poll | on-demand | Feasible; pieces exist (`refresh_index_option_chains`, `subscribe_depth`, greeks enrichment) |

**Promotion/demotion rules (the subscription policy):**
- **Promote C→B** when a name enters the ranked intraday set (directional's rank/rotation) OR a policy's `requirements()` asks for walls/tick-CVD at intraday horizon. **Demote B→C** when it falls out of rank OR readiness fails (`filter_ready_stock_symbols:175`), returning it to batched BULK.
- **Promote B→A** only for names with sized-L1 in `market_ticks` (the 84% cohort) and a real book symbol; **demote A→B** when L1 sizes go stale/frozen (`DataQualityAgent` frozen-value detection) — an A name that loses real ticks is not silently kept at A (it would masquerade as REAL_TICKS).
- **On-demand D** is granted per request and released after use (ref-counted, like `subscribe_depth`), never promoted to a standing tier.
- **Enforcement:** tier → quota class is bound at the `rate_limiter.py` `broker_class` contextvar. Tier membership without contextvar enforcement leaks (the collapse proves it) — so promotion/demotion **must** re-stamp the contextvar, not just a label.
- **Budget guard:** the chain builder's INDEX@60s / STOCK@180s cadence (`chain_candle_builder.py:48`) and `source_policy` cadence routing are the existing knobs the tier cadences formalize.

### Sufficiency-gate matrix (horizon × minimum accepted tier)

| Horizon | Order flow | Bars / profiles | Options / greeks | Regime / VIX | Sessions / rollover |
|---|---|---|---|---|---|
| **Scalp** (auction scalp, convergence-fast) | **REAL_L1_TRADES or REAL_L2 — BAR_PROXY forbidden** | completed 3m + current profile | ATM greeks REAL for premium sizing | fresh regime | intraday only |
| **Intraday** (S2, auction swing-lite, directional intraday) | **REAL_TICKS min** (BAR_PROXY only in explicit degraded mode) | completed **3m + 15m** + current/prior **profiles** + quote | chain + **walls** + IV (fraction-unit) | regime + **VIX available** | current + prior session |
| **Swing** (auction swing/positional-lite) | REAL_TICKS preferred, **BAR_PROXY acceptable** | **15m/30m** profiles + composite | greeks + walls; expiry-sufficiency (DTE≥7 per option-candle note) | longer-window regime | current + prior + composite |
| **Positional** (directional positional, CBE) | OF **not required** (positioning replaces it) | **daily + composite** profiles | daily greeks / **IV percentile** (low-IV favors long premium), positioning (OI/GEX) | **long regime + India-VIX percentile** | **rollover-safe futures**, multi-session |

Grounded directly in today's honest gates: convergence's scalp/intraday `real_tick_cvd` requirement (`engine.py:355`), the IV-percentile-conditions-size finding (memory: low IV favors long premium), and the `dte<7 skip` (memory: option-candle coverage) as swing/positional expiry-sufficiency. **Hard blockers:** `index_futures_candles` empty (positional index-futures NO-GO) and the greeks-vs-watchlist table split — both must be resolved before the positional/options floors can be enforced (Step 7).

---

## 6. Ledger + Risk Consolidation (preserving per-policy exits + the UNCAPPED bypass)

Covered mechanically in Steps 5–6. The two doctrine-preserving invariants:

- **`SIGNAL_VALIDATION_UNCAPPED` (`config.py:229`) is preserved per gate, not globally.** Today five governors bypass *different* caps and keep *different* infra checks; the shared governor keeps this as gate-level `bypassed_by_validation` tags. A single global `if UNCAPPED: skip` is explicitly rejected — it would either disable the Commodity catastrophe kill-switch backstop (`commodity_strategy_agent.py:3357`, which the flag does NOT disable) or re-impose Auction daily-loss (changing doctrine). Auction keeps `max_concurrent` + `min_model_confidence` + 15-min close buffer even uncapped (`governor.py:73,77,91`); Directional's IV factor **sizes-never-vetoes** (`risk.py:94-112`); Convergence reports circuit state but suppresses the entry lock when uncapped (`paper.py:130`); S1/MACD-Refined are already uncapped and size off a notional base.
- **Exits stay lane-owned.** The governor never closes a position. Convergence CVD-reversal / wall exits (`paper.py:96-119`), Directional flip-confirmation (`FLAT_CONFIRMATION_SECONDS`, `paper.py:134`), Commodity cooldowns (`:2005-2011`), and Auction exit-confirmation cycles (`paper/book.py:93`) are genuinely different and correct — they remain in each policy's `manage()`.

---

## 7. What BREAKS if Consolidated Naively (load-bearing)

**Signal union**
- **Direction-encoding collapse.** `SHORT` (short future) and `PE` (long-premium put) both profit when spot falls but are opposite balance-sheet positions; treating them as one enum mis-nets. Must normalize to `LONG_PREMIUM_PUT` vs `SHORT_FUT` (`schemas.py:76` CE/PE vs `schemas.py:8` LONG/SHORT).
- **Confidence-scale merge.** Convergence emits `score` 0-100 + `quality` A+/VALID (`engine.py:421,431`), not `[0,1]`. A min-confidence gate assuming `[0,1]` (Auction `min_model_confidence=0.55`) passes every Convergence signal (score≥1) or rejects all. Keep both in `conviction_extras`; never collapse.
- **Setup-dedup loss.** Convergence's consumed-setup dedup depends on `setup_id=symbol:action:bar_time` (`paper.py:138`). Minting a fresh `signal_id` per cycle without preserving `setup_id` re-opens the same setup every scan (the churn flip-confirmation was built to stop).

**Risk governor**
- **Bypass flattening** (§6) and **kill-switch fan-out** — global kill firing one lane's breach halts healthy lanes.
- **Cross-lane exposure changes behavior.** Every lane sized against its OWN ₹1,000,000 book. A shared exposure cap suppresses entries that pass today — it is new alpha-affecting policy, shipped OFF/opt-in/measured, never a side effect of the merge.

**Ledger**
- **Two realized-pnl truths** — Directional sums lifetime from a DB-wide payload sum (`paper.py:875-892`); `agent_positions` has per-row `realized_pnl`; Convergence sums a JSON array. A naive UNION double-counts lanes that write both a position row and a trade-book row (Commodity). One authoritative realized source (trade-book rows).
- **Capital-semantics drift** — S1's `reconcile_available_capital` rebuilds cash from realized+reserved (`portfolio.py:311`, its own warning at `:106`). Unscoped in a shared store it corrupts S1's cash. Keep every capital computation `strategy_key`-scoped.
- **Session-id collision** — legacy `orders/positions` FK to `paper_sessions.id`; `agent_positions.session_id` is unconstrained; JSON books use date strings. Don't unify "session" naively.
- **Journal volume** — 14,492 Directional journal rows vs hundreds in JSON; keep per-lane retention as store policy.

**MarketContext / substrate**
- **Context is not free to share.** Lanes compute at different cadences/scopes (S2 throttles MP to 90s `:89`; Convergence adaptive 45-90s `:101`; each caches its own MP). One eager build re-introduces the 2026-07-13 event-loop seizure. MarketContext must be **lazily materialized per (instrument, tier, feature)** and reuse existing caches.
- **Capability tier must fail closed but roll out advisory-first.** Only Convergence expresses tier today; S1/Directional trade on bars. Making the tier blocking on day one **newly blocks working lanes** — introduce it as advisory metadata (Step 4), wire sufficiency gates only in Step 7, measured.

---

## 8. Non-Goals (what this ADR does NOT do)

- **Does not merge the four policies** into one engine (§1.3). `evaluate()` bodies stay separate and lane-owned.
- **Does not change any lane's trading edge, entry logic, or exit logic.** Steps 1–6 are behavior-preserving by construction and gated on parity; Step 7's behavior changes are opt-in and measured.
- **Does not enable cross-lane exposure netting / portfolio construction as part of the refactor.** It becomes *possible* (Step 6) but ships OFF pending owner sign-off.
- **Does not build the REAL_L2 tier.** No per-level book is ingested; every depth ladder today is synthetic (`live.py:1462-1484`). REAL_L2 is a data-ingestion decision, not a refactor (owner decision, §11).
- **Does not fix the option-candle REST-only defect or fill stock greeks as a precondition** — it is sequenced into Step 7 because the options sufficiency floors depend on it, but the substrate ships without waiting on it.
- **Does not touch live-money paths** — all lanes are paper; the `orders/positions/paper_sessions` legacy live-engine tables are retired only after confirming no live reader (Step 5), otherwise left alone.
- **Does not build the index-futures MP sleeve** (`index_futures_candles` empty — data NO-GO).
- **Does not add a second Market-Profile or order-flow algorithm** — it consolidates onto one of each (retiring `MarketProfileBuilder`), it does not invent a third.

---

## 9. How this maps to the reference architectures

| Reference | Their construct | This ADR's mapping |
|---|---|---|
| **QuantConnect** | Universe → multiple Alphas → Portfolio Construction → Risk → Execution | Tiered Universe (§5) → policy `evaluate()` alphas emitting `SignalIntent` → opt-in Portfolio Construction/netting (Step 7) → shared `RiskGovernor` (§6) → canonical `OrderLedger` (§5) |
| **NautilusTrader** | Central instruments / data / cache / risk / execution + **reconciliation** | `InstrumentRef` (instruments) + `MarketContext`/Feature Store (data + cache) + shared `RiskGovernor` + `OrderLedger`; the **paisa-match reconciliation gate** before any read-path flip (Step 5) is Nautilus's reconciliation pattern directly |
| **Hummingbot** | Market-data controllers vs position-owning executors | Feature Store providers = data controllers (compute-once, no positions); policy `manage()` + lane exits = position-owning executors — the exact split this ADR keeps (§6) |
| **Freqtrade** | Short refreshed informative universe + tiered subscription | Tier B ranked/rotating/readiness-gated ~50 (`service.py:175-227`) = the refreshed informative universe; A/B/C/D quota-class subscription = the tiered subscription, enforced at the shared REST token bucket (§5) |

---

## 10. Consequences

**Positive:** one build per `(instrument, timeframe)` kills the cross-lane recompute (the 200-320s auction block and the ~72-query/cycle convergence fan-out stop being duplicated); one reconcilable book makes cross-strategy exposure/netting *possible*; the validation doctrine lives in one gate registry instead of five drifting copies (the auction governor already flipped-and-restored once, comment `governor.py:52-55`); capability tiers make "why didn't it trade" explainable and stop BAR_PROXY masquerading as real flow; versioned features let an algo change fork a lineage instead of silently rewriting history; the lane registry reports real ledger/risk state for all lanes.

**Negative / accepted:** a dual-write period transiently doubles ledger writes; direction-enum normalization + confidence-scale preservation add per-lane adapter code; MarketContext must respect existing caches (no clean-slate rebuild); the Feature Store becomes a central dependency (mitigate: fail-safe to the lane's current path on any provider error, mirroring `source_policy`'s reorder-never-drop discipline; version-pinning tests to prevent cache splits). **The single largest latent hazard is treating any of these as a pure refactor** — cross-lane exposure, capability-tier sufficiency gates, tick/lot correction, and a shared realized-pnl source each change behavior and must ship measured and opt-in.

---

## 11. Open Owner-Decisions and Sign-off Checkpoints

Decisions the owner must make; each is a **sequencing checkpoint** where the migration pauses for sign-off.

1. **S2 sufficiency: named-fallback vs block.** S2 is BAR_PROXY today (`of_source="bar_inference"`). At Step 7, does S2 at intraday horizon **block** (honest, but stops S2 trading until real ticks are wired) or run in an **explicit degraded/named-fallback mode** (keeps trading, tier-labeled)? — *Checkpoint before Step 7 S2 cutover.*
2. **Scalp tier build-out.** The scalp horizon needs REAL_L1_TRADES/REAL_L2 and forbids BAR_PROXY, but REAL_L2 is structurally unavailable today. Build scalp now (A-tier only, L1) or defer until an L2 feed exists? — *Checkpoint before enabling scalp sufficiency gates.*
3. **Second broker token for tiers.** A-tier L2 persistence + broad-C batched REST may exceed one shared token's budget (ties to the app-split study). Provision a 2nd broker token? — *Checkpoint before Step 4 enforcement if budget telemetry shows starvation.*
4. **How far to consolidate ledgers.** Retire legacy `orders/positions/paper_sessions` entirely, or keep for the live-engine? Fully absorb Directional's `directional_paper_*` schema, or bridge it? — *Checkpoint before Step 5 legacy retirement.*
5. **Cross-lane exposure netting.** Each lane is sized against its own ₹1M book. When (if ever) to enable shared exposure/netting given it suppresses entries that pass today? — *Checkpoint before Step 7 exposure gate ON.*
6. **`CHAIN_CANDLE_BUILDER_ENABLED` vs the enrichment stopgap.** Turn on the real-greeks chain builder in the live budget (fixes stock greeks / the options sufficiency floors) or keep the index-only snapshot-copy stopgap? — *Checkpoint before Step 7 options-floor enforcement.*
7. **REAL_L2 ingestion.** Ingest a real per-level depth feed to unlock the REAL_L2 tier, or accept that scalp/L2 gates are permanently unsatisfiable? — *Owner call, independent of the migration.*
8. **"MP+OF" identity.** Is MP+OF the Commodity desk, the S2 index port, or both under one policy — they share the `commodity_mp_signal` evaluator but own different books. — *Checkpoint before Step 7 MP+OF cutover.*
9. **Sequencing against the concurrent edit.** A concurrent workflow is editing `strategy2_mp_of.py` / convergence engine+service / desks. Sequence Step 3/7 to avoid a merge collision on those files. — *Checkpoint before Step 3 touches those files.*

---

## Appendix A — Key files (absolute)

- Spine: `/Users/ramachandran/CLAUDE PROJECTS/Nomad Curie/TradeBot/backend/core/lane_registry.py`, `…/backend/core/laneset.py`
- Standing flag: `…/backend/core/config.py:229`
- Substrate seed: `…/backend/market_data/market_intelligence_runtime.py`, `…/backend/market_data/atm_watchlist.py`, `…/backend/market_data/fo_universe_bootstrap.py`, `…/backend/agent/window_calculator.py:245`
- Feature engines: `…/backend/auction_intelligence/market_profile/engine.py`, `…/backend/market_data/market_profile.py`, `…/backend/analytics/orderflow.py`, `…/backend/auction_intelligence/order_flow/engine.py`
- Per-lane loaders/emitters: `…/backend/auction_intelligence/live.py` (`:594-960`, `:1209-1524`, `:1657`), `…/backend/auction_intelligence/service.py:183`, `…/backend/institutional_convergence/service.py:384`, `…/backend/institutional_convergence/engine.py:322-358`, `…/backend/paper_engine/strategy2_mp_of.py:126-639`, `…/backend/paper_engine/commodity_mp_signal.py:264-1113`, `…/backend/directional_options/data.py:138-401`, `…/backend/directional_options/signals.py:276`, `…/backend/paper_engine/strategy_agent.py`
- Metadata: `…/backend/analysis/instruments.py`, `…/backend/market_data/symbols.py`, `…/backend/market_data/commodity_contract_specs.py`
- DataQuality / source: `…/backend/market_data/data_quality_agent.py`, `…/backend/market_data/source_policy.py`, `…/backend/market_data/greeks_enrichment.py`, `…/backend/market_data/chain_candle_builder.py`
- Risk: `…/backend/auction_intelligence/risk/governor.py`, `…/backend/directional_options/risk.py`, `…/backend/institutional_convergence/paper.py:200`, `…/backend/paper_engine/commodity_strategy_agent.py:1974,3357`, `…/backend/paper_engine/portfolio.py:311`
- Ledger anchor: DB tables `agent_positions` + `paper_trade_book`; audit `agent_audit_events`
- Budget: `…/backend/brokers/rate_limiter.py`

## Appendix B — DB ground-truth (live, Sat 2026-07-18)

- `fo_underlying_catalog`: 217 rows (6 INDEX + 211 STOCK, all keyed, 211/211 lot_size, no tick_size). `fo_contract_catalog`: 50,482 (only source of tick_size/freeze/min-lot). `fo_expiry_catalog`: 3,095.
- `market_ticks` last 2d: 152 symbols / 1.99M rows / **84% real L1 sizes**. 9,990 distinct option symbols on WS over 3d.
- `option_premium_candles` last 3d: upstox **97.4% iv-NULL**, fyers 98.9% NULL, **0 `fyers_chain` rows**.
- `option_chain_snapshots` last 2d: 323,844 rows / 189,461 iv-set / **5 symbols (indices only)**.
- `market_profiles`: 291 rows / 4 symbols / 1 timeframe since 2026-02-12. `index_futures_candles`: **0 rows**.
- Ledger: `agent_positions` 78 open/65 closed; `paper_trade_book` 128 closed; `directional_paper_positions` 266 / `_journal` 14,492; legacy `orders/positions` 261/144; `agent_audit_events` 620,560.
- Budget: Fyers 10/s·200/min·100k/day; Upstox 50/s·2000/30min. Quota CRITICAL 40% / STANDARD ≥35% / BULK ≤25%.
