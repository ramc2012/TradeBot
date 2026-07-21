# MACD-divergence setup — generalisation study (Part 2)

**Date:** 2026-07-21 · **Scope:** RESEARCH ONLY — no lane code, no flags, nothing wired.
**Origin:** the owner's PNB worked example (see `divergence_pnb_case_study_2026-07-21.md`).
**Code:** `backend/directional_options/research/divergence/`
`div_defs.py` (a-priori element definitions) · `div_build.py` (episodes + controls) ·
`div_test_causality.py` (prefix invariance) · `div_analyse.py` (spot results) ·
`div_hourly.py` (element e) · `div_opt_extract.py` (option tape, **no moneyness band**) ·
`div_options.py` (strike grid) · `div_positioning.py` (element f + coverage audit).
Raw output: `div_results.txt`, `opt_results.txt`, `pos_results.txt`,
`data/{tests,opt_tests,positioning_tests,combined_tests}.csv`.

---

## 0. Verdict first

**The setup does not beat its controls.** Across 116 registered comparisons, **nothing
survives Bonferroni (0 at p<0.05)** and only **three survive BH q<0.10** — one of which is
a *negative* result and the other two are in the thinnest, most strike-selected option
cells and are carried by the **bare crossover, not by divergence**.

The single most important finding is a definitional one:

> **The owner's PNB signal is NOT captured by the strict textbook divergence definition.**
> Comparing the last two confirmed pivot lows, PNB's 2026-05-25 crossover shows **no**
> divergence (05-18 low 98.50 vs 05-05 low 105.45 — a lower low, but MACD −2.734 vs
> −0.834 is a *lower* MACD low too). It is a divergence only against the **earlier**
> 04-02 pivot (99.79 / MACD −4.925). Our alternate `div_any` definition — most recent
> confirmed pivot low vs *any* earlier confirmed pivot low in the lookback — reproduces
> the owner's chart exactly, at both 05-25 and 07-17.

So `div_any` is the definition that matches the worked example, and it was chosen
*after* seeing that example. It is therefore reported as the owner-matching arm, but
**it is in-sample with respect to PNB and its result carries less weight than the strict
`div` arm** — which is the honest cost of a study that starts from one chart.

And `div_any` shows nothing: `cross_divany` P(large) **0.290** vs unconditional **0.300**.

---

## 1. Element definitions (a priori, causal, with confirmation lag)

All fixed in `div_defs.py` before any measurement. Every predicate is causal —
value at daily session *i* depends only on sessions ≤ *i*. Proven mechanically by
`div_test_causality.py` (prefix-invariance, rtol **1e-12**, 10 underlyings × 6 truncation
points): **PASS**.

