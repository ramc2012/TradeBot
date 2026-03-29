# Nomad Curie Project Status

Last updated: 2026-03-28

This file reconciles the March 2026 implementation plan with the current codebase and live system state. The plan is directionally correct, but several items listed there as "missing" are now partially or fully implemented. The focus below is only on what is still left.

## Summary

Nomad Curie is no longer at the "UI only" stage. The platform now has:

- a working FastAPI + TimescaleDB + Redis stack
- broker connectivity for Fyers, Upstox, ICICI Breeze, and 5paisa
- paper/live trading mode plumbing
- market, analytics, agent, backtester, and MACD analysis pages
- a Timescale-backed NSE F&O research-cache pipeline with recurring Upstox sync
- an expired-options MACD backtest flow with UI monitoring and cached-data visibility

The highest-value remaining work is no longer basic plumbing. It is:

1. validating the strategy properly
2. finishing the real-time scanner and paper-trading loop
3. upgrading frontend architecture for a true low-latency terminal experience
4. closing testing, monitoring, and compliance gaps

## Plan Reconciliation

### Already implemented or substantially progressed

#### Foundation and platform

- Docker Compose stack is in place.
- TimescaleDB is in use for time-series storage.
- Redis is wired in.
- FastAPI backend is running with REST + WebSocket endpoints.
- Zustand and TanStack Query are already present in the frontend.
- Major UI surfaces exist: trading, analytics, market, agent, analysis, backtester, settings, data.

#### Options MACD data/backtest work

- `backend/analysis/backtest.py` contains the Upstox expired-options MACD engine.
- `backend/backtester/options_macd_backtester.py` exists for dataset-driven backtests.
- Timescale schema now includes:
  - `option_premium_candles`
  - `underlying_spot_candles`
  - `fo_underlying_catalog`
  - `fo_expiry_catalog`
  - `fo_contract_catalog`
  - `fo_option_chain_metrics`
- recurring Upstox sync is implemented in `backend/data/upstox_research_sync.py`
- recurring 30-minute sync service is running via `research-sync` in `docker-compose.yml`
- the analysis page now shows live cache population progress and populated/populating instruments

#### Agent / risk / paper trading

- agent chat is present, so the March plan's "AI chat missing" note is stale
- risk configuration/status endpoints exist
- paper trading engine exists
- live order manager and risk manager exist

### Still incomplete

#### Strategy validation

- no US-data validation path has been implemented
- no OptionsDX/DoltHub ingestion exists
- no strategy validation report exists against the plan's success criteria
- walk-forward validation exists in the generic backtester, but the Upstox expired-options research flow has not been carried through a complete validation program yet

#### Live NSE scanner

- there is no dedicated live "options MACD scanner" API endpoint yet
- there is no continuous intraday ATM +/- strikes scanner integrated with the agent UI
- paper-trading comparison against backtest expectations has not been built out

#### Frontend architecture upgrades

- charts are not yet moved to TradingView Lightweight Charts
- there is no FlexLayout/react-mosaic style multi-panel workspace
- navigation is still page-based rather than a single SPA-style terminal shell
- WebSocket usage is still endpoint-specific, not a unified in-memory streaming model across the app

#### Monitoring / operations

- Prometheus is not configured
- Grafana dashboards are not configured
- no CI/CD workflow is present
- no task queue such as Celery/Dramatiq is wired in

#### Compliance / auditability

- SEBI-focused audit trail and algo registration workflow are still pending
- there is no explicit order/event audit model covering end-to-end decision traceability

#### Testing

- there was effectively no test suite in the repo before this pass
- coverage is still far below the plan target

## Recommended Remaining Sequence

### 1. Finish research and validation

This is the most important unfinished block because it determines whether the strategy is worth deploying at all.

- complete NSE local cache population
- add validation queries/reports for:
  - IV regime
  - OI change
  - PCR / volume PCR
  - CE vs PE edge
  - exit-rule comparisons
- run walk-forward validation on the locally cached NSE data
- if still needed, add a US-data ingestion path for external validation

### 2. Build the live scanner

Once validation is trustworthy:

- add scanner endpoint(s) in FastAPI
- compute live ATM contracts from spot + active expiry chain
- stream scanner signals to the agent/trading UI
- connect scanner outputs to paper-trading execution rules

### 3. Upgrade the frontend terminal architecture

Only after the data and strategy path is stable:

- replace charting with TradingView Lightweight Charts
- consolidate live market state into a single store/socket model
- introduce a terminal-style multi-panel layout
- reduce route-driven refetch behavior

### 4. Close reliability and governance gaps

- add tests around critical APIs and engines
- add Prometheus/Grafana
- add background task queue for scheduled scans/alerts
- add compliance-grade audit logging

## Concrete "Left To Do" Checklist

### Highest priority

- [ ] complete local NSE research cache backfill
- [ ] add SQL/report layer for IV, OI change, PCR, volume PCR, and exit-rule analysis
- [ ] run walk-forward validation on cached NSE data
- [ ] define go/no-go report against plan success criteria

### Next priority

- [ ] implement live options MACD scanner API
- [ ] connect scanner results to UI
- [ ] connect scanner to paper-trading workflow

### Platform upgrades

- [ ] migrate market/analysis charts to TradingView Lightweight Charts
- [ ] build multi-panel terminal workspace
- [ ] unify browser-side real-time data flow

### Operations and safety

- [ ] add Prometheus/Grafana
- [ ] add CI workflow
- [ ] add background task queue
- [ ] add audit trail/compliance tables and flows
- [ ] expand tests on core backend modules

## Notes

- The running research-sync process should be treated as long-lived infrastructure and not interrupted for ordinary feature work.
- The plan document understates the amount already built in the codebase. Future planning should use this file as the current baseline instead of the March 2026 plan text.
