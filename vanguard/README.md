# Vanguard — Phase 1 status (2026-08-27)

Trade-selection research system per `PROJECT VANGUARD — Claude Code Handoff
Specification v1.0`. This file records what Phase 1 actually delivered
against that spec, where reality diverged from its assumptions, and what a
reviewer should check before trusting any of it.

## Decisions this build made (asked and answered before writing code)

1. **Data source**: reads Fyers via MACD mini's already-authenticated daily
   token (`~/CLAUDE PROJECTS/MACD mini/runtime/credentials.json`, read-only),
   not a revived Nomad Curie Fyers session. Nomad Curie's own credentials are
   Upstox-only by a deliberate 12-Aug-2026 decision; the spec's assumed
   "fyers-apiv3 primary" stack does not match this repo.
2. **Module reuse**: `auction_intelligence` / `cbe_scanner` /
   `fractal_market_profile` are full live subsystems (RL versions, position
   discipline, tolerance scaling), not simple classifiers. Vanguard does not
   import or depend on them. Where it needs comparable feature logic (M3
   GEX regime, M5 timing) it will be reimplemented independently in a later
   phase, at the cost of not reusing their production-hardened code.
3. **Branch**: `vanguard/phase-1`, off `origin/main`, in an isolated
   `git worktree` (`.claude/worktrees/vanguard-phase-1`) so it never touches
   the dirty, in-flight `ui/consolidation-0-3` checkout.
4. **Missing "Phase 0" files**: `fno_universe_aug2026_series.csv`,
   `fyers_intraday_pipeline.py`, `build_sector_indices.py` and
   `sector_proxy_daily_closes.csv` do not exist anywhere on this machine.
   Built fresh below — except the bars pipeline, which turned out to be
   unnecessary (see next section).

## The spec assumed less existing infrastructure than this repo actually has

Before writing any collector, the live `nomadcurie` Postgres was inventoried
(72 tables). Five things the spec calls "new" already exist, live, and
current as of today:

| Spec asset | Already exists as | Coverage found |
|---|---|---|
| `bars_30m` / `bars_1d` | `underlying_spot_candles` | 225 underlyings, 30m/15m/5m/3m/1m, 2021-present |
| `option_chain_snap` | `option_chain_snapshots` | 22.5M rows; narrowed to 4 index underlyings recently |
| PCR/OI ingredient (M2.5) | `fo_option_chain_metrics` | oi_pcr, volume_pcr, ce/pe oi+volume, 30m, 476 chunks |
| Universe + lot sizes | `fo_underlying_catalog` | 211 stocks + 7 indices, instrument keys, lot sizes |
| Ban list | `fo_security_ban` | Sourced from NSE's `fo_secban.csv` archive already |

**Vanguard reads all five directly rather than duplicating them.** This is
why there is no `fyers_intraday_pipeline.py` in this build: the 3-month OHLCV
fetch it would perform is already running, has been for years, and covers
5 years rather than 3 months. Re-fetching it would be pure waste and a second
source of truth to keep in sync.

Genuinely absent from the schema, and built fresh:
`participant_oi`, `sector_taxonomy` (sector/sector_group/sector20 — no
existing table carries this), `ingest_log`, `results_calendar`, `sector_rs`,
`leadlag`. See `db/migrations/001_schema.sql` for the full reasoning.

## Status (2026-08-27)

**M1 through M10 are all built and running.** An earlier revision of this file
declared M2/M3/M5 and M6–M10 "not started"; that was true when it was written
and false by the time anyone read it. Corrected here rather than left to
mislead the next reader into re-implementing modules that already exist.

