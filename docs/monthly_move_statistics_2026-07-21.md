# Study A — Monthly move statistics for the directional stock universe

**Date:** 2026-07-21 · **Scope:** RESEARCH ONLY. No lane code, no flags, nothing wired.
**Code:** `backend/directional_options/research/moves_rs/` · **Raw output:** `moves_rs/results.txt`

Answers the owner's first question — *"study monthly returns of stocks and find statistical
move with number of such moves in a month"* — i.e. **how many significant moves actually exist
per stock per month**, which sizes the opportunity set and says how selective the lane can afford
to be. (Study B, relative strength vs NIFTY, is a separate pass.)

---

## Headline

| | |
|---|---|
| **Moves per stock per month** (≥3×ATR) | **0.81** mean, median 1, **35% of stock-months have none** |
| **Median move** | **+11.7%** spot, **12 sessions** long |
| **Largest move / month's whole high-low range** | median **0.76** — one move IS the month |
| **Time in a significant move** | 49% of sessions (K=3); consolidation 51% |
| **Universe-wide opportunity** | **~170 qualifying moves per month** across 211 names ≈ 8.5 new setups per session |
| **Does move-richness persist month to month?** | **NO.** ATR-normalised: significantly **negative** (ρ = −0.157, t = −5.27, p<sub>Bonf</sub> = 0.0009). Fixed-%: **zero** (ρ = +0.06, p = 0.11). |

**The verdict that matters: a "movey" stock is not reliably movey next month.** Historical
move-richness cannot be used to pick instruments. The lane must select on something
**contemporaneous** — a live setup — not on a name's recent track record of moving.

---

## 1. Data and coverage

Daily bars built from `underlying_spot_candles` 30-minute bars (there is no `day` interval in the
table). Session date = IST (UTC + 5:30); sessions with < 6 of the 13 thirty-minute bars dropped.

* Source 30m CSVs were **reused read-only** from the panel_2d3d extraction — this study added
  **zero new PG load**. Reuse was verified row-for-row against PG for 2026-06: 67,312 bars /
  224 names in both. `moves_rs/extract.py` reproduces the pull from a cold DB, one month per
  query, `time` bounded directly by literal UTC timestamps (chunk exclusion preserved).
* 71,594 daily bars, 225 underlyings, 2025-01-01 → 2026-07-20.
* 12 non-stock underlyings removed (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX, NIFTYNXT50,
  CRUDEOIL, NATURALGAS, GOLD, SILVERM, COPPER, NICKEL) → **213 stocks**.
* Inclusion rule: ≥ 12 calendar months each having ≥ 15 sessions. **211 included, 2 excluded:
  `CONCOR`, `CUMMINSIND`** (insufficient history).
* **Per-stock coverage is uniform and short:** 15 months for essentially every name (max 18),
  320 daily sessions (max 386). The broad universe only starts 2025-03-28.

### Data limits (these bound every number below)

1. **~15 monthly observations per stock. One regime.** 2025-04 → 2026-06 was, in aggregate, a
   trending market for Indian single stocks. Nothing here is validated across a bear phase.
2. **14 usable month-pairs** for the persistence test. That is the binding constraint on the
   persistence verdict — see §6 for how it was handled.
3. Everything is **SPOT**. Per the established finding, stock spot edges have repeatedly failed to
   translate to option premium (PCR-OI: spot XS IC −0.084 t≈−9, but CE-premium fwd5 XS IC +0.005
   t=0.73). **Nothing here is an option-level result and it must not be read as one.**

---

## 2. The move definition (fixed a priori, before any result was seen)

Each stock's daily **close** series is segmented by an **ATR-thresholded zigzag**:

* **Noise filter — FIXED at 1.0 × ATR14, not swept.** A leg is considered reversed when close
  retraces 1 ATR from the leg's running extreme, ATR frozen at the extreme's own bar. One ATR is
  by definition the smallest retracement that is not a single average day of noise. This
  parameter was declared fixed and never tuned.
* **Significance bar — reported at K ∈ {2, 3, 4} × ATR14.** A confirmed leg is a MOVE at level K
  iff its close-to-close excursion ≥ K × ATR14 **measured at the leg's start bar** (known before
  the move begins). Legs failing the bar are CONSOLIDATION.
* **End condition, stated explicitly:** the leg ends at its favourable extreme; that extreme is
  *confirmed* only on the later bar where price has retraced 1 ATR. Both dates are recorded and
  **every tradeability statement uses the confirm date, never the extreme date.**

