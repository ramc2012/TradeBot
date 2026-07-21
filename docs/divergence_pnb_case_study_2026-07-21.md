# PNB worked example — case-study reconstruction (Part 1 of the divergence study)

**Date:** 2026-07-21 · **Scope:** RESEARCH ONLY, no lane code, no flags, nothing wired.
**Code:** `backend/directional_options/research/divergence/case_pnb.py`, `case_pnb_options.py`, `case_pnb_full.py`
**Data:** `backend/directional_options/research/divergence/data/*.csv`, extracted from PG with **literal UTC time bounds only** (no function on the partitioning column).

This part verifies the owner's account against **our own store**. No generalisation.
**No moneyness band is applied anywhere** — the contract under test travels OTM → deep ITM, and the
`setups_2d3d/extract.py:53` ±8% filter would delete exactly the tape being tested.

---

## Verdict in one line

The owner's *structure* is real and mostly reconciles, but **three of the specifics are off**: the daily
crossover is **2026-05-25, not 05-22**; the "higher low" on 07-08 is higher only against the **May low**
(it is a *lower* low against the June pivots); and — the important one — **the 600% did not come from the
OTM→ITM transition**. Further-OTM strikes did the same or better on identical entry/exit dates.

---

## A. Data quality (must be read before the numbers)

| Item | Finding |
|---|---|
| PNB 30m spot | 4,162 rows, 321 sessions, 2025-03-28 → 2026-07-20; 13 bars/session everywhere except 2025-10-21 |
| **Missing session** | **2026-07-13 is absent from PNB `underlying_spot_candles` entirely.** It was a real trading day — only 104 of ~225 underlyings have 07-13 data (broad partial outage). PNB option candles for 07-13 exist. |
| 07-13 repaired | Put-call parity on the 105 and 110 strikes reconstructs 07-13: **O 105.53 / H 106.75 / L 105.53 / C 106.73** (the two strikes agree to ~0.1). The missing bar was an **up** day, so the stored tape *understates* the recovery. |
| MACD sensitivity to the gap | Re-running daily MACD with the synthetic 07-13 bar: the **2026-07-17 bull cross survives** (hist +0.0500 repaired vs +0.0130 as-stored). No crossover date changes. The as-stored cross was razor-thin (+0.013) — it happens to be robust here, but a one-session gap can flip a cross of that size, and that must be a standing caveat for the generalisation stage. |
| Spot source mix (Jun 01→) | upstox_spot 403 rows, fyers 39 rows (sessions **07-14, 07-15, 07-20** only) |
| **Fyers cross-symbol contamination** | **No signature found on PNB.** Max abs bar-to-bar move on fyers rows = 4.05% (the legitimate 07-20 gap-up); on upstox rows = 5.16%. External anchor: our 07-20 close **111.70** vs owner's TradingView **111.76** = **−0.05%**. TV's stated +5.66% implies a prior close of 105.77 vs our 07-17 close 106.01 (+0.23%, i.e. our last-30m-bar close ≠ official close — expected, not corruption). |
| Option 28-JUL 30m | 5,914 raw rows → 4,110 after source-preference dedupe (upstox > upstox_expired > fyers > fyers_chain). **1,804 of 4,110 (44%) keys are duplicated across sources**; median disagreement 0.0000, mean 0.29%, **max 19.4%**. Source mix skews fyers (3,474) over upstox (2,392). |
| Greeks | **IV populated on 1% of rows, delta on 1%** for PNB stock options. Confirms the known gap. |
| OI | 57% (CE) / 60% (PE) populated at 30m. |

---

## B. `macd_cross_may22` — date found, divergence quantified

**Daily MACD(12,26,9) on close, IST-session daily bars resampled from 30m.**

Bull signal-line crossovers in 2026: **2026-02-18, 2026-04-08, 2026-05-25, 2026-07-17**.

There is **no 2026-05-22 crossover**. 05-22 (Fri) had hist **−0.2750**; the cross fired on
**2026-05-25 (Mon)** with hist **+0.1137**, MACD −2.2637 vs signal −2.3774. The owner is one session early —
almost certainly reading the chart at the point where the histogram is visibly about to cross.

**Divergence: CONFIRMED, and quantified.** Using causal fractal pivot lows on the daily LOW (L=3, R=3,
confirmed R sessions later), the pivot low preceding the cross is **2026-05-18 (low 98.50, MACD −2.7337)**:

| prior pivot low | price | MACD | verdict |
|---|---|---|---|
| 2026-04-02 (99.79) | 99.79 → 98.50 = **−1.29%** | −4.9248 → −2.7337 = **+2.1912** | **BULLISH DIVERGENCE** |
| 2026-03-16 (108.20) | −8.96% | −3.0001 → −2.7337 = +0.2664 | bullish divergence (weak) |
| 2026-05-05 (105.45) | −6.59% | −1.8993 | no divergence |
| 2026-04-13 (107.82) | −8.64% | −0.1615 | no divergence |

The 04-02 → 05-18 pair is a textbook bullish divergence: price makes a marginally lower low while MACD
makes a **much** higher low (+2.19, i.e. ~44% less negative).

The **07-17** cross also had a divergence available: pivot **07-08 (low 100.45, MACD −0.8519)** vs pivot
**06-02 (low 102.67, MACD −1.3288)** → price −2.16%, MACD **+0.4769** = bullish divergence.

---

## C. `higher_low_jul8` — confirmed, but only against the *May* low

2026-07-08 **is** a causal pivot low (L3/R3), low **100.45**, close 101.09, on the highest volume of the
month to that point (29.0M).

| vs prior pivot | price change | MACD change |
|---|---|---|
| 2026-06-29 (106.11) | **−5.33%** | −1.2401 |
| 2026-06-12 (104.37) | **−3.76%** | −0.7850 |
| 2026-06-02 (102.67) | **−2.16%** | **+0.4769** (divergence) |
| **2026-05-18 (98.50)** | **+1.98% → HIGHER LOW** | **+1.8818** |
| 2026-04-02 (99.79) | +0.66% → higher low | +4.0730 |

So the owner's "higher low on 8th July" is **true against the May-18 swing low that anchored the original
divergence** — which is coherent, that is the low the divergence was measured from. It is **not** a higher
low against the nearer June pivots. Any generalised predicate must state *which* prior low it compares to;
the answer flips with the choice.

**Causal confirmation date: 2026-07-14** (three sessions after 07-08, and the 07-13 hole is why it is 07-14
rather than 07-13). PNB closed **105.05** on 07-14 — i.e. **4.6% above the low** by the time the structure
was confirmable.

---

## D. `spot_return_reconciliation` — 11% checks out, from the 07-08 low only

Exit = 2026-07-20 close **111.70** (ours) / 111.76 (TV).

| entry | price | return to 07-20 close |
|---|---|---|
| 2026-05-22 close (owner-stated cross) | 102.88 | +8.57% |
| **2026-05-25 close (actual daily cross)** | 106.49 | **+4.89%** |
| 2026-05-26 open (tradeable) | 106.13 | +5.25% |
| **2026-07-08 low (the higher low)** | 100.45 | **+11.20%** ← the owner's 11% |
| 2026-07-08 close | 101.09 | +10.50% |
| 2026-07-09 close (hourly cross day) | 103.70 | +7.71% |
| **2026-07-14 close (higher low CONFIRMED)** | 105.05 | **+6.33%** |
| 2026-07-17 close (2nd daily cross) | 106.01 | +5.37% |
| 2026-07-20 open (tradeable after that cross) | 108.26 | +3.18% |

**The 11% is the low-to-close reading, not a tradeable return.** The honest causal figures are
**+6.33%** (buy the confirmed higher low) and **+3.18%** (buy the open after the second daily cross).
The May crossover — the element the owner leads with — delivered **+4.89%** over ~2 months.

---

## E. `option_reconstruction` — PNB 106 CE 28-JUL-26

**The 2026-07-20 exit does not exist in our store.** The 106 CE 30m tape ends at **2026-07-17 15:15 IST,
close 2.72**. The contract dropped off the ATM tracker as it went ITM.

The owner's numbers nevertheless **cross-validate against our tape**:
- Owner: last 6.45, **+138.89%** on 07-20 ⇒ implied prior close **6.45/2.3889 = 2.70**. Our stored 07-17
  close is **2.72** (0.7% apart — consistent with last-30m-bar vs official close).
- Owner: "~1.10 around 2026-07-09". Our 106 CE **low on both 07-08 and 07-09 is exactly 1.11**.

