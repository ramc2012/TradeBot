# Index directional long-options swing lane

**Scope:** NIFTY, BANKNIFTY, SENSEX. Long premium only. Holding horizon **1–5
trading sessions**.

| Package | What it is |
|---|---|
| `directional_options/vol/` | Volatility substrate: Black-76 with a refusing IV solver, arbitrage-checked SVI fits, constant-delta coordinates, realised vol, cones, variance risk premium. |
| `directional_options/index_paper/` | The lane: factor panel, swing horizon, geometry-ranked contract selection, authoritative book, five journals, greek attribution, replay. |

Additive. Nothing imports these yet; the existing `directional_options.service`
and `paper.py` lane are untouched.

---

## The decision

The lane decides from the **whole factor panel**, not one signal. That is forced
by measurement, not preference: bucketing 1–5 day outcomes by IV level, by
variance risk premium and by realised-vol trend puts every bucket at 17–25% with
±5.5% standard errors. No single vol-state variable separates the days when long
premium pays from the days it does not.

Four outputs, deliberately separate:

- **Direction** — which way. From direction-role factors that must both clear a
  conviction floor and *agree* (a small net of two factors pulling apart is not
  the same as two quietly agreeing, and a weighted mean cannot tell them apart).
- **Size** — how much. From size-role factors: variance risk premium, futures
  open-interest z-score, breakeven geometry, realised-vol percentile.
- **Gates** — whether to trade at all: holdability against expiry, breakeven
  ceiling, round-trip cost, exposure.
- **Stand aside** — a first-class outcome, journalled with its reason.

### Factors

| Factor | Role | Confidence | Basis |
|---|---|---|---|
| `vrp_spread` | size | measured | Implied minus trailing realised. A long-premium lane pays this. |
| `futures_oi_z` | size | measured | IC **+0.139** vs \|5d return\| (t = 1.84, date-clustered); **+0.025** vs the *signed* return (t = 0.33). It sizes; it must not point. |
| `breakeven_ratio` | size | measured | A delta-0.40 call clears its 3-day breakeven 49.0% of the time at 25 DTE, 40.7% at 4 DTE. |
| `rv_percentile` | size | plausible | Trailing realised vol in its own 2.7-year cone. |
| `rv_compression` | size | speculative | Shadow — compression-precedes-expansion measured zero lift here. |
| `futures_oi_state` | direction | speculative | Shadow — price/OI quadrant; the signed IC behind it is 0.03. |
| `chain_oi_asymmetry` | direction | speculative | Near-money CE vs PE open-interest change. Volume is 0 on 97.5% of rows, so OI is the only positioning signal the chain carries. |
| `trend_20d` | direction | speculative | Shadow — momentum measured anti-predictive in this stack. |
| `reversion_10d` | direction | speculative | Shadow — the fade was the entry edge, before fill lag flipped it. |

Confidence sets weight (measured 1.00, plausible 0.35, speculative 0.15).
Unmeasured factors get **small but non-zero** weight on purpose: a lane whose
direction factors all carry zero never trades, never produces outcomes, and can
therefore never measure the factors that would let them be promoted. Every
factor is written to `index_paper_factors` at every decision — acting or not —
so `journal.factor_ic()` can grade each one later against outcomes this lane
actually had, rather than by re-fitting on the history that suggested it.

---

## Two clocks

Diffusion runs on **trading sessions**; decay runs on **calendar days**. Five
sessions is five chances to be right and seven days of theta. Conflating them
understates decay by ~40% on the one term that makes long premium hard.

The first version measured the hold in wall-clock 30-minute bars, so an
overnight gap counted as 36 bars and every position that survived a night
tripped its max-hold next morning — 34 trades, average hold 0.262 days, 27 of
them same-session. `horizon.py` owns the arithmetic now.

## Expiry is chosen by geometry, not proximity

Taking the nearest expiry systematically selects the worst contract for a
multi-session hold. Measured on 2.7 years of clean bars, against each index's
own breakeven at a 3-session hold:

| | DTE | breakeven | vs 1σ implied | P(clears it) |
|---|---|---|---|---|
| BANKNIFTY | 25 | +0.18% | 0.15σ | **49.0%** |
| SENSEX | 6 | +0.35% | 0.32σ | 42.5% |
| NIFTY | 4 | +0.40% | 0.43σ | 40.7% |

The binding constraint is that only **one expiry per index carries breadth**.
BANKNIFTY's is the 25–29 DTE monthly (ideal); NIFTY's and SENSEX's are the 0–6
DTE weeklies. NIFTY therefore often cannot hold three sessions at all — the
journal records that as `no_candidate_expiries_holdable` rather than silence.