| Module | State | Output |
|---|---|---|
| M1 participant OI + bhavcopy/delivery, bulk-block, announcements, USDINR | built | `participant_oi`, `bhavcopy_delivery`, `bulk_block_deals`, `corporate_announcements`, `usdinr_daily` |
| M2 options informed flow | built | `features_flow` — 9,240 rows, 44 sessions (2026-05-25 → 07-28), 210 names |
| M3 GEX regime | built | `regime` — 13,231 rows |
| M4 sector RS + lead-lag | built | `sector_rs` (960), `leadlag` (207) |
| M5 microstructure timing | built | `timing` — 128,459 rows |
| M6 fusion & selection | built | `tickets` (emitted AND gated near-misses) |
| M7 risk & sizing | built | consumed by M6; no table of its own |
| M8 backtest harness | built | `vanguard_backtest_runs` |
| M9 paper execution | built | `decisions`, `fills`, `outcomes`, `paper_capital_daily` |
| M10 journal & attribution | built | `attribution_runs` |
| UI | built | `/strategies/vanguard` + `/api/vanguard/*` (read-only), on branch `vanguard/ui` |

239 offline tests pass (`make test`).

## The lane emits nothing, and the reason is DATA, not tuning

This is the single most important fact about Vanguard today, and the desk's
Decision-flow tab exists to keep it visible.

M6 needs four inputs to coincide: flow + sector RS (prior session) and regime
+ timing (same bar). They do not coincide any more:

- `features_flow` ends **2026-07-28**. Stock-level option-chain collection was
  retired around 2026-08-12, so there is no newer input to compute it from.
- `regime` collapses to 0-5 symbols per session from 2026-07-29 onward.
- `timing` alone stays healthy (~2,800 rows/session, 213 names).

Since the 2026-08-27 review this is no longer an inference from coverage
counts -- it is measured, per symbol, per bar, in `candidate_evaluations`.
Across 116,238 journaled symbol-bar evaluations (29-May to 26-Aug):

| leg | deaths | share |
|---|---:|---:|
| flow_fresh | 59,239 | 51.0% |
| flow_strength | 43,937 | 37.8% |
| flow_present | 10,007 | 8.6% |
| sector_rs | 2,505 | 2.2% |
| regime | 534 | 0.5% |
| timing | 16 | 0.01% |
| **survived all six** | **0** | **0%** |

Flow availability, freshness and strength account for **97.4%** of all
deaths. Only 550 evaluations in three months ever reached the regime leg and
only 16 reached the timing leg. The long-standing assumption that IGNITION is
the scarce ingredient is simply wrong: by the time a candidate gets there it
almost always passes. Lowering the conviction gate would change nothing,
because nothing reaches the gate.

## What the 2026-08-27 review changed

Six defects from that review are fixed. Each has a test that fails if it
returns.

1. **The EOD joins had no maximum age.** `features_flow`, `sector_rs` and
   `leadlag` were joined as "the newest row strictly before this day" with no
   lower bound at all, and the cycle daemon runs M6 every 30 minutes. With
   flow frozen at 2026-07-28, live bars were joining a month-old score as
   "yesterday's reading" and nothing objected -- only the accident of `regime`
   being NULL kept tickets from being emitted on it. There are now explicit
   `flow_fresh` / `sector_rs` / `regime` shelf lives (3 sessions / 3 sessions /
   2 bars), and the age of every joined input is journaled next to the input.
2. **The daemon woke on the wrong grid.** NSE opens at 09:15, so its 30-minute
   bars close at 09:45/10:15/10:45. The daemon woke on the wall-clock :00/:30,
   so every live pass evaluated a bar that had closed ~17 minutes earlier --
   over half a 30-minute trigger's life spent waiting for the scheduler.
   `m5_timing` additionally now drops off-hours bars and the second,
   15-minute-offset grid: those carried ~5 symbols against an NSE bar's ~210,
   and `max(ts)` regularly landed the whole lane on a 5-symbol phantom bar.
3. **Sizing made every risk limit non-binding.** `sizing_risk_rupees` held the
   FULL PREMIUM while the stop sat at -15% of it, so a stop-out cost 0.1125% of
   capital and the -2% daily stand-down needed ~18 of them against a
   3-position cap. Risk-at-stop and premium are now two separate numbers and
   both caps bind. **A 3.3x looseness remains and it is in the CONFIG, not the
   code** -- see `sizing_coherence()` and the open decision below.
