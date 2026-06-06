# Strategy Testing — Run Log

First execution of the Phase-0/1 validation harness (docs/STRATEGY_TESTING_PLAN.md)
on real prod candles, in the isolated gann-sweep sidecar (no prod-backend load, no OOM).

## Run 1 — Gann TP-Delta, conviction-floor sweep (2026-06-06)

Sidecar: `tradebot-backend:latest`, `--memory=1300m`, direct asyncpg, guarded candles.
Windows: IS=42d / OOS=14d / stride=7d (sized to the available 1m depth).
Grid: `entry_conviction ∈ {4.0,4.5,5.0,5.5,6.0,6.5}` × anchor=auto_pivot × h=median_tpd.

### Out-of-sample gate verdicts — ALL FAIL (this is the harness working)

| Underlying | OOS trades | WFE (med) | DSR | MC SR p05 | MinBTL vs have | IS-best floors / window | Verdict |
|---|---|---|---|---|---|---|---|
| NIFTY | 54 | **−0.54** | 0.016 | −0.37 | 3.9y vs 0.22y | [5.5, 4.0, 4.0, 4.0] | FAIL 2/6 |
| BANKNIFTY | 26 | −0.38 | 0.231 | −0.43 | 1.7y vs 0.16y | [4.0] | FAIL 1/6 |
| SENSEX | 0 | — | 0.000 | — | 1.7y vs 0.08y | [] | FAIL 2/6 |

(PBO = `nan` = not computed — too few windows/trades for a CSCV perf-matrix.)

### NIFTY in-sample floor sweep (the "tuning signal", 82d)

| floor | trades | win% | totalR | PF | expR |
|---|---|---|---|---|---|
| 4.0 | 17 | 41.2 | 7.63 | 1.95 | 0.449 |
| 4.5 | 17 | 41.2 | 7.63 | 1.95 | 0.449 |
| 5.0 | 17 | 41.2 | 7.63 | 1.95 | 0.449 |
| **5.5** | 14 | 50.0 | **10.17** | **2.69** | **0.726** |
| 6.0 | 11 | 45.5 | 0.87 | 1.17 | 0.079 |
| 6.5 | 4 | 50.0 | 0.16 | 1.08 | 0.041 |

### Interpretation (expert read)

1. **The harness is correct and is doing its job** — it produced all six gates on real
   data and *refused to validate* an under-sampled, in-sample-overfit configuration.
   Negative WFE (OOS Sharpe < 0 while IS > 0) is the textbook overfit signature; the MC
   5th-pct Sharpe is negative; the deflated Sharpe is ~0.

2. **Data depth is the binding constraint.** 1m history is only **~82 days** (NIFTY),
   vs a MinBTL of **1.7–3.9 years** for the trial count. No lane can be promoted past
   Stage A until the 1m history is backfilled. This empirically confirms the plan's
   **Phase-0 backfill** as the gating prerequisite, not optional.

3. **The current Gann floor tuning is NOT out-of-sample validated.** The IS-best floor
   is unstable across windows (5.5, then 4.0×3) and the in-sample peak on 82d is **5.5,
   not the deployed 5.0** — a sharp peak, not a plateau (5.5 spikes, 6.0 collapses to
   +0.87R). On the available data the prior tuning should be treated as a *hypothesis*,
   not a validated parameter. Do not over-trust it.

### Actions

- [ ] **Phase 0 backfill (now):** deepen 1m index history (broker historical pull to
      ≥2y) so walk-forward windows reach IS≥210/OOS≥60 and MinBTL is satisfiable.
- [ ] Re-run this exact sweep at depth; only then is a floor (5.5 vs 5.0) a *validated*
      choice rather than an in-sample artifact.
- [ ] Fan the harness out to the other data-ready lanes (Fractal, NSE-S1) for the same
      honest read; expect the same data-depth ceiling until backfill lands.
- [ ] Keep gann live floors at the current conservative settings until OOS-validated.

The pipeline (guards → walk-forward → six gates → MC → regime) is proven end-to-end on
live data; the limiting factor is data, exactly as planned for.
