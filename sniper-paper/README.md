# sniper-paper

Live paper-trading deployment of the MP + Order Flow expectancy engine. Trades NIFTY, SENSEX, and CRUDE futures **on paper only** — no live broker order calls. Deployed on the same EC2 instance as nomad-curie (15.206.56.206).

## Quickstart

```bash
# Local dev (Mac Mini)
cd nomad-curie/sniper-paper
pip install -e ".[dev]"
pytest -q                                  # leakage + cost-model + settings tests must pass
sniper-paper introspect-db                 # verify NIFTY data in nomad-curie DB

# Train and promote a model
sniper-paper extract-nifty --start 2023-01-01 --end 2026-05-01 --out data/nifty_candles.parquet
sniper-paper train --candles data/nifty_candles.parquet
sniper-paper list-models
sniper-paper promote nifty_candle_v0_<timestamp>

# Run the live paper trader (long-running)
sniper-paper run                           # uses configs/paper.yaml + configs/secrets.yaml
sniper-paper api                           # dashboard on port 8001
```

For cloud deployment to `15.206.56.206` see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
For day-to-day operations see [docs/OPERATIONS.md](docs/OPERATIONS.md).

## What this does

- Subscribes to Fyers WebSocket for NIFTY, SENSEX, CRUDE near-month futures.
- Every 30 seconds during the relevant exchange's trading hours, runs six MP setup detectors → scores each candidate with LightGBM (p_win + expected_net_R) → applies an EV gate → emits a paper order if gate + risk governor approve.
- Tracks open paper positions tick-by-tick for stop/target/timeout exits, applying a slippage model to entries and exits.
- Writes every signal (taken or skipped, with reason) to TimescaleDB for audit and future retraining.
- Exposes a live dashboard at `http://15.206.56.206:8001/`.

## What this is NOT

- **Not** the Phase 0 retroactive-validation harness (that's `../sniper-phase0/`).
- **Not** a live trading system. No live order placement. Ever.
- **Not** options. Futures only.
- **Not** multi-model. v0 uses a single NIFTY-trained model for all three instruments — SENSEX and CRUDE signals are flagged as OOD in the database and UI.

## Honest known limitations of v0

- Model trained on **candle-derived** features only. Order-flow features (delta, absorption, book imbalance) are stubbed because the historical tick data doesn't exist. They start filling in once live tick capture has accumulated history; planned v0.1 retrain after ~3 months of capture.
- Cross-instrument generalisation is unproven. NIFTY model → SENSEX/CRUDE is a guess. Treat SENSEX/CRUDE paper P&L as exploratory.
- Pseudo-tick reconstruction from 30-min candles (4 samples per candle: open, high, low, close evenly spaced) is a coarse approximation of intraday MP. Real intraday MP will look slightly different once live ticks back-fill.
- No automated contract rollover. Update `near_month_symbol` in `configs/paper.yaml` monthly.

See [CLAUDE.md](CLAUDE.md) for the full design rationale and hard rules.