4. **The near-miss journal did not journal the near-misses that mattered.**
   Candidates failing the sector-RS, regime or timing leg were dropped with a
   bare `continue` before a Candidate existed, so `tickets` could explain four
   conviction failures and nothing about the thousands the filter killed.
   `candidate_evaluations` now records one row per (bar, symbol) with each
   leg's own verdict; the funnel is a GROUP BY over it rather than a second
   copy of the filter.
5. **Component ICs were computed inside the filter's own acceptance region.**
   `research/cross_section_ic.py` scores the full universe instead. Results
   below.
6. Restoring an IV feed is deliberately NOT done. It was the sixth item on
   the review's list and explicitly gated behind (5): collecting more of a
   feature nobody has shown is predictive is the wrong order.

## The first honest measurement of the components

`make ic` -- 43 sessions, ~98,000 symbol-bar observations, Spearman rank IC
per bar averaged per session, standard error taken ACROSS sessions (n in every
t-statistic is the session count, never the observation count).

| component | h=1 IC | t | reading |
|---|---:|---:|---|
| conviction (vs abs return) | +0.0426 | +8.23 | real, and about MAGNITUDE only |
| M2 flow (signed) | -0.0017 | -0.50 | **indistinguishable from zero** |
| M3 gamma pct (centred) | +0.0059 | +1.78 | unproven, CI includes zero |
| M4 sector RS (signed) | -0.0123 | -2.35 | **significantly NEGATIVE** |
| M5 timing (signed by VA side) | -0.0194 | -3.63 | **significantly NEGATIVE** |

Three things follow, and none of them is comfortable:

- **M2 -- the lane's "core new edge" -- has no measurable cross-sectional
  ordering power.** Not weak: zero, at every horizon tested (1, 2 and 4 bars).
- **M4 and M5 are anti-predictive at one bar**, and M6 requires sector RS to
  CONFIRM the flow direction. The confirmation leg is pushing toward the wrong
  side. This agrees with this programme's own earlier finding that intraday
  FADE, not momentum, is the entry edge.
- **Conviction predicts SIZE OF MOVE, not direction** (it is scored against
  the absolute return). That is a real and useful signal, and it is not a
  trade on its own.

Caveats that must travel with those numbers: forward returns are on the
UNDERLYING, not on the option, so this measures signal quality and not
tradeable P&L after spread and decay; 43 sessions is a start, not a verdict;
and the ICs are small in absolute terms even where the t-statistics are not.

## Why "today" was always missing, and what now fills it

Two independent defects, both silent, found 2026-08-27.

**1. The durable same-day writer targeted a broker this deployment does not
use.** `backend/market_data/stock_spot_sweeper.py` exists precisely to fill the
F&O stock spot grid and is scheduled post-close. It fired on time every day
and logged:

    10:05:16 WARNING [stock-spot-sweep] no Fyers session — skipping this pass
    10:05:16 INFO    [MarketHoursSupervisor] stock_spot_sweep completed

Nomad Curie has been Upstox-only since a deliberate 12-Aug-2026 decision, so
`skipped_no_broker` was its outcome on every scheduled pass, for as long as the
deployment has been Upstox-only. The supervisor recorded "completed" each time.

**2. The only writer left standing calls an endpoint that cannot return
today.** `data/upstox_research_sync.py` uses Upstox `/historical-candle`, which
never includes the current session even when today is passed as the `to` date
— verified: asking for 25-Aug..27-Aug on the 27th returned 26 candles covering
only the 25th and 26th. So today's bars could appear only once today became
yesterday, which is exactly what was measured: every one of 26-Aug's thirteen
session bars first landed at 27-Aug 00:00:56 UTC.

Upstox serves the current session from `/historical-candle/intraday/...`, which
returned all 13 of 27-Aug's bars when asked. **Both endpoints are PUBLIC** —
they answer with no Authorization header — so the fix needed no broker session,
no token, and no share of the authenticated rate budget. Nothing in the
codebase had ever called the intraday one.

