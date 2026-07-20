# (C) Labelled setup dataset — do spot-indicator setups pay at option level over a 2-3 day hold?

**Date:** 2026-07-20 · **Status:** research only, nothing wired, no flags changed
**Harness:** `backend/directional_options/research/setups_2d3d/`
**Full numeric dump:** `backend/directional_options/research/setups_2d3d/results.txt`
**Companion passes:** (A) external practice, (B) panel study →
`docs/directional_2_3day_panel_2026-07-20.md`

---

## Verdict, up front

**No setup family clears costs at a 2-3 day hold. Not one.**

48 cells were measured (7 signal families × 2 moneyness bands × index/stock ×
long/short). **6 of 48 have a positive net mean. 15 of 48 beat their matched
control. Exactly 1 of 48 beats its control with an episode-clustered t > 2 —
which is the number you expect from chance alone at 48 comparisons.**

Worse than "no edge": **every signal family is directionally WORSE than a
coin-flip at the spot level.** Spot 3-day win rate — unconditional-long control
51.1%, coin-flip control 47.3%, and then every family below both: MACD-cross
45.9%, trend-pullback 45.8%, RSI-fade 45.8%, MA-cross 43.8%, Donchian-breakout
42.2%, ADX-trend 42.2%. The owner's specified indicator stack is not merely
failing to add signal, it is **selecting bars that go the wrong way**, which
reproduces the earlier finding (ADX-confirmed trend was the worst construct, IC
−0.173) on completely independent machinery.

This is a kill, and it is the fourth independent method to reach the same
conclusion on this lane's momentum thesis.

---

## What was built

`extract.py` → `harness.py` → `analyse.py`, with `features.py` (indicators) and
`test_causality.py` (lookahead proof).

* **Inputs.** All 30-minute bars of `option_premium_candles` inside a ±8%
  moneyness band (2.97M bars, 11,554 contracts) plus the 30-minute
  `underlying_spot_candles` tape (927,867 bars, 225 underlyings, 384 sessions,
  2025-01-21 → 2026-07-06). Every PG range predicate bounds `time` directly with
  literal UTC timestamps and pins `interval`; no function ever touches the
  partitioning column.
* **Output.** `data/trades.parquet` — **25,933 labelled setups**, 212
  underlyings, 4,351 distinct contracts, each with entry/exit timestamps, the
  barrier that resolved it, spot and option outcome, and net return under three
  cost scenarios and an execution-lag variant.

### Feature set (the owner's specified stack) and its causality proof

| Timeframe | Features |
|---|---|
| 30m (decision) | EMA20, EMA50, RSI14, MACD(12,26,9) line/signal/histogram, ADX14 with +DI/−DI, ATR14, Donchian-40 high/low |
| Daily (higher TF confirmation) | SMA20, SMA50, RSI14, MACD(12,26,9), ADX14 with +DI/−DI, ATR14, ATR% |

Every feature is a causal filter — only `.rolling()`, `.ewm()`, positive
`.shift()`, and elementwise arithmetic; never `center=True`, never a negative
shift, never normalised against a full-sample statistic.

**The proof is empirical, not a promise.** `test_causality.py` runs
prefix-invariance: for 25 random cut points it recomputes the entire feature
block on rows `0..k` alone and asserts the value at row `k` is identical to the
value computed on the full series (`rtol=1e-12`). A feature that peeked at row
`k+1` cannot survive this, because the prefix does not contain row `k+1`. Both
the intraday and daily blocks pass. Three further guards are asserted:

1. **Donchian excludes the current bar** (`shift(1)` before the rolling max) —
   otherwise a breakout is trivially self-confirming.
2. **The daily block is lagged one full session.** A 30m bar inside session `s`
   only ever sees daily values computed through session `s−1`; session `s`'s own
   daily bar is not closed yet. Same for the ATR that sizes the barriers.
3. **Entry strictly follows the decision.** Asserted on the produced dataset:
   `entry_time > decision_time` for all 25,933 rows and the gap is ≤ 30 minutes
   — entry is the very next 30m bar, at its **open**.

Two further anti-lookahead choices in the harness itself:

* **Contract selection happens at the 15:15 snapshot of the *prior* session**,
  not at the decision bar. A real lane maintains a tracked contract per name per
  day; this makes the instrument known before the session that trades it opens,
  and removes any chance of picking the contract that happened to move.
* **Decision bars are capped at 14:45 IST** so the entry bar is inside the same
  session — no overnight gap is silently absorbed into the entry price.

