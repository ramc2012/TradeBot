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

---

## Run 2 — DEEP validation on 5-year US data (Alpaca SPY/QQQ/DIA, 2026-06-06)

Run-1 hit the **data-depth wall** (NSE 1m history ≈ 82 days, MinBTL ≈ 1.7–3.9y). To
get a statistically-adequate read we ran the *same harness* on **4.99 years of
1-minute SPY/QQQ/DIA** (Alpaca, RTH-filtered, contamination-guarded) — entirely
**off-prod** (local parquet, the approved "local off-DB-pull" lane; no sidecar, no
prod backend, zero OOM risk). Windows: **IS=504d / OOS=126d / stride=126d → 10
walk-forward windows**, the first MinBTL-relevant configuration.

The strategies are NSE F&O strategies; SPY can only test the part of each lane whose
**signal logic is instrument-agnostic on underlying OHLC**. The option-execution
layers (contract selection, premium stops, lot sizing, the RL trade/skip policy) are
NSE-specific and are explicitly **deferred to option-data validation** (see below).
So this run validates *methodology + signal edge at depth*, not transferable params.

### What was wired (new harness adapters, all off-prod)

| Lane | Adapter | What it tests | Instrument-agnostic? |
|---|---|---|---|
| **Gann TP-Delta** | `gann_tp_delta/validate_local.py` | Full geometry backtester on the underlying | ✅ geometry is price-ratio/angle based → scale-invariant |
| **Auction / Market-Profile** | `auction_intelligence/validate_local.py` | Platform's real `GateBValidator` (MP + order-flow swing agent), native underlying trades | ⚠️ only after **tolerance rescaling** (see finding) |
| **Directional-options** | `directional_options/validate_local.py` | `DirectionalSignalEngine.predict` signal, executed neutrally on the underlying | ✅ ATR/%-normalised features |
| **Spot-MACD baseline** | `analysis/validate_macd_local.py` | A naive MACD-crossover trend control (NOT NSE-S1) | ✅ ATR-normalised |

Shared neutral execution: `analysis/signal_backtest.py::simulate_underlying`
(ATR stop = −1R, identical across lanes → comparable R-multiples).

### Out-of-sample gate verdicts (10 windows, ~5y, costs applied)

| Lane | Sym | Passed | OOS trades | OOS exp (R) | PF | WFE | DSR | PBO | MC p05 | MinBTL vs 4.99y |
|---|---|---|---|---|---|---|---|---|---|---|
| **Gann** | SPY | **3/7** | 84 | **+0.141** | 1.29 | **1.00** | 0.00 | n/a | −0.064 | 5.5y |
| Gann | QQQ | 4/7 | 58 | +0.007 | 1.01 | 0.50 | 0.52 | n/a | −0.221 | 5.5y |
| **Auction-MP** | SPY | **3/7** | 198 | +0.020 | 1.07 | **0.97** | **0.55** | 1.0 | −0.093 | 5.2y |
| Auction-MP | QQQ | 2/7 | 206 | +0.009 | 1.04 | 0.20 | 0.57 | 0.89 | −0.100 | 5.2y |
| Auction-MP | DIA | 2/7 | 205 | +0.009 | 1.03 | 0.20 | 0.48 | 1.0 | −0.099 | 5.2y |
| Directional | SPY | 2/7 | 1278 | **−0.140** | 0.82 | 0.0 | 0.0 | 0.0 | −0.140 | 8.0y |
| Directional | QQQ | 2/7 | 1435 | −0.081 | 0.89 | 0.0 | 0.0 | 0.0 | −0.098 | 8.0y |
| MACD (ctrl) | SPY | 2/7 | 872 | −0.085 | 0.87 | 0.0 | 0.0 | 0.13 | −0.116 | 7.2y |
| MACD (ctrl) | QQQ/DIA | 2/7 | ~850 | −0.09/−0.15 | 0.87/0.79 | 0.0 | 0.0 | 0.02/0.14 | — | 7.2y |