**Why ATR-normalised rather than a fixed %:** a fixed-% bar counts moves almost in proportion to a
stock's volatility, so "which stocks move most" degenerates into "which stocks are most volatile" —
a question already answered and not a selection edge. ATR-normalisation asks the harder question:
*which stocks trend relative to their own noise.* A **fixed-% robustness pass (5/8/12%) is run
alongside** and reported (§6), and it turns out to matter for the persistence verdict.

**Causality is proved, not asserted.** `test_causality.py` recomputes ATR and the full
segmentation on truncated prefixes rows 0..k and requires every leg confirmed at or before k to be
identical to the full-history run at **rtol 1e-12**. Result: *PASS — 25 names, 2,329 leg-field
comparisons, ATR prefixes identical.*

**Segmentation output:** 8,117 confirmed legs across 211 names (median 39 per name over 15 months).
Of these, K=2 keeps 4,548 (56%), K=3 keeps 2,567 (32%), K=4 keeps 1,506 (19%). Up/down split is
near-balanced at every K (K=3: 1,385 up / 1,182 down).

---

## 3. Moves per stock per month — the full distribution

| K | mean | median | sd | P(0) | P(1) | P(2) | P(3) | P(≥4) | max |
|---|---|---|---|---|---|---|---|---|---|
| **2 ATR** | 1.42 | 1 | 0.89 | 14.3% | 41.7% | 33.2% | 9.6% | 1.2% | 5 |
| **3 ATR** | 0.81 | 1 | 0.70 | **35.4%** | 49.4% | 14.3% | 0.8% | 0.0% | 4 |
| **4 ATR** | 0.47 | 0 | 0.58 | **56.8%** | 39.1% | 4.1% | 0.0% | — | 3 |

n = 3,171 stock-months (211 names × ~15 months).

**Read:** at the ≥3 ATR bar the modal stock-month contains **exactly one** significant move, and
more than a third contain **none at all**. Two or more is rare (15%). At the ≥4 ATR bar the modal
stock-month contains **zero**. This is a direct quantitative confirmation of the owner's model —
*moves are brief, consolidation dominates* — and it says the lane should expect roughly **one
tradeable excursion per name per month, not several.**

---

## 4. Magnitude and duration

| K | median \|ret\| | p25 → p75 | p90 | median duration (sessions) | p75 dur | p90 dur |
|---|---|---|---|---|---|---|
| 2 ATR | 8.77% | 6.42 → 12.66% | 18.64% | 9 | 14 | 20 |
| **3 ATR** | **11.67%** | 8.87 → 16.29% | 22.37% | **12** | 17 | 23 |
| 4 ATR | 14.80% | 11.21 → 20.07% | 25.93% | 14 | 20 | 26 |

Direction asymmetry is mild and favours upside at every K (K=3: up median 12.49% / 12 sessions vs
down 10.85% / 11 sessions) — consistent with the trending regime of the sample, not a
generalisable claim.

**The capturability discount.** The end of a move is only knowable after the 1-ATR confirmation
retrace, so a mechanical exit gives back 1 ATR. The fraction of the move surviving that give-back
is **median 0.69 at K=2, 0.77 at K=3, 0.81 at K=4** — bigger moves are proportionally cheaper to
exit. Separately, the confirm lag is a median of **2 sessions** after the extreme (p90 = 5).

**A hard warning for the options implementation.** The median K=3 move takes **12 sessions**. The
measured holdability floor is stock slight-ITM **−8.65% per 3 flat sessions** and stock deep-ITM
−3.79%. A 12-session long-premium hold on a single-stock option is therefore many multiples of the
3-session carry that was actually measured; an 11.7% spot move over 12 sessions is **not** obviously
enough to pay for it. (This is an extrapolation from a 3-session measurement, not a measured
12-session number — it needs its own study before any lane is built on it.)

---

## 5. How much of the month is the move?

**Share of the month's high-low range delivered by the largest move** (months with ≥1 move):

| K | p10 | p25 | **p50** | p75 | p90 | n |
|---|---|---|---|---|---|---|
| 2 ATR | 0.50 | 0.63 | **0.74** | 0.83 | 0.88 | 2,719 |
| 3 ATR | 0.39 | 0.61 | **0.76** | 0.84 | 0.89 | 2,048 |
| 4 ATR | 0.27 | 0.57 | **0.75** | 0.85 | 0.90 | 1,370 |

**Three-quarters of a month's entire range is delivered by a single move.** Miss it and the month
is, for a directional lane, a write-off.

