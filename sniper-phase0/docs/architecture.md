# Architecture — Nomad Curie Sniper Phase 0

## One-paragraph summary

Phase 0 takes the FY25-FY26 Zerodha trade log and asks: *does a feature set built from Market
Profile + inferred order flow + market regime context retrospectively identify the trades that
hurt P&L the most?* If yes, build the full Sniper. If no, fix the feature set first.

## Data flow

```
data/raw/zerodha_trades_*.csv                  data/raw/upstox_*_fut_*.parquet
            │                                              │
            ▼                                              ▼
   load_zerodha_trades                             load_minute_bars
            │                                              │
            ▼                                              │
   pair_round_trips ──────► RoundTrip[]                    │
            │                                              │
            ├──────► label_actual_trades ──► labels.parquet (net_pnl, quartile, decile)
            │                                              │
            └──► (trade_id, entry_at, underlying) ────┐    │
                                                      ▼    ▼
                                       build_features_for_trades
                                                      │
                                                      ▼
                                              features.parquet
                                                      │
                                                      ▼
                                  walk_forward + train_skip_classifier
                                                      │
                                                      ▼
                                            oos_predictions.parquet
                                                      │
                                                      ▼
                                            run_phase0_verdict
                                                      │
                                                      ▼
                                       artifacts/phase0_verdict.json
```

## Module responsibilities

| Module | What it owns | What it must NOT do |
|---|---|---|
| `data.trades` | Parse Zerodha CSV → strict `Trade` objects | Compute P&L |
| `data.round_trips` | FIFO pair legs into `RoundTrip`s | Apply costs |
| `data.bars` | Load Upstox minute bars by underlying | Compute features |
| `features.market_profile` | POC/VAH/VAL, IB, opening location | Touch order-flow proxies |
| `features.order_flow` | Inferred delta/intensity from bars | Claim it's true MBO data |
| `features.context` | Time of day, expiry, ATR regime | Hold any per-trade state |
| `features.pipeline` | Stitch all three into a snapshot | Bypass the leakage check |
| `labels.cost_model` | Convert (entry, exit, qty) → cost | Read trade history |
| `labels.actual_trades` | Net-P&L labels from round trips | Generate synthetic candidates |
| `labels.triple_barrier` | Forward-walk barriers on bars | Be used for Phase 0 primary verdict |
| `models.lightgbm_skip` | Train + predict for skip-or-take | Pretend to be a Phase 7 NN |
| `evaluation.splits` | Walk-forward + purge | Random shuffles |
| `evaluation.metrics` | Skip accuracy, counterfactual P&L, Sharpe | Define the go/no-go thresholds |
| `evaluation.phase0` | Compute verdict against pre-committed thresholds | Adjust thresholds based on results |

## Why the rules exist

**Why `data_available_at` on every feature.** Without it, look-ahead bias is invisible and
silently inflates results until live trading exposes it. With it, leakage is detected at
the snapshot-builder layer and again in CI tests.

**Why costs go into labels, not evaluation.** If costs are only subtracted at evaluation, the
model trains on gross P&L and learns to chase high-turnover gross-positive setups that are
net-negative. By putting `net_pnl` into the label, the model directly learns the only number
that matters.

**Why walk-forward only.** Financial time series violate iid. Random splits leak future
information into training. Purged k-fold can work but is overkill for the data we have;
walk-forward is simpler and harder to get wrong.

**Why pre-committed thresholds.** The number one mistake in retail ML trading is to "tune"
the success criteria after seeing results. We don't.

## Why Phase 0 looks different from the document's Sections 4–10

The document describes the full Sniper. Phase 0 is the *gate* for building it. Phase 0:

- Uses one feature pipeline (no temporal sequence — that's Phase 7)
- Uses one model family (LightGBM — Phase 7 adds CNN/Transformer)
- Uses one label (was the trade a winner — Phase 1 adds expected_R, MFE, MAE heads)
- Labels actual trades only — Phase 1 adds triple-barrier on synthetic candidates

The motivation is sequencing. Each later phase adds cost and complexity. We don't pay that
cost until Phase 0 demonstrates the feature set has signal on data we already have.

## Out of scope for this repo

| Concern | Where it lives | When |
|---|---|---|
| Live data ingestion | Separate service | Phase 1 |
| TimescaleDB / Redis pub-sub | nomad-curie-data | Phase 1 |
| Greeks Confluence Engine | Existing Scanner repo | Already shipped |
| Options strategy selection | nomad-curie-options | Phase 6 |
| Risk Governor | nomad-curie-risk (separate service) | Phase 5 |
| Deep learning | nomad-curie-sequence | Phase 7, only if Phase 0+1 succeed |
