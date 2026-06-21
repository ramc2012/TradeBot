# Directional Long Options

This package adds an isolated long-premium engine to Nomad Curie without reusing or mutating the existing NSE strategy supervisors.

## Scope

- Directional signal generation on the underlying spot series
- Regime-aware weekly/monthly option selection through a Distributional Strike-Expiry Optimizer
- Positive-expectancy hurdle with p-vs-q tail edge, synthetic spread, slippage, theta, IV drag, skew tax, and model-error buffers
- Bounded event-driven backtest over the persisted local option dataset
- FastAPI workspace endpoints plus an optional Dash mount

## Package Layout

- `config.py`: module defaults and risk/execution thresholds
- `data.py`: cached spot/contract dataset access over `backend/runtime/index_analytics_data`
- `features.py`: EMA/ADX/ATR/volatility and breakout features
- `ai_model.py`: hybrid AI decision layer; deterministic rules score spot trend, breakout, volatility, option quality, chain confirmation, and execution timing before the RL bandit ranks candidates
- `regime.py`: trade/no-trade and expiry/delta preferences
- `signals.py`: p-distribution forecast inputs including direction, move size, timing precision, jump/tail probability, IV expectation, and model uncertainty
- `selector.py`: Distributional Strike-Expiry Optimizer. It compares the physical p-distribution against market-implied q tails, builds per-contract edge/utility scores, and rejects cheap options whose theta, skew, liquidity, or timing profile destroys edge
- `risk.py`: sizing and approval gates
- `backtest.py`: single-position, conservative long-option simulator
- `analytics.py`: expectancy, drawdown, rolling stability, and engine score
- `dashboard.py`: optional Dash mount at `/directional-options/dashboard/`
- `service.py`: orchestration for the API/router layer

## Agent Learning System

The directional agent is designed as a closed learning loop, not a fixed CE/PE ruleset:

1. **Observe:** spot features, regime, contract candidates, live option-chain analytics, and execution health are snapshotted at decision time.
2. **Explain relationships:** `chain_analytics.py` converts raw chain rows into relation features: PCR, IV skew/risk reversal, DEX/GEX, dealer gamma flip, call/put walls, straddle-implied range, sigma bands, NTM VolX/VXR, writer-cash proxy, spectrum wall pressure, gamma-density skew, TRACE-style second-order exposures, and unusual activity.
3. **Score safely:** `ai_model.py` blocks broken liquidity/spread/expiry cases and produces dense component scores. It does not decide profit; it only prevents malformed trades from reaching the bandit.
4. **Act and explore:** `policy.py` uses a Bayesian contextual bandit. The v4 feature vector appends the option-relation features so the agent can learn when a relation helps directional long-premium trades and when it is noise.
5. **Attribute reward:** `paper.py` records the entry-time feature vector with the paper position. On close, realized P&L is converted to an R-multiple and credited back to the same feature vector, preserving causal context.
6. **Constrain risk:** `risk.py` caps quantity, loss budget, and premium exposure after the policy chooses direction, strike, and size. The agent can maximize expected R, but cannot bypass capital safety.

The goal is directional long-option profit under uncertainty: learn which combinations of spot trend, option pricing, dealer positioning, writer behavior, liquidity, volatility, and time-to-expiry actually pay after costs. The policy should be judged by walk-forward R, drawdown, hit rate by setup, and feature attribution stability, not by any single chain metric.

## API

- `GET /api/directional-options/summary`
- `GET /api/directional-options/universe`
- `GET /api/directional-options/workspace`
- `GET /api/directional-options/backtest`
- `GET /api/directional-options/live-snapshot`
- `GET /api/directional-options/chain-analytics`
- `POST /api/directional-options/paper-proposal`
- `GET /api/directional-options/paper-journal`
- `GET /api/directional-options/paper-positions`

## Notes

- Research/backtest endpoints run on persisted runtime data. Live paper-trading endpoints consume shared market-intelligence spot history plus locally persisted ATM watchlist snapshots, and they do not call brokers directly from the strategy path.
- The Dash surface is feature-gated. If `dash` is not installed, the API still works and the frontend shows the mount status cleanly.
- The engine does not buy CE/PE directly from directional bias. A signal must clear contract discovery, the hybrid rule model, the RL policy, and risk approval. The rule layer blocks broken or untradable candidates; the bandit learns strike choice, trade/skip, and sizing from realized paper-trade R-multiples.
- Ordinary directional views favor 0.45-0.65 delta. OTM options are treated as conditional jump/tail instruments and are penalized unless jump score and timing precision are high enough.
