# Auction Intelligence Module

This package introduces a separate Market Profile + order-flow strategy module without changing the current strategy runtime. Nothing in this package is started from application lifespan hooks. The only integration point is an opt-in API router.

Deployment and rollout planning for this module is documented in [DEPLOYMENT_BLUEPRINT.md](/Users/chinnadurairamachandran/Claude%20Projects/TradingBot/nomad-curie/backend/auction_intelligence/DEPLOYMENT_BLUEPRINT.md).

## MVP Scope

- Primary live test underlyings: `NIFTY` and `SENSEX`
- Secondary future scope: `BANKNIFTY`
- Live paper proposals now map sleeve signals into buy-only CE/PE contracts with expiry, strike, premium, and lot-size selection
- Rollout target: deterministic rules, paper proposals, journaling, then supervised/RL extensions

## Package Layout

```text
auction_intelligence/
├── config/              Default JSON config for scope, thresholds, and risk
├── market_profile/      TPO/value-area/IB/comparative profile engine
├── order_flow/          Spread/imbalance/delta/microstructure feature engine
├── regime/              Deterministic regime classifier
├── agents/              Positional, swing, and scalp sleeves
├── meta_controller/     Conflict resolution and sleeve coordination
├── risk/                Shared pre-trade governor
├── execution/           Execution-style planner
├── paper/               JSONL journaling for paper proposals
├── shadow/              Persisted shadow-mode observations and divergence tracking
├── validation/          Gate A/B/C validators and validation persistence
└── service.py           End-to-end orchestration entry point
```

## Canonical Schemas

- `MarketBar`: shared bar schema for research, paper, and live
- `TradePrint`: canonical aggressive trade print representation
- `QuoteSnapshot`: top-of-book state
- `DepthSnapshot`: multi-level depth snapshot
- `SessionContext`: runtime session health and timing
- `PortfolioSnapshot`: sleeve-safe risk inputs
- `MarketProfileSnapshot`: explicit numeric auction-state output
- `OrderFlowSnapshot`: timing/execution microstructure output
- `RegimeAssessment`: explainable master-context label
- `AgentDecision`: sleeve-local action proposal
- `ExecutionInstruction`: broker-agnostic execution hint enriched with option contract details for the live paper path

## Deterministic Rules

### Market Profile

- 30-minute TPO periods
- POC and value area via TPO-count expansion from POC
- Initial balance from first two TPO periods
- Range extension from IB high/low
- Single prints and buying/selling tails via one-TPO price runs
- Poor highs/lows via repeated top/bottom TPOs
- Spike detection when the last TPO is completely outside value
- Comparative metrics: value overlap, POC shift, value migration, prior POC untouched, bracket state

### Order Flow

- Spread and micro price
- Top-of-book and multi-level imbalance
- Aggressive buy/sell volume
- Delta and cumulative delta
- Signed trade imbalance
- Best-book order flow imbalance from quote updates
- VWAP drift
- Queue pressure
- Book pressure and micro-price offset
- Trade intensity and quote repricing rate
- Volatility burst
- Passive/aggressive fill proxies
- Toxicity and adverse-selection proxies

### Regime Labels

- `breakout_acceptance`
- `breakout_rejection`
- `failed_auction`
- `trend_continuation`
- `trend_day`
- `balance`
- `developing_balance`
- `reversal`
- `no_trade`

## Agent Responsibilities

- `PositionalAgent`: higher-timeframe bias scaffold only in MVP
- `SwingAgent`: first deployable sleeve; uses daily/session auction state and order-flow confirmation
- `ScalpAgent`: conservative placeholder until replay quality is validated

## Risk Rules

- daily loss cap
- per-agent drawdown cap
- per-symbol exposure cap
- correlated exposure cap
- max concurrent positions
- stale data stop
- broker disconnect stop
- no new positions near session close
- model confidence floor

## Execution Workflow

1. Build current and prior market profiles
2. Compute order-flow timing features
3. Classify regime
4. Run agent sleeves independently
5. Coordinate sleeves through the meta-controller
6. Apply shared risk governor
7. Map approved sleeve direction into a buy-only CE/PE option contract with expiry and strike selection
8. Build execution instructions
9. Optionally write paper-trade journal entries

## Training Workflow

1. Deterministic expert rules in this package
2. Supervised labels from regime/acceptance/timing outcomes
3. Imitation learning from strong deterministic regions
4. RL only for timing, sizing, and execution after paper validation
5. RL state is built from Market Profile structure plus order-flow context
6. RL reward must stay slippage-aware and adverse-selection-aware, not gross-PnL-only
7. Automatic RL retraining runs only after market close on shadow data and promotes only if holdout checks improve

## Backtest / Paper Workflow

- Bar-based backtests should reuse `MarketBar` and `MarketProfileEngine`
- Event-driven replay should reuse `TradePrint`, `QuoteSnapshot`, and `OrderFlowEngine`
- Paper proposals should flow through `AuctionIntelligenceService.analyze_and_record_paper`
- Shadow observations should be captured through the `/api/auction-intelligence/shadow-record-live` endpoint
- Gate C should be monitored through `/api/auction-intelligence/validate-gate-c`
- JSONL journals land under `backend/runtime/auction_intelligence/`

## Test Strategy

- unit tests for market-profile calculations
- unit tests for order-flow features
- unit tests for regime classification
- unit tests for swing-agent gating
- unit tests for risk kill conditions
- API smoke tests can be added later without touching the current runtime
