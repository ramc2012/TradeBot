# Adversarial verification of the cascade / maturity / pyramid series — 2026-07-21

Scope: verify studies (1) regime duration, (2) the two-stage cascade, (3) maturity
detection and (4) pyramid economics before any of them is allowed near a lane.
Research only. No lane code, no flags, no restarts. Suite green (1367 passed / 8
skipped) — no file owned by the Gann or expiry-hold workflows was touched.

**Bottom line.** The series' final verdict (*kill it*) survives, but **two of its
headline numbers do not**. A moneyness truncation in the option extract understated
every arm by 0.8–12.0 percentage points, and the ordering it produced — "the pyramid
beats fixed sizing" — **inverts** once the tape is repaired. After repair, still
nothing beats a matched control: **0 of 24 signal-vs-control comparisons survive BH
correction**, and the best raw p is 0.0245 against `ctrl_short`.

---

## Per-check verdicts

| # | Check | Verdict |
|---|---|---|
| 1 | No lookahead | **PASS** (end-to-end, and the test discriminates) |
| 2 | Option price path complete | **FAIL — defect found, quantified, repaired** |
| 3 | Costs materially applied | **PASS** (1.60% of turnover = 23–112% of gross P&L) |
| 4 | Episode clustering + multiplicity | **PASS**, recomputed independently |
| 5 | Ex-top-3 and per-quarter | **PASS** — negative with and without the winners |
| 6 | Execution stress (extra bar lag) | **PASS** — ≤0.4pp |
| 7 | Regime + cascade re-derived independently | **PASS** — both reproduce |
| 8 | Hygiene | **PASS** |
| — | Session-index holes | **FAIL — second defect found, immaterial (0.12%)** |

---

## Defect 1 (P0): the ±8% moneyness wall truncated the winners

`backend/directional_options/research/setups_2d3d/extract.py:53`

```sql
AND abs(strike - underlying_price) <= 0.08 * underlying_price
```

This is a **per-bar** predicate, not a per-contract one. A contract's bars vanish
from the CSV the moment spot moves more than 8% away from the strike. A pyramided
winner drives its contract deep ITM — straight through that wall — so **the winners
are exactly the trades whose exit bar goes missing**. `pyr_run.prem()` then falls
back silently to the last surviving bar.

Measured (`ver_trunc.py`), deep-ITM signal episodes:

| | stale-price lookup rate | mean ROC |
|---|---|---|
| winners | **71.0%** | +0.094 |
| losers | 30.6% | −0.111 |

15.4% of traded contracts have tapes that die within 0.5pp of the 8% wall; 5.5% die
at exactly 0.0800.

**Repair.** `ver_full_tape.py` re-pulled the tape for all 5,443 traded contracts with
the moneyness predicate removed (time bounded directly by literal UTC timestamps, one
window at a time, per the PG rule). **117,096 bars recovered — 100% of them at
|moneyness| > 8%.** Contract *selection* is unaffected (selection happens inside a
|mny| ≤ 6% band): `identical_contract = 7897/7897` and `11497/11497`. So the diff is
clean and attributable to the missing bars alone.

Hand-derivation of one full episode (`ver_hand.py`, ICICIGI 2026-03-13 PE) confirms
the recovered bars are genuine and liquid — volume 10,400 / OI 136,500 on the tranche-2
contract — and that the shipped run priced its exit off a bar 12 days stale.

### Effect (mean return on allocated capital, base cost 1.6%)

| band | arm | shipped | **repaired** | Δ |
|---|---|---|---|---|
| deep_itm | pyramid | −4.37% | **−2.77%** | +1.6pp |
| deep_itm | fixed_t1 | −8.19% | **+0.36%** | +8.6pp |
| deep_itm | fixed_hold | −12.55% | **−0.55%** | +12.0pp |
| deep_itm | s2_only | −13.11% | **−3.34%** | +9.8pp |
| slight_itm | pyramid | −3.85% | **−3.08%** | +0.8pp |
| slight_itm | fixed_t1 | −6.77% | **−3.03%** | +3.7pp |

