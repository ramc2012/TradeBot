# Auction Intelligence Deployment Blueprint

This document translates the staged rollout into an operator-ready deployment plan for the isolated `backend/auction_intelligence/` module. It keeps the current strategy runtime untouched and defines the production path for:

- `BANKNIFTY` futures first
- one structural Market Profile swing sleeve first
- order flow as a confirmation and execution layer
- shadow options mapping only after futures validation
- multi-agent expansion only after the first sleeve survives hard validation gates

## Scope

### Trading rollout order

1. `BANKNIFTY` futures structural swing sleeve
2. `BANKNIFTY` futures with order-flow timing confirmation
3. shadow weekly-options mapping
4. live weekly-options sleeve
5. second sleeve (`positional` or `scalp`)
6. multi-agent allocator and sleeve netting

### Validation principle

Validation is stricter than model development. No stage is promoted because the logic is elegant. Promotion only happens when data quality, reproducibility, execution realism, and operational safety all pass together.

## Deployment Rings

### Ring 1: must-have now

- Python 3.12
- FastAPI
- Pydantic
- PostgreSQL + TimescaleDB
- Redis Streams
- Docker Compose
- Prometheus + Grafana
- broker-agnostic execution adapter
- deterministic Market Profile engine
- deterministic rule engine
- paper-trading loop

### Ring 2: later

- MLflow
- Gymnasium environments
- SB3-Contrib `RecurrentPPO`
- RLlib offline RL / multi-agent stack
- ABIDES execution simulator
- feature-store extraction only if scale requires it

## Service List

The current repo already runs `db`, `redis`, `backend`, `research-sync`, and `frontend` via [docker-compose.yml](/Users/chinnadurairamachandran/Claude%20Projects/TradingBot/nomad-curie/docker-compose.yml). The staged production service layout below preserves that base and adds explicit workers around the isolated strategy module.

| Service | Runtime | Initial deploy stage | Responsibility | Main inputs | Main outputs |
|---|---|---:|---|---|---|
| `backend-api` | FastAPI | 0 | Operator API, broker auth, control plane, REST access to auction-intelligence analysis | HTTP, DB, Redis | HTTP APIs, control commands |
| `market-feed-worker` | Python asyncio | 0 | Broker tick ingestion, top-of-book snapshots, heartbeat, session clock | Fyers/Upstox WS | `md.ticks.raw`, `md.quotes.raw`, `md.heartbeat` |
| `bar-builder-worker` | Python asyncio | 0 | Deterministic 1m, 5m, 30m, daily bar aggregation from raw market events | `md.ticks.raw` | `md.bars.1m`, `md.bars.5m`, `md.bars.30m`, `md.bars.1d` |
| `research-sync` | Python batch/daemon | 0 | Historical spot and later derivatives backfill into TimescaleDB | Broker REST | research cache tables, validation snapshots |
| `reference-data-worker` | Python batch | 0 | Session calendar, holidays, expiry maps, instrument master, rollover references | static data, broker master | reference tables |
| `mp-feature-worker` | Python asyncio | 1 | Market Profile computation and deterministic recomputation checks | `md.bars.30m`, reference tables | `ai.mp.snapshots`, `ai.mp.references` |
| `order-flow-worker` | Python asyncio | 2 | Spread, imbalance, delta, queue pressure, micro-price, burst metrics | `md.ticks.raw`, `md.quotes.raw` | `ai.orderflow.snapshots` |
| `regime-worker` | Python asyncio | 1 | Structural regime classification using MP and session context | `ai.mp.snapshots`, `md.bars.30m` | `ai.regime.labels` |
| `swing-agent-worker` | Python asyncio | 1 | First deployable sleeve using MP structure and later order-flow confirmation | `ai.regime.labels`, `ai.mp.snapshots`, `ai.orderflow.snapshots` | `ai.signals.swing` |
| `risk-governor-worker` | Python asyncio | 1 | Shared hard-risk rules, stale data shutdown, exposure caps, loss guards | agent signals, portfolio state, broker state | `ai.risk.decisions`, kill-switch events |
| `paper-execution-worker` | Python asyncio | 1 | Paper fills, state reconciliation, journal writes, simulated exit path | risk-approved intents | paper orders, journals, attribution |
| `live-execution-worker` | Python asyncio | 4 | Broker-agnostic order routing, child order slicing, cancel/replace, reconciliation | risk-approved intents | live orders, execution reports |
| `options-mapper-worker` | Python asyncio | 3 | Map approved futures bias into weekly options candidates in shadow mode first | futures signals, option chain, Greeks | `ai.options.shadow_candidates` |
| `allocator-worker` | Python asyncio | 5 | Sleeve budgets, directional permissions, conflict resolution | all sleeve signals, risk state | `ai.allocation.decisions` |
| `metrics-exporter` | FastAPI/Prometheus | 0 | Expose runtime metrics and gate counters | DB, Redis, app metrics | Prometheus scrape endpoint |
| `prometheus` | Prometheus | 0 | Scrape system/application metrics | exporter, backend | metrics store |
| `grafana` | Grafana | 0 | Dashboards for health, signals, fills, reconciliation, gates | Prometheus, PostgreSQL | dashboards and alerts |