### What changed

- `stock_spot_sweeper` now calls Upstox directly and needs no broker session.
  History and intraday are separate legs that **fail independently**: a history
  error must not cost a symbol today's bars, because history can be re-fetched
  on any later pass and the current session cannot once it has gone stale.
- A new `stock_spot_intraday` supervisor runner sweeps the universe **during**
  the session every 30 minutes. The post-close sweep is deliberately
  post-close-only because a 211-symbol sweep of AUTHENTICATED history would
  compete with live decision traffic; that argument does not carry to a public
  endpoint, and CLASS_BULK admission still keeps it behind any CRITICAL waiter.
- Backfilled: **2,743 rows across 211 equity underlyings for 27-Aug**, then
  M5 / M3 / OI / IV / surface / sentiment / M6 recomputed. Every Vanguard table
  is now current to the session that just closed rather than the one before it.

**One more silent filter, caught in verification.** `RunnerConfig.plane`
defaults to `"strategies"` and this container boots `LANESET=core`. Left at the
default the new runner was dropped from the built list — the live supervisor
showed ten runners with `stock_spot_sweep` present and `stock_spot_intraday`
absent. It is tagged `plane="core"` like its sibling, and a test now asserts
both. Registered in source is not the same as built at runtime.

### The residual lag, stated rather than hidden

The intraday sweep's 30-minute cadence is not phase-aligned to the exchange's
bar closes, so the gap between a bar closing and its row existing is uniform
over 0-30 minutes and no fixed delay can cover it. A Vanguard pass whose bar has
not landed evaluates the previous one and the next pass picks it up. That is
bounded and self-healing, and it is visible: M6 prints the age of the bar it
evaluated, and the freshness legs reject inputs that have aged out, so a late
bar can never be mistaken for a current one.

## The bigger thing found while fixing the bar grid

**`underlying_spot_candles` is not an intraday feed for NSE equities. It is a
T+1 overnight batch.** Verified 2026-08-27: every one of 26-Aug's thirteen
session bars -- the 09:15 bar included -- first appeared at 27-Aug 00:00:56
UTC, and the session was re-touched again at 06:10 UTC. Not one bar arrived
during the session it describes.

The cycle daemon's 30-minute "live pass" therefore cannot see the current
session at all. At best it re-evaluates yesterday; on 27-Aug it saw only the
13 commodity symbols that trade a different session. M6 now prints how old the
bar it is evaluating actually is, and the desk shows the same lag in its
header, but the cadence itself is an owner decision:

- run the lane ONCE after the overnight batch lands (~05:30 IST) and stop
  calling it live, or
- find a genuine intraday spot source for the equity universe.

Nothing in this build pretends the current arrangement is live.

## M2's "no stock-level OI source exists" was wrong

`features/m2_flow.py`'s module docstring states, as a verified live finding,
that no stock-level OI source exists anywhere in the schema, and on that basis
its fourth ingredient -- the delta-OI conjunction, 15% of the flow composite --
has been hardcoded NULL for every row the lane has ever written.
`classify_oi_state()` sat implemented and unit-tested and was never once called
with real data.

Two live per-symbol sources exist, both fresh on 2026-08-27:

| source | what it is | coverage |
|---|---|---|
| `fo_mwpl_snapshot.open_interest` | NSE's own aggregate F&O OI per symbol -- the same MWPL publication the ban list is read from | 211 symbols, daily, from 2026-07-09, same-day |
| `option_premium_candles.oi` | per-contract chain OI | 213 underlyings, latest print 2026-08-27 09:57 UTC |

Only the GREEKS on `option_premium_candles` died in July; `close`/`oi`/`volume`
never stopped. That also revives an aggregate everyone had written off:
`fo_option_chain_metrics` (ce_oi/pe_oi/oi_pcr) tops out at 2026-08-03 and is
treated as dead, but its INPUTS are alive, so the CE/PE split is now recomputed
from them rather than read.