**Two shipped claims are now wrong:**

1. *"All four arms are significantly below zero (q=0.0024)."* False. After repair
   only the pyramid arms remain significantly negative; deep-ITM `fixed_t1` is
   +0.36% (95% CI [−3.3%, +4.2%], p=0.86) and `fixed_hold` −0.55% (p=0.83).
2. *"The pyramid beats fixed_t1 by +3.83pp (q=0.0024) — a capital-utilisation
   artefact."* **Inverts.** Repaired, the pyramid is **3.13pp worse** than fixed_t1
   on deep-ITM (95% CI [−6.01%, −0.34%], p=0.028, q=0.21). The bug had been
   suppressing fixed_t1 harder than the pyramid, because fixed_t1 stakes 3 units on
   the truncated tranche-1 contract while the pyramid stakes 1.

The direction of the bias was *toward* the shipped conclusion, which is why it was
not caught: it made a losing strategy look more losing.

---

## Defect 2 (P2, immaterial): session-index holes

Every horizon in the series is counted in **session index**, not calendar time. The
spot table has gaps for 10 of 225 names — CUMMINSIND has **no session at all between
2025-09-23 (sidx 39) and 2026-03-23 (sidx 40)**, a 181-day hole. An episode entered
at sidx 33 therefore has its "10-session" outcome resolved against prices six months
later, and shows MFE 9.0 ATR purely because the clock jumped. `simulate()` drops
*truncated* tapes but a hole is not a truncation.

This is what the end-to-end lookahead test surfaced (1 label mismatch in 2,612).
Blast radius: **11 of 9,423 episode-trades (0.12%)**, all CUMMINSIND and CONCOR.
Removing them moves every headline by ≤0.15pp. Reported, not hidden — the same class
of defect could matter a lot in a study with fewer episodes.

---

## Check 1 — lookahead: PASS, and the test discriminates

`ver_lookahead.py` deletes everything after 2026-01-05, rebuilds the entire pipeline,
and compares every episode whose stage-2 window and label horizon close before
2025-11-15:

```
episodes in comparable window: full=2612  after-cut=2612
  episode-set mismatches                        0
  [PASS] s2 / s2_lag / con1 / con2 mismatches   0 / 2612
  [PASS] px1 / px2 / atr   (rtol 1e-12)         0
  [FAIL] large                                  1 / 2612   <- defect 2, not lookahead
DISCRIMINATION: contaminated variant (30m ADX shifted 1 bar into the future)
                differs by 331 keys -> the test CAN detect lookahead
```

The shipped `test_cascade_causality.py` passes 6/6 but only proves prefix invariance
of the indicator filters on synthetic data; its one path test perturbs bars *before*
the entry, which is the wrong direction for lookahead. Contract selection provably
does not peek: `con1`/`con2`/`px1`/`px2` are byte-identical when the future is deleted.

## Check 3 — costs

| band | arm | premium turnover | cost | as % turnover | as % of gross P&L |
|---|---|---|---|---|---|
| deep_itm | pyramid | Rs 10,650,000 | Rs 170,400 | 1.60% | 31.0% |
| deep_itm | fixed_t1 | Rs 32,250,000 | Rs 516,000 | 1.60% | 81.8% |
| slight_itm | fixed_t1 | Rs 53,325,000 | Rs 853,200 | 1.60% | **111.6%** |

Costs are real and, in the largest cell, exceed the entire gross edge.

## Check 4 — clustering and multiplicity, recomputed

346 deep-ITM pyramid rows = 346 distinct (underlying, entry_time, side); 162
underlyings; median gap between consecutive episodes of the same name 76 days, with
7.1% overlapping a 10-session hold. Clustering the bootstrap by **underlying** is
therefore the correct unit and is what `ver_stats.py` does (4,000 resamples).