## Redis Event Topics

Move strategy coordination to Redis Streams for append-only event flow and replayable debugging. Existing Redis pub/sub can remain for frontend push updates, but strategy-critical paths should use Streams.

| Stream | Producer | Consumer group(s) | Retention | Required payload keys |
|---|---|---|---|---|
| `md.ticks.raw` | `market-feed-worker` | `bar-builder`, `orderflow`, `replay` | 7 sessions | `symbol`, `ts`, `ltp`, `bid`, `ask`, `bid_qty`, `ask_qty`, `source`, `sequence` |
| `md.quotes.raw` | `market-feed-worker` | `orderflow`, `execution`, `shadow` | 7 sessions | `symbol`, `ts`, `bid`, `ask`, `bid_size`, `ask_size`, `source` |
| `md.depth.raw` | `market-feed-worker` | `orderflow`, `execution` | 3 sessions | `symbol`, `ts`, `levels`, `source` |
| `md.bars.1m` | `bar-builder-worker` | `research`, `validation` | 180 sessions | `symbol`, `bar_start`, `open`, `high`, `low`, `close`, `volume`, `bar_hash` |
| `md.bars.30m` | `bar-builder-worker` | `mp-feature`, `regime` | 365 sessions | `symbol`, `bar_start`, `session_date`, `open`, `high`, `low`, `close`, `volume`, `bar_hash` |
| `ai.mp.snapshots` | `mp-feature-worker` | `regime`, `swing-agent`, `validation` | 365 sessions | `symbol`, `session_date`, `profile_id`, `poc`, `vah`, `val`, `ib_high`, `ib_low`, `features_version` |
| `ai.orderflow.snapshots` | `order-flow-worker` | `swing-agent`, `execution`, `validation` | 30 sessions | `symbol`, `ts`, `spread`, `top_imbalance`, `depth_imbalance`, `delta`, `micro_price`, `feature_version` |
| `ai.regime.labels` | `regime-worker` | `swing-agent`, `allocator`, `validation` | 365 sessions | `symbol`, `session_date`, `label`, `confidence`, `allowed_directions`, `rules_version` |
| `ai.signals.swing` | `swing-agent-worker` | `risk-governor`, `paper-execution`, `shadow` | 365 sessions | `signal_id`, `symbol`, `side`, `setup`, `entry_ref`, `stop_ref`, `target_ref`, `confidence`, `policy_version` |
| `ai.options.shadow_candidates` | `options-mapper-worker` | `validation`, `operator-ui` | 90 sessions | `signal_id`, `underlying`, `expiry`, `strike`, `option_type`, `reason`, `shadow_only` |
| `ai.risk.decisions` | `risk-governor-worker` | `paper-execution`, `live-execution`, `alerts` | 365 sessions | `signal_id`, `approved`, `reasons`, `kill_switch`, `risk_version`, `exposure_snapshot` |
| `ai.execution.requests` | `risk-governor-worker` | `paper-execution`, `live-execution` | 365 sessions | `request_id`, `signal_id`, `mode`, `order_plan`, `execution_style`, `ts` |
| `ai.execution.reports` | `paper-execution-worker`, `live-execution-worker` | `portfolio`, `validation`, `operator-ui` | 365 sessions | `request_id`, `status`, `fills`, `slippage`, `latency_ms`, `broker_order_ids` |
| `ai.audit.events` | all workers | `compliance`, `validation`, `alerts` | 365 sessions | `event_type`, `component`, `severity`, `ts`, `payload` |
| `ai.validation.events` | validation jobs | `operator-ui`, `alerts` | 365 sessions | `gate`, `run_id`, `status`, `metric_name`, `metric_value`, `threshold` |

