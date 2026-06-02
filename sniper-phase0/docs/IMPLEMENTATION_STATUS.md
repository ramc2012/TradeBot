# Implementation Status

Maps the uploaded blueprint (`mp_of_neural_alpha_machine_spec.md`) to what is built in this repo
versus what remains. Updated 2026-05-30.

## North-star vs implemented scope

The uploaded spec describes the full alpha machine. This repo now implements the
**feature engine + directional Phase-0 validation harness**: decision-grid features, option-aware
labels, multi-head LightGBM validation, purged walk-forward evaluation, and cross-instrument
transfer checks. `docs/feature_contract.md` is the authoritative implemented contract.

## Spec stage → repo state

| Spec stage (§32 / §37) | State | Where |
|---|---|---|
| 1. Data ingestion / storage | partial | `data/bars.py` (underlying), `data/trades.py`, `data/option_bars.py` (ATM CE/PE loader); production DB pending |
| 2. Market Profile engine | **done** | `profiles/profile.py` — POC/VAH/VAL, HVN/LVN, single prints, poor highs, excess, shape, IB, range extension |
| 3. Higher-timeframe profiles | **done (v1)** | `features/htf.py` — prev/developing week + month + 20-day composite, location/alignment/compression |
| 4. Order Flow engine | partial | `features/order_flow.py` — inferred-from-bars features normalized; depth features (absorption/sweeps/replenishment/book imbalance) stubbed null pending tick/depth data |
| 5. Normalized feature store | **done** | `utils/normalize.py` + `features/*` — MP+HTF+OF+option+context features, all instrument-independent/null-safe |
| 6. Auction-state features | **done** | `profiles/open_type.py`, `profiles/day_type.py`, folded into `features/market_profile.py` |
| 7. Labeling (triple-barrier, net_R, MFE/MAE, judgement, no-trade) | **done (Phase-0)** | `labels/directional.py`, `labels/profitability_gate.py` — grid labels + ATR/BS/actual-option gate interfaces |
| 8. Baseline model (multi-head) | **done** | `models/directional.py` — direction / is_move / magnitude / time / mae heads |
| 9. Walk-forward + purged + cross-instrument + cost stress | **done (v1)** | `evaluation/splits.py`, `evaluation/metrics.py`, `evaluation/cross_instrument.py`, `evaluation/phase0.py` |
| 10. Signal output + explanation | **done (v1)** | `live/signal_engine.py`, `live/explanation.py`, CLI `build-signal` |
| Regime classifier (§15) | **done (v1)** | `regime/classifier.py` deterministic regime layer; trained model calibration pending |
| Meta-label / no-trade (§16, §21) | **done (v1)** | `meta/labeler.py`, `is_move` head, no-trade verdict criteria |
| Payoff-distribution model (§17) | **done (v1)** | `payoff/distribution.py` from multi-head outputs; empirical distribution calibration pending |
| Execution / slippage (§18) | **done (v1)** | `execution/models.py` order-style/slippage/adverse-selection decision layer |
| Options engine (§19) | **done (v1)** | `data/option_bars.py`, `features/option_structure.py`, `options/selector.py` |
| Risk governor (§20) | **done (v1)** | `risk/governor.py` dynamic sizing + kill-switch checks |
| MoE setup experts (§23) | **done (v1)** | `setups/experts.py` archetype gating scores |
| Drift monitor (§30) | **done (v1)** | `live/drift_monitor.py` calibration/slippage/feature-drift action layer |
| Neural multi-encoder (§7) | **done (scaffold)** | `models/neural.py` optional PyTorch MP/OF/HTF/context encoder; training pipeline pending real sequences |

## What changed in this update (2026-05-30)

- New `profiles/` package: `profile.py` (full MP primitive), `open_type.py`, `day_type.py`.
- New `utils/normalize.py`: ATR-reference, ATR/profile-width normalization, rolling same-TOD
  z-score, percentile — all leak-free.
- Rewrote `features/market_profile.py`: 41 normalized MP features (geometry, nodes/magnets,
  shape/quality, IB, auction state). Raw price levels removed.
- New `features/htf.py`: 15 higher-timeframe features (multi-timeframe profile stack).
- Rewrote `features/order_flow.py` (12 features, z-scored + depth stubs) and
  `features/context.py` (10 features, `c_`-prefixed, India-VIX-ready).
- New tests: `test_profiles.py`, `test_instrument_independence.py` (the §3 guard).
- Added `data/option_bars.py` and `features/option_structure.py` for ATM CE/PE/straddle
  families C/D, null-safe when option history is absent.
- Added grid directional labels and gates: `labels/directional.py`,
  `labels/profitability_gate.py`.
- Added multi-head validation model: `models/directional.py`.
- Added purged walk-forward embargo, sample-uniqueness weights, directional metrics,
  acted-EV, and cross-instrument transfer harness.
- CLI now has blueprint commands: `build-grid-features`, `build-labels`,
  `train-directional`, `evaluate`, `build-signal`, and `validate-overlay`.
- Added full alpha-machine v1 layers: regime classifier, meta-labeler, payoff estimator,
  execution selector, options expression selector, risk governor, setup-expert gating,
  drift monitor, final signal/explanation object, and optional neural multi-encoder scaffold.
- Full suite: 42 tests passing; `scripts/smoke_test.py` runs the grid-directional pipeline.

## Remaining work after Phase-0 blueprint completion

1. Calibrate `m_breakeven`, slippage, and gate mode from real ATM weekly option history.
2. Feed real ATM option bars into `resolve_atm_series(...)` and validate families C/D on history.
3. Promote `build_alpha_signal(...)` outputs into the production sniper-paper runner/dashboard.
4. Replace v1 deterministic regime/meta/payoff/execution/risk layers with calibrated trained
   models once real OOS and paper-trading distributions exist.
5. Train the optional neural multi-encoder only after sequence datasets and GBDT evidence exist.

## Data still required

- Underlying minute bars: `upstox_<underlying>_fut_<YYYYMMDD>.parquet`.
- (For option families C/D and the option-economics gate) ATM CE/PE history or an IV series.
- `m_breakeven` calibration (the ATR move an ATM weekly clears after theta+cost).