`features/m_oi_positioning.py` (migration 007, table `oi_positioning`) assembles
16,152 symbol-sessions from 2026-05-29 covering aggregate OI and its session
change, MWPL utilisation, the CE/PE split and PCR, close and 1/5/20/60-day
returns, and the four-state conjunction:

| state | reading | count |
|---|---|---:|
| short buildup | price down, OI up -- fresh shorts | 3,729 |
| long buildup | price up, OI up -- fresh longs | 2,965 |
| short covering | price up, OI down -- shorts leaving | 2,502 |
| long unwinding | price down, OI down -- longs leaving | 2,343 |

It also surfaces something the lane had no visibility of at all: **12 symbols
are at or past 95% MWPL utilisation**, where NSE bans fresh F&O positions.
M7 still does not veto on this. The desk says so on the row.

Two things this got wrong on the first run, both now tested:

- `fo_mwpl_snapshot` **publishes on non-trading days** (2026-08-23 was a Sunday
  and carried 207 rows). Those rows joined the grid with a real OI and no close,
  so shifting `close` over the raw grid put a NaN in front of the next real
  session and blanked its positioning read -- 24-Aug had 207 OI rows and ZERO
  states. Price and OI deltas are now both taken over trading sessions only, so
  the conjunction always pairs two moves measured across the same interval.
- The two OI sources are different quantities. A delta is never taken across a
  change of `oi_source`, or a collection gap would render as a position unwind.

## Implied volatility is now COMPUTED, not sourced

`option_premium_candles.iv` and `.delta` stopped for the equity universe on
2026-07-28 and `.gamma` around 2026-06-23, which is why M2's first three
ingredients have been uncomputable for a month. Checked 2026-08-26: 5,856
contract rows, every one with a `close`, not one with an `iv`.

Fyers can return a chain with greeks and this repo can reach one through a
sibling project's token. It is still the wrong answer: its `optionchain`
endpoint resolves LIVE contracts only, so it cannot backfill 2026-07-28 to now
and cannot produce a single historical IV -- the cross-sectional IC study, the
backtest and every trailing z-score in M2 would all stay unserviceable. It
would also make a core feature depend on another project's daily OAuth token.
Everything needed to SOLVE for IV has been in Postgres for years.

`features/m_implied_vol.py` (migration 008, table `option_iv`) solves it:
Black-Scholes-Merton, European exercise (NSE index AND stock options have been
European-style cash-settled since 2011), vectorised Newton on vega with a
bisection fallback, and analytic delta/gamma/vega/theta once sigma is known.
**174,487 contracts solved across 2026-05-29 to 08-27, 50.5% good quality**,
in about ten seconds per three months.

**The conditioning gate is the part that matters.** An IV solver fails
QUIETLY: a wrong answer is a plausible number in a plausible range, and
everything downstream keeps working while measuring nothing. Measured here: at
8% vol, 0.15y and 15% out of the money, a contract's entire time value is
6e-08 rupees and vega is 3e-05, so one 5-paise tick is worth ~1,800 vol points
of sigma -- and the solver returned **9.3% for a true 8%**. It now computes
`iv_uncertainty = TICK_SIZE / vega` at the solution and returns NULL wherever
that exceeds 5 vol points. The uncertainty is stored even for refused rows, so
a NULL is inspectable rather than mysterious.

`quality` / `quality_flags` are first-class columns (`no_solution`,
`vol_not_identified`, `below_tick`, `thin_oi`, `no_volume`, `far_otm`,
`extreme_iv`, `near_expiry`) and only `good` rows feed the surface.

### The chain has never been wide enough for a 25-delta skew

`features/m_iv_surface.py` (migration 009) aggregates to one row per
(symbol, session): **13,156 rows, ATM IV populated on 100%, IVS on 91%** --
including the whole month after the vendor's IV died.

`skew_25d` is populated on **10%**. Contracts per symbol per day, measured
across the collected history:

| period | contracts/symbol/day |
|---|---:|
| late May | 3.1 |
| late June | 6.5 |
| early July | **6.6 (the widest it ever got)** |
| late August | 1.2 |

