# Gann time cycles — practice-faithful re-test (part C)

**Date:** 2026-07-21 · **Scope:** research only, nothing wired, no lane code touched.
**Code:** `backend/gann_tp_delta/research/practice/` (new). Shipped Gann code read-only.
**Data:** 221 daily frames cached once to parquet from `underlying_spot_candles` (30-minute
source resampled to IST daily, via the shipped `daily_data.fetch_daily_frame`). One bounded
query per instrument, literal UTC bounds on `time`. No further DB access.

---

## 1. Fidelity gap list — shipped implementation vs documented practice

| # | Aspect | Shipped code | Documented practice | Verdict |
|---|---|---|---|---|
| 1 | Counting unit | calendar days from anchor (`cycles.project_cycle_dates`, `anchor.pivot_date + timedelta(days=cycle.days)`) | calendar days ("true understanding of cycles is obtained from the calendar days") | **FAITHFUL** |
| 2 | Anchor causality | pivot emitted with `confirmed_date` = pivot + `right` sessions; projection dropped if it precedes it (`cycles.causal_anchors`, `project_cycle_dates`) | practice does not defend against this at all | **STRICTER** (correctly) |
| 3 | Anchor *selection* | **every** confirmed 5-bar swing pivot (`causal_anchors(left=5, right=5)` in `cycle_prominence.score_instrument`) — 60–250 anchors per instrument | only "the most recent, obvious and significant" extremes; a handful | **DIFFERENT — and looser, not stricter.** The single most material gap. See §4. |
| 4 | Cycle repetition | one repetition only: `anchor + 1×days`. `cycles.next_projection` supports `max_repeats=8` but the *scoring* path never uses it | 30/60/90/120 from the same high are all counts of the 30-day cycle | **DIFFERENT** |
| 5 | Tolerance unit | ±3 **trading sessions**, flat (`TOLERANCE_SESSIONS = 3`) | calendar days; no standard convention; only explicit published rule is ±1 short / ±3 intermediate / ±7 long | **DIFFERENT** |
| 6 | Turn definition | confirmed 5-bar swing pivot of ≥ median magnitude (`identify_turns`) | 2-bar bar-reversal at the cycle end; and popularly also "consolidate or accelerate" | **STRICTER** (defensibly — the loose version is unfalsifiable) |
| 7 | Standalone vs confluence | standalone: cycle date → turn? nothing else required | never standalone; cycle date **AND** price level **AND** confirmation; 3–4 tools converging | **DIFFERENT** |
| 8 | Cycle set actually scored | `testable_cycles(span, 20)` requires `days × 20 ≤ history span` → **≤92 days on indices, nothing at all on stocks** (`run_cycle_mapping.py`) | 90 / 144 / 180 / 360 are the named cycles | **DIFFERENT — a bug-grade gap.** It conflates "20 repetitions of the cycle" with "20 independent observations". Observations come from many *anchors*, not from repetitions of one count. The cycles practitioners actually name were never scored. |
| 9 | Timeframe | daily bars | daily and higher | **FAITHFUL** (the legacy 15-minute-bar `geometry.time_cycles` is not; that path is not used by the mapper) |
| 10 | Prominence per instrument | statistical, FDR-controlled | eyeballed; no published statistical test exists | **STRICTER** |
| 11 | Base-rate / coverage guard | none | none | **absent in both** — added here |

---

## 2. Arms re-run, and why practice implies each

All nine arms share the shipped guards, re-implemented so each arm is its own family:
disjoint-window thinning, minimum 20 non-overlapping in-sample observations, empirical null,
in-sample era halves, 70/30 OOS holdout scored once, global BH-FDR at q=0.10 across the whole
grid of that arm, and **its own matched random-length placebo through the identical pipeline**.
Two guards were added: an **exact null** (see §3) and a **coverage guard** (a cell whose null
already exceeds 0.50 is reported `UNTESTABLE_BY_COVERAGE`, not scored).

