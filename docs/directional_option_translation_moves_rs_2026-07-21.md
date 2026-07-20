# (E) OPTION-LEVEL TRANSLATION — does move-richness or relative strength
# select instruments that PAY at premium level?

Date: 2026-07-21. **Research only.** No lane code, no flags, nothing wired.

Code: `backend/directional_options/research/moves_rs/opt_selection.py` →
`opt_analyse.py`. Full numeric dump: `opt_results.txt`. Intermediate panel:
`data_opt/opt_sel.parquet`, per-cell tables `data_opt/arms.csv`,
`data_opt/lifts.csv`.

**Zero PG load.** This pass issues no database queries at all. It reuses the
already-extracted local option panel from the `panel_2d3d` pass
(`panel_opt.parquet`, read-only) joined to the Study A and Study B feature
parquets. The only new computation on the option side is the extension of
forward premium returns to 5 and 10 sessions, done by the same
session-index merge the original panel used.

---

## What was tested

The lane's practical question, stated exactly:

> Does RS-based selection improve the OPTION-level outcome versus trading the
> same setups on an unselected universe, and versus a matched random-selection
> control? And does selecting for MOVE-RICHNESS beat selecting for RS, or are
> they the same names?

**Trade construct** (the holdable spec, unchanged from prior work): monthly
contracts, DTE 8-22, premium ≥ Rs 1, EOD 15:15 IST decision bar. Two bands —
`deep_ITM` (signed moneyness −10%..−3%, ≈0.65-0.8 delta) and `slight_ITM`
(−3%..−0.75%). One contract per (underlying, session, side, band): the one
closest to the band centre, so no name is over-weighted by strike count.
Horizons 3, 5 and 10 sessions. CE and PE reported separately throughout.

**Panel size.** stock: 33,356 contract-sessions, 211 underlyings, 162 sessions
(2025-04-15 → 2026-07-20). index: 1,004 contract-sessions, 6 underlyings, 217
sessions (2025-01-14 → 2026-07-17).

**Costs.** Round-trip on premium: index 1.6%, stock 8.0%. The panel carries no
bid/ask, so spread is *assumed, not measured*; 8% for single stocks is
deliberately more generous than the established ~10% round-trip figure. A full
sensitivity grid at 0 / 2 / 5 / 10% is reported so the reader can move the
number themselves.

**Selection arms.** `unselected` (whole universe) · `RS_decile` /
`RS_quintile` (top decile/quintile of `rs_ret_21` XS rank for CE, bottom for
PE) · `alpha_decile` (beta-stripped) · `moverich_quintile` / `moverich_decile`
(top of **prior-month** Study A K=3 move count — strictly lagged one calendar
month) · `rv_quintile` and `rv_bottom_quintile` (trailing 21-session realised
vol, the movement selector Study B found actually works) ·
`RS_and_moverich` (both) · **`RS_decile_WRONGWAY`** — the placebo, high-RS→PE
and low-RS→CE.

**Controls and honesty.**
- **Matched random control**: for every arm, 300 draws that reproduce the arm's
  *trade count on every individual session*, sampling names at random from the
  same eligible pool that session. This holds date composition and position
  count fixed, so it isolates the selector itself.
- **Episode clustering**: every statistic is computed on session means — all
  trades opened on a session are one cluster, because they share that day's
  market move.
- **De-overlapping**: overlapping holds are removed by restricting to entry
  dates spaced `h` sessions apart and averaging over all `h` phase offsets
  (`t_deov`). The raw-vs-de-overlapped gap is reported everywhere.
- **Multiplicity**: Bonferroni and BH-FDR across the whole grid.
- **Causality**: no new predictive feature is constructed here. Every selector
  is inherited unchanged from Study A / Study B, both of which proved
  prefix-invariance empirically (Study A: 2,329 leg-field comparisons at rtol
  1e-12; Study B: 756 feature comparisons plus a positive control that
  correctly FAILS). The one lagged join — prior-month move count → the
  *following* calendar month's sessions — is constructed by an explicit
  `PeriodIndex + 1` shift, so a month can never see itself. Band assignment and
  contract choice use only the snapshot bar's own moneyness. Everything with an
  `f`/`ret` prefix is an outcome, never a conditioner.
- **Paired-lift test**: `d_t = mean(arm net on session t) − mean(unselected net
  on session t)`, which cancels the common market move exactly. This is the
  cleanest possible test of "does the selector add anything".

---

## 1. Baseline: the unselected universe, net of costs

