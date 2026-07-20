# Directional options — (3) saturation/maturity and (4) pyramid economics

2026-07-21. Research only: nothing wired, no flags, no lane code touched.

Third and fourth passes of the series testing the owner's model verbatim:

> "directional trade — first small time frame confirms a move — then as direction
> builds higher time frame confirm — then sustained large move happens — move
> matures/saturates — stock goes to consolidation mode. Moves happen for small
> time and consolidation dominates… When one timeframe lower to that large move
> confirm we enter the position with small qty and adding to that when large
> time frame confirms. We exit similarly as trade matures."

Pass (1) `docs/directional_regime_duration_2026-07-21.md` established that
consolidation does dominate (60–66% of sessions) but that moves are **long, not
fast** (13–18 sessions, ~1/month, no time concentration).
Pass (2) established that the cascade is a real *ordering* phenomenon
(P(stage-2 | stage-1) = 20.3% vs 9.0% control, q=0.0009) that carries **no
forward information**: measured at the tradeable stage-2 bar, P(large move) is
0.269 cascade / 0.278 control / 0.264 unconditional, all below the 33.3%
break-even of the 2:1 barrier, before costs.

This document answers the two remaining questions. **Both come back negative,
and the reason is the same one pass (2) found.**

## Code

| file | what |
|---|---|
| `backend/directional_options/research/cascade/mat_defs.py` | maturity/saturation rules, fixed a priori |
| `backend/directional_options/research/cascade/mat_run.py` | study (3) — capture measurement → `mat_results.txt`, `data/mat_episodes.parquet` |
| `backend/directional_options/research/cascade/pyr_run.py` | study (4) — option pyramid simulator → `data/pyr_trades.parquet` |
| `backend/directional_options/research/cascade/pyr_analyse.py` | study (4) — statistics → `pyr_results.txt` |
| `backend/directional_options/research/cascade/test_maturity_causality.py` | 6 causality proofs, all pass |

Reuses unchanged: `../setups_2d3d/harness.py` (spot grid, contract selection,
cost model), `../setups_2d3d/features.py` (causal filters), `./stages.py`
(a-priori stage definitions), `./run_cascade.py` (episode clustering, control
masks, cluster bootstrap, BH). **No PG queries were issued** — everything runs
off the parquet/CSV caches the earlier passes built.

---

# (3) Saturation — is maturity detectable causally, and in time to act?

## Definitions (fixed a priori, `mat_defs.py`)

Every rule is evaluated on the **daily** frame from sessions ≤ s, fires on the
close of session s, and is **actionable only at the open of the first 30m bar of
session s+1** (the daily bar of s does not close until 15:30 IST). Each rule
also carries an *arming* precondition, which is what makes it a maturity rule
rather than a weak-tape filter — something must first have matured.

| rule | armed when | fires when |
|---|---|---|
| `adx_roll` | daily ADX14 reached ≥ 25 since entry | ADX < post-entry peak − 4 **and** down 2 consecutive sessions |
| `atr_contract` | daily ATR14 expanded ≥ 1.15× its entry level | ATR14 < ATR14 three sessions ago |
| `macd_fade` | side-signed daily MACD histogram positive since entry | hist < 0.5 × post-entry peak **and** down 2 consecutive sessions |
| `ma_ext` | — | side·(close − SMA20)/ATR14 ≥ 3.0 (exit into extension) |
| `range_compress` | — | 3-session range < 1.5 × ATR14 |
| `state_off` | the daily confirmed-trend state was True since entry | that state turns False = **"goes to consolidation mode"** |

Benchmarks: `fix_3` / `fix_5` / `fix_10` fixed-time exits, `hold_full` (hold the
whole 20-session horizon), and `oracle_mfe` — the ex-post maximum favourable
excursion, which is not tradeable and exists only as the denominator that turns
every other number into a **capture fraction**. Hard protective stop −1.0 ATR
throughout, monitored on 30m bars.

## `maturity_signals_tested` — capture vs fixed-time vs hold-to-consolidation

Population A, **all 5,323 stage-1 episodes** (the tradeable population). Mean
MFE 1.80 ATR, 74.7% get stopped.

