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
- `regime.py`: trade/no-trade and expiry/delta preferences
- `signals.py`: p-distribution forecast inputs including direction, move size, timing precision, jump/tail probability, IV expectation, and model uncertainty
- `selector.py`: Distributional Strike-Expiry Optimizer. It compares the physical p-distribution against market-implied q tails, builds per-contract edge/utility scores, and rejects cheap options whose theta, skew, liquidity, or timing profile destroys edge
- `risk.py`: sizing and approval gates
- `backtest.py`: single-position, conservative long-option simulator
- `analytics.py`: expectancy, drawdown, rolling stability, and engine score
- `dashboard.py`: optional Dash mount at `/directional-options/dashboard/`
- `service.py`: orchestration for the API/router layer

## API

- `GET /api/directional-options/summary`
- `GET /api/directional-options/workspace`
- `GET /api/directional-options/backtest`
- `GET /api/directional-options/live-snapshot`
- `POST /api/directional-options/paper-proposal`
- `GET /api/directional-options/paper-journal`
- `GET /api/directional-options/paper-positions`

## Notes

- Research/backtest endpoints run on persisted runtime data. Live paper-trading endpoints consume shared market-intelligence spot history plus locally persisted ATM watchlist snapshots, and they do not call brokers directly from the strategy path.
- The Dash surface is feature-gated. If `dash` is not installed, the API still works and the frontend shows the mount status cleanly.
- The engine does not buy CE/PE directly from directional bias. A signal must clear the distributional optimizer: positive mark-to-market trading edge, p-tail greater than q-tail by enough margin, acceptable skew tax, enough timing fit, and risk approval.
- Ordinary directional views favor 0.45-0.65 delta. OTM options are treated as conditional jump/tail instruments and are penalized unless jump score and timing precision are high enough.
