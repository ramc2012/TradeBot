# Phase 0 decision gate

These thresholds are pre-committed. Do not adjust them mid-experiment. If the model misses, the right response is to refine features or labels — not to relax the gate.

## Pass criteria (all must be met)

| Metric | Threshold | Rationale |
|---|---|---|
| **Skip-accuracy on bottom decile of `expected_net_R` (EV-ranked)** | **≥ 0.65** | Phase 0 is about *avoiding losers*, not picking winners. Ranking by EV (not p_win) captures the asymmetric-payoff case: low confidence + large payoff can be positive-EV; high confidence + small payoff can be negative-EV. |
| Net profit factor at 2× slippage on the traded set (top 9 deciles) | **≥ 1.5** | After paying 2× the calibrated slippage, the model's "trade" set must still be net positive with a margin. 1.0 is breakeven; 1.5 leaves room for live-trading degradation. |
| Max drawdown in R-units on traded set | **≤ 15** R | Empirical proxy — for a Sniper sized at 1R per trade, a drawdown deeper than 15R in walk-forward suggests size/risk-budget rework, not a deployable system. |

The `phase0_pass` flag is True only when all three gates are True simultaneously.

## Diagnostic metrics (reported, not gated)

- `skip_accuracy_by_pwin` — secondary diagnostic. If it's much higher than EV-ranked, the regressor head is weak; if much lower, the classifier head is weak. Either way, retraining is warranted before declaring failure.
- `per_regime` — skip-accuracy stratified by expiry_day / expiry_week / gap_day / opening_hr / closing_hr / normal. If aggregate fails but one regime clears the gate cleanly, that's a candidate carve-out for v0.1.
- `purging.n_train_purged_total` — how many training rows were dropped because their label window leaked into validation. High purge counts (>5% of training set) imply trade clustering and warrant a larger `purge_minutes`.

## Reporting

The CLI (`phase0 eval`) writes `data/processed/reports/phase0_report.json` with:

- Per-fold metrics (skip accuracy, mean net_R, win rate, n_trades)
- Overall aggregated metrics
- Gate pass/fail booleans
- The final `phase0_pass` flag (all gates must be True)

## If Phase 0 passes

Phase 1 (data infrastructure) begins. Useful modules (cost model, walk-forward harness, feature schema) get lifted into the platform; the rest of `sniper-phase0/` is archived.

## If Phase 0 fails

Do **not** proceed to Phase 1. Instead, in order:

1. **Compare run sources.** Re-run with `phase0 candidates` as the decision_ts source instead of the Zerodha trade log. If the candidate-driven run passes while the trade-log-driven run fails, the original discretionary entries were the bottleneck — proceed to Phase 1 using setup-family candidates as the input to the live signal engine.
2. Inspect feature importance from the LightGBM models. If <5 features have non-trivial gain, the feature set is impoverished — expand MP/OF/Context families.
3. Check label distribution. If >70% of trades are losers under the cost model, "skip everything" is locally rational — the candidate run in step 1 is the only legitimate way to test the signal in this case.
4. Validate cost calibration. Recompute realised slippage from Zerodha fills (entry/exit mid vs fill); if it exceeds 1.5 bps, raise `slippage_bps_default` and re-run.
5. Check the per-regime breakdown. If the aggregate fails but `expiry_day` or `gap_day` cleanly passes the gate, consider a regime-restricted v0.1 carve-out.
6. Only after the above five checks, consider feature engineering iterations.