| market | band | side | h | n | sess | gross % | **net %** | median net % | win % | t_clu | t_deov |
|---|---|---|---|---|---|---|---|---|---|---|---|
| index | deep_ITM | CE | 3 | 143 | 73 | +8.91 | **+7.31** | +3.62 | 56.6 | 2.04 | 1.18 |
| index | deep_ITM | PE | 3 | 137 | 79 | −0.38 | −1.98 | −5.96 | 43.1 | −0.44 | −0.23 |
| index | slight_ITM | CE | 3 | 300 | 145 | +6.99 | **+5.39** | −4.08 | 48.3 | 1.39 | 0.81 |
| index | slight_ITM | CE | 5 | 170 | 79 | +10.28 | **+8.68** | +4.17 | 52.9 | 1.39 | 0.61 |
| index | slight_ITM | PE | 3 | 286 | 138 | −3.55 | −5.15 | −31.20 | 29.0 | −1.08 | −0.61 |
| index | slight_ITM | PE | 5 | 169 | 86 | −8.23 | −9.83 | −38.47 | 30.8 | −1.45 | −0.81 |
| stock | deep_ITM | CE | 3 | 7,543 | 159 | −5.33 | **−13.33** | −17.42 | 33.1 | −7.06 | −4.07 |
| stock | deep_ITM | CE | 5 | 3,529 | 85 | −5.41 | −13.41 | −22.38 | 34.3 | −4.32 | −1.98 |
| stock | deep_ITM | PE | 3 | 6,159 | 159 | −10.34 | −18.34 | −26.40 | 26.9 | −8.03 | −4.66 |
| stock | deep_ITM | PE | 5 | 3,357 | 85 | −19.50 | −27.50 | −45.52 | 18.8 | −8.58 | −3.92 |
| stock | deep_ITM | PE | 10 | 364 | 9 | −38.25 | −46.25 | −65.51 | 14.6 | −6.96 | — |
| stock | slight_ITM | CE | 3 | 8,558 | 159 | −4.24 | −12.24 | −21.09 | 33.3 | −5.28 | −3.03 |
| stock | slight_ITM | CE | 5 | 4,600 | 85 | −0.73 | −8.73 | −24.67 | 35.1 | −2.59 | −1.22 |
| stock | slight_ITM | CE | 10 | 466 | 10 | +12.19 | +4.19 | −28.42 | 39.7 | 0.32 | — |
| stock | slight_ITM | PE | 3 | 7,816 | 159 | −8.78 | −16.78 | −28.70 | 28.5 | −6.41 | −3.68 |
| stock | slight_ITM | PE | 5 | 4,303 | 85 | −19.81 | −27.81 | −42.84 | 22.0 | −9.27 | −4.19 |
| stock | slight_ITM | PE | 10 | 470 | 9 | −36.19 | −44.19 | −62.54 | 18.7 | −7.67 | — |

**Every stock cell is a loss, net of costs, at every band and both sides, at
h=3 and h=5 — the sole exception is slight-ITM CE at h=10, which has only 10
entry clusters. These losses are the most statistically robust results in the
entire study**
(de-overlapped t between −1.2 and −4.7). Win rates are 19-40% against a
break-even that needs 55-60%. The put side is materially worse than the call
side at every band — this is one regime (a broadly rising market for Indian
single stocks) and the put leg pays the drift as well as the theta.

The **index** cells are the only positive ones. They are **not** a finding:
the mean forward 3-session *spot* move on those exact rows is +0.47% and the
median option-to-spot leverage is 20.8× (deep ITM) / 33.5× (slight ITM). The
entire +7.31% is unhedged long-index beta in a rising sample, and the put side
of the same cells is negative by a matching amount. Index is included as the
benchmark it was asked to be, not as an edge.

---

## 2. Does selection lift the option outcome?

Selection *reduces the loss* materially and consistently. Best cells, stock,
net of 8% round trip:

| cell | unselected | RS_decile | moverich_quintile | RS_and_moverich |
|---|---|---|---|---|
| deep_ITM CE h=3 | −13.33 | −9.67 | −10.85 | −13.19 |
| deep_ITM CE h=5 | −13.41 | −9.56 | −7.69 | −6.98 |
| slight_ITM CE h=3 | −12.24 | −6.75 | −7.60 | −3.44 |
| slight_ITM CE h=5 | −8.73 | −5.15 | **+2.34** | **+4.70** |
| slight_ITM PE h=5 | −27.81 | −22.19 | −19.43 | −18.94 |

Against the **count-matched random control**, most of these lifts sit at the
0.95-1.00 percentile of the 300-draw null — i.e. the improvement is not an
artefact of trading fewer positions or of trading on different days. The
random control did its job.

