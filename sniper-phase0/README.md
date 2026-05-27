# Sniper Phase 0

Retroactive validation gate for the MP + Order Flow expectancy engine planned for Nomad Curie. See [CLAUDE.md](CLAUDE.md) for the full context.

## One question

> Does an MP + order-flow feature set, scored by LightGBM, reliably flag the worst losers in FY25-FY26 Zerodha trades as "skip"?

Pass → Phase 1 (data infrastructure) is justified. Fail → refine features/labels before any infrastructure investment.

## Setup

```bash
cd sniper-phase0
uv sync                          # installs deps from pyproject.toml
uv run pytest -q                 # leakage test must pass before anything else
```

## Run

```bash
# 1. Drop your Zerodha tradebook CSV at data/raw/zerodha_tradebook_fy25_fy26.csv
# 2. Tick/book data goes under data/raw/underlying_ticks/{NIFTY,BANKNIFTY}/date=YYYY-MM-DD/

# Run A — score the trade log
uv run phase0 features
uv run phase0 label
uv run phase0 train
uv run phase0 eval

# Run B — score MP setup-family candidates instead (parallel sanity check)
uv run phase0 candidates --out data/processed/setup_candidates.parquet
# Then re-point paths.trade_log at setup_candidates.parquet and re-run features → eval.
```

The final report lands at `data/processed/reports/phase0_report.json` with a single `phase0_pass: true|false` flag against the criteria in [docs/DECISION_GATE.md](docs/DECISION_GATE.md).

## Interactive

```bash
uv run jupyter lab notebooks/01_phase0_validation.ipynb
```
