# Index directional long-options lane

Scope: **NIFTY, BANKNIFTY, SENSEX only.** Long premium only — the lane buys
calls or puts, it never writes them.

Two packages:

| Package | What it is |
|---|---|
| `directional_options/vol/` | The volatility substrate: Black-76 with a refusing IV solver, arbitrage-checked SVI fits, constant-maturity/constant-delta coordinates, realised vol, cones and the variance risk premium. |
| `directional_options/index_paper/` | The paper lane: authoritative book, four journals, vol-derived sizing, honest fills, greek P&L attribution, historical replay. |

Neither touches the existing `directional_options.service` / `paper.py` lane.
They are additive; nothing imports them yet.

---

## Why single-stock names are excluded

Chain breadth in this database peaks around 6.6 quoted contracts per stock per
day. Five SVI parameters cannot be identified from six points. The three
indices carry 20–130 strikes per (expiry, bar) on the 30-minute grid and fit
cleanly at 0.27–0.50 vol points of RMSE.

---

## What the data actually supports

Measured, not assumed, before any of this was built:

| Capability | Verdict |
|---|---|
| Front-expiry smile | **Yes.** 167 good fits across the three indices. |
| 25Δ risk reversal / butterfly | **Usually.** Available on ~75% of fitted bars; refused, not extrapolated, otherwise. |
| Term structure | **No.** Over a full month, *never* more than one expiry per bar carries ≥12 strikes (`b2 = 0` for all three indices). Constant-maturity interpolation across expiries is not possible. |
| IV history / IV percentile | **Not yet.** Full chain breadth only begins 2026-08-31. The substrate accumulates it going forward. |
| Realised vol, cones, percentiles | **Yes.** 30-minute spot back to 2024-01-01, ~600 clean daily bars per index. |
| Variance risk premium | **Yes.** Needs one implied number and a long realised series, both of which exist. |

Because IV history does not exist yet and realised history does, the lane's vol
gate is the **variance risk premium**, not an IV percentile.

---

## Data defects handled inside these modules

Each of these silently corrupts a vol number, so each is fixed at the point of
use rather than assumed away upstream:

- **Two instrument-key conventions per index** in `underlying_spot_candles`. The
  Fyers-keyed rows carry cross-symbol tick contamination — a 28,532 high on
  NIFTY, a 48,243 low on BANKNIFTY, both on 14/15-Jul-2026. Folding a day
  without picking one key first took `max(high)`/`min(low)` across both and put
  60-day realised vol on NIFTY at 38% against a true ~11%.
- **Post-close bars.** The `live_tick` source writes as late as 18:00 IST.
  Filtered to the 09:15–15:30 session.
- **Phantom highs under the canonical key too.** Caught by an excursion test
  against the open/close body, which a total-range ceiling alone misses.
- **Gap-spanning returns.** Dropping a contaminated day and then differencing
  consecutive survivors turns a multi-day move into a one-day return; it
  inflated close-to-close realised vol from 5.5% to 10.0%. Returns that span a
  dropped session are excluded, not stretched.
- **The last two bars of every session** carry only the 1–2 ATM-tracker strikes,
  because the chain sweep lands 45–60 minutes behind its bar. Bar selection
  carries a breadth floor.
- **Duplicate option candles.** Every chain read is `DISTINCT ON` the contract key.
- **Volume is absent.** 97.5% of index option rows carry `volume = 0` while OI is
  populated on essentially all of them. Any gate written against volume is an
  unpassable veto; liquidity uses OI.

---

## Running it

```bash
# Build and persist the vol substrate (idempotent per bar)
python -m directional_options.vol.builder --bars 60 --lookback-days 45

# One live pass of the paper lane
python -c "import asyncio, json; from directional_options.index_paper.engine import run; print(json.dumps(asyncio.run(run()), indent=2, default=str))"

# Historical replay, with the cost floor and gate tallies
python -m directional_options.index_paper.replay --bars 60 --reset

# Cost sensitivity — re-check any conclusion at 2x and 3x
python -m directional_options.index_paper.replay --bars 60 --reset --spread-multiplier 3
```

---

## Tables

| Table | Role |
|---|---|
| `index_vol_surface_slices` | one fit per (bar, underlying, expiry) |
| `index_vol_cm_points` | the fixed surface coordinates |
| `index_vol_tenor_metrics` | skew and VRP per tenor — what the lane reads |
| `index_vol_quarantine` | every quote the solver or fit refused, with why |
| `index_paper_positions` | **the book.** Authoritative, current state only |
| `index_paper_decisions` | every evaluation, including skips, with gate results |
| `index_paper_fills` | what each entry and exit actually cost |
| `index_paper_marks` | the mark-to-market trail |
| `index_paper_attribution` | greek decomposition of each closed trade |

The journals are **not** the book. Nothing reads them to decide what is held,
and the book is never rebuilt by replaying them.

---

## The measured cost floor

From a 60-bar replay driven by a deliberately uninformative baseline view
(28-Jul to 04-Sep-2026, 35 closed trades):

```
avg round-trip cost   Rs   109 / trade
avg theta paid        Rs  -370 / trade
avg realised P&L      Rs  -635 / trade
```

Tripling the spread assumption moves the cost to Rs 149 and the loss to
Rs 681 — so **the conclusion is not an artifact of the spread prior.** Theta,
not transaction cost, is what a long-premium index lane has to beat, by roughly
3.4 to 1.

Any candidate signal must clear ~Rs 480/trade of drag before it is interesting.

---

## Known limits

- **Spread is assumed, not measured.** There is no bid/ask anywhere in
  `option_premium_candles`. `calibrate_half_spread_ticks` is the seam where real
  fills replace the prior.
- **Path-wise attribution explains ~51%** of gross P&L on the replay. The mark
  trail is only as dense as the bars the lane processes; overnight legs are
  large and land in the residual.
- **Only the front expiry is tradeable** until chain breadth widens to a second
  maturity. `no_expiry_in_window` in the decision journal counts how often that
  bites.
- **The lane supplies no alpha.** It supplies discipline around a view: strike
  by delta, size by vol, honest fills, attributed outcomes. `adapters.latest_view`
  reads the existing directional lane's journal; inject any other provider.
