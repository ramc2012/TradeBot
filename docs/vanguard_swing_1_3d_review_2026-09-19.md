# Vanguard swing lane review — 19 September 2026

This review is paper-only. The lane emits a fixed top-10 CE and top-10 PE
research ranking; `paper_position=true` records simulated positions and never
authorizes a broker order.

## Data and selection fixes

- The training cohort now requires exact option marks through D+3 and an
  expiry on or after D+3. The previous D+2 expiry floor admitted contracts
  that could not support the requested holding period.
- Direction and contract rankers train on separate `source session × CE/PE ×
  horizon` groups. A mixed CE/PE loss could let one side crowd out the other.
- D+1, D+2, and D+3 are persisted independently on each watchlist item and
  shown as separate UI columns and run-level aggregates.
- The latest replay emitted 20 rows: 10 CE and 10 PE, with horizons spanning
  D+1 through D+3. It remains shadow/research-only because the historical gate
  did not pass.

## Holdout evidence

The corrected cohort contains 15,166 exact-contract paths from 63 source
sessions. At the current 30-epoch challenger fit:

| Ranker | Validation overlap@10 | Test overlap@10 | Test selected mean | Historical gate |
|---|---:|---:|---:|---|
| Direction | 4.03 | 1.58 | +0.33% | Not passed |
| Contract | 3.33 | 1.23 | +13.10% | Not passed |

The gate requires test overlap@10 ≥ 2, positive selected mean, and positive
group rate ≥ 50%. Keeping the artifacts shadow-only is intentional until
prospective paper outcomes establish hit rate on untouched sessions.

## Sources and reruns

- `candidate_evaluations`, `underlying_spot_candles`, and
  `option_premium_candles` in `nomadcurie` provide the causal 14:15 decision,
  14:45 entry, and exact-contract exit marks.
- `vanguard_swing_watchlist_items` is the durable paper-position ledger;
  `vanguard/research/watchlist_coverage.py --sessions 3` reports the per-side
  top-10 comparison and D+1/D+2/D+3 resolution counts.
- The reproducible companion notebook is
  `vanguard/research/selection_benchmark_20260918.ipynb`.