So the owner's chart and our tape are the same instrument. Taking 6.45 as the exit (**clearly labelled
unverified**):

| entry (close) | premium | → 6.45 gross | net of 8% RT (assumed; no spread data) | at that day's LOW |
|---|---|---|---|---|
| 2026-07-03 | 3.00 | +115.0% | +107.0% | +150.0% |
| 2026-07-07 | 2.07 | +211.6% | +203.6% | +239.5% |
| **2026-07-08** | **1.24** | **+420.2%** | **+412.2%** | **+481.1%** |
| 2026-07-09 | 1.83 | +252.5% | +244.5% | +481.1% |
| 2026-07-10 | 2.65 | +143.4% | +135.4% | +243.1% |
| **2026-07-14 (higher low confirmed)** | **2.49** | **+159.0%** | **+151.0%** | +204.2% |
| **2026-07-17 (daily cross)** | **2.72** | **+137.1%** | **+129.1%** | +201.4% |

**Does ~600% reconcile?** Only as a **low-to-high chart reading**: 1.11 (07-08/09 low) → 6.88
(07-20 high) = **+520%**. The owner's "600%" is that reading, rounded up. The best *closing-basis*
number from a causally identifiable entry is **+159%** (07-14 confirmed higher low) or **+137%**
(07-17 daily cross). The +420% requires buying the exact session low of the panic day, before any
element of the setup was confirmable.

---

## F. `strike_choice_comparison` — the headline correction

**Fully data-backed common window** (07-08 close → 07-17 close; spot 101.09 → 106.01, +4.87%; all five
strikes have complete tape):

| strike | moneyness at 07-08 | premium | gross | net |
|---|---|---|---|---|
| 105 CE | −3.9% (OTM) | 1.48 → 3.27 | +120.9% | +112.9% |
| 106 CE | −4.9% (OTM) | 1.24 → 2.72 | +119.4% | +111.4% |
| 110 CE | −8.8% (OTM) | 0.55 → 1.29 | +134.5% | +126.5% |
| 111 CE | −9.8% (OTM) | 0.45 → 1.03 | +128.9% | +120.9% |
| 112 CE | −10.8% (OTM) | 0.36 → 0.84 | +133.3% | +125.3% |

**Every strike returned 119–135%. The strike choice was worth ~15pp on a 125% move — i.e. noise.**

Extending to 07-20 (110/111/112 are the only strikes with any 07-20 tape):

| strike | 07-17 → 07-20 | 07-08 low → 07-20 | low → high (chart reading) |
|---|---|---|---|
| **106 CE** | 2.72 → **6.45** = +138.9% *(owner, unverified)* | 1.11 → 6.45 = **+481%** | 1.11 → 6.88 = **+520%** |
| 110 CE | 1.29 → 2.36 = +82.9% *(tape stops 12:45)* | 0.49 → 2.36 = +382% | 0.46 → 3.40 = **+639%** |
| 112 CE | 0.84 → **2.00** = **+138.1%** *(full session)* | 0.33 → 2.00 = **+506%** | 0.33 → 2.34 = **+609%** |

The 112 CE — **10.8% out of the money at entry and still ~0.3% OTM at the exit** — returned
**+138.1%** on 07-20 against the 106's +138.9%, and **beat it** on the full-move close-to-close
(+506% vs +481%). The 110's lower +82.9% is a **stale-print artefact**: its last stored bar is 12:45 IST,
before the afternoon push (its 07-20 high is 3.40, i.e. +164% off the 07-17 close).

**Conclusion: the ~500–600% is a property of the MOVE and of 8-days-to-expiry gamma, not of the
OTM→ITM transition.** The framing "the large multiple came from an OTM→ITM transition" is **not supported
by our data** — the transition contributed roughly nothing relative to simply owning near-expiry convexity
through a +10% underlying thrust. This matters for Part 2: the strike-grid question may have a much flatter
answer than expected, and the real lever is **time-to-expiry**, not moneyness.

### Stale-exit rate by outcome (the proof the moneyness filter would have inverted this)

| bucket | strikes | no 2026-07-20 tape |
|---|---|---|
| finished ITM (strike ≤ 107) — **the winners** | 7 | **100%** |
| still ~ATM/OTM (strike ≥ 110) — the laggards | 3 | **0%** |
| all CE strikes | 12 | 75% |