**Time in move vs consolidation** (fraction of the month's sessions inside a significant leg,
clipped to the month):

| K | mean in-move | mean consolidating | p25 | p50 | p75 |
|---|---|---|---|---|---|
| 2 ATR | 0.68 | 0.32 | 0.52 | 0.76 | 0.95 |
| **3 ATR** | **0.49** | **0.51** | 0.19 | 0.50 | 0.78 |
| 4 ATR | 0.34 | 0.66 | 0.00 | 0.25 | 0.64 |

Nuance worth stating plainly: at the ≥2 ATR bar, time is *not* mostly consolidation (68% in-move).
The owner's "consolidation dominates" picture is correct at the **≥4 ATR** bar (66% consolidating)
and is a coin-flip at ≥3 ATR. What is unambiguous is the *count* (§3) and the *concentration*
(0.76 of the range in one move) — the month has one story, and the rest is noise around it.

---

## 6. Persistence — **NO**

> Is a movey stock reliably movey next month? **No.** Explicit verdict: **historical
> move-richness cannot be used to select instruments.**

**Method.** Stocks in the same month share market regime, so a pooled correlation over
stock-months is badly dependence-inflated. Primary test: **cross-sectional Spearman of month *m*
vs month *m+1* computed within each month-pair** (one ρ per pair, median n = 211 stocks), then a
t-test over the **14 pair-level ρ's**. The naive pooled figure is printed alongside so the
inflation is visible.

**Primary (ATR-normalised):**

| metric | n pairs | mean ρ | sd | t | p | **p Bonferroni** | p BH | frac ρ>0 |
|---|---|---|---|---|---|---|---|---|
| K=2 count | 14 | −0.138 | 0.103 | −5.02 | 2.1e-4 | **0.0014** | 0.0003 | 0.07 |
| K=2 magnitude | 14 | −0.109 | 0.185 | −2.21 | 0.046 | 0.274 | 0.055 | 0.29 |
| K=3 count | 14 | **−0.157** | 0.111 | **−5.27** | 1.5e-4 | **0.0009** | 0.0003 | 0.07 |
| K=3 magnitude | 14 | −0.145 | 0.130 | −4.17 | 1.1e-3 | **0.0066** | 0.0022 | 0.21 |
| K=4 count | 14 | −0.148 | 0.069 | **−7.99** | 2.0e-6 | **1.2e-5** | 7e-6 | **0.00** |
| K=4 magnitude | 14 | −0.137 | 0.093 | −5.48 | 1.0e-4 | **0.0006** | 0.0003 | 0.07 |

Multiplicity applied over the full grid of 6 tests (Bonferroni and Benjamini-Hochberg); five of six
survive Bonferroni. Naive pooled ρ's are −0.07 to −0.10 with p < 1e-4 — same sign, and the
pair-level test is the honest one.

**Control.** The identical test on **realised volatility** — the quantity known to persist —
gives mean ρ = **+0.497, t = 17.5, p = 2e-10, positive in 14/14 pairs**. The machinery detects
persistence when persistence exists. It is not detecting it here.

**Quintile read-across** (rank by this month's count, look at next month's) is monotone
*downward* at every K:

| K | Q1 (fewest) | Q2 | Q3 | Q4 | Q5 (most) |
|---|---|---|---|---|---|
| 2 | 1.61 | 1.53 | 1.51 | 1.42 | 1.29 |
| 3 | 1.02 | 0.86 | 0.81 | 0.82 | 0.70 |
| 4 | 0.62 | 0.55 | 0.51 | 0.40 | 0.41 |

