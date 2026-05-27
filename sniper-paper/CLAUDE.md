# Sniper Paper — Live Paper Trading System

A live paper-trading deployment of the MP + Order Flow expectancy engine. Deployed alongside the nomad-curie auction-intelligence platform on EC2 `15.206.56.206`.

**This project does not place real orders. Ever.** Paper executor only.

## Scope (v0)

- **Instruments:** NIFTY futures (NSE), SENSEX futures (BSE), CRUDE futures (MCX).
- **Live data:** Fyers WebSocket.
- **Model:** LightGBM trained offline on NIFTY historical candles from the existing nomad-curie TimescaleDB. The same model serves predictions for all three instruments in v0 — cross-instrument generalisation is an explicit risk surfaced in the UI.
- **Decision cadence:** 30 seconds.
- **Risk governor:** Daily loss cap, max 1 open position per instrument, max 3 positions across instruments, kill switch after 3 consecutive losses.
- **Storage:** Reuses nomad-curie's TimescaleDB instance with isolated `paper_*` tables.

## What this is NOT

- **Not** the retroactive validation gate (that's [sniper-phase0](../sniper-phase0/CLAUDE.md), still pending data).
- **Not** a live-trading system. No live broker order calls. Paper executor only.
- **Not** options. NIFTY/SENSEX/CRUDE futures only.
- **Not** a multi-model system in v0. One LightGBM, shared across all three instruments. Per-instrument models are v0.1 work once we have enough captured tick data for SENSEX and CRUDE.

## Honest risks surfaced in the UI

1. **Cross-instrument generalisation.** The model is trained on NIFTY candles. SENSEX has similar microstructure but ~2 years of history. CRUDE is MCX commodity futures — fundamentally different participant mix and trading hours. The UI must flag SENSEX/CRUDE predictions as "out-of-distribution" until they have their own model.
2. **Candle-based v0 model can't use OF features.** The v0 model uses MP + context only. Order-flow features (`of_inferred_delta_*`, `book_apparent_imbalance_*`) require tick capture, which only starts when this system goes live. v1 model with OF features is retrained after ~3 months of capture.
3. **Paper ≠ live.** Simulated fills use next-tick LTP + slippage from the cost model. Real fills will differ. This system's job is to flag *whether the model has any edge at all* — not to predict live P&L to the rupee.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                         EC2 15.206.56.206                            │
│                                                                      │
│  ┌────────────────┐   ┌─────────────┐   ┌──────────────────────┐    │
│  │ Fyers WS       │──▶│ Redis pub/  │──▶│ Feature Service       │    │
│  │ (3 symbols)    │   │ sub channel │   │ (30s cadence)         │    │
│  └────────────────┘   └─────────────┘   └──────────┬───────────┘    │
│                                                     │                │
│                            ┌────────────────────────▼────────┐       │
│                            │ Signal Engine                    │       │
│                            │ • Setup detectors                │       │
│                            │ • LightGBM predictor             │       │
│                            │ • EV gate                        │       │
│                            └─────────────┬───────────────────┘       │
│                                          │                            │
│                            ┌─────────────▼───────────────────┐       │
│                            │ Risk Governor                    │       │
│                            │ • daily loss cap                 │       │
│                            │ • position limits                │       │
│                            │ • kill switch                    │       │
│                            └─────────────┬───────────────────┘       │
│                                          │                            │
│                            ┌─────────────▼───────────────────┐       │
│                            │ Paper Executor                   │       │
│                            │ • simulate fill at next tick     │       │
│                            │ • track MAE/MFE, exits           │       │
│                            └─────────────┬───────────────────┘       │
│                                          │                            │
│                            ┌─────────────▼───────────────────┐       │
│                            │ TimescaleDB (existing instance)  │       │
│                            │ paper_ticks (hypertable)         │       │
│                            │ paper_signals, paper_orders,     │       │
│                            │ paper_positions, paper_pnl       │       │
│                            └─────────────┬───────────────────┘       │
│                                          │                            │
│                            ┌─────────────▼───────────────────┐       │
│                            │ FastAPI + minimal frontend       │       │
│                            │ port 8001 (8000 = nomad-curie)   │       │
│                            └─────────────────────────────────┘       │
└──────────────────────────────────────────────────────────────────────┘
```

## Hard rules

1. **Paper executor never imports the Fyers order module.** If a `place_order` call ever appears in this codebase, it is a bug. Lint rule + test enforces this — see [tests/test_no_live_orders.py](tests/test_no_live_orders.py).
2. **Risk governor runs in-process before every paper order**, not as a service call. Failure modes: open it fails the order, never approves it.
3. **All decisions are auditable.** Every signal writes a row to `paper_signals` with the full feature vector, model output, gate decision, and rejection reason if skipped.
4. **Daily session-reset is mandatory.** SEBI April 2026 requires daily Fyers re-auth; the runner handles this at 08:50 IST automatically.
5. **Model artifact is immutable.** Once trained and promoted, the `.txt` file in `artifacts/` is read-only. Retraining produces a new artifact with a new SHA in its filename — never overwrites.
6. **Cross-instrument risk is surfaced, not hidden.** Every signal carries an `instrument_distribution_match` flag set to True only for NIFTY in v0. SENSEX/CRUDE signals show as "OOD" in the UI and in the database column. Don't hide this.

## Directory layout

```
sniper-paper/
├── CLAUDE.md              # this file
├── README.md              # quickstart + deployment runbook
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml     # extends nomad-curie's compose
├── configs/
│   ├── paper.yaml         # instruments, lot sizes, hours, risk limits
│   └── secrets.yaml.example
├── sql/
│   └── schema.sql         # paper_* tables, hypertables
├── artifacts/             # trained model artifacts (read-only at runtime)
├── frontend/              # minimal single-page dashboard
├── src/sniper_paper/
│   ├── common/            # settings, time, ist helpers
│   ├── ingest/            # fyers_ws, tick_writer, tick_buffer
│   ├── features/          # live MP + context (OF features stubbed until tick capture matures)
│   ├── signals/           # setup detectors, signal_engine, ev_gate
│   ├── model/             # loader, predictor
│   ├── execution/         # paper_executor, risk_governor, positions
│   ├── persistence/       # db pool, repositories
│   ├── training/          # extract_from_db, train, evaluate, promote
│   ├── api/               # FastAPI app + routes
│   ├── runner.py          # async orchestrator (the long-running process)
│   └── cli.py             # typer
├── docs/
│   ├── DEPLOYMENT.md      # exact steps to deploy to 15.206.56.206
│   └── OPERATIONS.md      # runbook: session reset, kill switch, model promotion
└── tests/
    ├── test_no_live_orders.py
    ├── test_risk_governor.py
    ├── test_paper_executor.py
    └── test_signal_engine.py
```

## Working in this repo

- This is a live-data project. Local development uses recorded tick replays where possible — see `cli.py replay`.
- Production lives on the EC2 box. Local edits → `docker compose build` → push → `docker compose up -d` on the host.
- The Fyers daily session reset at 08:50 IST is **non-negotiable** post-April 2026. Do not add code that tries to keep a session open across the cutoff.
- Do not import anything from `sniper-phase0`. Useful detectors and cost-model code are copied in deliberately; the two projects evolve independently.