| # | Element | Predicate | Confirmation lag | Actionable |
|---|---------|-----------|------------------|-----------|
| — | **Pivot** (shared primitive) | fractal low: `low[i] < low[i-3..i-1]` and `low[i] <= low[i+1..i+3]` (strict left / non-strict right, so a flat double bottom resolves to its first bar) | **3 sessions** | close of *i+3* |
| a1 | **MACD crossover** | `MACD(12,26,9)`: `macd−sig > 0` and prior `<= 0` on the daily | 0 | open of *s+1* |
| a2 | **Divergence** `div` | the **two most recent** pivot lows confirmed by *s* (`p+3 <= s`), separated 5–40 sessions, most recent within 40 sessions: `low[p2] < low[p1]·(1−0.1%)` **and** `macd[p2] > macd[p1]` | inherits the 3-session pivot lag | open of *s+1* |
| a2′ | **Divergence** `div_any` *(alternate — matches the owner's chart)* | as above but `p1` = **any** earlier confirmed pivot low in the 40-session lookback | same | open of *s+1* |
| b | **Crossover strength** | six scale-free measures read **at the cross session**: `str_hist` = hist/ATR14 · `str_slope` = Δhist/ATR14 · `str_below0` = −MACD/close · `str_thrust` = Δclose/ATR14 · `str_volz` = 20-session volume z · `str_div_macd` = ΔMACD across the divergence pivots / ATR14 | 0 | — (conditioner) |
| c | **Higher low** `HL` | a confirmed pivot low *p* after the cross, within 25 sessions, with `low[p] > low[p_prev]·(1+0.1%)` | **3 sessions** | open of *p+4* |
| d | **Trendline break** | descending line through the two most recent confirmed pivot highs (span ≤ 60 sessions, `high[i2] < high[i1]`); break when `close[t] > line(t)` and `close[t−1] <= line(t−1)`; element = a break at *s* or in the prior 10 sessions | 3 sessions (pivot) | open of *s+1* |
| e | **Hourly early clue** | hourly bars = consecutive 30m pairs aligned to 09:15 IST, never straddling a session; hourly MACD(12,26,9) bull cross | 0 | open of the next 30m bar |
| f | **Option positioning** | at the 15:15 IST snapshot of the session before entry, on strikes within ±5% : total CE/PE OI, 5-session ΔOI, PCR(OI), CE volume/OI | 0 | — (conditioner) |

**Outcome (fixed a priori, same numbers as the cascade study so results are comparable):**
triple barrier on the 30m tape from the entry open — target **+2.0 daily ATR**, stop
**−1.0 ATR** (stop wins ties), cutoff **15 sessions** (widened from the cascade's 10
because the established median qualifying move takes ~12 sessions). Plus fixed-horizon
spot returns at 5/10/15 sessions. Episode clustering: triggers ≤5 sessions apart on one
name = ONE observation. Long-only (the owner's setup is bullish).

**Universe:** 211 stocks + 6 indices, 69,377 underlying-sessions, 2025-03-28 → 2026-07-20
(stocks ~15 months, indices longer). One broadly-rising regime; survivorship-selected;
no delisted names. Commodities excluded (no options lane).

---

## 2. n per element and per arm

| Arm | Elements | n episodes | names | P(large) | mean term_ATR |
|---|---|---|---|---|---|
| `ctrl_unconditional` | every session (base rate) | 66,339 | 217 | **0.2995** | +0.179 |
| `ctrl_random` | hash 1-in-40, episode-clustered | 1,447 | 216 | 0.2875 | +0.163 |
| `ctrl_matched_cross_div` | 20 replicates, same name + same quarter | 2,811 | 97 | 0.2689 | −0.084 |
| `cross` | a1 only (the cascade's bare signal) | 2,305 | 216 | 0.2954 | +0.122 |
| **`cross_div`** (primary) | a1 + a2 | **143** | 97 | 0.3287 | +0.307 |
| `cross_div_tl` (= `abl_no_hl`) | a1 + a2 + d | 48 | 42 | 0.3333 | −0.027 |
| `cross_div_hl` (= `abl_no_tl`) | a1 + a2 + c | 102 | 78 | 0.2745 | +0.349 |
| **`full`** | a1 + a2 + c + d | **33** | 31 | 0.2727 | −0.130 |
| `abl_no_div` | a1 + c + d | 863 | 215 | 0.2746 | **−0.323** |
| `abl_no_cross` | a2 + c + d | 114 | 90 | 0.2982 | −0.278 |
| **`cross_divany`** (owner-matching) | a1 + a2′ | **441** | 172 | 0.2902 | +0.229 |
| `cross_divany_hl` | a1 + a2′ + c | 295 | 164 | 0.3119 | +0.046 |
| `full_any` | a1 + a2′ + c + d | 140 | 104 | 0.3214 | −0.303 |

Element rarity, per name over ~320 sessions: a daily MACD bull cross fires **once every
28 sessions**; divergence-at-the-cross survives on **6.2%** of crosses (`div`) or **19%**
(`div_any`); adding the higher low and the trendline break cuts the full setup to
**33 episodes across 31 names in 15 months** — roughly two trades a month across a
211-name universe.

---

## 3. Spot-level results vs controls

Cluster bootstrap by **underlying** (2,000 draws), from `run_cascade.cluster_boot_diff`.
Full table: `data/tests.csv` (93 spot comparisons).

| Comparison | metric | n_a | n_b | mean_a | mean_b | diff | 95% CI | p | q_BH (spot grid) |
|---|---|---|---|---|---|---|---|---|---|
| `cross_div` vs unconditional | P(large) | 143 | 66,339 | 0.3287 | 0.2995 | +0.029 | [−0.046, +0.112] | 0.474 | 0.872 |
| `cross_div` vs unconditional | term_ATR | 143 | 66,339 | +0.307 | +0.179 | +0.128 | [−0.269, +0.539] | 0.543 | 0.872 |
| `cross_div` vs `ctrl_matched` | P(large) | 143 | 2,811 | 0.3287 | 0.2689 | +0.060 | [−0.003, +0.131] | 0.083 | 0.858 |
| `cross_div` vs `ctrl_matched` | term_ATR | 143 | 2,811 | +0.307 | −0.084 | +0.392 | [+0.004, +0.766] | **0.045** | 0.837 |
| `cross_div` vs `cross` (divergence alone) | P(large) | 143 | 2,305 | 0.3287 | 0.2954 | +0.033 | [−0.040, +0.116] | 0.407 | 0.872 |
| `cross_divany` vs unconditional | P(large) | 441 | 66,339 | 0.2902 | 0.2995 | −0.009 | [−0.049, +0.031] | 0.652 | 0.872 |
| `full` vs unconditional | P(large) | 33 | 66,339 | 0.2727 | 0.2995 | −0.027 | [−0.183, +0.145] | 0.743 | 0.900 |
| `full` vs `ctrl_matched_full` | term_ATR | 33 | 658 | −0.130 | −0.017 | −0.113 | [−0.827, +0.622] | 0.782 | 0.900 |
| **`abl_no_div` vs unconditional** | term_ATR | 863 | 66,339 | **−0.323** | +0.179 | **−0.502** | [−0.677, −0.326] | **0.0005** | **0.023** |
| **`abl_no_div` vs `ctrl_random`** | term_ATR | 863 | 1,447 | −0.323 | +0.163 | −0.486 | [−0.698, −0.272] | **0.0005** | **0.023** |

Read:

* The primary arm's only nominally-significant spot result is **`cross_div` vs its
  matched control on term_ATR (p=0.045)** — and it dies at q_BH 0.84 across the spot
  grid alone, let alone the combined grid. The same comparison against the
  *unconditional* base rate is p=0.54, and the matched control's own mean (−0.084) is
  visibly below the unconditional mean (+0.179), i.e. the "lift" is largely the control
  being unlucky rather than the setup being good.
* The **only robust effect in the whole spot grid is negative**: `cross + higher low +
  trendline break WITHOUT divergence` returns **−0.32 ATR** against a **+0.18 ATR** base
  rate, q_BH 0.023 on both controls. Waiting for structure and a line break after a
  crossover, with no momentum disagreement, buys extension and loses.
* `full` (33 episodes) is indistinguishable from everything, in both directions.

---

## 4. Element ablations — which element carries it?

| Ablation | metric | full | ablated | diff | p | q_BH |
|---|---|---|---|---|---|---|
| full vs `abl_no_div` (drop divergence) | P(large) | 0.273 | 0.275 | −0.002 | 0.983 | 0.983 |
| full vs `abl_no_div` | term_ATR | −0.130 | −0.323 | +0.192 | 0.716 | 0.899 |
| full vs `abl_no_tl` (drop trendline) | term_ATR | −0.130 | +0.349 | −0.479 | 0.294 | 0.872 |
| full vs `abl_no_hl` (drop higher low) | P(large) | 0.273 | 0.333 | −0.061 | 0.512 | 0.872 |
| full vs `abl_no_cross` (drop crossover) | term_ATR | −0.130 | −0.278 | +0.148 | 0.783 | 0.900 |
| `cross_div` vs `cross` (divergence's own marginal) | term_ATR | +0.307 | +0.122 | +0.186 | 0.372 | 0.872 |

**No element carries it.** Every ablation is inside noise. The two point-estimate
patterns worth naming, both statistically empty:

* Dropping the **trendline break** *improves* the setup (`abl_no_tl` +0.349 vs full
  −0.130). The trendline element, as operationalised, is the most damaging of the four.
  It is objective and causal — it just does not help.
* Dropping the **higher low** raises P(large) (0.333 vs 0.273) while lowering term_ATR.
  The higher-low wait costs entry price: at option level (§7) every `*_hl` arm is worse
  than its non-`hl` twin in every strike band.

---

## 5. Crossover strength — monotone, or top-decile only?

Measured on the widest arm (`cross`, n=2,305) for power. Deciles in `div_results.txt` §4.

| Measure | Spearman(x, P(large)) | Spearman(x, term_ATR) | decile monotonicity | D1 → D10 P(large) |
|---|---|---|---|---|
| `str_hist` (hist/ATR) | −0.011 | +0.018 | −0.139 | 0.286 → 0.307 |
| `str_slope` (Δhist/ATR) | +0.006 | +0.056 | +0.055 | 0.299 → 0.273 |
| **`str_below0`** (−MACD/close) | **+0.066** | **+0.090** | **+0.839** | 0.286 → **0.368** |
| `str_thrust` (Δclose/ATR) | +0.026 | +0.035 | +0.509 | 0.281 → 0.342 |
| `str_volz` (volume z at cross) | +0.013 | +0.026 | +0.377 | 0.287 → 0.292 |
| `str_div_macd` (divergence size, n=139) | +0.014 | +0.014 | −0.282 | 0.357 → 0.571 |

**Answer: one measure is genuinely monotone, and it is not the one the owner named.**
`str_below0` — *how far below zero the MACD was when it crossed* — is monotone across
deciles (rank correlation of decile-mean vs decile index **+0.84**) with P(large) rising
0.29 → 0.37 and term_ATR 0.06 → **+0.98** from D1 to D10. That is the "crossing from
deep oversold" idea, and it is the only strength construct here with a shape rather than
a top-decile spike.

But the underlying effect size is tiny (Spearman +0.066 on a binary, +0.090 on term_ATR,
n=2,305) and — critically — **`str_below0` is a property of the bare crossover, not of
the divergence family.** It was measured on 2,305 crossovers, not on the 143 divergence
setups. It was not registered as a formal comparison in the multiplicity grid because it
is a conditioning scan, not an arm; treated as a discovery it would need its own
out-of-sample test before anyone traded it.

Histogram magnitude and slope — the owner's first-named "strength at MACD crossover" —
show **nothing** (|ρ| ≤ 0.06, non-monotone). Volume at the cross shows nothing.
Divergence magnitude shows nothing (D10 P=0.571 on n=14 is a top-decile spike on
fourteen observations, exactly the shape that does not replicate).

---

## 6. Hourly early clue — does acting on the hourly beat waiting for the daily?

Base rates first, because they settle the question:

* hourly MACD bull crosses: **18,062** over 69,377 underlying-sessions = one every
  **3.84 sessions per name**;
* daily MACD bull crosses: **2,472** = one every **28.07 sessions per name**.

So an hourly cross precedes essentially every daily cross by construction. The lead,
measured on the hourly cross that *immediately* precedes the daily cross of a
`cross_div` episode (n=143): **median 1 session, mean 1.65, p75 = 3, and 29.4% fire in
the same session**. The owner's PNB case (5 sessions of lead) is at the far tail.

| Arm | n | P(large) | term_ATR | mfe_ATR | mae_ATR |
|---|---|---|---|---|---|
| daily `cross_div` (wait for the daily) | 143 | 0.329 | +0.307 | 2.231 | 1.958 |
| **`hourly_tradeable`** (causal: any hourly cross while divergence is true and the daily has not yet crossed) | 269 | **0.283** | +0.296 | 2.126 | 1.969 |
| `hourly_oracle` (NOT tradeable — the hourly cross that turned out to precede the daily) | 143 | **0.510** | **+1.050** | 2.894 | 1.358 |

Paired, oracle vs its own daily entry: P(large) **+0.182 (t = +4.79)**, term_ATR
**+0.743 (t = +6.07)**, entry price **1.2% cheaper (median)**.

**The oracle arm looks superb and is worthless.** It requires knowing in advance which of
the ~7 hourly crosses in the preceding month was "the one" — precisely the information
the daily cross is later used to supply. The *causal* version of the same idea,
`hourly_tradeable`, is **worse than waiting** (0.283 vs 0.329). This is the cleanest
result in the study: the hourly does lead, by about one session, and you cannot act on
the lead.

---

## 7. Option-level economics across the strike grid

**Extraction (the `±8%` bug is not inherited).** `div_opt_extract.py` pulls the option
tape with **no moneyness predicate at all**. Measured directly: the `±8%` band used by
`setups_2d3d/extract.py` keeps 90.36% of the CE tape and **deletes 9.64%** — small in
volume, and concentrated exactly on contracts that have moved.

**Design.** Monthly expiries only; `near` = first monthly with DTE ≥ 8 at entry, `far` =
the next. Strike targets K/S−1 ∈ {+6%, +3%, 0, −3%, −6%}, nearest available strike within
**±2.5%**, and **a band that cannot be filled is counted as MISSING, never substituted**.
Contract chosen from the 15:15 IST snapshot of the prior session. Entry = open of the
first 30m bar of the entry session, floored at intrinsic. Exit = the same 30m bar at
which the *spot* barrier resolved, never later than expiry − 2 days.

**Stale-ITM exits are MODELLED, not read.** Implied vol is inverted from the entry
premium (Black-Scholes, r=q=0); the exit is that vol re-priced at the exit spot and exit
DTE, floored at intrinsic. Where a real quote exists both are reported.

### 7a. Stale-exit rate by outcome — the requested proof

| Strike band | outcome | n | stale-exit rate |
|---|---|---|---|
| ATM | loss/time | 1,756 | 0.024 |
| ATM | **large (target hit)** | 649 | **0.054** |
| ITM3 | loss/time | 1,457 | 0.053 |
| ITM3 | **large** | 434 | **0.154** |
| ITM6 | loss/time | 467 | 0.109 |
| ITM6 | **large** | 175 | **0.223** |
| OTM3 | large | 489 | 0.020 |
| OTM6 | large | 211 | 0.028 |

By the contract's **final** moneyness: deep ITM **17.2%** stale, ITM 7.8%, near ATM 3.3%,
OTM 3.3%, deep OTM 2.5%. **The tape preferentially dies on ITM winners — by a factor of
5-7×** — which is the ±8%-band failure reproduced through the data instead of the code.
Modelling the exit is therefore mandatory, not optional. Model-vs-tape where both exist
(n=6,288): median difference **+0.33pp**, mean +0.08pp, p05 −23.6pp / p95 +22.4pp — the
model is unbiased in the centre and noisy in the tails.

### 7b. Strike-band coverage — a hard data limit

Our stock chains hold a **median of 2 distinct strikes per underlying-session**, spanning
a median of **−1.5% to +1.7%** moneyness. Share of episodes for which each band could be
filled:

| Arm | ITM6 | ITM3 | ATM | OTM3 | OTM6 |
|---|---|---|---|---|---|
| `cross` | 16% | 42% | 43% | 18% | 5% |
| `cross_div` | 17% | 44% | 55% | 27% | 3% |
| `cross_divany` | 14% | 41% | 50% | 24% | 5% |
| `ctrl_random` | 8% | 25% | 40% | 27% | 9% |

**Only the ATM and ITM3 columns are broad enough to reason about.** ITM6 and OTM6 are
5–17% subsamples, selected on names whose strike ladder happens to be wide in our
ATM-tracker store — every result in those cells is conditional on that selection.

### 7c. Payoff distribution — lottery or grinder? (near expiry, net of 1.6%)

`cross_divany` (owner-matching), near-month:

| Band | n | hit rate | mean | median | p10 | p25 | p75 | p90 | p95 | max |
|---|---|---|---|---|---|---|---|---|---|---|
| ITM6 | 61 | **0.475** | +0.111 | −0.016 | −0.370 | −0.292 | +0.527 | +0.712 | +0.951 | +1.198 |
| ITM3 | 181 | 0.293 | −0.062 | −0.269 | −0.577 | −0.416 | +0.255 | +0.846 | +0.970 | +1.807 |
| ATM | 220 | 0.241 | −0.090 | −0.340 | −0.681 | −0.519 | −0.016 | +0.892 | +1.382 | +2.647 |
| OTM3 | 106 | 0.274 | −0.040 | −0.457 | −0.819 | −0.651 | +0.627 | +1.290 | +1.825 | +3.335 |
| OTM6 | 21 | **0.333** | +0.148 | **−0.542** | −0.937 | −0.858 | +1.033 | +1.725 | +3.584 | +3.717 |

`cross_div` (strict), near-month: ITM6 n=24 hit **0.542** median **+0.278**;
ATM n=78 hit 0.231 median −0.332; OTM3 n=38 hit 0.316 median −0.373.
`ctrl_random`, near-month: ITM6 hit 0.308 median −0.222; ATM hit 0.300 median −0.307;
OTM3 hit 0.358 median −0.350; OTM6 hit 0.357 median −0.462.

**Answer to the owner's question: it is a lottery at the OTM end and a grinder at the ITM
end, and the shape is a property of the STRIKE, not of the setup.** The identical shape
appears in the random control. Concretely:

* **OTM6** — hit rate ~33%, **median −54%**, p95 +358%, max +372%. That is the PNB-style
  payoff: you lose half your money most of the time and occasionally 4×.
* **ITM6** — hit rate **48–54%**, median ≈ 0 to +28%, max +120%. A grinder, with a much
  tighter distribution and no lottery ticket.
* **ATM/OTM3 are the worst of both** — the lottery's median (−34% / −46%) without the
  lottery's tail.

This reconciles with the case study: PNB's large multiple came from the **move × short
DTE gamma**, not from the strike choice, and the whole 105–112 ladder returned 119–135%
over the common window. Here too, across 6,655 priced legs, the ITM/OTM axis changes the
*shape* of the distribution far more than its *mean*.

### 7d. Setup vs control at option level

20 comparisons (near-month, base cost, cluster bootstrap by underlying). Full table:
`data/opt_tests.csv`.

| Arm | Band | n_a | n_b | mean_a | mean_b | diff | 95% CI | p | q_BH (option grid) |
|---|---|---|---|---|---|---|---|---|---|
| `cross` | OTM6 | 111 | 129 | +0.503 | −0.009 | **+0.512** | [+0.185, +0.849] | 0.0025 | 0.048 |
| `cross_divany` | ITM6 | 61 | 117 | +0.111 | −0.060 | +0.171 | [+0.039, +0.301] | 0.011 | 0.095 |
| `cross_div` | ITM6 | 24 | 117 | +0.227 | −0.060 | +0.287 | [+0.062, +0.517] | 0.015 | 0.095 |
| `cross_div` | ITM3 | 63 | 355 | +0.011 | −0.093 | +0.104 | [−0.045, +0.266] | 0.180 | 0.539 |
| `cross_divany` | ATM | 220 | 573 | −0.090 | −0.053 | −0.037 | [−0.137, +0.073] | 0.472 | 0.799 |
| `cross_divany_hl` | OTM6 | 12 | 129 | −0.450 | −0.009 | −0.441 | [−0.945, +0.096] | 0.110 | 0.523 |

Every `*_hl` cell is negative — **waiting for the confirmed higher low costs 3–44
percentage points of option return in every band**, because you pay a higher premium with
less time left. That is the clearest economic statement in the study.

The two ITM6 divergence cells are the study's best-looking option results, and they sit
on **14–17% strike coverage, n=24 and n=61**, with the *bare crossover* producing a
larger effect in OTM6 on the same grid. Cost sensitivity does not rescue anything: at the
owner's assumed 8% round trip, `cross_divany` ATM is −15.4%, OTM3 −10.4%, ITM6 +4.7%.

### 7e. Concentration

| Arm / band | n | mean | ex-top-3 | median | hit | PNB legs | top 3 |
|---|---|---|---|---|---|---|---|
| `cross_divany` ATM | 220 | −0.090 | −0.126 | −0.340 | 0.241 | 1 | DIVISLAB +265%, PHOENIXLTD +254%, SENSEX +231% |
| `cross_divany` OTM3 | 106 | −0.040 | −0.126 | −0.457 | 0.274 | 0 | CROMPTON +334%, NIFTY +274%, SENSEX +263% |
| `cross_divany` OTM6 | 21 | **+0.148** | **−0.329** | −0.542 | 0.333 | 0 | ICICIPRULI +372%, CROMPTON +358%, KPITTECH +173% |
| `cross_divany` ITM6 | 61 | +0.111 | +0.060 | −0.016 | 0.475 | 0 | APOLLOHOSP +120%, PGEL +114%, KFINTECH +96% |
| `cross_div` ITM6 | 24 | +0.227 | +0.103 | +0.278 | 0.542 | 0 | APOLLOHOSP +120%, PGEL +114%, KFINTECH +96% |

**Removing three trades flips every positive OTM mean negative.** Only the ITM6 cells
survive ex-top-3 — on 24 and 61 observations at 14–17% coverage.

---

## 8. Option positioning (element f)

**Correction to the case study's data note:** at the **15:15 IST snapshot** (not across
all 30m bars) our option store is much better populated than PNB alone suggested — for
stocks, OI non-null **100%** (>0 on 96.8%), IV non-null **92.7%**, delta **92.7%**,
volume>0 **80.3%**. The "IV populated on 1% of rows" figure in Part 1 is a property of
*intraday* rows, not of the enriched end-of-day snapshot. So element (f) **is**
measurable — the binding limit is not nullity, it is **breadth: a median of 2 strikes per
underlying-session spanning −1.5%…+1.7%**. Feature coverage on setup episodes is 59–71%.

Conditioning test on the widest arm (`cross`, quintiles, cluster bootstrap top-vs-bottom):

| Feature | n | Spearman(x, term_ATR) | Q5−Q1 term_ATR | 95% CI | p | q_BH |
|---|---|---|---|---|---|---|
| Δ5-session CE OI | 1,447 | −0.003 | −0.011 | [−0.414, +0.405] | 0.957 | 0.957 |
| Δ5-session PE OI | 1,444 | −0.003 | +0.124 | [−0.286, +0.525] | 0.557 | 0.861 |
| PCR (OI) | 1,368 | −0.015 | +0.103 | [−0.339, +0.556] | 0.646 | 0.861 |
| CE volume / CE OI | 1,440 | +0.049 | +0.238 | [−0.223, +0.694] | 0.307 | 0.861 |

**Nothing.** No OI-build/unwind or turnover measure conditions the outcome, in either
direction. The honest limit stands: with ~2 near-ATM strikes per name we are measuring
*ATM* positioning only — we cannot see the OI walls, skew, or far-strike build that a
real positioning read would need. **That part remains a data gap, and no proxy was
substituted for it.**

---

## 9. Multiplicity, per-quarter, ex-top-3, and PNB's share

**Multiplicity.** 116 registered comparisons (93 spot + 20 option + 4 positioning +
element scans reported separately). Over the **combined** grid:

* **survive Bonferroni p<0.05: 0**
* survive BH q<0.10: **3** — `abl_no_div` vs unconditional and vs random on term_ATR
  (both **negative**, q=0.029), and `cross OTM6` vs control (q=0.097, the **bare**
  crossover on a 5%-coverage cell).
* The two divergence ITM6 option cells fall to q=0.255 and q=0.290 on the combined grid.

**Per non-overlapping quarter** (`cross_div`, P(large) / term_ATR):
2025Q2 0.417/+0.72 (n=12) · 2025Q3 0.360/+0.33 (n=25) · 2025Q4 0.366/+0.55 (n=41) ·
2026Q1 **0.167/−0.01** (n=30) · 2026Q2 0.464/+0.09 (n=28) · 2026Q3 **0.000/+0.30** (n=7).
The unconditional base rate moves the same way quarter to quarter (2026Q1 0.207,
2026Q2 0.363) — **the arm is tracking the market, not adding to it.** `full` has ≤11
observations in every quarter and is uninterpretable per-quarter.
Option-level, `cross_divany` ATM per quarter: +0.30, −0.02, −0.14, −0.19, −0.11 — one
positive quarter (n=13) and four negative.

**Ex-top-3.** `cross_div` term_ATR +0.307 → **+0.177** ex-top-3 (median +0.117);
`cross_divany` +0.229 → +0.165 (median **−0.058**); `full` −0.130 → **−0.693**;
`full_any` −0.303 → −0.451. Top-5 names carry 18–21% of total positive term_ATR in the
broad arms, but **60% in `full`** (n=33).

**How much rests on PNB and similar names.** Directly: **very little, and that is itself
the finding.**

* PNB contributes **0 of 143** `cross_div` episodes — the strict definition does not fire
  on the owner's own example at all.
* PNB contributes **2 of 441** `cross_divany` episodes (**0.5%**): 2026-05-26 (term_ATR
  +0.99, barrier outcome **stop**) and 2026-07-20 (term_ATR +1.57, **truncated** — our
  data ends that session).
* At option level PNB has **1 leg** in the whole `cross_divany` grid: **2026-05-26,
  106 CE, 35 DTE, entry ₹2.89 → modelled exit ₹1.37 = −54% net.**

So **the owner's own worked signal, traded by the owner's own rule at the owner's own
entry, lost 54% on the option.** The move he is describing came from the **second**
signal (2026-07-17 cross), whose outcome our tape cannot yet score — it ends on
2026-07-20 with the trade open and +1.57 ATR in favour. The study neither confirms nor
refutes that second trade; it is one open observation.

The top-5 contributors are unrelated banks/PSU-adjacent names (BHEL, IDFCFIRSTB,
BANDHANBNK, OBEROIRLTY, FEDERALBNK) — so the result does **not** rest on PNB or a cluster
of PNB-like names. It simply is not there.

---

## 10. Honest verdict

**KILL, with two things worth keeping.**

1. **The full setup does not beat its matched control.** `full` (n=33): P(large) 0.273 vs
   0.258 matched, term_ATR −0.130 vs −0.017, p=0.78. The primary `cross_div` arm's single
   nominal win (term_ATR vs matched control, p=0.045) does not survive its own family's
   BH correction, let alone the combined 116-test grid where **nothing survives
   Bonferroni**.
2. **No element carries it.** All four ablations are inside noise. The trendline element
   actively hurts; the higher-low element costs 3–44pp of option return in every strike
   band by delaying entry.
3. **The owner's setup was not what the textbook definition says.** The strict "last two
   pivot lows" divergence does not fire on PNB. The definition that does (`div_any`) was
   selected after seeing the chart, is therefore in-sample, and shows **nothing**
   (P(large) 0.290 vs 0.300 unconditional).
4. **The hourly lead is real and unusable.** Median 1 session, 29% same-session, and the
   causal version of the rule underperforms simply waiting for the daily.
5. **Positioning is a genuine data gap** at the strikes that matter, and what we *can*
   measure (near-ATM OI build/unwind, PCR, turnover) conditions nothing.

Worth keeping, as **hypotheses for a separate, out-of-sample test — not as anything to
wire**:

* **`str_below0`** — MACD's distance below zero at the crossover — is the only strength
  construct with a monotone decile shape (+0.84), P(large) 0.286 → 0.368 and term_ATR
  0.06 → +0.98 across deciles, on n=2,305 crossovers. Small effect, discovered by a scan,
  untested out of sample.
* **The ITM6 / OTM6 payoff-shape contrast is real and is a strike decision, not a signal
  decision**: ~48–54% hit / median ≈ 0 for deep ITM versus ~33% hit / median −54% with a
  +370% tail for deep OTM, the same shape in the random control. Whatever entry signal is
  eventually shipped, this is the trade-off it will face, and the OTM lottery's positive
  mean is entirely three trades wide.

**Caveats that bound all of the above.** One broadly-rising regime, ~15 months of stock
history, survivorship-selected universe. Round-trip cost is **assumed** (0.6/1.6/4.0/8.0%)
— no spread data exists in our store. Exits on ITM winners are **modelled** because the
tape dies on them 5–7× more often than on losers. Deep strike bands rest on 5–17%
coverage of a 2-strike-wide ATM tracker. And the whole study originates from one chart,
which is the classic route to overfitting — the discipline applied (a-priori definitions,
prefix-invariance proof, matched controls through identical machinery, episode
clustering, combined-grid multiplicity, per-quarter and ex-top-3 reporting) is what
allowed a candidate that *looked* like a 600% winner to be scored honestly as a −54%
loss on its own worked example.

---

## APPENDIX V — adversarial verification (same day, independent pass)

Verification code: `backend/directional_options/research/divergence/verify/`
(`ver_opt_extract2.py`, `ver_options2.py`, `ver_hourly2.py`, `ver_multiplicity.py`).
Repaired artefacts: `data/{optfull2.parquet, opt_trades2.parquet, opt_tests2.csv,
combined_tests_repaired.csv}`. **Three defects were found. The verdict does not change;
several published numbers do.**

### V-1 (MATERIAL) The option tape was 42% deleted by an undisclosed predicate

`div_opt_extract.py` filters `underlying_price IS NOT NULL AND underlying_price > 0`.
In our store that column is written **only** by the post-expiry backfill writer
(`upstox_expired`) and by the 5 index symbols on fyers. It is NULL on **100% of live
`upstox` rows and 100% of stock `fyers` rows**:

| source | rows (30m, 2026-04-01→07-21) | underlying_price populated |
|---|---|---|
| upstox (live) | 690,702 | **0** |
| fyers | 657,344 | 94,446 (5 index symbols) |
| upstox_expired | 778,753 | 467,313 |

Effect across the full study span (CE, `close>0`): the predicate kept 1,645,227 of
2,832,875 rows and **6,415 of 11,072 distinct CE contracts — it deleted 4,657 contracts
(42%)**. This is the same *family* of bug as the ±8% moneyness band the study was
told not to inherit: an incidental predicate that removes contracts. Most pointedly,
it deleted **2,681 of the 2,705 rows of the owner's own PNB 2026-07-28 tape**.

**Fix:** predicate dropped; spot joined from our own 30m spot panel
(`cascade/data/intra.parquet`) on `(underlying, time)`. Tape 1,674,272 → **2,752,410
rows**, 6,486 → **12,178 contracts**; priced legs 6,655 → **10,282 (+54%)**.

Numbers in §"option_level_results_by_strike_grid" are therefore **superseded**. Repaired
near-leg distribution, net 1.6%:

| Arm | Band | n | hit | mean | median | p90 | p95 | max | stale |
|---|---|---|---|---|---|---|---|---|---|
| `cross_div` | ITM6 | 33 | **0.455** | +0.133 | −0.053 | +0.891 | +1.034 | +1.198 | 0.212 |
| `cross_div` | ATM | 93 | 0.247 | −0.055 | −0.334 | +1.060 | +1.416 | +2.545 | 0.054 |
| `cross_div` | OTM6 | 13 | 0.538 | +0.416 | +0.239 | +1.709 | +1.987 | +2.380 | 0.154 |
| `cross_divany` | ITM6 | 79 | 0.392 | +0.028 | −0.189 | +0.692 | +0.791 | +1.198 | 0.139 |
| `cross_divany` | ATM | 298 | 0.252 | −0.059 | −0.346 | +1.069 | +1.477 | +5.175 | 0.074 |
| `cross_divany` | OTM6 | 60 | 0.483 | +0.457 | −0.141 | +2.016 | +2.904 | +4.274 | 0.150 |
| `ctrl_random` | ITM6 | 198 | 0.237 | −0.118 | −0.261 | +0.569 | +0.836 | +1.408 | 0.167 |
| `ctrl_random` | OTM6 | 231 | 0.385 | +0.090 | −0.403 | +1.454 | +1.873 | +7.475 | 0.104 |

The **OTM-lottery vs ITM-grinder contrast survives the repair and gets sharper**
(ITM6 hit 0.46–0.48 median ≈ 0 vs OTM6 median −0.14 with a +400% tail), and it is
**still present in the random control**, i.e. it remains a property of the strike, not
of the signal.

The **stale-exit-by-outcome proof also survives** on the repaired tape, at smaller
magnitudes than published: ITM6 winners **0.224** vs losers 0.136; ITM3 0.173 vs 0.076;
ATM 0.131 vs 0.096. By final moneyness: deep-ITM **0.218**, ITM 0.149, near-ATM 0.083.
Model-vs-tape where both exist (n=9,026): median **+0.004**, mean +0.002, p05 −0.245,
p95 +0.243 — no systematic bias, wide dispersion. Known model limit, newly visible:
PNB's 2026-07-20 112 CE went **0.99 → 2.00 on the tape (+102%)** but is modelled at
**1.57 (+59%)**, because holding entry IV fixed cannot reproduce an IV expansion. The
model is conservative on exactly the fast winners.

### V-2 (MINOR, but it flips a headline's wording) The hourly arm read a same-session daily value

`div_hourly.py` gates each hourly cross on session `a` using `div[a]` and the daily
`macd − signal` **of session `a`** — daily quantities that do not exist until 15:30 IST.
An hourly cross at 11:15 was being filtered on information four hours in its own future,
and the `diff > 0` clause specifically drops hourly crosses landing on the same session
as the daily cross (29.4% of the oracle sample). Re-gated on session `a−1`:

| arm | n | P(large) | term_ATR |
|---|---|---|---|
| hourly_tradeable, as shipped (lookahead) | 269 | 0.283 | +0.296 |
| hourly_tradeable, **repaired (causal)** | 282 | **0.298** | **+0.316** |
| daily `cross_div` | 143 | 0.329 | +0.307 |

Cluster-bootstrapped, repaired hourly − daily: P(large) −0.031 (p=0.50), term_ATR +0.009
(p=0.96). So the published claim *"acting on the hourly LOSES to waiting"* is **too
strong**; the correct statement is **"acting on the hourly is statistically
indistinguishable from waiting, at twice the trade count"** — still no reason to act on
it, but not a loss. The base-rate argument (one hourly cross every 3.84 sessions/name vs
one daily every 28.07; median lead 1 session) is unaffected and remains the real reason
the hourly "lead" is vacuous.

### V-3 (MINOR) The "IV 92.7% at the 15:15 snapshot" correction is itself a selection artefact

§"option_positioning_result" corrects Part 1's "IV populated on 1% of rows" to "IV 92.7%,
delta 92.7% at the 15:15 snapshot". That measurement was taken on the tape *after* the
V-1 predicate, i.e. on the enriched `upstox_expired` subset only. On the **full** 15:15
stock snapshot (2026-04-01→07-21, n=150,488): **OI 71.4%, IV 17.4%, delta 17.4%,
volume>0 82.0%** — by source, `fyers` 0% IV / 0% OI, `upstox` 0% IV / 100% OI,
`upstox_expired` 48.9% IV / 100% OI. Part 1's ~1% was low (it measured intraday rows);
Part 2's 92.7% is too high. **The truth is ~17% for IV/greeks and ~71% for OI.** The
positioning null itself is unaffected in direction — it was computed on the enriched
subset, which is the *most* favourable data available — but the data-gap statement should
read "IV/greeks usable on about a sixth of near-ATM snapshot rows", not "92.7%".

### V-4 Checks that PASSED unchanged

* **Prefix-invariance** re-run: 10 underlyings × 6 cut points, rtol 1e-12, **PASS**.
  `piv_low`/`piv_high` correctly excluded as non-causal-at-index-by-design, with every
  consumer (`div`, `div_any`, `hl_conf`, `tl_break`, `tl_recent`, all `str_*`) asserted.
* **The central definitional finding**, re-derived independently from `elem.parquet`:
  PNB daily bull crossovers 2026 = 04-08, **05-25**, 07-17 (no 05-22 cross); `div` is
  **False at both 05-25 and 07-17**, `div_any` True at both. Confirmed pivot lows
  04-02 (99.79 / −4.925), 05-05 (105.45 / −0.834), 05-18 (98.50 / −2.734),
  06-29 (106.11 / +0.388), 07-08 (100.45 / −0.852) — exactly as published. Note this
  also means **element (c) fails on the owner's example too**: 07-08's low 100.45 is
  *below* the prior confirmed pivot low 106.11, so `hl_conf` does not fire. **Two of the
  owner's three named structural elements do not fire on his own chart under their strict
  definitions.** 2026-07-13 confirmed absent from the PNB daily panel.
* **PNB hand-derivation to the paisa** against raw PG (`option_premium_candles`, 30m,
  `strike=106`, `expiry=2026-07-28`): 07-08 low **1.11** close **1.24**; 07-09 low 1.11
  close **1.83**; 07-14 close **2.49**; 07-17 close **2.72**. All match Part 1 exactly.
  The Part 2 PNB leg matches exactly too: 2026-05-26, 106 CE, JUN expiry, 35 DTE,
  entry open **₹2.89** (raw PG: 2.89), modelled exit **₹1.3697**, net **−54.2%**; the
  real tape exit was ₹1.59 (−46%), so the model was conservative, not flattering.
* **Concentration**, re-derived: `cross_div` contains **0** PNB episodes (ex-PNB series
  byte-identical); `cross_divany` contains exactly **2**, both `large=0` (2026-05-26
  outcome *stop*, 2026-07-20 *truncated*). Ex-top-3-names: `cross_div` term_ATR
  +0.307 → **+0.179**; `cross_divany` +0.229 → +0.145; `full` −0.130 → **−0.693**.
  On the **repaired** option grid ex-top-3 is still decisive: `cross_div` ITM6
  +0.133 → **+0.036**, OTM6 +0.416 → **−0.034**, ATM −0.055 → −0.135.
* **Global multiplicity recomputed from scratch** over the recombined grid (97 non-option
  + 20 repaired option = **117** comparisons): **Bonferroni p<0.05 survivors = 0**;
  **BH q<0.10 survivors = 3** — `abl_no_div` vs both controls (q=0.0195, *negative*) and
  `cross OTM6` vs random (q=0.0195, the **bare** crossover). The divergence ITM6 cell
  improved on the repaired tape (p 0.0075, q **0.146**) but still does not survive.
* **`str_below0` reproduced and strengthened.** Decile shape +0.73 (Pearson on decile
  means), Spearman +0.090, D1 term_ATR +0.063 / P(large) 0.286 → D10 +0.984 / 0.368 —
  matching the published cell values. New, and better than the report claims: it holds in
  a **split-half** test — first half (2025Q1–Q3, n=734) D9-10 vs D1-2 diff +0.479
  (p=0.09); second half (n=1,571) diff **+0.898 (p=0.0005)**. Caveat the report did not
  make: `str_slope` has a comparable decile shape (+0.70), so `str_below0` is not the
  *only* monotone construct — it is the strongest.
* **Hygiene:** working tree contains only new files under
  `research/divergence/` and `docs/divergence_*.md`; the sole modified tracked files are
  live-app runtime state (`backend/runtime/…`), untouched by this study. No lane code, no
  flags, no commits. Suite green.
