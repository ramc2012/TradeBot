# Regime duration — is the owner's premise true? (study 1 of the cascade series)

**Date:** 2026-07-21  ·  **Scope:** premise test only. Nothing wired, no flags, no lane code touched.
**Code:** `backend/directional_options/research/cascade/regime_*.py`, `test_regime_causality.py`
**Raw output:** `backend/directional_options/research/cascade/regime_results.txt`

The claim under test, from the owner's model:

> *"Moves happen for small time and consolidation dominates … then sustained large move happens — move matures/saturates — stock goes to consolidation mode."*

Two separable assertions:
- **(P1)** consolidation occupies most of the calendar;
- **(P2)** the large moves are *brief* — they deliver the year's range in a small slice of time.

**Verdict: P1 holds. P2 is false in every market class.** Big moves are the *longest* episodes, not the fastest ones. That matters for the rest of the series: it removes the "must be in early or miss it" urgency that motivates the small first tranche, and it makes the carry cost of a long hold, not entry latency, the binding constraint.

---

## 1. Regime definition (fixed a priori, and why it was not swept)

Two independent operationalisations were written down in `regime_defs.py` **before** any statistic was computed, together with a declared one-parameter sensitivity grid each. Nothing was re-chosen after seeing a result; the full grid is reported below including the case where it contradicts the headline.

**A — Causal classifier: Wilder ADX(14), threshold 25.** Canonical published trend-strength gauge with a threshold Wilder fixed in 1978 — not fitted here. It is one of the tools the owner named, and it is strictly recursive, so the label at session *t* uses only sessions ≤ *t*. Declared grid: {20, 25, 30}.

**A2 — Causal cross-check: Kaufman efficiency ratio over 20 sessions, cut 0.50.** ER = |net move| / |total travel| over the trailing window. 0.5 is a natural constant ("price kept at least half of what it travelled"), not a fitted one. Added as a second, unrelated construction because the ADX verdict turns out to be threshold-sensitive; showing that with a second lens is more honest than picking the flattering ADX cut. Declared grid: {0.3, 0.5, 0.7}.

**B — Descriptive swing decomposition (EX-POST, description only, never an entry rule).** Directional-change decomposition of daily closes with per-instrument reversal threshold θ = 3 × median(daily true range / close). 3 ATRs is the smallest round multiple that clears the honest round-trip option cost floor (−5..−6% of premium on a barrier-managed multi-session long-premium trade) established in the 2-3 day study. Declared grid: multiplier {2, 3, 4}.