A 25-delta contract sits well out on the wing and needs a chain spanning ten
to twenty strikes. Three to six strikes clustered around the money do not
contain one, and never did. **m2_flow computes its skew with
`idxmin(|delta - 0.25|)` and no tolerance band**, so it accepted whatever
strike was nearest however far away -- a near-ATM contract. Its SKEW
ingredient (25% of the composite) was therefore measuring approximately the
same quantity as its IVS ingredient (30%): **55% of the flow score was one
quantity counted twice.**

This build refuses that substitution. `skew_25d` requires contracts within
0.08 delta of ±0.25 on BOTH wings, and `skew_reason` names what was missing
otherwise. `n_strikes` and `delta_span` travel with every row.

## Sentiment

`features/m_sentiment.py` (migration 009, table `market_sentiment`) blends
five families per session: FII/DII/Client/Pro positioning and its session
CHANGE, market-wide OI and volume PCR, NIFTY ATM IV and the stock median,
breadth (advances/declines, % above 20-day), and the OI-buildup rollup.

**Market-wide only, by construction.** NSE's participant-wise OI is an
aggregate by instrument class with no per-symbol dimension, so FII positioning
can describe a regime and can never be attributed to a name. It gets its own
tab rather than a column for that reason.

Two failures caught on the first runs, both now tested:

- **A composite over one family IS that family.** 2026-08-27 had only the
  options family (the spot feed is an overnight batch, so the session had no
  price or IV) and renormalising the weights reported **+100.0 from a single
  input**. There is now a `MIN_FAMILIES = 3` gate, the same shape as M2's
  ingredient minimum. The individual readings are still stored when the blend
  is suppressed -- they are measurements even when the composite is not.
- **Session changes were being differenced against weekends.** 2026-08-24 had
  a real FII net, a real PCR and a real NIFTY IV and still reported two
  families, because all three of its CHANGE columns had been differenced
  against an empty Saturday row.

`sentiment_score` is a summary for reading, **not a validated signal**: no
cross-sectional IC study stands behind it, the participant series began in
August 2026, and several families are contemporaneous rather than predictive.
The desk says so on the panel.

### A grid rule that was over-generalised

NSE **stocks** bar on the exchange's 09:15-anchored `:15/:45` grid. NSE
**indices** bar on `:00/:30` -- NIFTY and BANKNIFTY have 115 of 128 recent
bars there while RELIANCE has none. Applying the equity grid rule to the IV
solver's spot join therefore discarded the index spot entirely: NIFTY had a
solved chain on 8 sessions and a joinable spot on 1, and
`market_sentiment.index_atm_iv` was almost always NULL. The grid rule exists to
keep BAR SEQUENCES consistent for RVOL and VWAP; a daily closing spot needs
only to be inside the session.

## M2 is now unblocked, and that is a decision rather than a step

Every input M2 lost is available again: ATM IV, IVS, and real deltas for the
O/S weighting, on 208 of 208 evaluated symbols. Pointing M2 at `iv_surface`
instead of the dead `option_premium_candles.iv` is a small change.

It is deliberately NOT made here. The cross-sectional IC study measured
`signed_flow` at **IC -0.0017, t = -0.50** over 43 sessions and ~98,000
observations -- no cross-sectional ordering power at any horizon tested. Reviving
the feed makes M2 computable; it does not make it predictive, and the ingredient
mix should be re-tested against the IC before the lane trades on it. The skew
finding above is a live candidate for why: two of the five ingredients were
close to the same number.

## The desk

`/strategies/vanguard`. The Market tab presents the same rows through three
lenses -- **Decision inputs**, **Positioning & OI**, **Price performance** --
with symbol, legs and conviction anchored in all three, and every column
sortable. GEX is a diverging colour scale on the percentile (teal = short
gamma, which M6 permits; amber = long gamma, which it blocks) rather than a
word. Buildup states are filled when fresh money is taking a side and hollow
when old money is leaving, because a buildup and an unwind are not the same
claim. Every joined input is drawn beside the age of the row it came from.