**Is the negative sign mechanical? Substantially yes — and this is the important caveat.** A movey
month inflates the stock's own ATR, which raises next month's K × ATR bar. Measured directly:
rank corr(this month's move count, next month's ADR ÷ this month's ADR) = **−0.122**. So a chunk
of the −0.15 is a normaliser feedback loop, not a genuine tendency for movers to go quiet.

**The fixed-% robustness pass removes that feedback entirely — and finds nothing:**

| definition | moves/stock/month | P(0) | median \|ret\| | persistence mean ρ | t | p |
|---|---|---|---|---|---|---|
| ≥ 5% | 1.54 | 14% | 8.5% | +0.040 | 1.22 | 0.24 |
| ≥ 8% | 0.85 | 37% | 11.6% | +0.059 | 1.71 | 0.11 |
| ≥ 12% | 0.40 | 65% | 16.5% | +0.026 | 0.84 | 0.42 |

**Zero, at all three thresholds, before any multiplicity correction.** (The faint positive sign is
consistent with the residual volatility-persistence leak: contemporaneous rank corr between move
count and realised vol is +0.24, so a fixed-% count is partly a volatility proxy — and even that
leak is not enough to make it predictive.)

**Era stability.** K=3 count persistence is negative in 12 of 14 pairs, first-half mean ρ = −0.199,
second-half −0.114. Stable in sign; no era where it turns usefully positive.

**Cross-sectional dispersion — the killer.** Per-stock mean move count (K=3) spans only **0.40
(NAUKRI) to 1.20 (NBCC, PERSISTENT, PNB, WAAREEENER)**, sd across stocks = **0.149**, versus
sd of a single stock-month = **0.704**. Even with *perfect* foresight of a stock's long-run
move-richness, the signal is ~1/5 the size of the month-to-month noise. There is almost nothing to
select on, which is exactly why no formulation finds persistence.

### Consequence for the lane

The instrument-selection filter **cannot** be "names that have been moving." It must be
**contemporaneous** — a live, causally-computed setup evaluated on the current bar. Whether
relative strength versus NIFTY is such a filter is Study B's question, and this result raises the
bar for it: RS must be shown to select *for movement in the forward window*, not merely to
describe movement that has already happened.

---

## 7. Opportunity count across the universe

Restricted to the 15 full-universe months (211 names each; 2025-01..03 had only 2 stocks and are
excluded from these aggregates):

| K | moves/month, mean | median | min | max | per name/month | **per trading session** |
|---|---|---|---|---|---|---|
| 2 ATR | 299 | 317 | 139 | 394 | 1.42 | **~15.0** |
| **3 ATR** | **170** | 173 | 62 | 260 | 0.81 | **~8.5** |
| 4 ATR | 100 | 101 | 27 | 175 | 0.47 | **~5.0** |

Monthly counts are strongly regime-clustered (K=3: 62 in 2025-04 and 112 in 2026-04 vs 260 in
2026-01) — the universe moves together, which is precisely why the persistence test had to be run
per month-pair.

**Sizing implication.** At the ≥3 ATR bar there are roughly **170 qualifying moves per month, i.e.
~8-9 new setups per session across 211 names.** The lane can therefore afford to be **very**
selective — taking, say, the best 10-20% still leaves 1-2 candidates per session — and given the
option-level cost floor (stock long premium starts 4-9% behind before direction matters), it
almost certainly **should** be. The binding constraint on this strategy is not opportunity count.

---

## 8. What this study does and does not establish

**Established**
- The move-count, magnitude, duration and concentration distributions above, on 211 stocks over 15
  months, from a causal segmentation proved by prefix-invariance at rtol 1e-12.
- Move-richness does **not** persist month to month, in either an ATR-normalised or a fixed-%
  formulation, with a positive control confirming the test has power.
- The opportunity set is large enough that selectivity costs nothing.

**Not established (do not extrapolate)**
- **Any option-level claim.** All of this is spot. The established precedent is that spot findings
  of this size have not survived translation to premium.
- **Anything out of regime.** 15 months, one broadly trending phase, 14 month-pairs.
- **That these moves are tradeable.** Entry timing is untested here; the 12-session median duration
  versus a 3-session-measured carry floor of −5% to −6% (stock slight-ITM −8.65%) is a serious open
  problem, and quantifying a 12-session stock long-premium carry is the natural next study.
- **That any signal predicts a move.** This study counts moves; it does not forecast them. The
  single-shot indicator study already found spot directional accuracy *below* controls for every
  indicator family tested.

---

## Files

| path | what |
|---|---|
| `backend/directional_options/research/moves_rs/build_daily.py` | 30m → causal IST daily bars → `data/daily.parquet` |
| `backend/directional_options/research/moves_rs/extract.py` | cold-DB PG fallback, one month per query, chunk-exclusion-safe |
| `backend/directional_options/research/moves_rs/moves.py` | the move definition + ATR-zigzag segmentation engine |
| `backend/directional_options/research/moves_rs/test_causality.py` | prefix-invariance proof (rtol 1e-12) |
| `backend/directional_options/research/moves_rs/analyse.py` | Study A analysis, writes `results.txt` |
| `backend/directional_options/research/moves_rs/results.txt` | full raw output |
| `backend/directional_options/research/moves_rs/data/legs.parquet` | 8,117 segmented legs |
| `backend/directional_options/research/moves_rs/data/panel_K{2,3,4}.parquet` | stock-month panels |