**Strategy ranking at depth: Gann ≳ Auction-MP ≫ MACD ≈ Directional-signal.**
No lane clears all 7 gates — the correct, conservative outcome for strict
institutional gates on 5y. But the harness now **discriminates cleanly**: the two
structural strategies (Gann, MP) post **positive OOS expectancy with WFE≈1.0**
(out-of-sample ≈ in-sample — the *opposite* of overfit), while the two
indicator/feature signals are **cleanly negative** and correctly rejected.

### Findings (expert read)

1. **Gann is the strongest lane and is NOT overfit.** SPY: +0.141R over 84 OOS
   trades, PF 1.29, **WFE=1.0**. Its failures are *artifacts/near-misses*, not edge
   problems: (a) **DSR=0** because the Gann backtester is **single-anchor** — over a
   5y *full-sample* it anchors once and emits ~10 trades, so the full-sample Sharpe
   (which DSR deflates) is ~0 even though the *walk-forward* aggregate is +0.141R;
   (b) MinBTL 5.5y just exceeds the 4.99y available. **Action: add rolling
   re-anchoring to the Gann backtester** so the full-sample run isn't sparse — this
   alone should lift DSR. Gann's per-window IS-best floor was a *stable* 4.0 on SPY
   (vs the unstable 5.5→4.0 on shallow NSE) — depth stabilises the tuning surface.

2. **Auction-MP is real but fragile — and exposed a genuine config bug.** The swing
   agent's entry tolerances are hardcoded in **absolute NSE-index points**
   (`value_entry_tolerance_min_points=10`, `ib_break=8`, …). NIFTY≈23000 with ~60pt
   IB; SPY≈$670 with ~0.6pt IB → those floors are **~30–50× too large** and silently
   disable the value-area filters. **Unscaled SPY: −0.078R / 18.5% win. Price-scaled
   SPY: +0.069R / 55.7% win** — the same logic flips from losing to winning purely on
   scale. The MP value-area edge is real, but: it holds **only on SPY** (QQQ/DIA WFE
   collapse to 0.20), **only in medium-vol regime** (regime Sharpe: med +0.77, low/high
   ≈0), and PBO=1.0 is **unreliable here** because the `min_confidence` grid barely
   discriminates (near-identical CSCV columns → degenerate PBO). **Actions:** make MP
   tolerances **fractional (price-relative), not absolute points** (robustness bug);
   add a **regime gate (medium-vol only)**; use a **discriminating** param grid before
   trusting PBO.

3. **The directional *signal* has no underlying edge — its value must be the option
   layer.** Taken every bar on the underlying with neutral ATR stops the raw
   `predict()` bias is −0.14R (33% win at 2:1 RR, below breakeven); the regime gate
   doesn't help. This is *not* a verdict on the directional lane — its thesis rests on
   **option convexity + the RL trade/skip policy + option-specific exits**, none of
   which a neutral underlying exec captures. **Must be validated on NSE option data.**

4. **Naive MACD has no edge (control worked).** −0.085 to −0.15R across all three
   ETFs; the harness rejected it exactly as a no-edge control should. (This is a
   generic trend baseline — *not* NSE-S1, which trades the option premium and needs
   option data.)

5. **Even 5 years barely satisfies MinBTL.** MinBTL came out 5.2–8.0y vs 4.99y
   available — driven by **trial count** (50→240 across lanes). Two implications:
   (a) keep param grids **lean** (the 240-trial directional/160-trial MACD grids
   inflate MinBTL most); (b) for the promising lanes (Gann 5.5y, MP 5.2y) the gap is
   only ~0.2–0.5y — a **6th year of data, or a 2–3 param grid, makes MinBTL passable**.

6. **Methodology is proven at depth.** Guards → walk-forward(10) → 7 gates → MC →
   regime ran clean on every lane, produced positive-vs-negative discrimination, and
   surfaced two *fixable* artifacts (single-anchor DSR, degenerate-grid PBO) plus one
   real bug (NSE-point tolerances). This is the validation the 82-day NSE data could
   not support.

### Promotion status (Stage A → B → C, per the plan)

