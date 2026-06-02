# Decision Log

One line per architectural decision. Most-recent first. Format:

```
YYYY-MM-DD | DECIDED: <what>. REASON: <one sentence>. ALTERNATIVE: <what we rejected>.
```

---

2026-05-30 | DECIDED: Added v1 alpha-machine modules for final signal/explanation, regime, meta-labeling, payoff, execution, options expression selection, risk governance, setup experts, drift monitoring, and optional neural encoders. REASON: the full spec requires callable alpha-machine layers beyond Phase-0 validation; v1 deterministic modules provide production interfaces while awaiting real-data calibration. ALTERNATIVE: block on trained/live artifacts before adding interfaces (rejected because downstream integration needs stable contracts).

2026-05-30 | DECIDED: Completed the directional Phase-0 blueprint path: ATM option loader/features, grid directional labels, option-economics gates, multi-head LightGBM, embargo/uniqueness weighting, acted-EV verdict, cross-instrument harness, and grid CLI. REASON: this turns the normalized auction engine into the actual supervised validation machine described by the feature contract. ALTERNATIVE: leave the downstream pieces as roadmap-only (rejected).

2026-05-30 | DECIDED: Implemented the full normalized auction feature engine (profiles/ package + MP geometry/shape/auction-state + HTF multi-timeframe stack + normalized OF + context), aligning the module to the uploaded alpha-machine spec. REASON: the spec centers on a learned auction-judgement engine; the feature engine is its heart and the highest-value testable upgrade. ALTERNATIVE: build downstream alpha modules first (deferred — they depend on this feature contract).

2026-05-30 | DECIDED: OF depth features (absorption, sweeps, replenishment, book imbalance) stubbed as nulls until tick/depth data is wired. REASON: Phase-0 has only OHLCV; emitting proxies under depth-feature names would be dishonest. ALTERNATIVE: fake them from bars (rejected).

2026-05-30 | DECIDED: Uploaded spec is the north-star; feature_contract.md is the authoritative implemented subset, with alpha-machine stages tracked in IMPLEMENTATION_STATUS.md. REASON: one implemented source of truth while preserving the full vision.

---

2026-05-29 | DECIDED: Target is directional-move detection on a time grid over the underlying, option-economics-aware — not a skip-classifier on realized trades. REASON: realized trades are a censored sample (selection bias) and cannot teach general judgement; the goal is to read structure and detect moves to express via options. ALTERNATIVE: skip-classifier on actual trades (demoted to a validation overlay), and MP-rule candidate generation (rejected — injects the judgement the model should learn).

2026-05-29 | DECIDED: Strict instrument-independence law — no price/volume/premium units as model features; everything ATR-normalized, z-scored, ratio, %, or categorical. REASON: raw levels break cross-instrument transfer and create spurious walk-forward splits on price levels that never recur OOS. ALTERNATIVE: mixed raw+normalized features (the first scaffold; rejected).

2026-05-29 | DECIDED: Read ATM CE/PE/straddle directly for move/no-move, IV regime, and directional lean — added as feature families C/D. REASON: a balancing underlying with disproportionate premium behaviour reveals whether a move is coming; theta/IV/skew are signal for the move/no-move question even though they contaminate the direction question. ALTERNATIVE: underlying-only features (rejected — leaves move/no-move information on the table).

2026-05-29 | DECIDED: Direction is detected on the underlying, not on the option. REASON: option price for directional read is dominated by theta/vega/gamma; both legs recombine to the synthetic future via parity, i.e. the underlying, but degraded. ALTERNATIVE: option-price MP as the directional detector (rejected for direction; retained for move/no-move).

2026-05-29 | DECIDED: Handle forward-label overlap with embargo + sample-uniqueness weights. REASON: adjacent grid points share future bars; without this every CV score and feature-importance ranking is inflated. ALTERNATIVE: purge gap alone (insufficient).

2026-05-29 | DECIDED: Keep LightGBM (multi-head) as the signal-validation model; NN deferred to the next milestone on the identical feature contract. REASON: engineered tabular features favour GBDT; NN edge needs sequences not yet built; normalization work transfers either way.

---

2026-05-27 | DECIDED: LightGBM is the Phase 0 model, not a baseline to beat. REASON: Tabular features dominate; NN gains are incremental and not worth the Phase 0 complexity. ALTERNATIVE: Start with CNN/Transformer over engineered OF sequences (moved to Phase 7, conditional).

2026-05-27 | DECIDED: Order-flow features named `inferred_*`. REASON: Phase 0 has no tick-level MBO data; calling these "delta" or "imbalance" without qualifier is misleading. ALTERNATIVE: Standard names (rejected — leads to false confidence when Phase 1 ships real OF).

2026-05-27 | DECIDED: Phase 0 labels actual trades only, not synthetic candidates. REASON: Cheapest possible test of whether the feature set has signal on data the user has. ALTERNATIVE: Triple-barrier on synthetic MP setups from day one (deferred to Phase 0.5).

2026-05-27 | DECIDED: Pre-commit go/no-go thresholds in `evaluation/phase0.py`. REASON: Prevents post-hoc rationalization of marginal results. ALTERNATIVE: Tune after seeing distributions (rejected — that's how retail ML trading fails).

2026-05-27 | DECIDED: `data_available_at` mandatory on every Feature. REASON: Look-ahead bias is the #1 silent killer in trading ML. Enforcing the timestamp at the type level catches it at the snapshot-builder layer, not in production. ALTERNATIVE: Trust developers to be careful (rejected — that's not been a working strategy in this domain).

2026-05-27 | DECIDED: Costs baked into labels (`net_pnl`), not subtracted at evaluation. REASON: Training on `gross_pnl` teaches the model to chase high-turnover positions that are net-negative. ALTERNATIVE: Subtract costs only at evaluation (rejected — selection vs cost is the entire game).

2026-05-27 | DECIDED: Walk-forward CV with purge gap, no random splits. REASON: Financial returns violate iid; random splits leak future information into training. ALTERNATIVE: Stratified k-fold (rejected for this data regime).

2026-05-27 | DECIDED: All timestamps IST-aware via `ensure_ist`; naive datetimes raise. REASON: NSE timestamps drifting to UTC silently shift sessions and corrupt every downstream computation. ALTERNATIVE: Convert lazily in display layer (rejected — once a bug is in the data, it's already too late).

2026-05-27 | DECIDED: No database in Phase 0; parquet files on disk. REASON: Adds operational complexity for zero analytical gain at this size. ALTERNATIVE: TimescaleDB from day one (deferred to Phase 1).