**The tape survives exactly for the contracts that did NOT convert, and dies for every contract that did.**
This is the ATM-tracker following spot: as PNB ran from 101 to 112, strikes 101–107 fell out of coverage.
Any study that treats "last available print" as the exit will systematically mark the winners at a stale,
much lower price — the same failure mode as the ±8% moneyness band, arriving through the data instead of
the code. **Part 2 must model the exit for ITM contracts (parity or BS), not read it.**

---

## G. `realtime_confirmable_dates` — hindsight vs causal

| element | visible in hindsight | **confirmable in real time** | lag |
|---|---|---|---|
| Bullish divergence (04-02 low vs 05-18 low) | 2026-05-18 | **2026-05-21** (pivot needs R=3; the 04-02 pivot confirmed 04-08) | 3 sessions |
| Daily MACD bull cross | 2026-05-22 (owner) | **2026-05-25 close**; tradeable 2026-05-26 open (106.13) | +1 session |
| The 07-08 higher low | 2026-07-08 | **2026-07-14** (R=3, extended by the 07-13 data hole) | 4 calendar days, spot already +4.6% |
| Hourly MACD bull cross | 2026-07-09 11:15 IST | same bar (hourly MACD is causal) | 0 |
| 2nd daily MACD bull cross | 2026-07-17 | **2026-07-17 close**; tradeable 2026-07-20 open (108.26) | +1 session |
| The volume thrust | 2026-07-20 09:15 (vol z = **+26.0**) | same bar, but the bar itself is the +3.1% gap | 0 |

Note the divergence was confirmable **before** the crossover (05-21 vs 05-25) — the element order the
owner describes is causally available in that sequence.

---

## H. `hourly_lead_time`

Hourly bars built as pairs of 30m bars aligned to the 09:15 session open (7 buckets/session, last = 1 bar).

Hourly MACD bull crossovers after the 07-08 low: **2026-07-09 11:15 IST** (close 103.48) and
**2026-07-17 15:15 IST** (close 106.01).

- **Lead of the 07-09 hourly cross over the 07-17 daily cross: 5 sessions / 8 calendar days.**
  Entry cost of acting early: 103.70 vs 106.01 = **2.2% better entry**, and it captured
  **+7.71%** to the exit vs **+5.37%** for the daily.
- **Lead of the 07-17 15:15 hourly cross: 0 bars** — it fired on the same bar that produced the daily cross.
- The owner's "thrust starting 07-17": our hourly shows the 07-17 14:15 bar with volume z **+0.79**
  (3.88M, the day's largest) and a +0.8% push — a mild tell, not a thrust. **The actual thrust is
  2026-07-20 09:15, volume z = +26.0** (54.8M in one hour vs a 14.2M *full-day* average that week).
  Nothing on 07-17 is a volume event by any objective threshold.

So: the hourly did lead — **by 5 sessions, once, at the 07-09 cross**. The second hourly cross gave zero
lead. n = 1 in each direction; this is a candidate for Part 2 (e), not a result.

---

## I. Causality check

Prefix-invariance at cut dates 2026-05-25 / 07-08 / 07-14 / 07-17: **12/12 MACD, signal and hist values
computed on the prefix are identical (rtol 1e-12) to the full-sample values.** Pivots use an explicit
R-bar confirmation lag, so no pivot is used before its confirmation date. (Caveat: EWM is recursive, so a
*different series start* changes early values; all comparisons use the same 2025-03-28 start.)

---

## What the owner should take from Part 1

1. **The setup elements are real and causally available** — divergence (05-21), cross (05-25), higher low
   (07-14) — in that order. Nothing here required hindsight.
2. **The tradeable spot returns are far smaller than 11%**: +6.33% from the confirmed higher low,
   +3.18% from the second daily cross, +4.89% from the May cross.
3. **The option multiple is not a strike-selection story.** A 10.8%-OTM call returned as much as the
   106 on the thrust day and more over the full move. Part 2's strike grid should therefore be run against
   an *expiry* grid too — 8 DTE convexity is doing the work.
4. **Our store cannot see the exit for any winning contract.** Fixing that (parity/BS-modelled exits for
   ITM contracts) is a prerequisite for Part 2, not an optional refinement.
5. Everything above is **n = 1**. It establishes definitions and a data-handling protocol; it establishes
   no edge.