| Arm | Change from shipped | Practice basis |
|---|---|---|
| A0 | shipped config, on the widened cycle set (gap 8) | control |
| A1 | tolerance ±3 **calendar** days | gap 5 (unit) |
| A2 | ±1 / ±3 / ±7 calendar by cycle length | gap 5 (only published convention) |
| A3 | anchors = 41-session extremes, top 40 % magnitude; repeats 1–4 | gaps 3, 4 |
| A4 | anchors = running all-time high/low only; repeats 1–8 | gap 3, narrowest reading |
| A5 | turn = 2-bar bar-reversal off a 5-day extreme | gap 6 |
| A6 | repeats 1–4 from every pivot | gap 4 |
| A7 | cycle date **AND** price within 1 ATR of a Square-of-Nine level from the same anchor | gap 7 |
| A8 | A3 anchors **AND** A7 price confluence — the full documented construct | gap 3+7 |
| A9 | convergence box: only dates where enough distinct (anchor, cycle) projections agree | gap 7, "3–4 tools converge" |

Placebo construction is matched per arm. For A7/A8 the placebo randomises **both** the cycle
lengths **and** the Square-of-Nine level geometry (each level displaced by a uniform draw
inside its own ±22.5° gap, preserving level density) — otherwise the arm would measure whether
price levels work, not whether cycles do.

---

## 3. A measurement bug found and fixed in the shipped null

The shipped null (`cycle_prominence.turn_window_coverage`) is the union of ±3-**session**
windows around turns. That is exact **only** when the projection window is itself exactly ±3
sessions. Under any calendar-day tolerance the projection window is narrower, so the shipped
null over-states the chance rate and the test becomes conservative. This is the mechanism
behind the first run's odd signature — *fewer* raw p ≤ 0.05 cells than chance predicts.

`practice_harness.exact_null` replaces it: the window is constructed the identical way for
every candidate centre session in the region, so the null is exact by construction under every
tolerance convention.

**It did not rescue the result.** After the fix, every arm still shows a small **negative**
mean lift — and shows it *equally in the genuine and the placebo arm*:

| Arm | genuine mean lift | placebo mean lift |
|---|---|---|
| A0 | −0.0120 | −0.0119 |
| A2 | −0.0278 | −0.0301 |
| A6 | −0.0309 | −0.0300 |

So the residual offset is a property of the pipeline (projections sit at a fixed lag from
clustered anchors, and turns cluster in time), not of Gann's numbers. **The correct contrast is
therefore genuine-vs-its-own-placebo, which cancels the offset — not hit-rate-vs-null.**

---

## 4. Results per arm, with corrected thresholds

### 4.1 Grid-wide (221 instruments), BH q = 0.10

| Arm | tested cells | BH rank-1 threshold (p must be ≤) | best genuine p | best placebo p | raw p≤0.05 obs / expected | **PROMINENT genuine** | **PROMINENT placebo** |
|---|---|---|---|---|---|---|---|
| A0 shipped replica | 968 | 1.03e-4 | 0.0150 | 0.0347 | 8 / 48.4 | **0** | **0** |
| A1 calendar unit | 3552 | 2.8e-5 | 0.0072 | 0.0199 | 7 / 177.6 | **0** | **0** |
| A2 scaled tolerance | 4465 | 2.2e-5 | 0.0101 | 0.0154 | 12 / 223.2 | **0** | **0** |
| A3 major anchors | 502 | 1.99e-4 | 0.0056 | 0.0056 | 12 / 25.1 | **0** | **0** |
| A4 extreme anchors | 5961 | 1.7e-5 | 0.0200 | 0.0252 | 7 / 298.1 | **0** | **0** |
| A5 bar-reversal turn | 3414 | 2.9e-5 | 0.0070 | 0.0168 | 15 / 170.7 | **0** | **0** |
| A6 repeats | 6863 | 1.5e-5 | 0.0217 | 0.0292 | 5 / 343.2 | **0** | **0** |
| A7 price confluence | 1100 | 9.1e-5 | 0.0168 | **0.0024** | 9 / 55.0 | **0** | **0** |
| A8 major + confluence | **0** | — | — | — | — | — | — |
| A9 convergence (cov≤10 %) | 5 | 2.0e-2 | 0.0949 | — | 0 / 0.2 | **0** | **0** |