**No survivorship.** A trade is only recorded if the contract has a real quote
at the entry bar (premium ≥ Rs 1) and a resolvable quote at the exit bar. A
time-barrier trade is **discarded, not truncated**, if the tape does not extend
to the full 3rd session — a shortened hold is never allowed to masquerade as a
completed one. 12.2% of exits fall back to the last prior quote and are flagged
(`stale_exit_quote`).

### Triple barrier

Fixed **a priori** from the (A) and (B) passes. Nothing was swept — per López de
Prado's *Determining Optimal Trading Rules Without Backtesting*, a swept ATR
multiple will always produce a positive-looking result and it will not be real.

```
target = entry_spot + 1.5 × ATR14_daily(prior session) × side
stop   = entry_spot − 1.0 × ATR14_daily(prior session) × side
time   = last bar of (entry session + 3 sessions)
```

* Monitored on **30m spot high/low**, not daily extremes — this is the
  resolution the (B) pass said a real triple barrier needs.
* First 30m bar to touch wins. If both barriers fall inside the same 30m bar the
  **stop is assumed** (conservative).
* Exit fill = that bar's **close**, i.e. a one-bar execution lag is baked into
  the baseline. A further +1-bar variant is recorded separately.
* Consistent with (A): the barrier lives in **spot space** and is executed as a
  conditional order on the option; the premium is never the trigger.

### Instrument (holdable contracts only)

Monthly expiry, **DTE 8-22 at entry** (median 15, range 7-22), ITM, in two
bands: `deep_itm` (−6%..−3% moneyness) and `slight_itm` (−3%..−0.75%). Weeklies
and ATM are excluded by construction — (B) measured them at −59% to −86% carry,
so including them would only manufacture a bigger loss.

### Costs

Round-trip as % of premium: **optimistic 0.6% / base 1.6% / pessimistic 4.0%**,
from the (B) cost work. Sizing at Rs 25,000 premium per leg (above the flat-
brokerage cliff (B) identified at ~Rs 20,000).

### Setup families

| Family | Rule (30m decision, daily confirmation) |
|---|---|
| `ma_cross` | EMA20 crosses EMA50 on 30m, daily close on the same side of daily SMA20 |
| `adx_trend` | 30m ADX14 crosses above 25, ±DI ordering gives side, daily agrees |
| `macd_cross` | 30m MACD crosses its signal, daily MACD histogram agrees |
| `rsi_fade` | 30m RSI14 exits oversold(30)/overbought(70) — the mean-reversion form |
| `donchian_break` | 30m close breaks the 40-bar (~3 session) high/low |
| `trend_pullback` | daily trend + daily ADX>20, 30m RSI leaves the 40/60 pullback zone |
| `control_random` | **control** — coin-flip side. The pure cost + carry floor |
| `control_long` | **control** — always long. The benchmark a long-biased family must beat |
| `control_short` | **control** — always short |

The three controls run through **identical** instrument selection, barriers and
costs, and are the single most important design element in this pass: the sample
period rose, so "always long" *looks* like edge. `control_long` on index
slight-ITM returns **+2.9% net per trade with a 52.6% hit rate**. Any family
that does not beat that number is selling beta as alpha.

---

## Results

### Base rates by barrier

| Family | stop | target | time |
|---|---|---|---|
| `control_long` | 31.6% | 18.9% | 49.5% |
| `control_random` | 36.0% | 20.3% | 43.7% |
| `trend_pullback` | 36.7% | 14.7% | 48.7% |
| `macd_cross` | 37.2% | 18.3% | 44.5% |
| `rsi_fade` | 37.1% | 15.8% | 47.1% |
| `ma_cross` | 37.4% | 18.6% | 44.0% |
| `donchian_break` | 40.3% | 19.5% | 40.2% |
| `adx_trend` | 41.6% | 19.0% | 39.4% |
| `control_short` | 42.9% | 14.1% | 42.9% |

With a 1.5×ATR target and a 1.0×ATR stop the *fair* ratio would be well under
1:1 stops-to-targets. Every family runs **~2:1 stops to targets**, and the
signal families are all worse than the coin flip. ~40-49% of trades exit on the
time barrier and pay pure carry.

### Net of base cost, all trades (Rs on 25k premium/leg)

