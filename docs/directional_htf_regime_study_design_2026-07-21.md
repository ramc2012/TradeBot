# HTF Regime → LTF Timer → Short-Horizon Option — Study Design + Data Layer
**2026-07-21 · design pass (definitions frozen BEFORE measurement) · research-only**

Module: `backend/directional_options/research/htf_regime/`
Status: data layer BUILT and smoke-verified end-to-end on the June–July 2026
tape; regime/timer/control/grid definitions frozen a priori in code; the
measurement pass has NOT run — no outcome has been looked at.

## 0. The hypothesis (owner's model, and why it is not the dead cascade)

> "Options are short life instruments. Hence we need high time confirmation
> before taking trade in options. Like after confirmed uptrend in daily
> timeframe options are traded with short time frame like 30m, 1hr etc."

The cascade study killed 30m-confirms-then-daily-confirms as a *prediction
chain* (the daily confirm was mechanically downstream of the move). This
study inverts the roles: **daily = standing regime STATE evaluated every
bar; 30m/1h = entry TIMER inside that state; option held hours to 2–3 days
so theta per trade is small.** Our own prior results are consistent with
the state framing (median regime run 12–18 sessions; moves long not fast;
2–3 day holds fill-insensitive; the lone surviving construct, `str_below0`,
is an entry-timing object). Untested until now: (a) daily regime as a
standing filter, (b) LTF entries inside it, (c) short-horizon option
expression.

## 1. regime_definitions (`regime_defs.py`, frozen)

Causality contract: the state governing session *t* is computed from bars
**through t−1's close** (`r*_lag1`); consumers may never read same-day state.
Both directions: up-state → CE only, down-state → PE only.

