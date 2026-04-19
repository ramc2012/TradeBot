# Directional Long Options

This package adds an isolated long-premium engine to Nomad Curie without reusing or mutating the existing NSE strategy supervisors.

## Scope

- Directional signal generation on the underlying spot series
- Regime-aware weekly/monthly option selection
- Positive-expectancy hurdle with synthetic spread, slippage, theta, and IV drag
- Bounded event-driven backtest over the persisted local option dataset
- FastAPI workspace endpoints plus an optional Dash mount

## Package Layout

- `config.py`: module defaults and risk/execution thresholds
- `data.py`: cached spot/contract dataset access over `backend/runtime/index_analytics_data`
- `features.py`: EMA/ADX/ATR/volatility and breakout features
- `regime.py`: trade/no-trade and expiry/delta preferences
- `signals.py`: directional expected-move forecasts
- `selector.py`: contract scoring with Black-Scholes Greeks and liquidity filters
- `risk.py`: sizing and approval gates
- `backtest.py`: single-position, conservative long-option simulator
- `analytics.py`: expectancy, drawdown, rolling stability, and engine score
- `dashboard.py`: optional Dash mount at `/directional-options/dashboard/`
- `service.py`: orchestration for the API/router layer

## API

- `GET /api/directional-options/summary`
- `GET /api/directional-options/workspace`
- `GET /api/directional-options/backtest`

## Notes

- The module runs only on persisted research/runtime data; it does not interfere with live strategy agents.
- The Dash surface is feature-gated. If `dash` is not installed, the API still works and the frontend shows the mount status cleanly.