| Band | Family | n | hit% | mean% | t (episode) | PF | PnL | PnL ex-top-3 |
|---|---|---|---|---|---|---|---|---|
| deep_itm | `trend_pullback` | 1,070 | 42.7 | **−4.23** | −2.35 | 0.77 | −1,131,681 | −1,220,208 |
| deep_itm | `control_random` | 192 | 41.7 | −4.51 | −1.57 | 0.75 | −216,332 | −304,813 |
| deep_itm | `control_long` | 1,073 | 43.0 | −4.75 | −4.59 | 0.71 | −1,272,877 | −1,403,204 |
| deep_itm | `macd_cross` | 1,850 | 42.0 | −5.79 | −7.49 | 0.64 | −2,678,846 | −2,784,025 |
| deep_itm | `rsi_fade` | 760 | 39.0 | −7.89 | −3.81 | 0.66 | −1,499,257 | −1,588,331 |
| deep_itm | `ma_cross` | 282 | 39.0 | −8.00 | −4.46 | 0.51 | −564,161 | −616,388 |
| deep_itm | `donchian_break` | 3,253 | 34.7 | −9.86 | −21.92 | 0.39 | −8,015,077 | −8,070,662 |
| deep_itm | `adx_trend` | 610 | 36.7 | **−10.33** | −8.98 | 0.42 | −1,575,264 | −1,626,943 |
| deep_itm | `control_short` | 887 | 34.3 | −12.08 | −9.53 | 0.45 | −2,678,908 | −2,744,849 |

The `slight_itm` band is the same picture (`adx_trend` −10.00%,
`trend_pullback` −8.20%, `donchian_break` −7.20%, `macd_cross` −5.85%,
`control_random` −2.84%). **Every single signal family loses more than the
coin-flip control in both bands.** PnL ex-top-3 is more negative than PnL
everywhere — there is no winner concentration to strip out, because there are no
winners.

Robustness (§7 of `results.txt`) is degenerate in the honest direction:
removing the best underlying, the best quarter, or the top 3 trades makes every
cell *more* negative. Per-quarter, `adx_trend` and `ma_cross` are negative in
**0 of 6** quarters in both bands; the rest manage 1-3 of 6-7.

### The correction that decides the study: episode clustering

A 30m rule fires on many consecutive bars of the same move, so raw trade counts
badly overstate the independent sample. Clustering to **one observation per
(underlying, entry session)** is applied to every t-statistic reported here.

Its effect is best shown on the one cell that initially looked like a winner —
index slight-ITM long Donchian breakouts:

| | raw | episode-clustered |
|---|---|---|
| n | 262 | 109 |
| mean net | **+9.11%** | **+1.78%** |
| t | **+4.47** | **+0.53** |
| matched `control_long` | +2.92% | +2.30% |
| Welch t vs control | +1.31 (p=0.19) | **−0.10 (p=0.92)** |

An apparently overwhelming t = 4.47 is entirely an artefact of counting the same
breakout up to a dozen times, and the surviving mean is **below** the
unconditional-long benchmark. It was long-index beta in a bull sample, nothing
more. The `deep_itm` sibling of the same rule clusters to −0.05% against a
control of +5.10%.

### The single survivor — and why it should not be wired

One cell of 48 has positive excess over its control with episode t > 2:

**`macd_cross`, index names, slight-ITM, LONG only** — n = 59 (55 episodes),
mean net **+10.5%**, episode mean +11.2%, excess over `control_long` **+8.9%**,
t_ep = 2.56, hit 57.6%, spot win 67.8%. It survives cost stress (+8.1% at the
pessimistic 4% round trip), survives a +1-bar execution lag (+11.3%), keeps
+103k of its +155k after removing the top 3 winners, and is positive in 5 of 6
quarters.

That reads well. It should still be treated as **noise until proven otherwise**:

1. **It is exactly the count expected by chance.** 1 significant cell out of 48
   at α≈0.02 is the null hypothesis' own prediction. No multiple-comparison
   correction survives that.
2. **Its siblings all die.** The identical rule returns +1.1% on deep-ITM index
   (the other holdable band — an elasticity difference should shrink it to ~+7%,
   not to zero), −2.9% on stocks, and **−22.3%** on the short side. An effect
   that exists in one of four adjacent cells is a coincidence, not a mechanism.
3. **n = 59 across a six-quarter sample with one regime.** This is the same
   sample-size and one-regime footing that produced the last two candidates the
   lane killed.
4. It is long-only in a rising market, which is the exact failure mode the
   controls were added to catch.

**Do not wire it.** If it is to be pursued at all, it should be pre-registered
(rule, band, side, index-only) and evaluated purely out-of-sample going forward.