| exit | capture (Σexit/ΣMFE) | mean ATR | fired% | median fire session |
|---|---|---|---|---|
| `adx_roll` | −0.014 | −0.025 | 35% | 20 (never) |
| `atr_contract` | −0.020 | −0.036 | 25% | 20 |
| `macd_fade` | −0.020 | −0.036 | 91% | 9 |
| `ma_ext` | −0.009 | −0.016 | 21% | 20 |
| `range_compress` | +0.008 | +0.014 | 100% | 3 |
| `state_off` (hold to consolidation) | −0.012 | −0.022 | 39% | 20 |
| `fix_3` | +0.022 | +0.040 | — | — |
| `fix_10` | −0.008 | −0.014 | — | — |
| `hold_full` (20 sessions) | −0.017 | −0.031 | — | — |
| `oracle_mfe` | 1.000 | 1.803 | — | — |

Everything is ≈ 0. Paired, episode-clustered, BH across K=36 comparisons:
**every** maturity rule is significantly *worse* than the 3-session exit
(`atr_contract` −0.076 ATR, q=0.004; `macd_fade` −0.076, q=0.002; `adx_roll`
−0.064, q=0.011; `state_off` −0.062, q=0.014; `ma_ext` −0.056, q=0.018), and
none differs from `hold_full` or `fix_10`. On an entry with no edge, every extra
session held costs money; there is nothing for an exit rule to save.

Population B, the **ex-post subset that actually reached +2 ATR MFE** (n=1,700,
mean MFE 4.22 ATR) — "given a move happened, how much do you keep":

| exit | capture | mean ATR | vs `hold_full` (paired, q) | vs `fix_10` (paired, q) |
|---|---|---|---|---|
| `ma_ext` | **0.460** | 1.939 | −0.000 (q=0.997) | **+0.192 (q=0.0016)** |
| `atr_contract` | 0.456 | 1.923 | −0.016 (q=0.654) | **+0.177 (q=0.0030)** |
| `state_off` | 0.445 | 1.874 | −0.065 (q=0.146) | +0.127 (q=0.0051) |
| `adx_roll` | 0.428 | 1.806 | −0.133 (q=0.0016) | +0.060 (q=0.377) |
| `macd_fade` | 0.397 | 1.675 | −0.264 (q=0.0016) | −0.071 (q=0.046) |
| `range_compress` | 0.237 | 0.997 | −0.942 (q=0.0016) | −0.749 (q=0.0016) |
| `hold_full` | 0.460 | 1.939 | — | — |
| `fix_10` | 0.414 | 1.746 | — | — |
| `fix_3` | 0.238 | 1.004 | — | — |

The best maturity rules beat a fixed 10-session exit by ~0.18–0.19 ATR
(q ≤ 0.003) but **are statistically indistinguishable from simply holding the
whole horizon**. There is no maturity rule here that saves anything a
do-nothing hold does not already save.

## Timing — the signals are late, or they are not maturity signals

On the same ex-post subset, the median session of the MFE peak is **13**.

| rule | fired% | median fire session | median sessions AFTER the peak | % that fire after the peak |
|---|---|---|---|---|
| `adx_roll` | 41% | 13 | +5 | 63% |
| `atr_contract` | 29% | 13 | +4 | 69% |
| `macd_fade` | 96% | 11 | +2 | 54% |
| `state_off` | 67% | 13 | +3 | 67% |
| `ma_ext` | 45% | 6 | −5 | 13% |
| `range_compress` | 100% | 4 | −8 | 16% |

The three rules that genuinely track trend exhaustion (`adx_roll`,
`atr_contract`, `state_off`) fire **4–5 sessions after** the move already
topped, 63–69% of the time. The two that fire early (`ma_ext`,
`range_compress`) are not detecting maturity at all — `range_compress` fires on
100% of episodes by session 4 and is simply a short fixed-time exit wearing a
costume, and it is the worst rule in the table (capture 0.237).

**By market:** index n=175, stock n=5,148; the pattern is identical and every
capture number is within ±0.07 of zero on the full population.

**Sensitivity grid** (27 cells, one parameter per rule, reported in full and
used for nothing): mean exit ATR ranges −0.051 to +0.020 across all cells. No
threshold rescues anything.

## Verdict on (3)