No cell in any arm — genuine or placebo — was rejected by BH at q=0.10. In A7 the **placebo's**
best p (0.0024) beats the genuine arm's (0.0168), repeating the pattern from the original run.
A8 — the full documented construct (significant anchors AND price confluence) — produced **zero
scorable cells across all 221 instruments**: restricting to major extremes and then requiring
price confluence leaves fewer than 20 non-overlapping observations everywhere.

### 4.2 The decisive contrast: each arm against its own placebo

Lift = hit rate − exact null. Welch on cell-level lift, plus a per-instrument paired mean so
cycle-length composition differences between the two arms cannot drive the contrast.

| Arm | Δ mean lift (genuine − placebo) | Welch t | Welch p | paired Δ | instruments with genuine > placebo |
|---|---|---|---|---|---|
| A0 | −0.00017 | −0.07 | 0.947 | +0.0019 | 55 % |
| A1 | +0.00116 | 0.66 | 0.508 | +0.00005 | 54 % |
| A2 | **+0.00234** | **1.58** | **0.114** | +0.0028 | 54 % |
| A3 | +0.00782 | 1.35 | 0.177 | +0.0021 | 50 % |
| A4 | −0.00090 | −0.87 | 0.383 | +0.0007 | 53 % |
| A5 | +0.00377 | 1.25 | 0.210 | +0.0051 | 54 % |
| A6 | −0.00092 | −0.91 | 0.364 | 0.00000 | 51 % |
| A7 | −0.00434 | −1.04 | 0.299 | −0.0004 | 48 % |

**No arm beats its own placebo.** The best is A2 at +0.23 percentage points of hit rate,
t = 1.58, two-sided p = 0.114 — and that is *before* any correction for having run eight arms
(Bonferroni across arms: 0.114 × 8 = 0.91).

### 4.3 The confluence arm measures the price level, not the cycle

On indices only, A7 is the one arm with a **positive** mean lift: **+0.0205 genuine**. But the
matched placebo — random cycle lengths with randomised Square-of-Nine geometry of the same
density — scores **+0.0336**, i.e. *higher*, with a better best-p (0.0024 vs 0.0170) and 5
era-stable-and-OOS-confirming cells against the genuine arm's 0. Requiring price to be near a
level does add hit rate. It adds it whether or not the date is a Gann cycle date, and whether
or not the levels are Gann levels. This is the skeptics' prediction, confirmed on our data.

### 4.4 The convergence box is arithmetically self-defeating

Pooling all cycles × all pivot anchors × repeats 1–4 with the ±1/±3/±7 tolerance, the projected
windows tile the calendar. To get projected coverage down to a level where a hit rate is
informative you need an absurd agreement threshold:

| Instrument | agreement count needed for ≤25 % coverage | for ≤10 % coverage |
|---|---|---|
| NIFTY | 210 | 229 |
| BANKNIFTY | 219 | 244 |
| SENSEX | 210 | 226 |
| NATURALGAS | 221 | 237 |
| RELIANCE (1.3 y) | 63 | 79 |

At the practitioner-quoted "3 or 4 tools converging", coverage is **~100 %** of the calendar —
the statement "a turn occurred near a convergence" carries no information. Even at the
pre-declared ≤10 % coverage threshold only 5 of 221 instruments reach 20 disjoint convergence
dates in-sample; best genuine p = 0.0949; the placebo produced zero scorable cells.

---

## 5. Positive control — and the reason the null is not a refutation

A null is worthless unless the instrument can detect the thing. I planted a known calendar
cycle in a 1,250-session synthetic series and ran it through the identical scoring path,
sweeping the cycle's amplitude, its *shape*, and the jitter between the projected grid and the
realised turn. Target-cycle p-values:

**Sharp-cusp cycle (turn exactly on the grid), planted 90 d:**

| amp \ jitter | 0 d | 2 d | 4 d | 7 d | 12 d | 20 d |
|---|---|---|---|---|---|---|
| 4 % | 0.29 | 0.33 | 0.35 | 0.35 | 0.57 | 0.22 |
| 6 % | 0.0019 | 0.010 | 0.028 | 0.0087 | 0.015 | 0.21 |
| 10 % | **3.1e-7** | 0.0023 | 0.0037 | 0.0066 | 0.0056 | 0.0075 |

**Smooth sinusoidal cycle (the realistic shape), planted 90 d:**

| amp \ jitter | 0 d | 2 d | 4 d | 7 d | 12 d | 20 d |
|---|---|---|---|---|---|---|
| 4 % | 0.24 | 0.028 | 0.20 | 0.10 | 0.068 | 0.05 |
| 6 % | 0.12 | 0.077 | 0.12 | 0.24 | 0.22 | 0.012 |
| 10 % | 0.032 | 0.0016 | 0.014 | 0.19 | 0.29 | 0.013 |

Planted 144 d is worse in every configuration (best cusp p = 0.0084, best sine p = 0.085).

Three conclusions, all of which bear directly on how the null should be read:

1. **Only the sharp-cusp, ≥10 %-amplitude, zero-jitter case reaches the BH rank-1 threshold**
   of ~2e-5 that the 221-instrument grid demands. A genuinely real 6 %-amplitude 90-day cycle
   on one index produces p ≈ 0.002 — which **fails** the grid-wide correction and is reported
   `TESTED_NOT_PROMINENT`, exactly as everything else was.
2. **A smooth cycle is essentially invisible**, even at 10 % of price. A smooth extremum plus
   noise locates its own turning point with an error of many sessions, so no ±1/±3/±7 tolerance
   can catch it. Market cycles, if they exist, are smooth, not cusped.
3. **Scoring across all pivots dilutes a real cycle to near-undetectability.** With ~120
   anchors of which ~14 sit on the true grid, ~88 % of projections are randomly phased. This is
   gap 3, and it is a mathematical, not empirical, limitation of the shipped design. A pure
   planted series with *no* noise pivots has only ~19 anchors in five years — below the
   20-observation minimum. **The shipped design cannot pass its own gate on a perfect cycle.**

---

## 6. Power — what the data can and cannot support

Median non-overlapping in-sample observations, arm A2 (genuine):

| class | ≤45 d | 46–90 d | 91–180 d | 181–400 d |
|---|---|---|---|---|
| index (~1,270 bars, 5.1 y) | 94 | 81 | 75 | 40 (only 17 % of cells scorable) |
| commodity (~1,290 bars) | 77 | 70 | 61 | 33 (66 % scorable) |
| stock (~321 bars, 1.3 y) | 24 | 19 (47 % scorable) | 14 (**1 %** scorable) | 2 (**0 %**) |

- **Indices and the four deep commodities** are the only place any cycle is genuinely testable,
  and even there the 181–400 day band is mostly unscorable.
- **Stocks (209 of 221 instruments) are underpowered above ~45 days and untestable above ~90.**
  Everything they contribute to the grid is multiplicity burden without information — they
  raise the BH rank-1 threshold from ~4e-4 (index-only sub-grid) to ~2e-5.
- **CRUDEOIL** (~4 months) did not clear the 120-bar minimum and is absent entirely.
- **Annual and longer cycles are untestable in principle** on this history, as the shipped
  `MAX_TESTABLE_DAYS` already asserted.
- Restricting to an **indices-only sub-grid** (the pre-committed, better-powered choice —
  BH rank-1 threshold 2.3e-4 to 9.8e-4 depending on arm) still yields **zero rejections in
  every arm, genuine and placebo**; A2 on indices produced **0 cells at raw p ≤ 0.05 out of
  303**, against a chance expectation of 15.

---

## 7. Verdict