**K = 60 comparisons** reported. Bonferroni α=0.05 requires raw p < 0.00083. Five
survive BH q<0.05 — and **every one of them is an arm being significantly negative**,
not a signal beating anything:

```
vs_zero  deep_itm   pyramid  ctrl_short  -0.0529  p=0.0010 q=0.0300
vs_zero  slight_itm fixed_t1 ctrl_short  -0.0830  p=0.0010 q=0.0300
vs_zero  slight_itm pyramid  s1_primary  -0.0308  p=0.0015 q=0.0300
vs_zero  deep_itm   pyramid  s1_primary  -0.0277  p=0.0025 q=0.0375
vs_zero  slight_itm fixed_hold ctrl_long -0.0923  p=0.0040 q=0.0480

SIGNAL-BEATS-CONTROL surviving q<0.05:  NONE
```

Best signal-vs-control result of all 24: deep-ITM `fixed_t1` vs `ctrl_short`,
+7.83pp, raw p=0.0245, **q=0.21** — and beating "always short" over a period the
market rose is not evidence of anything.

## Check 5 — winners and quarters

| band / arm / family | n | all | ex-top3 | ex-top5% | win rate | top-5% share of gains |
|---|---|---|---|---|---|---|
| deep_itm pyramid signal | 346 | −2.77% | −3.54% | −5.10% | 35.3% | 43.6% |
| deep_itm fixed_t1 signal | 430 | +0.36% | −0.83% | −6.15% | 39.8% | 35.7% |
| slight_itm pyramid signal | 624 | −3.08% | −3.88% | −6.14% | 34.9% | 48.6% |

The long-tail payoff shape the owner described **is real** — the best 5% of episodes
carry 36–49% of all gross gains. But the controls have the *same* shape (ctrl_long
top-5% share 36–46%), so it is a property of long options, not of the cascade. And
removing the top 3 makes every cell **more** negative: this is a robust negative, not
a fragile positive. Per non-overlapping quarter, the signal is positive in **1 of 5**
quarters (deep-ITM pyramid), 2 of 5 (deep-ITM fixed_t1), with the sign alternating.

## Check 6 — execution stress

+1 entry/exit bar lag on **both** tranches moves the repaired deep-ITM pyramid from
−2.77% to −3.03% and fixed_t1 from +0.36% to −0.12%. A liquidity-stressed fill
(only bars that actually traded, i.e. last-traded-price execution) moves them to
−2.81% and +0.16%. Neither changes any conclusion. Consistent with the established
fill-insensitivity of a multi-session hold.

## Check 7 — regime and cascade re-derived independently

**Regime**, recomputed with a Wilder ADX written from scratch (no shared code):

| ADX ≥ | index | commodity | stock |
|---|---|---|---|
| 20 | 56.1% | 60.5% | 55.7% |
| **25** | **31.1%** | **40.3%** | **34.9%** |
| 30 | 15.6% | 26.3% | 20.0% |