---

## Data reality

| Capability | Verdict |
|---|---|
| Front-expiry smile | **Yes.** 167 arb-clean slices, 0.27–0.50 vol points RMSE. |
| 25Δ risk reversal / butterfly | Usually — refused, not extrapolated, otherwise. |
| Term structure | **No.** Never ≥2 expiries per bar with ≥12 strikes, over a full month. |
| IV history / IV percentile | **Not yet.** Chain breadth began 2026-08-31. |
| Realised vol, cones | **Yes.** 30-minute spot to 2024-01-01, ~600 clean daily bars. |
| Variance risk premium | **Yes** — one implied number against a long realised series. |
| Option order flow / tape | **No.** Volume is 0 on 97.5% of rows; no option tape anywhere. |
| Per-index participant OI | **No.** `participant_oi` is a market-wide NSE aggregate; SENSEX absent. |

Dead ends verified, not assumed: `oi_positioning` has zero index rows;
`fo_mwpl_snapshot.utilisation_pct` is 100% NULL; `fo_security_ban` is empty;
`directional_positioning_daily`'s OI columns track chain breadth, not
positioning.

## Data defects handled at the point of use

- Two instrument-key conventions per index in `underlying_spot_candles`; the
  Fyers-keyed rows are tick-contaminated (28,532 high on NIFTY, 48,243 low on
  BANKNIFTY) and pushed 60-day realised vol to 38% against a true ~11%.
- `live_tick` writes bars as late as 18:00 IST, past the close.
- Phantom highs under the canonical key too — caught by an excursion test
  against the open/close body, which a range ceiling misses.
- Dropping a bad session then differencing survivors turned multi-day moves into
  one-day returns, inflating close-to-close RV from 5.5% to 10.0%.
- The last two bars of each session carry only ATM-tracker strikes.
- Duplicate option candles — every chain read is `DISTINCT ON` the contract key.
- Futures OI carries 20 BANKNIFTY rows with NIFTY-level closes; rollover and
  near-expiry rows are excluded (`dte > 3`) because the pre-expiry OI collapse
  is mislabelled `long_unwind`.

---

## Honesty properties

- **Fills are always adverse**, and happen one bar *after* the decision. The
  chain sweep lands 45–60 minutes behind its bar, so a decision at bar T uses
  information a live pass at T would not have had.
- **Slippage is counted once.** `entry_premium` is the fill price;
  `entry_cost` is charges only. Attribution `net_pnl` now equals book
  `realized_pnl` exactly (was mismatched on 33 of 34 trades).
- **Realised vol is as-of the bar.** The builder previously loaded it once and
  reused it, so every historical bar was stamped with future realised vol —
  NIFTY read 0.05942 identically across four sessions.
- **Risk management never pauses.** Marks, stops and expiry settlement run even
  when the surface fails to fit; the previous version returned early on
  `surface_unusable` (264 occurrences) before marks were taken.
- **Refusals carry their shortfall.** "The budget does not buy one lot" names
  the gap and the binding cap.

---

## Tables

| Table | Role |
|---|---|
| `index_vol_surface_slices` / `index_vol_cm_points` / `index_vol_tenor_metrics` / `index_vol_quarantine` | the vol substrate, and what it refused |
| `index_paper_positions` | **the book.** Authoritative, current state only |
| `index_paper_decisions` | every evaluation, incl. skips, with gate results |
| `index_paper_factors` | every factor value at every decision, acting or not |
| `index_paper_fills` / `index_paper_marks` / `index_paper_attribution` | costs, the mark trail, the greek decomposition |

The journals are **not** the book. Nothing reads them to decide what is held.

## Running it

```bash
python -m directional_options.vol.builder --bars 60 --lookback-days 45
python -m directional_options.index_paper.replay --bars 60 --reset --horizon-sessions 3
python -m directional_options.index_paper.replay --bars 60 --reset --spread-multiplier 3
```

## Known limits

- **Four sessions of wide chain history.** Any P&L from a replay is a machinery
  check, not evidence. `factor_ic()` refuses below 20 closed trades.
- **Spread is assumed, not measured** — there is no bid/ask anywhere in
  `option_premium_candles`. `calibrate_half_spread_ticks` is the seam.
- **Path-wise attribution explains ~80%** of gross P&L; overnight legs are large
  and land in the residual.
- **The futures-OI collector is a one-off backfill**, not a daily job. The
  factor refuses a row older than six days rather than reading stale positioning
  as live.