**Maturity is not detectable causally in time to act.** The exhaustion signals
fire ~4 sessions after the peak; the early signals are fixed-time exits in
disguise; nothing beats holding; on the tradeable population every maturity
exit is significantly *worse* than a 3-session exit. **The scale-out half of the
owner's model is decorative** — it does not add value over a do-nothing hold and
it costs money relative to getting out early.

---

# (4) Pyramid economics on options

## What was simulated

Vehicle: the only holdable one the data supports — **monthly expiry, DTE 8–22 at
entry, ITM**, in two bands (`deep_itm` −6…−3%, `slight_itm` −3…−0.75%). Tracked
contract per (underlying, side, session), chosen at the **15:15 snapshot of the
prior session** (test 5 proves it cannot peek at the entry session).
Unit = Rs 25,000 of premium; **every arm allocates the same maximum Rs 75,000**.

| arm | entry schedule |
|---|---|
| `pyramid` | **1 unit at stage-1, +2 units at stage-2** — the owner's structure |
| `fixed_t1` | 3 units at stage-1, same abandonment rule (pure sizing comparison) |
| `fixed_hold` | 3 units at stage-1, no abandonment |
| `s2_only` | 3 units at stage-2 only (skip the early tranche) |

All four also run on `ctrl_long`, `ctrl_short`, `ctrl_random` through
byte-identical machinery. Exits: hard −1.0 ATR spot stop (closes everything);
maturity scale-out (half at the first firing, the rest at the second, executed
at the next session's open); hard caps at 10 sessions and always out at
expiry − 2 calendar days; and, if the higher timeframe never confirms within 3
sessions, the first tranche is closed at the open of session s0+4. Costs
round-trip per unit, 0.6% / **1.6%** / 4.0% of premium.

**Pre-registered primary maturity rule: `atr_contract`** — the owner's own
first-named tool. The full 6-rule grid is reported and moves nothing (below).

**No-arbitrage premium floor.** The tape contains stale prints that quote an ITM
option *below* intrinsic (observed: CDSL 1340CE at 12.00 with spot 1358.4). Left
alone, one such print manufactured a fake 22× "winner" that on its own dominated
the concentration profile. All premiums are floored at max(quote, intrinsic)
using the same bar's underlying price — a same-bar, causal correction.
**3,599 of 62,096 entry quotes (5.8%) were floored.**

## `pyramid_vs_fixed_vs_stage2only` vs controls

Return on allocated capital, primary rule, base costs, episode-clustered
bootstrap by underlying (2,000 draws), K=43 comparisons, BH reported.

**deep_itm**

| arm | family | n | mean% | median% | util | on **deployed** % | lag+1 bar % |
|---|---|---|---|---|---|---|---|
| pyramid | s1 | 346 | **−4.37** | −5.31 | 0.41 | **−10.59** | −4.47 |
| pyramid | ctrl_long | 291 | −1.94 | −2.83 | 0.39 | −5.83 | −2.29 |
| pyramid | ctrl_random | 104 | −3.66 | −5.51 | 0.40 | −8.66 | −2.88 |
| fixed_t1 | s1 | 430 | −8.19 | −12.20 | 1.00 | −8.19 | −8.33 |
| fixed_t1 | ctrl_long | 340 | −4.88 | −7.84 | 1.00 | −4.88 | −5.95 |
| fixed_hold | s1 | 430 | −12.55 | −20.23 | 1.00 | −12.55 | −13.41 |
| s2_only | s1 | 133 | −13.11 | −23.60 | 1.00 | −13.11 | −13.65 |

**slight_itm**

| arm | family | n | mean% | median% | util | on deployed % | lag+1 % |
|---|---|---|---|---|---|---|---|
| pyramid | s1 | 624 | **−3.85** | −7.50 | 0.36 | **−10.69** | −3.98 |
| pyramid | ctrl_long | 489 | −2.72 | −5.85 | 0.35 | −6.34 | −2.83 |
| pyramid | ctrl_random | 204 | −2.05 | −8.05 | 0.35 | −6.88 | −1.97 |
| fixed_t1 | s1 | 711 | −6.77 | −19.24 | 1.00 | −6.77 | −7.95 |
| fixed_hold | s1 | 711 | −9.62 | −33.02 | 1.00 | −9.62 | −11.25 |
| s2_only | s1 | 72 | −17.40 | −33.56 | 1.00 | −17.40 | −17.01 |

Three things fall out.

1. **Every arm is significantly below zero.** `pyramid` −4.37pp
   (CI [−5.65, −2.84], q=0.0024), `fixed_t1` −8.19 (q=0.0024), `fixed_hold`
   −12.55 (q=0.0024), `s2_only` −13.11 (q=0.0024); the slight-ITM band is the
   same story. This confirms and slightly worsens the established −5…−6% honest
   carry/cost floor for a barrier-managed long-premium hold.
2. **The pyramid "wins" only because it deploys less capital.** It beats
   `fixed_t1` by +3.83pp (q=0.0024) and `s2_only` by +8.74pp (q=0.0065) on
   *allocated* capital — but its utilisation is 0.36–0.41, because the higher
   timeframe confirms in only ~1 episode in 5. On **deployed** capital the
   ranking inverts: pyramid −10.6%, `fixed_t1` −8.2%. Sizing small in front of a
   losing entry loses less money. That is arithmetic, not edge.
3. **No arm beats its matched control.** The only signal-vs-control results
   near significance run the *wrong* way: deep-ITM pyramid is **worse** than
   `ctrl_long` by −2.43pp (p=0.019, q=0.058). Every other comparison
   (`ctrl_random`, `ctrl_short`, both bands, all four arms) is null.

**Extra-bar-lag variant.** Adding one 30m bar of lag to every entry and exit
moves the pyramid by 0.10–0.13pp and `fixed_t1` by 0.14–1.18pp. Consistent with
the established fill-insensitivity of a multi-session hold; it does not rescue
anything.

**Per non-overlapping quarter** (mean roc_base %, deep_itm / slight_itm
pyramid): 2025Q2 −5.26 / −5.61, 2025Q3 −3.47 / −2.73, 2025Q4 −4.48 / −6.15,
2026Q1 −2.36 / −3.27, 2026Q2 −6.70 / +0.46. Negative in 9 of 10 quarter-cells;
no era carries it and none rescues it.

**Maturity-rule grid for the scale-out** (6 rules × 2 bands, reported in full):
deep-ITM pyramid ranges −3.86 (`range_compress`) to −4.58 (`macd_fade`);
slight-ITM −3.45 to −4.03. The exit rule moves the answer by ≤ 0.7pp against a
−4pp mean. Consistent with study (3): the scale-out is decorative.

## Inside the pyramid — what the early tranche actually costs

| band | leg | n | mean Rs | median Rs | win% | mean on deployed % |
|---|---|---|---|---|---|---|
| deep_itm | completed (s1+s2, 3u) | 40 | −8,136 | −15,317 | 25.0 | −10.85 |
| deep_itm | abandoned (s1 only, 1u) | 306 | −2,638 | −3,697 | 34.0 | −10.55 |
| slight_itm | completed (s1+s2, 3u) | 28 | −7,316 | −25,797 | 35.7 | −9.75 |
| slight_itm | abandoned (s1 only, 1u) | 596 | −2,683 | −5,431 | 34.4 | −10.73 |

88% (deep-ITM) and 96% (slight-ITM) of pyramids are abandoned after the first
tranche — higher than the 80% of study (2), because option fillability further
thins the confirmed cell. Completed pyramids are
**not better** than abandoned ones per rupee deployed (deep-ITM difference
−0.29pp, p=0.95; slight-ITM +0.98pp, p=0.93). The add-on tranche buys nothing;
it just triples the size on a base-rate trade — exactly what study (2)
predicted.

## `winner_concentration_profile` — the shape of the strategy

Rupees, primary rule, signal family. `carry` = how many of the largest winners
account for **half** of all gross gains.

| band | arm | n | total Rs | mean Rs | median Rs | win% | top-3 Rs | ex-top-3 Rs | carry | top-5% share of gains |
|---|---|---|---|---|---|---|---|---|---|---|
| deep_itm | pyramid | 346 | −1,132,765 | −3,274 | −3,985 | 32.9 | 129,738 | −1,262,503 | **20** | 46.4% |
| deep_itm | fixed_t1 | 430 | −2,641,841 | −6,144 | −9,148 | 37.0 | 201,534 | −2,843,374 | 38 | 33.8% |
| deep_itm | s2_only | 133 | −1,307,486 | −9,831 | −17,699 | 30.1 | 227,861 | −1,535,347 | 9 | 43.4% |
| slight_itm | pyramid | 624 | −1,803,883 | −2,891 | −5,628 | 34.5 | 232,830 | −2,036,713 | 38 | 44.8% |
| slight_itm | fixed_t1 | 711 | −3,609,226 | −5,076 | −14,427 | 36.1 | 749,984 | −4,359,210 | 52 | 40.2% |

Stated plainly, as a description of the strategy shape rather than only as a
flag:

* The payoff is **extremely concentrated by design**. In the deep-ITM pyramid,
  **20 of 346 episodes (5.8%) carry half of every rupee of gross gain**, and the
  best 5% of episodes deliver **46%** of all gross gains. Win rate is 33%,
  median episode −Rs 3,985: the arm is a long tail of small losses punctuated by
  rare large wins. That is the correct shape for the owner's design — it is what
  pyramiding into a trend is *supposed* to look like.
* **But the tail is not big enough.** Top-3 episodes contribute Rs 129,738 of
  gains against a total of −Rs 1,132,765. Removing the top 3 moves the mean from
  −4.37% to **−4.91%** (deep-ITM pyramid), −3.85% → −4.37% (slight-ITM pyramid),
  −8.19% → −8.88% (deep-ITM fixed). **The result does not depend on a few
  winners — it is negative with or without them.** That is unusual and is the
  cleanest possible read: this is not a fragile positive, it is a robust
  negative.
* The one cell that looks positive is the concentration lesson in miniature:
  slight-ITM **index** `fixed_t1`, n=32, mean **+12.01%**, median **+0.27%** —
  driven by one FINNIFTY episode returning +238% of allocation. Ex-top-1 the
  cell is +4.72%; **ex-top-3 it is −4.67%**. On n=32 this is noise wearing a
  headline.

## `vehicle_sensitivity`

| band | market | arm | n | mean% | median% | win% |
|---|---|---|---|---|---|---|
| deep_itm | index | pyramid | 16 | −3.19 | −0.53 | 37.5 |
| deep_itm | index | fixed_t1 | 19 | −1.64 | −1.60 | 42.1 |
| deep_itm | stock | pyramid | 330 | −4.42 | −5.60 | 32.7 |
| deep_itm | stock | fixed_t1 | 411 | −8.49 | −12.79 | 36.7 |
| slight_itm | index | pyramid | 26 | −0.66 | −0.53 | 46.2 |
| slight_itm | index | fixed_t1 | 32 | +12.01 | +0.27 | 50.0 |
| slight_itm | stock | pyramid | 598 | −3.99 | −7.91 | 33.9 |
| slight_itm | stock | fixed_t1 | 679 | −7.65 | −20.95 | 35.5 |

* **Index cells are n=7–32 and cannot settle anything.** They are directionally
  consistent with the established finding that index monthlies are the only
  holdable vehicle (index medians −0.5 to −1.6% vs stock medians −5.6 to −21%),
  but every index mean is one episode away from flipping.
* **Deep-ITM beats slight-ITM on stocks** for the fixed arms (median −12.8% vs
  −21.0% deep vs slight at `fixed_t1`), which reproduces the established
  moneyness ordering. It does not make either positive.
* **Stocks bleed at every moneyness and in every arm**, as established.

## `what_cleared_costs`

**Nothing.** Not one arm, band, market, quarter or maturity rule produced a
mean return on allocated capital above zero with an interval that excludes zero.
The best cell in the entire study is slight-ITM index `fixed_t1` at +12.01% on
n=32, which is −4.67% once its three largest episodes are removed.

## `what_did_not`

Everything, and at these magnitudes:

* the owner's pyramid: **−4.37%** (deep-ITM) and **−3.85%** (slight-ITM) per
  episode on allocated capital, **−10.6%/−10.7% on deployed capital**, q=0.0024;
* fixed-size at stage-1: −8.19% / −6.77%;
* fixed-size held through: −12.55% / −9.62%;
* stage-2-only (skip the early tranche): −13.11% / −17.40% — the worst arm, and
  the one the owner's model would pick if the early tranche were dropped;
* every arm against every matched control: null, except deep-ITM pyramid being
  **worse** than unconditional-long by 2.43pp;
* every maturity rule for the scale-out: within 0.7pp of each other, all
  negative.

## `honest_verdict`

**The scale-out is decorative and the pyramid does not make money. Kill it.**

Study (3): maturity/saturation is **not** detectable causally in time to act.
The rules that track exhaustion fire 4–5 sessions after the move has already
topped, two-thirds of the time; the rules that fire early are short fixed-time
exits in disguise; on the population you could actually trade, every maturity
exit is significantly worse than just leaving after 3 sessions; and on the
ex-post population where a move genuinely happened, no rule beats doing nothing
and holding. The half of the owner's model that says "we exit similarly as trade
matures" has no measurable content.

Study (4): the full structure, simulated end to end on the only holdable
vehicle, net of costs, loses **4–5% of allocated capital per episode** and
**~10–11% of deployed capital**, significantly below zero at q=0.0024, in 9 of
10 quarters, at every moneyness, on both markets, under all six maturity rules,
and with an extra bar of execution lag. It does not beat fixed sizing on
deployed capital, it does not beat stage-2-only on any honest basis, and it does
not beat a coin flip. The apparent advantage of pyramiding over fixed sizing is
entirely a capital-utilisation artefact: staking less on a losing entry loses
less.

The payoff shape is exactly what the owner described — 33% win rate, 5.8% of
episodes carrying half the gains, a long tail of small losses. That shape is not
the problem. The problem is the entry, and pass (2) already located it: the
higher-timeframe confirmation happens *because* the move happened, so the
cascade selects nothing, and no exit discipline or sizing schedule can rescue an
entry with no edge. Three passes have now converged on the same conclusion from
three directions.

**What would have to change before this is worth re-running.** A stage-2
definition that is not mechanically downstream of the realized move, and it must
be pre-registered. If and only if such a definition shows forward information at
the tradeable bar, the machinery in `pyr_run.py` is ready to price it — the
pyramid, scale-out and control arms all run off a single episode table.

## Data limits and honesty notes

1. **Single era, thin index cells.** 2025-02-06 → 2026-07-03, 225 instruments,
   5,717 stage-1 episodes; option fills survive for 346–711 episodes per arm/band
   and only **7–32 in the index cells**. Index conclusions here are directional
   only.
2. **The vehicle caps the hold at 10 sessions.** A DTE 8–22 monthly cannot be
   held to the 20-session maturity horizon of study (3). Study (3) is measured
   on spot over 20 sessions; study (4) on options over ≤10 sessions and always
   out at expiry − 2 days. The two are therefore not the same window, by
   necessity, and this is a property of the vehicle rather than a modelling
   choice.
3. **5.8% of entry quotes were below intrinsic** and were floored. Without the
   floor, one CDSL print alone produced a 22× episode that dominated the
   slight-ITM concentration table. Illiquid stock option prints remain the
   largest single data risk in this study.
4. **Multiplicity.** Study (3): K=36 paired comparisons plus 27 descriptive grid
   cells. Study (4): K=43 comparisons, Bonferroni α=0.00116, BH q reported next
   to every raw p. Nothing was selected on a p-value.
5. **Causality is proven, not asserted.** `test_maturity_causality.py` — 6/6
   pass: prefix invariance of every daily feature at rtol 1e-12; prefix
   invariance of the maturity fire session itself; future-bar perturbation
   (scaling all bars after the fire by 1.37× leaves it bit-identical);
   next-session-only execution on the real session grid; contract selection
   strictly from an earlier session; and the second tranche entering strictly
   after the confirming daily bar. These tests live outside `backend/tests` and
   do not touch the 1271/8 suite baseline.
6. **The primary maturity rule was named before study (4) ran but after study
   (3) had reported**, so it is not blind. This is disclosed rather than papered
   over; the full 6-rule grid is reported and spans 0.7pp, so the choice is
   immaterial.
7. **The ex-post "+2 ATR MFE" subset in study (3) conditions on the outcome** and
   is labelled as such throughout. It exists to answer "given a move happened,
   how much do you keep", which cannot be asked any other way, and it is never
   used as an entry rule.
8. Known Fyers cross-symbol tick contamination still affects mid-July 2026 stock
   spot; the contamination guard inherited from `harness.load_spot()`
   (ATR%>15%, |daily return|>25%) is applied unchanged.