**Causality is tested, not asserted.** `test_regime_causality.py` runs prefix invariance (recompute ATR/DI/ADX on rows 0..k, assert row *k* is identical at rtol 1e-12) at five cut points on real NIFTY/BANKNIFTY series, plus a future-bar perturbation test (scale every bar after *t+5* by 1.35 and assert today's ADX is bit-identical). All pass.

---

## 2. Data

| class | instruments | sessions | span | median sessions/series |
|---|---|---|---|---|
| index | 6 | 6,404 | 2021-06-21 → 2026-07-20 | 1,254 |
| commodity (MCX) | 5 | 5,187 | 2021-06-21 → 2026-07-20 | 832 |
| stock | 209 | 66,468 | **2025-03-28** → 2026-07-20 | **320** |

Built from 30m `underlying_spot_candles` aggregated to IST sessions (NSE 09:15–15:15, MCX 09:00–23:30), reusing the 2-3 day study's session convention. Pre-2025 bars were pulled fresh (`regime_extract.py`); 2025+ bars reuse `../panel_2d3d/data/spot_*.csv`. Every PG range predicate bounds `time` directly with literal UTC timestamps, quarterly windows, no function on the partitioning column.

Series are **cut** (not smoothed) at any |session return| > 20% — those are corporate actions (SIEMENS, TMPV, VEDL demergers) or bad prints, never tradeable moves; 6 cuts survive in the kept universe. Series shorter than 150 sessions are dropped (19 of 240), which removes the late-added GOLD/NICKEL/CRUDEOIL feeds and the corrupted GOLD print (a 277× "return" on 2026-07-17).

---

## 3. Time in each regime

**Causal ADX(14) ≥ 25 — % of sessions trending**

| class | trending | consolidating |
|---|---|---|
| index | **33.7%** | 66.3% |
| commodity | **39.7%** | 60.3% |
| stock | **34.9%** | 65.1% |

Per-instrument spread (stock, n=209): min 2.7%, p25 25.3%, median 34.1%, p75 43.5%, max 74.1% — the class average is not hiding a bimodal population, but individual names differ a lot.

**Declared ADX sensitivity — this is where the verdict is fragile**

| threshold | index | commodity | stock |
|---|---|---|---|
| ADX ≥ 20 | 59.6% | 58.7% | 55.7% |
| ADX ≥ 25 | 33.7% | 39.7% | 34.9% |
| ADX ≥ 30 | 17.0% | 26.2% | 19.9% |

At Wilder's 25 and at 30 consolidation dominates; **at the also-commonly-quoted 20 it flips** and "trending" becomes the majority. The label share alone cannot settle P1.

**Causal cross-check — efficiency ratio(20)**

Median ER20 is 0.19–0.21 in every class: the typical 20-session window retains about a fifth of the distance it travelled. Share of sessions with ER ≥ 0.5: index 9.4%, commodity 7.7%, stock 7.6% (ER ≥ 0.3: ~30–32%; ER ≥ 0.7: ~1–2%).

Agreement of the two lenses (% of all sessions):

| class | both trend | ADX only | ER only | **both chop** |
|---|---|---|---|---|
| index | 6.5 | 26.9 | 2.9 | **63.6** |
| commodity | 6.1 | 33.2 | 1.6 | **59.1** |
| stock | 6.1 | 28.0 | 1.5 | **64.5** |

Two unrelated constructions agree that ~60–65% of all sessions are chop by both measures, and that genuinely efficient directional travel (ER ≥ 0.5) is rare — under 10% of sessions. **P1 holds.**

---

## 4. How long does a move last once it starts

Run lengths of consecutive same-label sessions (causal ADX ≥ 25), in sessions:

| class | label | n runs | p25 | median | p75 | p90 | max |
|---|---|---|---|---|---|---|---|
| index | trending | 131 | 5 | **12** | 22.5 | 35 | 78 |
| index | consolidating | 134 | 9 | **20** | 40 | 68 | 162 |
| commodity | trending | 80 | 5 | **17.5** | 38.5 | 55 | 132 |
| commodity | consolidating | 78 | 15 | **27.5** | 55.5 | 84 | 189 |
| stock | trending | 1,102 | 6 | **14** | 26 | 42 | 142 |
| stock | consolidating | 1,202 | 9 | **23** | 48 | 73 | 284 |

Share of trending runs lasting ≥ k sessions — index 78% ≥5, 59% ≥10, 31% ≥20; commodity 81/64/48%; stock 82/65/36%.

Consolidation runs are ~1.6–1.7× longer than trending runs, and roughly equal in count — that is the mechanism behind the 1/3-vs-2/3 time split. But a *median trending run of 12–17 sessions* is not "small time": it is two to three-and-a-half weeks.

The ex-post swing decomposition agrees: qualifying moves (|size| ≥ θ) run a median 13 sessions (index), 15.5 (commodity), 18 (stock), and there are only ~9–12 of them per instrument-year — roughly one per month.

---

## 5. Move size

Close-to-close over each causal ADX trending run (absolute %): index median 1.44, p90 4.45; commodity median 4.02, p90 16.6; stock median 2.87, p90 10.45. Note the *signed* medians are ~0 for stocks and +0.77% for the index — an ADX trending run is frequently a strong move that ends near where it started, because ADX is direction-blind and catches whipsaw.

Ex-post qualifying swings (pivot-to-pivot, θ = 3× median TR): index median **6.1%** (p75 9.1, p90 14.6), commodity median **10.7%** (p90 34.6), stock median **13.1%** (p90 28.8). Up/down counts are balanced within 2% in every class — there is no long-side asymmetry in the move population itself.

---

## 6. Concentration — and the refutation of P2

Per instrument-year (≥100 sessions; 468 instrument-years):

| class | top-3 moves: share of net swing travel | sessions they occupy | travel-per-time ratio |
|---|---|---|---|
| index | 50.4% | 50.4% | **1.00** |
| commodity | 51.9% | 53.4% | **0.97** |
| stock | 69.7% | 62.6% | **1.11** |

The top 3 moves of a year do deliver about half (index/commodity) to two-thirds (stock) of all net directional travel — but they occupy **about the same fraction of the calendar**. The travel-per-time ratio sits at 0.97–1.15 for every top-N cut from 1 to 10. There is essentially **no time concentration**: you do not capture half the year's movement in a small slice of the year.

Why: **big moves are long, not fast.**

| class | spearman(size, duration) | spearman(size, velocity) | median velocity by size quartile Q1→Q4 (%/session) |
|---|---|---|---|
| index | **+0.682** (p=3.7e-46) | −0.206 (p=1.8e-04) | 0.61 → 0.59 → 0.42 → 0.47 |
| commodity | **+0.470** (p=3.9e-15) | +0.238 (p=1.5e-04) | 0.67 → 0.64 → 0.69 → 1.49 |
| stock | **+0.554** (p=1.3e-189) | +0.047 (p=2.3e-02) | 0.71 → 0.79 → 0.79 → 0.83 |

Six correlation tests; Bonferroni α = 0.05/6 = 8.3e-3 — the size↔duration results survive by tens of orders of magnitude, and so does the *sign* on velocity: for indices the biggest moves are the **slowest** per session (median 0.47%/session in Q4 vs 0.61 in Q1); for stocks velocity is flat across size quartiles (0.71→0.83, a 17% spread against a 4× spread in duration). Only MCX shows a genuine size-velocity link (Q4 1.49%/session), and that class carries a roll-jump caveat (§8).

The swing decomposition's own "time inside a move" number (96–99%) is **degenerate and should not be quoted**: a directional-change decomposition tiles the whole timeline by construction, so it cannot measure the consolidation share. It was pre-registered, it failed at that job, and it is reported here as a failure rather than quietly replaced. It remains valid for size, duration and concentration, which is what it is used for above.

---

## 7. Premise verdict, per class

| class | P1 — consolidation dominates? | P2 — big moves are brief? |
|---|---|---|
| index | **YES** — 66% of sessions non-trending (ADX 25); 63.6% chop on both lenses; consolidating runs 1.7× longer | **NO** — top-3 moves take 50% of the year's sessions to deliver 50% of its travel; biggest moves are the *slowest* (ρ(size,velocity) = −0.21) |
| commodity | **YES** — 60% non-trending; 59.1% chop on both lenses | **NO on the time test** (ratio 0.97) though this is the one class where the largest moves are genuinely faster (Q4 1.49%/session); roll-jump caveat applies |
| stock | **YES** — 65% non-trending; 64.5% chop on both lenses | **NO** — top-3 take 63% of sessions for 70% of travel (ratio 1.11); velocity is flat across size quartiles |

Caveat carried in full: at ADX ≥ 20 the trending share is 56–60% and P1 *inverts*. P1 is upheld on the canonical threshold, on the stricter one, and on the independent ER lens — but it is not threshold-proof, and any downstream claim resting on "we only trade a third of the time" should be quoted with that.

### What this implies for the rest of the cascade series (not tested here)
1. The tradeable object is a **~13–18 session** episode occurring **~1×/month per instrument**, with a median size of 6% (index) / 13% (stock) — not a multi-day burst. The 2-3 day horizon the previous study measured is *shorter than the median move*.
2. Because moves are long rather than fast, **entry latency is cheap and carry is expensive** — consistent with the earlier fill-insensitivity finding (≤1.2pp for an extra 30m lag) and with the index deep-ITM monthly being the only holdable vehicle. A stage-1 tranche bought early buys little extra travel and pays extra theta.
3. ADX ≥ 25 alone flags ~35% of sessions as trending with a median *signed* run outcome near zero for stocks — a stage-2 confirm built on ADX must be shown to beat that, which is exactly what study (2) has to measure against matched controls.

---

## 8. Data limits (read before quoting any number)

1. **Stocks are one era.** 2025-03-28 → 2026-07-20, ~320 sessions/name. Every stock-class number is single-regime and cannot be split into non-overlapping years. Index/commodity history is 5.1 years.
2. **MCX roots are not roll-adjusted.** Continuous root series splice contracts, so a minority of "moves" contain roll jumps (one commodity ADX run shows a 260% travel). The commodity size and velocity tails are inflated by an unknown amount.
3. **Known feed contamination.** Fyers cross-symbol tick contamination (2026-07-20 note) leaves corrupt rows in stock spot; daily aggregation absorbs most, and the |ret|>20% series cut removes the worst, but small distortions remain in mid-July 2026.
4. **Corporate actions cut, not adjusted.** Spot series are unadjusted; demergers/splits appear as breaks and are cut, which shortens 6 series and drops 19 series below the 150-session minimum.
5. **Daily grain.** Regimes are labelled on sessions built from 30m bars. The owner's stage-1 ("small timeframe") is intraday; nothing here measures sub-session regime structure.
6. **ADX warm-up** consumes 28 sessions per series; NIFTYNXT50 and the post-break stock segments contribute proportionally less.
7. **Multiplicity.** Descriptive grids: 3 classes × (3 ADX thresholds + 3 ER cuts + 3 θ multipliers) = 27 reported cells, all reported, none selected. Six inferential tests (size↔duration and size↔velocity per class); Bonferroni α = 8.3e-3; all six pass raw and corrected, and the two headline ones by >40 orders of magnitude.
8. **Ex-post components are labelled.** The swing decomposition, θ calibration and the concentration table use full-sample information and are descriptive only. Nothing in this study is, or may be used as, an entry rule.