### Consumer-group rule

- Every strategy-critical stream must use consumer groups.
- Every event must be idempotent by `(stream, message_id)` or a domain key such as `signal_id`.
- Frontend-only pub/sub channels can remain ephemeral.

## TimescaleDB Schema Blueprint

Use the existing tables in [models.py](/Users/chinnadurairamachandran/Claude%20Projects/TradingBot/nomad-curie/backend/db/models.py) and the current hypertables in [001_initial_schema.py](/Users/chinnadurairamachandran/Claude%20Projects/TradingBot/nomad-curie/backend/db/migrations/versions/001_initial_schema.py), then add isolated strategy tables instead of overloading the current generic order/proposal tables.

### Existing tables to keep using

- `broker_sessions`
- `orders`
- `positions`
- `agent_proposals`
- `agent_logs`
- `market_ticks`
- `market_profiles`
- `option_chain_snapshots`
- research-cache tables introduced by later migrations

### New reference tables

#### `trading_sessions`

Purpose: canonical session calendar for NSE trading and partial sessions.

Key columns:

- `session_date date primary key`
- `timezone text not null`
- `session_open timestamptz not null`
- `session_close timestamptz not null`
- `is_holiday boolean not null`
- `is_partial boolean not null`
- `holiday_name text null`
- `market_segment text not null`

#### `instrument_rollovers`

Purpose: deterministic futures rollover and continuous-contract references.

Key columns:

- `underlying text not null`
- `expiry date not null`
- `roll_date date not null`
- `front_contract text not null`
- `next_contract text not null`
- `selection_rule text not null`

Primary key: `(underlying, expiry)`

### New hypertables

#### `market_bars`

Purpose: canonical bars shared across research, replay, paper, and live.

Columns:

- `time timestamptz not null`
- `symbol text not null`
- `timeframe text not null`
- `session_date date not null`
- `open double precision not null`
- `high double precision not null`
- `low double precision not null`
- `close double precision not null`
- `volume bigint not null`
- `trade_count bigint null`
- `vwap double precision null`
- `source text not null`
- `bar_hash text not null`

Indexes:

- `(symbol, timeframe, time desc)`
- `(session_date, symbol, timeframe)`
- unique `(symbol, timeframe, time, source)`

#### `auction_profile_snapshots`

Purpose: immutable numeric Market Profile outputs per session/composite build.

Columns:

- `time timestamptz not null`
- `symbol text not null`
- `session_date date not null`
- `profile_scope text not null`
- `profile_id uuid not null`
- `poc double precision not null`
- `vah double precision not null`
- `val double precision not null`
- `ib_high double precision null`
- `ib_low double precision null`
- `ib_width double precision null`
- `range_extension_up double precision null`
- `range_extension_down double precision null`
- `single_print_count integer not null`
- `poor_high boolean not null`
- `poor_low boolean not null`
- `spike_direction text null`
- `vpoc_untouched boolean not null`
- `overlap_ratio double precision null`
- `poc_shift double precision null`
- `feature_version text not null`
- `raw_inputs_hash text not null`

Indexes:

- `(symbol, session_date desc, profile_scope)`
- unique `(symbol, session_date, profile_scope, feature_version)`

#### `auction_orderflow_snapshots`