The per-symbol panel opens with a market snapshot anchored on the last SETTLED
session -- one date for every figure -- with any newer, price-pending OI row
reported separately rather than blended in.

## Open owner decisions

1. **The three sizing numbers do not agree.** Risking 0.75% of capital behind
   a stop 15% away needs 5.00% of capital in premium; the cap is 1.50%. So the
   premium cap binds, effective risk is 0.225%, and the -2% daily stand-down
   needs 8.9 stop-outs against a 3-position cap. Raise the premium cap to
   5.0%, restate risk-per-trade as 0.225% and scale the stops, or widen the
   stop to 50%. `sizing_coherence()` reports the arithmetic; a test pins it.
2. **The live cadence** (above).
3. **M4/M5 enter M6 with the sign the IC study says is wrong.** Inverting them
   is a strategy change, not a bug fix, and should be tested before it ships.

## Known-and-unfixed

- **`fo_security_ban` is displayed on the desk but NOT enforced by M7.** A
  banned name can still be sized. The UI says so rather than implying a
  control that does not exist.
- **M2's 25-delta skew has no delta tolerance band** -- `idxmin` takes the
  nearest row however far away it is, so on a thin chain "skew" is a different
  quantity per name per day. No min-OI/volume/spread filter either, and the
  source carries no bid/ask, so the IV is last-trade IV.
- **M2's O/S ingredient is unsigned** (sum of |delta| x volume over the whole
  chain) but enters the composite as a signed component, so "busy chain" reads
  bullish. `FLOW_MIN_INGREDIENTS` now stops it saturating a score alone; the
  ingredient itself still needs a call-minus-put split.
- **M2 computes its front expiry as `min(expiry)` with no roll rule** while
  M6's `resolve_instrument` does roll, so the feature and the trade can be on
  different series.
- **M5's `SPOT_CHECKS` are hardcoded named events** (SAIL 25-Aug, MCX 10-Aug,
  GLENMARK 24-Aug). The live path now passes `--no-spot-check` -- without it
  those dates silently widened every "3-day" pass to weeks -- but they should
  move to a fixture.
- **M9 fill/decision/outcome writes are not atomic** under autocommit, and
  `fill_pending_tickets` re-checks no concurrency/heat cap.
- **`db/apply.py` has no migration version tracking** and re-applies every
  file each run. Every migration is idempotent, so this is cost, not risk.
- **Pre-006 `tickets` rows hold PREMIUM in `sizing_risk_rupees`.** They are
  labelled `sizing_risk_basis = 'full_premium'` rather than rewritten; do not
  compare that column across the 006 boundary without reading the basis.
- **Off-grid rows already in `timing` are not deleted.** M5 no longer writes
  them, and every reader selects on the NSE grid, but the history stays -- it
  is a true record of what the lane did.

## The P1 acceptance gate that cannot be satisfied in one sitting

Section 6 of the spec requires "5 consecutive sessions with zero missed EOD
feeds" before P1 is exited. That is 5 calendar trading days of `make ingest`
running unattended — it cannot be produced synchronously. `ingest_log` is the
evidence trail. Wiring an unattended daily run is still the next concrete step
before this gate can start being satisfied for real.

## Reproduce

```
cd vanguard
make db-init          # idempotent
make sector-taxonomy  # load config/fno_universe_aug2026_series.csv
make ingest           # today's participant OI
make features          # sector20 / sector_rs / leadlag, from existing bars
make test              # offline unit tests

# Since migration 006:
python scripts/backfill_evaluations.py --start 2026-05-29 --end 2026-08-26
make ic ARGS="--lookback-days 120 --horizons 1,2,4 --write"
```

The desk is at `/strategies/vanguard`: Market (per-symbol collected inputs,
each with the age of the row it came from), Decision flow (the attrition
ribbon and the conviction distribution), Research (the IC study and M7's
coherence), plus Book / Attribution / Backtest / Pipeline.