| id | definition | primary? | declared sensitivity |
|---|---|---|---|
| **R1** | close > SMA20 > SMA50 AND SMA20 rising (SMA20[t] > SMA20[t−3]); mirrored for down | **PRIMARY** (operationalises the owner's words) | rising-lookback {1, 3, 5} |
| **R2** | Wilder ADX(14) > 20 AND +DI > −DI (up) / −DI > +DI (down); exact recursion reused from cascade (already causality-verified) | cross-check | threshold {18, 20, 25} |

**Regime AGE** (sessions since state began) is computed and reported by
buckets {1–5, 6–12, 13–20, >20} — *descriptive only*, motivated by the
12–18-session median run: late entries are hypothesised to die. Age is not
a tunable parameter this pass.

## 2. timer_definitions (`timer_defs.py`, frozen)

Evaluated on 30m native and 1h (paired 30m bars from the 09:15 IST open,
complete pairs only), **only when the governing regime state is on**.
Fill: signal-bar close primary; next-bar-open declared as the fill-lag
sensitivity (the cascade showed 1-bar lag can flip a marginal edge — both
always reported).

| id | definition | note |
|---|---|---|
| **T1 deep_macd** | MACD(12,26,9) line crosses above signal, line < 0, depth −macd/close ≥ 0.0015 (sens. {0.0010, 0.0015, 0.0025}) | the `str_below0` survivor; inside an UP regime this is a PULLBACK entry — fade-inside-trend |
| **T2 pullback_anchor** | prev close > EMA20, bar low ≤ EMA20, close > EMA20 (touch-and-reclaim); session-VWAP anchor = declared variant | |
| **T3 orb** | close beyond the first bar's range, once per session, before 13:00 IST | with-regime breakout |
| **T4 macd_plain** | any MACD cross up | CONTROL timer — isolates what "deep" adds |

## 3. control_design (`controls.py`, frozen)

- **C1 timer-unfiltered**: identical timer, regime ignored. "Does the daily
  filter lift the timer at all?"
- **C2 random-inside-regime — THE LOAD-BEARING CONTROL**: random bars drawn
  from the same regime-on universe, matched per cell on entry count AND
  per-underlying composition, pushed through the identical contract
  selection, holds, and costs. 200 seeded draws (seed 20260721) → null
  distribution; the cell's percentile against it is the decisive number.
  *Why load-bearing*: in a 15-month broadly-rising sample, "daily uptrend +
  long CE" inherits market beta; C2 carries that beta in full, so beating
  C2 is the only evidence the timer adds anything beyond being long in an
  up-market.
- **C3 regime-value isolation**: C2 vs matched unconditional random bars —
  attributes the remainder to the regime itself (expected: mostly beta;
  will be reported plainly as such if so).

Pre-registered interpretation rule: a cell is positive **only if** it clears
C1 *and* its C2 percentile clears the corrected threshold. Clearing C1 but
not C2 = "the regime is beta, the timer adds nothing" — stated plainly.

## 4. option_read_layer (`option_read_layer.py`) — BUILT, the reusable artifact

**API** (pure functions over frames; no PG at query time; promotion into
`directional_options/` is mechanical):

```python
layer = OptionReadLayer(opt_frame, spot30m_frame)          # dedups both tapes
cs    = layer.contracts_for(und, session, side)            # tradeable set
        # → contract_id, expiry, strike, band, dte, mny, iv/oi_present
bars  = layer.bars(contract_id)                            # deduped 30m tape
mark  = layer.mark(contract_id, ts)                        # priced + flagged
        # → Mark(price, ts, bar_exact, stale_minutes,
        #        modelled_exit, model_method, iv_present, oi_present)
```

Inherited defect fixes (hard rules, not caller options):

- **D1 no moneyness filter at extraction** — bands (ATM |m|<0.75%,
  slight-ITM m∈[−3%,−0.75%] centre −1.8%) computed at selection time from
  the spot tape; winners that run ITM keep their tape.
- **D2 no `underlying_price` predicate anywhere** — the column is not even
  SELECTed; moneyness always from `underlying_spot_candles` joined by time.
- **D3 modelled exits** — a mark with no bar within 45 min is MODELLED:
  Black–Scholes carrying the last real broker IV over the spot at exit
  (`bs_carry_iv`); intrinsic floor when the tape never had IV
  (`intrinsic_floor`, flagged biased-low). `modelled_exit` + `model_method`
  flags let the analysis report modelled-exit rate BY OUTCOME (the
  walk-away censoring is winner-correlated, so this is mandatory).
- **D4 dedup, never sum** — key = (underlying, expiry, strike, option_type,
  time), source priority upstox > upstox_expired > fyers, ties by
  MAX(volume). Measured on the smoke tape: **22.6% cross-broker dup rate**
  (matches the known ~20%).
- **D4-spot (NEW FINDING this pass)** — `underlying_spot_candles` also
  carries cross-source duplicates (upstox_spot / fyers /
  source_1minute_aggregate / live_tick; ~65% duplicate timestamps in the
  legacy panel CSVs). This made prior studies' indicator paths
  order-nondeterministic (unstable quicksort over duplicate keys). Layer
  rule: source priority upstox_spot > fyers > 1m-aggregate > live_tick;
  legacy no-source CSVs deduped by max-volume proxy; all sorts stable.
- **D5** — every mark and contract row carries `iv_present` / `oi_present`
  (measured: IV 18.0%, OI 73.2% — matches the known 17% / 57–71%).

Extraction (`extract_opt.py`): half-month COPY windows, `time` bounded by
literal UTC timestamps directly on the partitioning column, no casts, no
functions, streamed to CSV. Default = June–July 2026 smoke (done, ~1.6M
rows). `--full` = 2025-03-15 onward, to run off-hours only; for the
measurement pass proper the preferred route is **signal-driven extraction**
(compute timer entries from spot first, then pull only entered
contract-months, as cascade/ver_full_tape.py did).

## 5. grid_size (`study_grid.py`, enumerated in code)

- Cells: regimes(2) × timers(4) × timeframes(2) × holds(4: 2h, EOD, 1d, 3d)
  × moneyness(2) = **128**.
- Decisive comparisons per cell: 2 (vs C1; vs C2) → **256 tests**.
- **Bonferroni α = 0.05/256 = 1.95e-4**; Benjamini–Hochberg FDR q = 0.10
  reported alongside.
- **Primary pre-registered cell** (alone at α = 0.05):
  R1 × deep_macd × 30m × 1-day hold × slight-ITM.
- Sensitivity parameters (depth grid, ADX threshold, rising-lookback,
  VWAP anchor, fill-lag) are descriptive; promoting any of them into a
  claim requires entering this count.

Outcome reporting per cell: full net-of-cost payoff distribution (stock
round-trip 8% of premium primary; grid {2%, 5%, 8%, 10%}), hit rate,
median, mean, **ex-top-3**, per non-overlapping period, episode-clustered
(overlapping same-name entries collapsed; session-block bootstrap), and
modelled/stale-exit rate by outcome.

## 6. data_coverage (measured, deduped, this pass)

| expiry | bars | underlyings | tape span (UTC) | verdict |
|---|---|---|---|---|
| 2026-06-30 | 409k | 216 | 06-01 → 07-17 | full life ✔ |
| **2026-07-28** | 687k | 216 | 06-01 → 07-21 (live) | full life so far ✔ primary |
| **2026-08-25** | 98k | 213 | 06-15 sparse; 7 sessions of 30m backfilled 07-13→07-21 | usable from 07-13 ✔ |
| 2026-05-26 | 7.8k | 193 | 06-22 → 07-17 | **ANOMALY: bars timestamped AFTER expiry** — DTE gate excludes them from selection; flagged for the cleanup workflow |

Spot: 110k deduped 30m rows / 225 names in-window; panel_2d3d CSVs extend
spot back to 2025-01. Option 30m tape begins ~2025-03-20 (panel evidence),
so the full study window is ~15 months ≈ 14–16 non-overlapping monthly
expiries — enough for per-period reporting but ONE macro regime
(survivorship + one-regime caveats mandatory in the results report).

## 7. causality_test_plan (`test_causality.py` — 5/5 PASSING)

1. **Prefix invariance, rtol 1e-12**: regimes and all timers recomputed on
   tapes truncated at 12 seeded random cutoffs; every value ≤ cutoff must
   equal the full-sample value. (This test CAUGHT the D4-spot duplicate/
   unstable-sort nondeterminism on real data before it could contaminate
   results.)
2. **Lag contract**: `r*_lag1[t] == r*_state[t−1]` exactly.
3. **Read-layer causality**: `contracts_for(asof)` and `mark(ts)` invariant
   to deleting all bars strictly after asof/ts (model carries IV from ≤ ts
   only).
4. **Dedup honesty**: cross-broker duplicate collapses to the preferred
   source, volume never summed.
5. Runs green with PG unreachable (real extract if present, seeded
   synthetic fallback).

## 8. What was deliberately NOT done this pass

No outcome measured, no cell computed, no parameter touched after seeing
data. The one number class looked at was coverage/defect rates (dup %, IV %,
OI %, expiry spans) — none of which is an outcome. Next pass: signal-driven
option extraction for timer entries over 2025-03→2026-07, then the 128-cell
measurement against C1/C2/C3 under the corrected thresholds above.