Purpose: order-flow features used for confirmation and later execution training.

Columns:

- `time timestamptz not null`
- `symbol text not null`
- `session_date date not null`
- `spread double precision not null`
- `top_imbalance double precision not null`
- `depth_imbalance double precision null`
- `delta double precision not null`
- `cumulative_delta double precision null`
- `micro_price double precision not null`
- `queue_pressure double precision null`
- `vwap_drift double precision null`
- `volatility_burst double precision null`
- `fill_prob_proxy double precision null`
- `adverse_selection_proxy double precision null`
- `feature_version text not null`

Indexes:

- `(symbol, time desc)`
- `(session_date, symbol)`

#### `auction_signal_events`

Purpose: immutable signal history from every sleeve.

Columns:

- `time timestamptz not null`
- `signal_id uuid not null`
- `symbol text not null`
- `agent_name text not null`
- `setup_name text not null`
- `regime_label text not null`
- `side text not null`
- `action text not null`
- `entry_ref double precision null`
- `stop_ref double precision null`
- `target_ref double precision null`
- `confidence double precision not null`
- `policy_version text not null`
- `approved_by_risk boolean null`
- `rejection_reason text null`

Indexes:

- `(symbol, time desc)`
- `(agent_name, time desc)`
- unique `(signal_id)`

#### `auction_execution_reports`

Purpose: paper/live execution outcomes and slippage attribution.

Columns:

- `time timestamptz not null`
- `request_id uuid not null`
- `signal_id uuid not null`
- `mode text not null`
- `broker text not null`
- `symbol text not null`
- `status text not null`
- `requested_qty integer not null`
- `filled_qty integer not null`
- `avg_fill_price double precision null`
- `arrival_price double precision null`
- `slippage_bps double precision null`
- `latency_ms integer null`
- `child_order_count integer not null`
- `error_code text null`
- `raw_report jsonb not null`

Indexes:

- `(symbol, time desc)`
- `(mode, status, time desc)`
- unique `(request_id)`

#### `auction_trade_attribution`

Purpose: one row per completed trade for setup-, regime-, and sleeve-level evaluation.

Columns:

- `closed_at timestamptz not null`
- `trade_id uuid not null`
- `signal_id uuid not null`
- `symbol text not null`
- `agent_name text not null`
- `setup_name text not null`
- `regime_label text not null`
- `entry_time timestamptz not null`
- `exit_time timestamptz not null`
- `gross_pnl double precision not null`
- `fees double precision not null`
- `slippage double precision not null`
- `net_pnl double precision not null`
- `mae double precision null`
- `mfe double precision null`
- `holding_minutes integer not null`
- `paper_live_tag text not null`

Indexes:

- `(closed_at desc, symbol)`
- `(agent_name, closed_at desc)`
- `(setup_name, closed_at desc)`

### Validation and governance tables

#### `validation_runs`

Purpose: record every gate run and its artifact lineage.

Columns:

- `run_id uuid primary key`
- `run_type text not null`
- `stage text not null`
- `started_at timestamptz not null`
- `completed_at timestamptz null`
- `status text not null`
- `code_version text not null`
- `data_snapshot_id text not null`
- `config_version text not null`
- `notes text null`

#### `validation_metrics`

Purpose: machine-readable pass/fail metrics by run.

Columns:

- `run_id uuid not null`
- `metric_name text not null`
- `metric_value double precision not null`
- `threshold_value double precision null`
- `comparator text not null`
- `passed boolean not null`

Primary key: `(run_id, metric_name)`

#### `promotion_decisions`

Purpose: auditable promotion record for Gate A through Gate E.

Columns:

- `stage text primary key`
- `approved_at timestamptz null`
- `approved_by text null`
- `decision text not null`
- `evidence_run_id uuid null`
- `rollback_trigger text null`

## Validation Matrix

### Gate A: data and feature engine

This gate blocks all strategy testing until the data and MP feature layer is stable.