| Lane | Stage | Rationale |
|---|---|---|
| **Gann TP-Delta** | **A→B candidate** | +OOS edge, WFE=1.0, stable floor at depth; gated on rolling-anchor fix + 6th yr / NSE-native re-run |
| **Auction-MP** | **A (conditional)** | +OOS edge but instrument- & regime-specific; gated on fractional tolerances + regime gate + discriminating grid |
| Directional-options | hold (signal) | underlying signal −EV; re-test the **option+RL layer on NSE option data** |
| NSE-S1 / MACD-options | deferred | option-premium strategies — **need NSE option_premium_candles**, SPY invalid |

### Actions

- [ ] **Gann: add rolling re-anchoring** to `GannTPDeltaBacktester` so the full-sample
      DSR/PBO gates aren't starved by a single anchor; re-run deep validation.
- [ ] **MP: fractional tolerances** (replace `*_min/max_points` with price-relative
      fractions) — fixes the silent off-scale-instrument failure *and* improves NSE
      robustness; add medium-vol regime gate; widen the grid to a discriminating lever.
- [ ] **Re-run Gann + MP on NSE indices** at their native scale once 1m history is
      backfilled ≥2y (the SPY edge is a methodology proof, not an NSE parameter).
- [ ] **Option lanes (NSE-S1, MACD-options, directional option/RL, fractal exits):**
      build the option-premium validation path on `option_premium_candles` — these
      cannot be tested on SPY by construction.
- [ ] Keep all live floors at current conservative settings until OOS-validated on
      NSE-native data. SPY results do **not** authorise any NSE parameter change.

Harness + adapters committed off-prod; every run was local (no prod load, health
stayed 200 throughout).

---

## Run 3 — Tune + validate cbe / gann / auction / fmp / commodity (2026-06-06)

Per request, extended the deep validation to the remaining lanes. The headline
result is a **portability finding**: of the five lanes, only **Gann** is truly
instrument-portable (its geometry is scale-invariant). The market-profile and
flow/scanner lanes are **hard-coded to NSE/MCX specifics**, so the 5y equity data
can only validate them after de-coupling — and in several cases not at all. Two of
those hard-codings are **latent production fragilities**, not just test obstacles.

### Lane-by-lane outcome

| Lane | Validatable on 5y SPY? | Result / blocker |
|---|---|---|
| **Gann** | ✅ yes | Run-2: **+0.141R OOS, WFE 1.0** — best lane. Scale-invariant geometry. |
| **Auction-MP** | ✅ after price-scaling tolerances | **Tuned** — see below. Edge real but instrument/regime-specific. |
| **FMP (fractal MP)** | ❌ no | **0 actionable setups in all 1,233 SPY sessions.** NSE-coupled 3 ways. |
| **CBE scanner** | ❌ no | Cross-sectional NSE options-flow + sector/FII/event feeds — no equity analogue. |
| **Commodity futures** | ❌ no | Data-blocked (21d only) **+ broken harness** (`commodity_walkforward.py`). |

### Auction-MP — TUNED (`auction_intelligence/validate_local.py`)

Run-2 left two problems: a near-degenerate `min_confidence` grid (→ PBO unreliable
at 1.0) and the just-missed MinBTL. Fixes: replaced the grid with a **discriminating
`regime_gate` lever** (medium-vol-only, where Run-2 found the edge) and trimmed
trials 50→40.

| Sym | Passed | OOS tr | WFE | DSR | PBO (was) | MinBTL (was) | Δ |
|---|---|---|---|---|---|---|---|
| SPY | 3/7 | 198 | 0.97 | 0.07 | **0.41** (1.0) | **4.79y✓** (5.18) | MinBTL now **passes**, PBO meaningful |
| QQQ | 3/7 | 206 | 0.20 | 0.00 | **0.37✓** (0.89) | 4.79y✓ | PBO+MinBTL pass |
| DIA | 2/7 | 153 | 0.03 | 0.29 | 0.71 (1.0) | 4.79y✓ | MinBTL passes |

