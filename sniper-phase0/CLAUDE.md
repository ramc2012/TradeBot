# Sniper Phase 0 — Retroactive Validation

This project is the **go/no-go gate** for the larger MP + Order Flow expectancy engine planned for Nomad Curie. Its only job is to answer one question on real FY25-FY26 Zerodha trades:

> Does an MP + order-flow feature set, scored by a simple LightGBM model, reliably flag the worst losers as "skip"?

If yes (skip-accuracy on the bottom decile of losers ≥ ~65%), Phase 1 (data infrastructure) is justified. If no, the feature set or label scheme needs rework before any infrastructure investment.

**This project is intentionally small and disposable.** Do not over-engineer it. Do not add abstractions for future phases. Do not import from the main `nomad-curie/backend`. When Phase 1 starts, useful parts get lifted into the platform; the rest is deleted.

## Hard rules

1. **No look-ahead.** Every feature carries an explicit `data_available_at` timestamp. The harness asserts `data_available_at <= decision_time` for every feature on every row. A leakage test exists and must stay green — see [tests/test_no_leakage.py](tests/test_no_leakage.py).
2. **Costs go in labels, not in evaluation.** The triple-barrier labeler returns `net_R` *after* brokerage, STT, exchange fees, GST, stamp duty, and a slippage model calibrated to actual Zerodha fills. Models train on net outcomes from day one. See [src/sniper_phase0/labels/cost_model.py](src/sniper_phase0/labels/cost_model.py).
3. **Walk-forward only, with purging.** No random shuffles, no k-fold. 6-month train / 1-month validate / 1-month test, rolled monthly. Training rows whose label `exit_ts + purge_minutes` extends into the validation window are dropped — see [src/sniper_phase0/evaluation/walk_forward.py](src/sniper_phase0/evaluation/walk_forward.py).
4. **EV-ranked skip is the primary metric, not p_win.** Bottom-decile skip-accuracy is ranked by `expected_net_R`. A high-confidence trade with low payoff can be net-negative; a low-confidence trade with large payoff can be net-positive. See [src/sniper_phase0/evaluation/skip_accuracy.py](src/sniper_phase0/evaluation/skip_accuracy.py). `skip_accuracy_by_pwin` is reported as a secondary diagnostic only.
5. **Honest feature naming.** NSE retail does not give true MBO data. Order-flow features inferred from 5-level book + tick prints are named `inferred_*` or `apparent_*` — never `true_delta`, `actual_absorption`. This is a research-integrity rule.
6. **Reproducibility.** Every artifact (features, labels, model, eval result) is written with a provenance header: git SHA, config hash, input data hashes, timestamp. See [src/sniper_phase0/utils/provenance.py](src/sniper_phase0/utils/provenance.py).
7. **No live trading code in this project.** Phase 0 is read-only on historical data. Anything that talks to Fyers/Zerodha live APIs does not belong here.

## Two decision_ts sources

Phase 0 supports two parallel sources for the set of moments to evaluate:

1. **Trade-log entries** — `paths.trade_log` points at the Zerodha CSV. Each row's `entry_ts` is a decision_ts. This answers "would the model have skipped your actual losers?"
2. **Setup-family candidates** — `phase0 candidates` runs the six MP-rule detectors (VA rejection, VA acceptance, IB breakout, LVN rejection, POC magnet, failed auction) at 30s sampling cadence across the trade window, producing a trade-log-compatible parquet. Pointing `paths.trade_log` at this parquet then runs the same pipeline.

Source #1 is more honest about your past performance; source #2 is more honest about whether the *signal itself* has edge independent of your discretionary entry choices. If Phase 0 fails on #1 but passes on #2, the original entries were the problem, not the model.

## What's in scope

- Load FY25-FY26 Zerodha trade log → align entries to historical NIFTY/BANKNIFTY tick + book data → reconstruct MP and order-flow features at each entry timestamp → label with triple-barrier on net P&L → fit LightGBM → evaluate skip-accuracy on losers.
- One Jupyter notebook ([notebooks/01_phase0_validation.ipynb](notebooks/01_phase0_validation.ipynb)) that produces the go/no-go report.

## What's out of scope (do not build)

- Live data ingestion, live signal generation, live order routing.
- Sequence models (Temporal CNN, LSTM, Transformer). LightGBM only.
- Options pricing, Greeks recomputation, IV surfaces. NIFTY/BANKNIFTY underlying only.
- Web UI, dashboards, FastAPI endpoints. CLI + notebook only.
- Database schemas, migrations. Files on disk are fine for Phase 0.

## Data sources (to be wired)

- **Trade log:** Zerodha Console tradebook export (CSV). Path configured in [configs/base.yaml](configs/base.yaml). Loader stub exists; user will provide CSV later.
- **Underlying ticks:** Upstox expired-instruments API for NIFTY/BANKNIFTY futures over the FY25-FY26 trade window.
- **Book snapshots:** Forward-captured only — the document explicitly notes book reconstruction beyond live capture is infeasible. Phase 0 uses tick-derived approximations where book data is missing.

## Project structure

```
sniper-phase0/
├── CLAUDE.md                          # this file
├── pyproject.toml                     # uv-managed, Python 3.12
├── configs/
│   ├── base.yaml                      # paths, cost params, walk-forward windows
│   └── features.yaml                  # feature family toggles
├── src/sniper_phase0/
│   ├── data/                          # loaders: trade log, ticks, book, MP state (intraday + completed-session)
│   ├── features/                      # base.py (leakage guard) + mp.py (intraday + prior-session), of.py, context.py
│   ├── labels/                        # cost_model.py + triple_barrier.py
│   ├── models/                        # lightgbm_baseline.py
│   ├── setups/                        # base.py + detectors.py (6 families) + generate.py (driver)
│   ├── evaluation/                    # walk_forward.py (purged), skip_accuracy.py (EV-ranked), regime.py, reports.py
│   ├── utils/                         # settings.py, time.py, provenance.py
│   └── cli.py                         # `phase0 features|label|train|eval|candidates`
├── tests/
│   ├── test_no_leakage.py             # MUST stay green
│   ├── test_cost_model.py
│   └── test_triple_barrier.py
├── notebooks/
│   └── 01_phase0_validation.ipynb     # produces the go/no-go report
└── docs/
    ├── DECISION_GATE.md               # the explicit pass/fail criteria
    └── FEATURES.md                    # feature dictionary with data_available_at semantics
```

## Decision gate

See [docs/DECISION_GATE.md](docs/DECISION_GATE.md) for the exact pass/fail criteria. Do not change these mid-build; if the model misses the gate, the right response is to refine features, not to relax the gate.

## How Claude Code should work in this repo

- Edit existing files; do not create new modules unless the structure above is missing a file.
- Run `uv run pytest -q` after any change to features, labels, or models. The leakage test is non-negotiable.
- When in doubt about whether a feature would leak, write the test first.
- Do not import from `nomad-curie/backend` — Phase 0 is standalone.
- Cost model parameters live in [configs/base.yaml](configs/base.yaml). If you need to change them, change the config and add a note in the commit, not the source.