| Check | Exact pass rule |
|---|---|
| Session coverage | `0` silent gaps during trading session for promoted symbols |
| Duplicate bars | `0` duplicate `(symbol, timeframe, time)` rows in promoted datasets |
| Out-of-order events | `0` out-of-order sequence violations after ingestion normalization |
| Deterministic rebuild | identical `bar_hash` and `raw_inputs_hash` across 3 repeated rebuilds |
| MP level precision | POC, VAH, VAL, IB values match reference build within `1 * tick_size` |
| Unit tests | `100%` pass for TPO, POC, VAH/VAL, IB, single prints, poor highs/lows, spike zones, composites |
| Known-day labels | at least `85%` agreement on manually labeled primary regimes and `100%` agreement on day direction for the curated set |

### Gate B: deterministic rule engine

This gate evaluates the structural setups before any model or execution learning.

Approved setup set:

- IB breakout
- IB failure
- 80% rule
- spike acceptance / rejection
- balance-area breakout
- gap continuation / gap failure
- VPOC rejection

| Check | Exact pass rule |
|---|---|
| Net expectancy | positive after fees and slippage on the aggregate sample |
| Profit factor | `>= 1.15` aggregate and `>= 1.00` in each required walk-forward regime bucket |
| Walk-forward stability | at least `70%` of walk-forward windows positive net expectancy |
| Setup concentration | no single setup contributes more than `45%` of total net PnL |
| Regime breadth | at least `3` distinct market regimes with positive net expectancy |
| Slippage resilience | expectancy remains positive after a `+25%` slippage stress bump |
| Drawdown control | max drawdown stays within planned sleeve budget and never exceeds `1.25x` modeled budget |

### Gate C: shadow mode

This gate runs the production data, features, signals, and risk stack without sending live orders.

| Check | Exact pass rule |
|---|---|
| Observation period | minimum `20` completed trading sessions, target `30` |
| Position drift | `0` unresolved broker/internal position mismatches persisting more than `5` minutes |
| Stale-signal count | `< 0.5%` of generated signals |
| Simulated vs observed fill drift | median absolute drift `<= 2 * tick_size`, p95 `<= 8 * tick_size` |
| Reconciliation incidents | `0` critical incidents and `<= 1` non-critical incident per week |
| Kill switch | tested successfully at least `2` times in controlled drills |
| Operator coverage | dashboards, alerts, and manual override exercised during the observation window |

### Gate D: paper trading

Paper trading must use the same data, features, rules, risk, and execution path as live, with only broker routing replaced.

| Check | Exact pass rule |
|---|---|
| Observation period | minimum `20` completed trading sessions, target `30` |
| Net expectancy | positive after modeled fees and slippage |
| Daily loss limit | `100%` enforcement in test and production-like drills |
| Reconciliation | `0` unexplained order-state mismatches |
| Operational incidents | `0` P1 incidents and `<= 2` P2 incidents over the full window |
| Time to recovery | any degraded service recovered within `15` minutes in drills |
| Auditability | every signal, approval, rejection, order, and override linked by `signal_id` or `request_id` |

### Gate E: live canary

This gate promotes the sleeve from paper to smallest-size live trading.

| Check | Exact pass rule |
|---|---|
| Scope | one symbol family, one sleeve, reduced size only |
| Canary duration | minimum `10` completed live sessions before size increase |
| Paper/live divergence | net expectancy and fill drift remain within `20%` of paper baseline |
| Operational surprises | `0` unknown failure classes during canary |
| Manual override | active throughout canary period |
| Risk controls | no breach of hard daily loss cap, exposure cap, or stale-data shutdown logic |

### Gate F: ML / RL promotion

No ML or RL layer is promoted unless it beats the deterministic baseline on the relevant task without worsening tail risk.

| Check | Exact pass rule |
|---|---|
| Supervised model | improves task metric over deterministic baseline on out-of-sample data |
| Imitation model | does not reduce net expectancy or worsen drawdown versus deterministic baseline |
| RL timing/execution | improves slippage-adjusted or timing-adjusted outcome, not just gross return |
| Tail risk | no degradation in max drawdown, time-to-recovery, or adverse-selection metrics |
| Offline reproducibility | training artifacts, checkpoints, and data snapshots fully reproducible |