---

## Why it fails — the mechanism

Combining this pass with (A) and (B):

1. **The signal is anti-predictive.** 42-46% spot win against a 47.3% coin-flip
   and a 51.1% drift baseline. Whatever these indicators select, it is
   systematically the wrong bars.
2. **The required accuracy is far higher than what is on offer.** (B) put
   break-even directional hit-rate at 3 sessions at **54.8-60.2%** on holdable
   contracts. The families deliver 42-46%. The gap is not marginal, it is ~12-15
   points, and it is on the wrong side of the ledger.
3. **Cost is *not* the killer here** — that was the intraday fade's problem, not
   this one. At 1.6% round-trip, cost is ~4% of the typical 3-session premium
   move. Even at 0% cost every signal family is still negative and still below
   its control.
4. **Carry is a real but secondary drag.** `control_random` loses 2.8-4.5% net
   per trade on the most holdable contracts available — that is the theta/vega
   floor of a 2-3 day long-premium hold with an ATR barrier, and it is
   consistent with (A)'s volatility-risk-premium literature and (B)'s refutation
   of the "slightly-ITM ≈ 0% carry" claim.
5. **The barrier geometry is asymmetric against the holder**, as (B) measured
   (~2:1 premium response). A symmetric-in-ATR spot barrier is already a losing
   geometry before any signal is applied; the observed ~2:1 stop:target ratio is
   that geometry showing up in the labels.

---

## What this means for the lane

* **The instrument and management half of the owner's construct is sound** and
  is corroborated by (A) and (B): monthly, DTE 8-22, ITM, ATR barrier monitored
  in spot space, force-roll before the decay knee. Keep it.
* **The signal half — MA / ADX / RSI / MACD on spot as the source of edge — is
  measured dead at the 2-3 day horizon on option instruments net of costs.**
  Four independent methods now agree (IC study, live-entry option backtest,
  positional trend lens, and this labelled-setup study).
* If the directional lane is to paper-trade a 2-3 day positional sleeve, the
  entry decision must come from **somewhere other than the spot indicator
  stack**. The indicator stack is defensible as a *timing/veto* layer on top of
  a signal with independent standing — but (B) failed to reconfirm the one such
  candidate we had (BANKNIFTY `oi_build` fwd3 fell from +0.54%/t=2.1 to
  +0.17%/t=0.94 on six months of new data).
* **Honest conclusion: as of today there is no validated 2-3 day directional
  entry signal for this lane.** Wiring the specified construct to paper would be
  shipping a measured loser. If the owner wants the sleeve live anyway, the
  defensible framing is *paper-only instrumentation to collect forward data*,
  explicitly not an edge claim, with the controls above run alongside so the
  comparison is available the moment there is enough forward sample.

---

## Limitations (what would change the answer)

* **One regime.** 2025Q2 → 2026Q2 rose. Short-side results are contaminated by
  that drift, which the `control_short` benchmark exposes (−11.9% to −12.1%) but
  cannot remove.
* **No bid/ask anywhere in the panel.** Costs are modelled, not measured. This
  cuts the optimistic direction only for the losing cells, so it does not
  rescue anything.
* **Barriers fixed a priori and deliberately not tuned.** A different multiple
  would change magnitudes. It would not plausibly flip a 42-46% directional hit
  rate into a profitable one — that is a signal problem, not a barrier problem.
* **Index sample is thin** (6 names, 1,066 index trades of 25,933). The
  index-only conclusions are much weaker than the stock ones.
* **Known Fyers cross-symbol tick contamination** still pollutes the 30m stock
  tape; ATR and daily-return outliers are filtered but 2026Q3 stock cells remain
  least trustworthy.
* **Nothing here tests debit verticals**, which (A) found is what practitioners
  actually use for a directional view at this horizon. That is the most
  promising untested branch, and it changes the carry term but not the 42-46%
  directional accuracy that is doing the damage.

## Reproducing

```bash
cd "TradeBot"
.venv/bin/python backend/directional_options/research/setups_2d3d/extract.py     # ~40s, ~440MB
.venv/bin/python backend/directional_options/research/setups_2d3d/harness.py     # ~60s -> data/trades.parquet
.venv/bin/python backend/directional_options/research/setups_2d3d/analyse.py     # -> results.txt
cd backend/directional_options/research/setups_2d3d && ../../../../.venv/bin/python test_causality.py
```

`data/` is gitignored (regenerable from PG).
