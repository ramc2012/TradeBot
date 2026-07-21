# HTF Regime → LTF Timer → Short-Horizon Option — MEASUREMENT RESULTS
**2026-07-21 · measurement pass (design frozen a priori in
`docs/directional_htf_regime_study_design_2026-07-21.md`) · research-only**

Module: `backend/directional_options/research/htf_regime/`
(`build_universe.py` → `spot_analyse.py` → `bulk_eval.py` →
`option_analyse.py` → `sensitivity.py`; raw outputs in
`data/spot_results.txt`, `data/opt_results.txt`, parquets alongside).

## 0. One-paragraph verdict

The owner's model — daily trend as a standing STATE, LTF timer inside it,
option held hours-to-days — was measured exactly as pre-registered, spot
level over 15 months (216 names, 388 sessions) and option level over
2026-03-02..07-21 on the real deduped option tape (354k evaluated 30m
bar-selections, equivalence-proven against the read layer). **The daily
regime state is real and long-lived but carries NEGATIVE short-horizon
drift: random long entries inside a confirmed daily uptrend LOSE money over
2h–3d (the load-bearing C2 control), so the filter's only measurable value
at option level is avoiding the worst counter-trend theta bleed — a
less-negative outcome, never a positive one. Zero of 128 option cells has a
positive net mean at the inherited cost model; the best cell is gross
+1.2%/trade and that gross is concentrated in one month (2026-03) and its
top-3 trades. The deep-MACD pullback timer (the `str_below0` survivor) is
again the only construct with real bar-selection value (Bonferroni-surviving
vs matched random at both spot and option level) — but it does not survive
option expression. No lane should be built from this pass.**

## 1. What was run

- Regimes (frozen): R1 = close>SMA20>SMA50 & SMA20 rising (primary);
  R2 = ADX(14)>20 with DI direction. State governing session t computed
  through t−1 close (`*_lag1`); up→CE only, down→PE only. Causality suite
  5/5 green (prefix invariance rtol 1e-12, lag contract, read-layer
  invariance, dedup honesty).
- Timers (frozen): deep_macd, pullback_anchor, orb, macd_plain (control),
  on 30m and paired-1h, evaluated only when the governing state is on.
- Holds 2h / eod / 1d / 3d; bands ATM / slight-ITM; near-month 8≤DTE≤40.
- Controls: C1 same timer unfiltered; **C2 random-inside-regime (load-
  bearing)** 200 seeded draws matched on per-(underlying, direction) entry
  count; C3 matched unconditional random. Episodes collapsed per
  underlying; session-block bootstrap (1000) for C1 differences.
- Option level through `option_read_layer` rules: bulk evaluator
  equivalence-checked against the layer on 300 selections + 300 marks per
  timeframe (exact contract and price match; rtol 1e-9). Entry requires an
  exact tradeable option bar (96.0% of 30m selections had one).
- Costs (inherited from moves_rs, spread is ASSUMED not measured):
  round-trip stock 8.0% / index 1.6% of premium; grid 0/2/5/10% reported.
- Multiplicity: 128 cells × 2 decisive comparisons = 256 tests;
  Bonferroni α = 1.953e-4 (via declared normal approximation of the
  200-draw null); BH-FDR q=0.10 alongside. Primary pre-registered cell
  (R1×deep_macd×30m×1d×slight_ITM) alone at α=0.05.

## 2. Spot level (full 15-month window)

Signed forward spot return per entry (r1, 30m, hold=1d; full table in
`data/spot_results.txt`):

| timer | filtered mean (n) | C1 unfiltered | C2 null (random-inside-regime) | C2 z | regime lift d_C1 (p) |
|---|---|---|---|---|---|
| deep_macd | **+6.1 bp** (3,940) | +10.6 bp (25,468) | **−10.8 bp** | **+4.7** | −4.5 bp (0.74) |
| pullback_anchor | −9.7 bp (11,076) | −1.3 bp | −7.9 bp | −0.8 | −8.4 bp (0.22) |
| orb | −1.9 bp (8,920) | +4.3 bp | −7.8 bp | +2.5 | −6.1 bp (0.44) |
| macd_plain | −2.9 bp (10,572) | +5.3 bp | −8.0 bp | +2.5 | −8.2 bp (0.37) |

- **The load-bearing fact: C2 nulls are negative at every hold** (r1:
  −1.7 bp at 2h → −10.8 bp at 1d → −23.2 bp at 3d). A confirmed daily
  uptrend predicts short-horizon REVERSION, not continuation — fully
  consistent with the prior momentum-anti-predictive / fade findings.
- Regime beta check at spot: C2 null − C3 null ≈ **−14 bp** at 1d and
  **−33 bp** at 3d (r1). The regime does not merely "add nothing beyond
  beta" — inside-regime bars are WORSE than unconditional bars at these
  horizons.
- No timer is lifted by the regime filter at spot level (all d_C1 ≤ 0).
  deep_macd inside the regime beats random-inside-regime overwhelmingly
  (z 3.6–4.7, percentile 200/200) — that is the TIMER's value; it is even
  larger unfiltered.
- 1h frame: nothing survives (deep_macd 1h 1d: −8.3 bp filtered).
- Regime AGE (descriptive, hold=1d, deep_macd r1): +10.3 bp (age 1–5),
  +14.8 bp (6–12), +2.9 bp (13–20), **−22.6 bp (>20)** — the
  late-entry-death hypothesis from the 12–18-session median run is borne
  out; the same age decay shows in r2.
- Sensitivities (declared, descriptive — `sensitivity.py`): conclusion is
  flat across deep_min {10,15,25 bp} (filtered mean 6.1→11.6 bp, always
  below unfiltered), rise_lb {1,3,5}, ADX thr {18,20,25}; VWAP anchor makes
  pullback WORSE (−9.7 bp vs EMA); next-bar-open fill does not flip the
  primary (+6.1 → +6.5 bp).