The **placebo also behaves correctly in the headline cell**: at
stock/slight_ITM/CE/h=3 the right-way RS decile lifts +5.5pp while the
wrong-way RS decile (low-RS→CE) *loses* an almost symmetric −5.5pp
(random percentile 0.013). And the RS arm is not a hidden low-volatility
selection — its mean IV is 0.31 vs 0.28 unselected and its mean ATR% is 2.90
vs 2.56, i.e. slightly *higher* vol, and the `rv_bottom_quintile` arm
produces essentially no lift (+0.13pp). The lift has a directional signature.

**But it is not significant, and it does not clear costs.**

Paired-lift test (`d_t`, market move cancelled), best twelve cells in the
entire grid:

| market | band | side | h | arm | sess | lift pp | t_deov | p | q(BH) |
|---|---|---|---|---|---|---|---|---|---|
| stock | slight_ITM | CE | 3 | RS_quintile | 151 | +6.22 | 1.95 | 0.051 | 0.874 |
| stock | slight_ITM | CE | 3 | alpha_decile | 97 | +10.28 | 1.90 | 0.058 | 0.874 |
| stock | deep_ITM | CE | 3 | RS_quintile | 152 | +2.37 | 1.78 | 0.075 | 0.874 |
| stock | deep_ITM | CE | 3 | RS_decile | 152 | +3.54 | 1.65 | 0.099 | 0.874 |
| stock | slight_ITM | CE | 3 | RS_WRONGWAY | 108 | −7.19 | −1.65 | 0.099 | 0.874 |
| stock | slight_ITM | CE | 3 | RIGHT−WRONG | 103 | +13.16 | 1.56 | 0.120 | 0.874 |
| stock | slight_ITM | CE | 3 | moverich_quintile | 155 | +5.33 | 1.50 | 0.133 | 0.874 |
| stock | slight_ITM | PE | 3 | RIGHT−WRONG | 78 | +11.54 | 1.49 | 0.135 | 0.874 |
| stock | slight_ITM | CE | 5 | moverich_quintile | 84 | +10.74 | 1.44 | 0.150 | 0.874 |
| stock | deep_ITM | PE | 3 | alpha_decile | 97 | +3.60 | 1.43 | 0.152 | 0.874 |

**Lift grid, k = 75 cells: positive lifts with de-overlapped p < 0.05 raw = 0.
Surviving BH-FDR 5% = 0. Surviving Bonferroni = 0. Best q = 0.87.**

Note the raw-vs-de-overlapped gap: the same cells show clustered t of 2.3-3.4
before de-overlapping and 1.0-1.9 after. Roughly 40% of the apparent
significance was overlap.

On the **level** grid (49 cells of arm net return), 24 cells survive BH-FDR
and 9 survive Bonferroni — **every one of them is a confirmed LOSS**
(the ten most extreme are all stock cells at net −11% to −32%). The only
statistically solid facts this panel produces are that stock long premium at
this spec loses money.

---

## 3. Are RS-selected and move-rich names the same names? — NO

25,167 stock name-sessions carrying both selectors.

- Spearman(RS rank, prior-month move-count rank) = **+0.044**
- Spearman(|RS| rank, move-count rank) = +0.110
- Spearman(rv_21 rank, move-count rank) = +0.184
- Per-session Jaccard(RS top quintile, move-rich top quintile) = **mean 0.108,
  median 0.093** — against a random-overlap expectation of 0.111 for two
  independent 20% sets.

**They are statistically independent selectors picking essentially disjoint
name sets.** That is the useful structural fact in this pass: RS and
move-richness are not substitutes and their combination
(`RS_and_moverich`) is a genuine intersection, not a redundancy — which is why
it produces the largest raw lifts (+8.3 to +13.4pp) on the smallest samples
(n = 103-220) and correspondingly the weakest t.

**Which selects better?** At h=3 RS is marginally ahead (best de-overlapped t
1.95 vs 1.50); at h=5 move-richness is ahead (+10.74pp, t 1.44, vs RS +5.55pp,
t 1.12) and is the only selector that turns any stock cell positive
(slight_ITM CE h=5, +2.34% net). Move-richness also wins the direction-free
test: it lifts the PE side at h=5 by +8.4pp, which RS does not. **But the gap
between them is far smaller than either's own standard error, so the honest
answer is that neither is measurably better than the other, and neither is
measurably better than nothing.** Note the tension with Study A: prior-month
move-richness lifts option outcomes here even though Study A proved move-count
does *not* persist month-to-month — with 0/75 significant, the parsimonious
reading is that this lift is noise, not a contradiction of Study A.

---

## 4. Do the selectors pick names whose options actually MOVE?