**Did any practice-faithful arm beat its own placebo? No.**

- Best arm-level result: A2 (scaled calendar tolerance), Δ lift **+0.0023**, Welch t = 1.58,
  p = 0.114 uncorrected, **0.91 after Bonferroni across the eight arms**.
- Zero cells reached BH significance at q = 0.10 in any arm, in either the genuine or the
  placebo grid. Corrected thresholds recomputed per arm are in §4.1 (1.5e-5 to 2.0e-2).
- The one arm with positive lift on indices (A7 price confluence, +0.0205) is **beaten by its
  own matched placebo** (+0.0336). The lift comes from the price level, not the cycle date.
- The convergence-box construct is arithmetically empty at practitioner settings: ~100 %
  calendar coverage at "3–4 tools agree".
- The full documented construct (A8: significant anchors AND price confluence) is **not
  scorable at all** on this data — zero cells out of 19,006 reached 20 non-overlapping
  observations.

**But this is not a refutation of Gann time cycles, and it must not be reported as one.**
The positive control shows the harness — including the shipped one — would fail to detect a
*real*, 6 %-amplitude, perfectly punctual 90-day cycle at the grid-wide correction, and would
fail to detect a smooth cycle of any amplitude we tested. The original null result was
underpowered by construction, chiefly through gap 3 (all pivots as anchors, which dilutes any
true cycle by ~90 %) and gap 8 (the cycles practitioners actually name were never scored).

The honest summary: **we tested Gann time cycles under nine practice-implied configurations on
the deepest data we hold, and found nothing that beats a random-length control. On this data
the method is not measurable at the fidelity practice implies, and no configuration we can
construct is both faithful to the method and adequately powered.**

## 8. What the data cannot settle

1. **Any cycle > ~180 days on any instrument**, and anything > 90 days on stocks. ~7 to ~20
   non-overlapping observations supports detecting only a very large effect.
2. **Annual, decennial, and master cycles** — zero observations. Untestable in principle.
3. **The confluence construct as documented** — needs an anchor set small enough to be
   "significant" and an observation count large enough to test. Those are mutually exclusive on
   5.1 years. This requires ~20+ years of daily history per instrument, which we do not have
   and cannot obtain from the current broker path.
4. **Smooth cycles of any length.** The turn-location error of a smooth extremum under realistic
   noise exceeds every tolerance convention in the literature. Detecting these needs a spectral
   or phase-coherence method, not a date-window hit test. That is a different instrument, not a
   different parameter.
5. **Whether the anchor-significance filter matters**, because no source supplies one. Our
   parameterisation (41-session pivot, top-40 % magnitude) is our invention; a different one
   might behave differently, and sweeping it would only add multiplicity.
6. **Intraday cycle work** — not attempted here, and per the external research it has no basis
   in the method as Gann specified it.

## 9. Recommendation

Do not wire anything. If the owner wants this question closed rather than parked, the only
instrument with adequate power on 5.1 years of index data is a **phase-coherence / spectral
test on indices only** — asking whether turn *timing* concentrates at any period, rather than
whether a projected date hits — with the same placebo and FDR discipline. That is a new
measurement, roughly a day's work, and it would settle the smooth-cycle case that the date-hit
test provably cannot reach.

---

### Artefacts

- `backend/gann_tp_delta/research/practice/fetch_frames.py` — one-time bounded parquet cache
- `backend/gann_tp_delta/research/practice/practice_harness.py` — parameterised scoring, exact null, coverage guard
- `backend/gann_tp_delta/research/practice/run_practice_arms.py` — the nine arms + placebos
- `backend/gann_tp_delta/research/practice/analyse_arms.py` — arm-vs-placebo contrast
- `backend/gann_tp_delta/research/practice/detection_limit.py` — positive control / detection sweep

Result files (scratch, not committed): `cells.parquet` (19,006 cells × 9 arms × 2 arms-of-arm),
`arm_report.json`, `analysis.json`, `by_class.json`, `detection_limit.parquet`.