## 3. Option level (2026-03-02..07-21, 5 monthly expiries, real tape)

Full 128-cell table in `data/opt_results.txt` / `data/opt_cells.parquet`.

**Zero of 128 cells has a positive net mean at the inherited cost model
(stock 8% RT). Zero is also positive at 5%. At 2% (index-like costs applied
to what is a ~97% stock universe) the best cell is still −0.8%/trade.**

Primary pre-registered cell — R1 × deep_macd × 30m × 1d × slight_ITM
(n=509 episodes):

| metric | value |
|---|---|
| gross mean / median | **+0.59%** / −9.8% per trade |
| net mean (8% RT) | **−7.3%** |
| hit rate (net) | 36.0% |
| ex-top-3 (net) | −8.0% → gross ex-top-3 ≈ **−0.1%** (all gross is in 3 trades) |
| vs C1 unfiltered (net −14.6%) | **+7.3 pp lift, p=0.022** (alone-α 0.05: nominally clears) |
| vs C2 random-inside-regime (null −13.0%) | z=3.89, percentile 200/200 (clears) |
| C3 unconditional null | −14.3% → regime beta ≈ **+1.3 pp** only |
| modelled-exit rate | 5.3% (wins 3.3% / losses 6.4%; floor-method 3.9%) |
| monthly gross (Mar..Jul) | **+20.4%**, −3.1%, +3.7%, −9.1%, −7.9% (2 of 5 months positive) |

- The primary cell technically clears both pre-registered comparisons at
  its alone-α=0.05 — and still loses 7.3%/trade net. What it "wins" is
  being less bad than unfiltered entries and than random-inside-regime
  bars, both of which bleed 13–15% per trade at 1d net. Clearing controls
  is not edge when the whole neighbourhood is deeply negative.
- Across the 256-test grid: **5 tests pass Bonferroni — all are C2
  comparisons** (deep_macd 30m 1d both bands z 4.3/3.9; orb 2h slight-ITM;
  macd_plain 2h/1d slight-ITM), i.e. the timers select better-than-random
  bars. **Zero of the 128 regime-lift (C1) comparisons passes Bonferroni**
  (best p = 0.022, the primary). 14 tests pass BH-FDR q=0.10 — again
  C2-dominated.
- Direction of the regime's option-level effect is the OPPOSITE of spot:
  d_C1 is positive (deep_macd 30m: +6.5 to +9.9 pp across holds/bands)
  because unfiltered entries include counter-regime trades whose options
  decay brutally; the filter avoids some of that. It is damage limitation,
  not alpha, and it does not survive multiplicity.
- Theta dominates holds: universe-wide gross option return at random bars
  is −1.0% (2h) → −8.0% (1d) → −14.1% (3d). The owner's "short holds keep
  theta small per trade" is true per trade but false per edge: the spot
  moves selected are ~0.06% while the option bleeds ~8%/day.
- 1h timers at option level: nothing (n 109–219 per cell, no significant
  comparisons).

## 4. Honesty accounting

- Contracts selected causally at the entry bar (asof = signal-bar close);
  prefix-invariance verified; deleting post-asof bars changes nothing.
- Dedup at contract level (upstox > upstox_expired > fyers, MAX(volume),
  never summed); measured cross-broker dup rate 22.6% (June–July audit).
- Modelled exits (D3) used on 5–18% of exits depending on hold; the
  intrinsic-floor subset (biased low) is 3.9–7.4%; modelled exits are MORE
  frequent among losses than wins in the surviving family (6.4% vs 3.3%),
  so the walk-away censoring is not inflating the reported gross.
- IV present on 22.5% of evaluated entries, OI on 87% — flags carried on
  every mark (D5); no computation silently assumed them.
- The 2026-05-26 post-expiry timestamp anomaly is excluded by the DTE gate.
- 200-draw nulls cannot reach 1.95e-4 directly; the normal approximation
  is declared wherever a Bonferroni claim is made. The d_C1 bootstrap
  (1000 session blocks) resolves to p≈0.002 at best — no d_C1 claim is
  made beyond nominal levels anyway.
- PG discipline: 6 additional half-month COPY windows (2026-03..05),
  literal UTC bounds directly on `time`, sequential, PG at 2.3→3.3 GiB of
  6 GiB throughout, zero incidents; no full-history option extraction was
  attempted with MCX live.

## 5. Caveats (mandatory)

- **One macro regime.** Spot spans ~15 months of a broadly rising tape;
  the option window is 4.7 months of the same. The March-2026 concentration
  of the only positive gross cell is exactly the 1-period dependence that
  killed the fade strategy at adversarial review.
- Spread is assumed (tape has no bid/ask). At 0% cost the primary cell is
  +0.59% gross with ex-top-3 ≈ 0 — the conclusion does not hinge on the
  cost number.
- Survivorship: the underlying set is today's F&O catalog projected back.
- Option window ≠ spot window: the regime-lift sign flip between spot
  (negative) and option (positive) levels is confounded by window; within
  the option window both were measured identically, so the option-level
  comparison stands on its own.

## 6. What this buys the codebase

- `option_read_layer.py` + `bulk_eval.py`: an analysis-ready, defect-
  inheriting, equivalence-tested read layer over the option tape —
  promotion into `directional_options/` is mechanical and is the reusable
  artifact of this study regardless of the null strategy result.
- A measured, controls-backed answer to the owner's model: the HTF-confirm
  construct as specified (daily state + LTF timer + short option hold) is
  NOT a lane. The one live construct remains the deep-MACD pullback timer;
  if it is ever expressed again it must be expressed where theta cannot
  eat 8%/day — or not in long premium at all.