Mean |option return|, gross, stock:

| cell | unselected | RS_decile | moverich_quintile | rv_quintile |
|---|---|---|---|---|
| deep_ITM h=3 | 34.71 | 33.09 | 33.99 | **36.16** |
| deep_ITM h=5 | 43.20 | 39.37 | 43.69 | 43.52 |
| slight_ITM h=3 | 43.27 | 46.19 | 46.29 | 44.70 |
| slight_ITM h=5 | 49.26 | 49.56 | 52.88 | 48.96 |

Movement selection at option level is weak for every selector — differences of
1-4pp on a base of 35-50%. Study B's spot-level conclusion (realised vol, not
RS, is the movement selector) does **not** cleanly carry over: `rv_quintile`
picks the most dispersed *deep-ITM* options but produces the *worst* net
returns in the study (lifts of −0.2 to −4.3pp, random percentiles 0.03-0.24).
Selecting for high realised vol buys more IV, and the extra bleed is larger
than the extra excursion. **Selecting for movement via volatility is
actively harmful at option level.**

---

## 5. What cleared costs, and what did not

**Cleared costs (with caveats that void it as a strategy):**
- Index deep-ITM CE, h=3: +7.31% net (t_clu 2.04, t_deov 1.18, n=143). Shown
  above to be pure long-index beta — the put side of the same cell is −1.98%.
- Index slight-ITM CE, h=5: +8.68% net (t_deov 0.61, n=170). Same.
- Stock slight-ITM CE h=5 under move-rich selection: +2.34% net (t_deov 0.18,
  n=635), and +4.70% under RS∩move-rich (t_deov 0.26, n=135). Both are
  single positive cells inside a 75-cell grid with nothing surviving
  multiplicity. Treat as noise.

**Did not clear costs:** everything else. Both stock bands, both sides, all
three horizons, all eight selection arms. At the honest 8% stock round-trip,
the *entire* stock grid is negative except the two cells above. At a 5%
round-trip the sign is unchanged in every stock cell of the reported grid
except slight-ITM CE h=5 move-rich (+5.34%) and the 10-session cell. Even at
**zero cost** the unselected stock cells are −5.3% (deep-ITM CE h=3), −4.2%
(slight-ITM CE h=3), −10.3% and −8.8% on the put side — i.e. **the carry alone
kills these trades before a single rupee of spread is paid.** That is the
single most important number in this document: cost assumptions are not what
decides this, theta is.

The 10-session horizon deserves a specific note, because Study A's median
qualifying move takes 12 sessions. Stock slight-ITM CE at h=10 is +4.19% net
on n=466 / **10 sessions** — nine or ten overlapping entry clusters is not a
sample, and the matching put cell is −44.19%. The h=10 column of this panel is
too thin to answer the question Study A raised. Answering "does a 12-session
stock long-premium hold pay for itself" needs a purpose-built extraction with
far more forward coverage, and is the natural next study.

---

## 6. Honest verdict

1. **Stock long premium at the holdable spec is a losing trade on this panel,
   before any selection is applied and before costs are applied.** This is the
   only claim in the study with de-overlapped |t| > 3 and it holds in 10 of the 11 stock baseline cells (the exception being the
   10-session cell, which has only 10 entry clusters).
2. **RS-based selection does improve the option-level outcome versus an
   unselected universe and versus a matched random control — by +2 to +6pp,
   with a correct wrong-way placebo signature — but the improvement is not
   statistically significant (0/75 after de-overlapping and multiplicity, best
   q = 0.87) and it is not large enough to lift any cell to profitability.**
   It converts a −12% trade into a −7% trade.
3. **Move-richness selection performs comparably to RS and is a statistically
   independent selector** (Jaccard 0.108 ≈ random). Neither dominates. Their
   intersection produces the biggest raw lifts on the smallest samples.
4. **Selecting for movement via realised volatility is counter-productive at
   option level**, despite being the correct movement selector at spot. The
   spot→option translation inverts it.
5. **The index cells are beta, not edge.**
6. This is one regime, ~15 months, 162 stock sessions, an assumed spread, and
   no bid/ask. It cannot distinguish "no edge" from "an edge too small to
   measure here". What it *can* do is bound the size of any surviving edge:
   whatever RS or move-richness is worth on this universe, it is worth less
   than the theta of the contract it would be expressed through.

**Recommendation: do not wire an RS-based or move-richness-based instrument
selector into a stock long-premium lane on this evidence.** The productive
direction is not a better selector — it is a construct whose carry is not
−5% to −13% before direction is even considered (spreads, ratio structures, or
index-only expression), or a materially longer data history.
