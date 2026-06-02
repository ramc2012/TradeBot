# Nomad Curie Sniper — Phase 0

Retroactive validation of the MP + Order Flow expectancy feature set on the FY25-FY26 Zerodha
trade log. **This is a research repo, not production.** See `CLAUDE.md` for the full charter.

## Quickstart

```bash
# 1. Create env and install
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 2. Drop raw data into data/raw/
#    - zerodha_trades_fy25_fy26.csv  (export from console.zerodha.com)
#    - upstox_nifty_fut_<expiry>.parquet  (one file per expired contract)
#    - upstox_banknifty_fut_<expiry>.parquet

# 3. Run the pipeline
sniper validate-trades        # sanity-check the trade log
sniper build-features         # MP + OF + Context features for every trade entry
sniper label-trades           # apply cost-adjusted triple-barrier labeler
sniper train-baseline         # walk-forward LightGBM
sniper evaluate-phase0        # produces artifacts/phase0_report.html with go/no-go verdict

# 4. Or just open the notebook
jupyter lab notebooks/00_phase0_validation.ipynb
```

## Phase 0 verdict

The pipeline writes a single JSON file at `artifacts/phase0_verdict.json` with:

```json
{
  "verdict": "go" | "no-go",
  "skip_accuracy_bottom_decile": 0.0,
  "net_pnl_improvement_pct": 0.0,
  "retained_sharpe_ratio": 0.0,
  "leakage_tests_passed": true,
  "reasons": [...]
}
```

All four criteria in CLAUDE.md must pass for `"go"`.

## What's NOT in this repo

Live trading, broker APIs, options selection, deep learning. See `CLAUDE.md` § "What you do
NOT do in this repo".

## Repo layout

See `CLAUDE.md` § "What lives where".