At ADX≥25 the mean trending share is 35.0% ⇒ **P1 "consolidation dominates"
CONFIRMED** (65.0% non-trending), matching the shipped 33.7/39.7/34.9 closely. The
**threshold fragility is real and reproduces**: at ADX≥20 trending is 55.9% and the
premise inverts. Median run length at ADX≥25: trending 15.0 sessions vs consolidating
26.0 — consolidation runs ~1.7× longer, as reported. P2 ("moves happen for small
time") remains correctly rejected.

**Cascade**, recomputed from the episode builder with an independent bootstrap:

- P(stage-2 within 3 sessions | stage-1) = **0.2012** vs control 0.0886 →
  **+0.1126, 95% CI [+0.1036, +0.1213], p=0.0003, ratio 2.27×**. The ordering
  phenomenon is **real** and reproduces exactly.
- Measured **from the stage-1 bar** (conditions on a later event — not decision-time
  information): cascade 0.4781 vs *control bars also followed by a confirm* 0.4670.
  Difference **+0.0091, p=0.60**. The entire apparent lift is reproduced by random
  bars that happened to be followed by the same daily confirm.
- Measured **from the stage-2 bar** — the only moment the second tranche is buyable:

  | arm | n | P(large) |
  |---|---|---|
  | cascade | 1,107 | 0.2692 [0.2447, 0.2941] |
  | ctrl_long | 572 | 0.2797 |
  | ctrl_short | 470 | 0.2766 |
  | ctrl_random | 235 | 0.2894 |
  | unconditional base rate | — | 0.2644 |

  All identical, all **below the 33.3% break-even** of the 2:1 barrier, before any
  option carry. **Confirmed: the higher timeframe confirms because the move already
  happened.**

## Maturity re-check on the repaired tape

The shipped maturity study is on spot and is unaffected by the option defect, but the
scale-out economics are. Re-running all six exit rules on the repaired tape: the
whole grid spans ≤1.1pp, the pyramid is negative under **every** rule, and the best
cell (deep-ITM fixed_t1 / `ma_ext`, +0.83%) beats its own `ctrl_long` (+0.42%) by
0.4pp. The choice of scale-out rule does not matter — confirming "the scale-out is
decorative" for a different reason than reported: the rules mostly never fire inside
the vehicle's 10-session cap.

---

## Survives / does not survive

**Survives (as a fact, not as an edge):**
- Consolidation dominates at ADX≥25 (65% of sessions), threshold-fragile at 20.
- Sequential confirmation is a real ordering phenomenon: 2.27× elevation, p=0.0003.
- The pyramid's long-tail payoff *shape* — but controls have the same shape.

**Does not survive:**
- Any forward information in stage-2 at the tradeable bar (0.269 vs 0.264 base).
- Maturity detection in time to act.
- Every entry arm against every matched control — 0 of 24 after BH.
- The shipped claim that the pyramid beats fixed sizing (**inverts** after repair).
- The shipped claim that all four arms are significantly negative (**false** after
  repair for the deep-ITM fixed arms, which are indistinguishable from zero).

**Nothing qualifies for a trading flag.**

---

# Paper-wiring design

**Recommendation: do NOT wire a trading lane.** Nothing in three studies and this
verification produced a signal that beats an unconditional-long control on the same
bars. What follows is therefore designed as a **falsification harness**, not as a
strategy: it exists to be killed cheaply, and it is deliberately structured so that
the decision to kill is made by a pre-registered rule rather than by judgement.

## D0. The power problem, stated first

The only *holdable* vehicle established by the panel study is the **index deep-ITM
DTE 8–22 monthly**. The cascade fires **16 index episodes in 408 days**. At the
observed per-episode SD that is a 95% confidence half-width of roughly ±8pp — a
decade of paper trading would be needed to resolve a 3% effect on indices alone.

| cell | episodes / month | SD per episode | 95% half-width at n=50 / 100 / 200 |
|---|---|---|---|
| deep-ITM pyramid, all names | 25.4 | 0.169 | 4.7pp / 3.3pp / 2.3pp |
| deep-ITM fixed_t1, all names | 31.6 | 0.441 | 12.2pp / 8.6pp / 6.1pp |
| deep-ITM, **index only** | ~1.2 | — | not reachable |

**Consequence:** a paper test that is powerful enough to conclude anything must run
on **stocks**, which the panel study already showed bleed at every moneyness
(3-session carry −3.79% deep-ITM, −8.65% slight-ITM). The harness is therefore
measuring a vehicle we already know is negative-carry. This is the single strongest
argument for not running it at all, and it is an owner decision, not mine.

## D1. Universe and vehicle

- **Universe:** the existing directional universe — 6 indices + NIFTY-50 stocks —
  filtered to names with a complete 30m spot tape for the trailing 60 sessions.
  **Hard exclusion:** any name whose last two session indices are more than 7
  calendar days apart (defect 2 guard).
- **Vehicle:** monthly expiry, DTE 8–22 at entry, signed moneyness in
  **[−6.0%, −3.0%]** (deep ITM). Slight-ITM is excluded — it was worse in every arm.
- **Contract tracking:** selected from the **15:15 snapshot of the prior session**,
  one tracked contract per (name, side, session). Never re-selected intraday.
- **Instrument-resolution guard:** reuse the existing fail-closed catalog resolution;
  decline the episode if the tracked key cannot be resolved.

## D2. Triggers (frozen — no re-tuning inside the paper run)

**Stage-1 (30m, decision bars 09:15–14:45 IST, actionable at the next 30m open):**
fresh MACD(12,26,9) signal-line cross in the trade direction **AND** 30m EMA20/EMA50
already ordered that way **AND** 30m ADX(14) > 20 **AND** the daily state is *not yet*
confirmed as of the last **closed** daily bar (session s−1).

**Stage-2 (daily, actionable at the first 30m open of the session AFTER the confirming
daily bar):** a False→True transition of {daily MACD histogram sign = side **AND**
close on the correct side of daily SMA20 **AND** daily ADX(14) > 20 and rising vs 3
sessions back}, occurring in sessions [s0 … s0+3].

**Episode clustering:** fires of the same (name, side) within 3 sessions collapse to
the first. One open episode per (name, side) at a time.

## D3. Sizing, adds, exits

| element | rule |
|---|---|
| tranche 1 | 1 unit at the stage-1 entry bar open |
| tranche 2 | +2 units at the stage-2 entry bar open, only if stage-2 fires within 3 sessions |
| unit | Rs 25,000 of premium; max Rs 75,000 per episode |
| abandonment | no stage-2 by the open of session s0+4 → close tranche 1 in full |
| hard stop | spot touches −1.0 × entry-session daily ATR14 → close everything at that 30m bar's close |
| scale-out | first `atr_contract` fire closes half at the next session's open; second fire closes the rest |
| time cap | 10 sessions from the arm's own first entry |
| expiry cap | always flat by expiry − 2 calendar days |
| lane cap | max 6 concurrent open episodes; max 2 per sector; Rs 450,000 gross premium |

`atr_contract` = ATR14 below its value 3 sessions ago, armed only after ATR14 has
first expanded to ≥1.15× its entry level.

## D4. Flag and kill-switch

- `DIRECTIONAL_CASCADE_PYRAMID_PAPER` — **default OFF**, paper-only. There is no
  live path and none is to be added.
- The flag gates *order emission only*. Signal computation and logging run
  unconditionally so a shadow record accrues even with the flag off — **this is the
  option I actually recommend: run it flag-OFF for one quarter and analyse the log.**
- Kill-switch `DIRECTIONAL_CASCADE_PYRAMID_HALT` — a single boolean that immediately
  stops new episodes and closes open ones at the next 30m open. Tripped
  automatically by any falsification criterion in D6, and settable by hand.
- Reuses the existing lane registry + `LaneSnapshot` so the lane cannot become a
  ghost lane, and the existing per-cadence broker routing (this lane is slow-cadence:
  decisions at 30m and daily only).

## D5. What gets logged (one row per episode, immutable)

Decision-time, before any outcome is known:
`episode_id, underlying, side, s0, stage1_bar_ts, m_macd, m_macd_sig, m_ema20,
m_ema50, m_adx14, daily_state_prev, pd_atr14, tracked_instrument_key, strike,
expiry, dte, signed_moneyness, entry_premium, entry_spot, units`

Then, appended as they occur:
`stage2_fired, stage2_session, stage2_lag, tranche2_bar_ts, tranche2_premium,
maturity_rule_fired, maturity_session, exit_reason ∈ {stop, mat1, mat2, timecap,
abandon, expiry}, exit_bar_ts, exit_premium, exit_spot, realised_pnl_gross,
costs_charged, realised_pnl_net, roc_allocated, roc_deployed`

Data-integrity fields — **these are the fields this verification proves are
mandatory**:
`option_bar_exact (bool), option_bar_staleness_minutes, quote_below_intrinsic (bool),
session_gap_days_at_entry, session_gap_days_over_hold, contract_tape_last_absmny`

An episode whose exit was priced off a stale bar, or whose hold spanned a session
gap, is flagged and **excluded from the headline** — reported separately. Had these
six fields existed, both defects in this report would have been caught on day one.

## D6. Falsification criteria — pre-registered, evaluated on `roc_allocated`

Evaluate at n = 50, 100, 200 completed episodes (stocks + indices pooled; index-only
is reported but never decides anything, per D0).

**STOP — kill the lane** if *any* of:

1. **n = 50:** mean `roc_allocated` < **−5.0%**. (Prior estimate −2.77%; a −5%
   realisation is worse than the established carry floor and needs no more data.)
2. **n = 100:** the 95% cluster-bootstrap CI of (signal − matched `ctrl_long` run on
   the same bars) **excludes +2.0%**. At n=100 the half-width is ~3.3pp, so this is
   the point at which "the cascade adds nothing" becomes decidable.
3. **n = 100:** stage-2 confirm rate falls below **12%** — the mechanism itself has
   stopped reproducing out of sample (measured 20.1%, control 8.9%).
4. **n = 200:** mean `roc_allocated` CI does not exclude 0 from below — i.e. two
   quarters of data and still no positive edge.
5. **Any time:** > 20% of exits flagged `option_bar_exact = false`, or > 5% flagged
   `quote_below_intrinsic` — the data is not good enough to measure the strategy,
   which is the failure mode that produced this report.
6. **Any time:** cumulative paper drawdown on allocated capital exceeds **12%**.

**CONTINUE** only if at n=100 the signal−`ctrl_long` CI lower bound is above 0 **and**
the result is positive in ≥3 of the trailing 4 non-overlapping months. Anything else
is a STOP. There is no "inconclusive, keep going" branch beyond n=200.

At ~25 episodes/month universe-wide, n=50 lands in ~2 months, n=100 in ~4, n=200 in
~8. **Criterion 1 is reachable inside one quarter, which is the point.**

## D7. Owner decisions needed

1. **Run it at all?** The measured expectation is −2.8% of allocated capital per
   episode and the only holdable vehicle (index monthlies) is unmeasurable within a
   decade. My recommendation is **shadow/log-only for one quarter** (flag OFF, D4),
   then decide from the log. Wiring paper fills buys almost nothing that the shadow
   log does not.
2. **Stocks or nothing.** Powering the test requires stocks, which the panel study
   already showed bleed at every moneyness. Accepting that is an explicit decision to
   measure a vehicle we expect to lose.
3. **Should the ±8% wall be fixed at source?** `setups_2d3d/extract.py:53` corrupts
   every future option study run off that cache, not just this one. I recommend
   re-pulling the option research cache without the per-bar moneyness predicate
   (a per-*contract* filter at selection time is the correct construction) — but the
   extract is shared, so this is a scheduling call, not mine.
4. **Should the spot session-gap guard be added to the live lanes?** CUMMINSIND,
   CONCOR, GOLD, NICKEL and six others have multi-week holes. Any lane that counts
   horizons in sessions rather than calendar days has the same latent defect.
5. **Re-pre-registration.** Study (2) showed the stage-2 definition is mechanically
   downstream of the realised move. If the owner wants the cascade rescued, the next
   pass needs a stage-2 that is **not** downstream, pre-registered before it is
   measured. `pyr_run.py` will price such a definition off a single episode table.