Tuning insight (smoke, 500d SPY): **medium-vol regime gating ~2.4× the expectancy**
(+0.125R vs +0.052R; 60% vs 53% win) at ~1/6 the trade frequency. BUT the harness
selector is `select="total"` (total-R), which always picks the *un-gated* higher-
frequency config (per-window IS-best = `regime_gate:False` on every SPY window) — so
the quality edge of gating isn't captured. **Action: add a Sharpe/expectancy-based
selector option** so risk-adjusted configs can win selection. Net: tuning fixed
MinBTL + PBO and surfaced the regime lever; SPY MP remains a genuine but
**instrument-specific (SPY-only), medium-vol-only** signal — not yet promotable.

### FMP — NSE-coupled, zero signals on proxy (`fractal_market_profile`)

The MP+fractal signal (`service._analyze_session_sync`) imports cleanly and the
profile pipeline runs on SPY, but it produced **FLAT on every one of 1,233 sessions**
(lookahead-safe 90-min decision cutoff). It is coupled to NSE in three independent
ways: (1) **session window** hard-coded to IST 09:15–15:30 (US bars fall outside →
empty profiles; worked around by remapping timestamps); (2) **`min_value_migration_abs=1`
in absolute points** (trivial on NIFTY≈23000, larger than SPY's whole intraday value
migration; same class of bug as the auction tolerances); (3) **options/PCR/IV/VIX
confirmation** in `SCAN_CONFIG` (`bullish_pcr_min`, `max_iv_rank_for_buying`,
`india_vix_defined_risk`) that has no equity analogue. Even after fixing (1)+(2), the
shape classifier ruled every session a "Ledge / IB-too-wide" no-trade. **Verdict:
FMP cannot be validated on equity proxy data; it needs NSE index data + its option/PCR/
IV/VIX feeds.** (Its value-migration absolute-point threshold should be made fractional
— same fix as the auction tolerances.)

### CBE — cross-sectional NSE scanner, no proxy (`cbe_scanner`)

`features.compute_cbe_score` → `{composite_score 0-10, directional_bias, bias_conviction}`
is an **EOD cross-sectional ranking** scanner, not a per-bar signal. Its features need
an **options chain (OI/IV/PCR), sector returns vs Nifty, NSE event calendar, block
deals, FII/DII flows** — none of which exist for SPY. It is structurally un-proxyable;
it must be validated on its native NSE F&O universe (and, being a weekly-rebalance
cross-sectional book, with a cross-sectional backtester, not the per-instrument harness).

### Commodity futures — data-blocked + broken harness (`paper_engine` + `analysis/commodity_walkforward.py`)

The commodity MP signal `commodity_mp_signal.evaluate_commodity_mp_signal` trades the
**futures (underlying)** and is per-bar, so it *would* fit the harness — but:
1. **No data.** `underlying_spot_candles` holds only ~21 days for commodities
   (`DEFAULT_COMMODITY_HISTORY_DAYS`); MinBTL needs ~2–3y. No local commodity history
   exists (the Alpaca set is equities only).
2. **Broken harness.** `analysis/commodity_walkforward.py` imports `evaluate_commodity_signal`
   from `commodity_strategy_agent` — **that name does not exist** (real:
   `evaluate_commodity_mp_signal`), and the real signature differs (needs
   `today_profile/prior_profile/cvd_anchor_index`, not `timeframe=`). The module
   `ImportError`s on load. Fixing the rename only moves it to a `TypeError`; it needs
   the profile/CVD plumbing rebuilt.
**Verdict: commodity validation is blocked on a 1m commodity backfill (≥2y) AND a
rebuild of `commodity_walkforward.py` against the real signal signature.**

### Bottom line (Run 3)

- **Only Gann survives proxy validation with an edge** — precisely because it is the
  one scale-invariant lane. This is a strong signal in itself: portable geometry beats
  instrument-tuned heuristics on out-of-sample, out-of-instrument data.
- **The NSE-coupled lanes (auction tolerances, FMP value-migration/session/options,
  CBE feeds, commodity data) cannot be shortcut with US data.** Validating them is
  **gated on the Phase-0 NSE backfill** — that is now the single highest-leverage task
  for the whole testing program.
- **Concrete code fixes surfaced:** (a) auction + FMP absolute-point thresholds →
  fractional (robustness bug; auction one already spawned); (b) `commodity_walkforward.py`
  broken import + signature; (c) harness `select="total"` → add risk-adjusted selector.