## Stage-by-Stage Promotion Plan

### Stage 0: infrastructure and data integrity

- Deploy `backend-api`, `db`, `redis`, `research-sync`, `frontend`
- Add Prometheus and Grafana
- Stand up `market-feed-worker`, `bar-builder-worker`, `reference-data-worker`
- Pass Gate A before any trading validation

### Stage 1: `BANKNIFTY` structural futures swing sleeve

- Enable `mp-feature-worker`, `regime-worker`, `swing-agent-worker`, `risk-governor-worker`, `paper-execution-worker`
- Trade only deterministic MP setups
- Pass Gate B and Gate C before paper promotion

### Stage 2: add order-flow confirmation

- Enable `order-flow-worker`
- Restrict order flow to confirmation, timing, and execution-style hints
- Re-run Gate B and Gate C using the new feature version

### Stage 3: shadow weekly-options mapping

- Enable `options-mapper-worker` in shadow mode only
- Map approved futures bias into options candidates without sending live options orders
- Add option-fill drift and premium-decay divergence metrics

### Stage 4: smallest live futures canary

- Enable `live-execution-worker` for the swing sleeve only
- Manual override stays on
- Pass Gate D before live, then Gate E during canary

### Stage 5: second sleeve and allocator

- Add `positional` or `scalp`, not both at once
- Enable `allocator-worker`
- Re-baseline all gates at the portfolio and sleeve levels

## Dashboards and Alerts

Required Grafana dashboards:

- feed health and session coverage
- bar-builder lag and rebuild checksum drift
- MP feature counts and regime labels by session
- signal funnel: generated -> approved -> executed -> attributed
- slippage and fill drift by time of day
- reconciliation mismatches
- daily loss cap, exposure cap, stale-data shutdowns, kill-switch events

Required alerts:

- feed heartbeat missing
- bar-builder lag exceeds 2 bars
- stale quote / stale signal
- unresolved order-state mismatch
- position drift
- risk kill switch fired
- validation gate regression

## Immediate Build Sequence

1. Add Prometheus and Grafana to Compose without changing the current trading path.
2. Split market ingestion, bar building, MP features, and paper execution into explicit workers.
3. Add the strategy-specific Timescale hypertables and validation tables.
4. Move strategy-critical coordination from pub/sub to Redis Streams.
5. Implement Gate A automation before expanding trading logic.
6. Lock the first live sleeve to `BANKNIFTY` futures only.

## Current Module Mapping

The current isolated module already covers the inner strategy logic:

- [service.py](/Users/chinnadurairamachandran/Claude%20Projects/TradingBot/nomad-curie/backend/auction_intelligence/service.py)
- [market_profile/engine.py](/Users/chinnadurairamachandran/Claude%20Projects/TradingBot/nomad-curie/backend/auction_intelligence/market_profile/engine.py)
- [order_flow/engine.py](/Users/chinnadurairamachandran/Claude%20Projects/TradingBot/nomad-curie/backend/auction_intelligence/order_flow/engine.py)
- [regime/engine.py](/Users/chinnadurairamachandran/Claude%20Projects/TradingBot/nomad-curie/backend/auction_intelligence/regime/engine.py)
- [agents/swing.py](/Users/chinnadurairamachandran/Claude%20Projects/TradingBot/nomad-curie/backend/auction_intelligence/agents/swing.py)
- [risk/governor.py](/Users/chinnadurairamachandran/Claude%20Projects/TradingBot/nomad-curie/backend/auction_intelligence/risk/governor.py)
- [execution/planner.py](/Users/chinnadurairamachandran/Claude%20Projects/TradingBot/nomad-curie/backend/auction_intelligence/execution/planner.py)
- [paper/service.py](/Users/chinnadurairamachandran/Claude%20Projects/TradingBot/nomad-curie/backend/auction_intelligence/paper/service.py)

What is still missing from this blueprint are the worker splits, Timescale schema additions, Redis Stream contracts, monitoring stack, and automated gate enforcement.
