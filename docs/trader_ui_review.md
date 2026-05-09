# Trader UI Review

## What a trader-facing UI should optimize for

The top surface should answer these questions immediately:

1. What is the portfolio doing right now?
2. Which strategy lanes are active, profitable, blocked, or stale?
3. What risk or operational issues require action?
4. Where does the operator go next to act?

For this application, that means the UI should always surface:

- app-wide results: equity, realized P&L, open P&L, open positions
- strategy-wise attribution: one lane per strategy with its own cadence and P&L
- live market context: watchlists, spot/futures state, current expiry mapping
- execution state: orders, approvals, kill switches, broker mode
- risk and service health: brokers, DB, Redis, research sync, market-data router

## External references used

- Grafana dashboard guidance emphasizes dashboards as decision surfaces, not panel collections. The important information should be visible first, with drill-downs available beneath it.
  - [Grafana webinar: Building advanced dashboards](https://grafana.com/files/building-advanced-grafana-dashboards.pdf?pg=webinar-building-advanced-grafana-dashboards&plcmt=related-content-2)
- Interactive Brokers Risk Navigator is a strong reference for how trading software surfaces risk: total portfolio risk, risk by slice, what-if portfolios, and capital allocation in one place.
  - [Interactive Brokers Risk Navigator](https://www.interactivebrokers.com/en/trading/risk-navigator.php)

## Review of the previous app structure

Before this change, the app behaved more like a tool launcher:

- the home page was a static list of links
- the sidebar was a flat icon strip
- service health existed in fragments but not as a single operator surface
- strategy results existed in analytics and strategy pages, but the top-level app did not summarize them

That structure was workable for development, but weak for operations. The operator had to reconstruct overall state by opening several pages.

## Recommended information architecture

### 1. Overview

This page should show:

- app-wide results
- strategy lane cards
- top blockers
- condensed service health
- grouped workspace navigation

### 2. Operate

- Positions
- Execution
- Market
- NSE Strategy
- Commodity

These are live decision surfaces.

### 3. Validate

- Analytics
- Auction IQ
- Research Monitor
- Backtester
- Agent

These are validation, improvement, and review surfaces.

### 4. System

- Health
- F&O Data
- Settings

These are operational dependency surfaces.

## UI principles applied here

- results before tools
- grouped navigation by operator intent
- strategy attribution separated by lane
- health treated as first-class, not a hidden support view
- compact but readable density suitable for a trading desk
- deeper pages preserved as drill-downs rather than merged into one dashboard

## Current implementation mapping

- `/` is now the operator overview
- `/health` is the deployed service monitor
- the sidebar is grouped by outcome: Operate, Validate, System
- existing detailed pages remain available for focused work
