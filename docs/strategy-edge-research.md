# Frozen prospective strategy research, v1

This is a research experiment, not a profitability or deployment approval for
the production lanes. It adds six independent one-lot shadow books: a control
and challenger for each hypothesis below. No broker order endpoint or production
paper-book API is used. Existing positions, capital settings and risk flags are
not inputs to sizing and are not changed.

## Registered hypotheses

All rules and the universe are frozen in `backend/edge_research/frozen_v1.json`.
Numbers below are starting hypotheses, not parameters selected from historical
profit. Every source/config/cost-model revision needs a NEW archive directory.
The source fingerprint is checked on every process restart.

| Book | Control | Challenger | Exit |
| --- | --- | --- | --- |
| Directional: NIFTY, BANKNIFTY | Completed close outside a preceding 12-bar range | Two completed closes outside that same range, plus 30-minute displacement efficiency >= 0.35 | Two-bar structural invalidation, 2x underlying stop-distance target, 60 minutes |
| Auction: NIFTY, BANKNIFTY | Range excursion followed by a completed close back inside | Excursion must also have closed outside on the preceding bar | Excursion extreme invalidation, range midpoint target, 30 minutes |
| Commodity: CRUDEOIL, GOLD | Completed close outside a preceding 12-bar range | Two-close acceptance and 30-minute displacement efficiency >= 0.35 | Two-bar invalidation, 2.5x underlying stop-distance target, 90 minutes |

Each uses complete, consecutive 5-minute bars from one source and one instrument
per current session. The range excludes both recent bars. Range width must be
at most 4 ATR(14). Efficiency is absolute displacement divided by total travel
over six completed bars. It is a simple 30-minute gate, not the production
commodity classifier. There is no fitted score floor, adaptive learning, GEX
direction claim, or sentiment substitution in this experiment.

Auction v1 deliberately tests a **failed rolling-range auction**. It does not
reproduce the production market-profile/order-flow stack. Aggressor trade flow
is unavailable and is explicitly recorded as such. Quote depth is archived but
is not mislabeled as trade aggressor/CVD history. Genuine prior-value-area and
order-flow challengers require a later registered version and sufficient
prospective evidence.

NSE uses the nearest non-expiry-day expiry and nearest catalog strike per CE/PE.
The next nearby listed contracts are archived as alternatives; they are not
searched retrospectively to change v1 fills. A bearish NSE thesis buys a PE.
MCX uses the next unexpired exact future from the Upstox instrument master;
bearish futures sell the bid. No continuous-contract candle substitution is
allowed for a held or selected future. Missing exact history blocks proposals.
MCX price P&L uses the instrument master's `qty_multiplier`, separately from
its raw `lot_size`. Depth quantities are modeled as exchange contract counts
(one contract per shadow entry), with that basis explicitly archived. This unit
mapping requires broker/exchange reconciliation before promotion; a missing
multiplier blocks execution. The existing production commodity multipliers are
not changed. Reference: [Upstox instrument metadata](https://upstox.com/developer/api-documentation/instrument-search/).

## Timing and execution

- A decision is first available when its inputs have actually been collected.
  Bar end time and receipt availability time are stored separately.
- A proposal can fill only on a **subsequent** exact-contract quote with both
  exchange quote time and receipt time later than the decision. The quote must
  be at most 15 seconds old, have sufficient top-of-book size for one exchange
  lot, and have a non-crossed positive spread. Maximum relative spread is 2%
  for options and 0.1% for futures. Missing quote time is never replaced by LTP
  time or the collector's clock.
- Proposals expire after 60 seconds. They are not backfilled at a candle close.
  Only one proposal/position per symbol per book is allowed; a close cannot
  immediately reopen on the same decision bar. State survives restarts.
- Entries buy the ask or sell the bid; exits use the opposite side. This is a
  top-of-book simulation, not evidence of an actual executable market fill.
- Structural stops and targets trigger on **completed underlying closes**.
  Intrabar stop protection, queue position and actual broker latency are not
  simulated. A pending exit waits for a valid later exact-contract quote; it
  cannot disappear at a stale mark. Time stops still become pending during
  candle gaps. Session exits are requested in the final minute of the configured
  exchange session. A fresh exit is allowed on expiry day for an already held
  contract, while new expiry-day entries are excluded.
- Every close applies the shared approximate statutory cost model. The stress
  result doubles those charges and adds another full displayed spread on EACH
  leg. This is a deliberately adverse sensitivity, not measured slippage.
- Greeks are sampled from Upstox option chains no more than once a minute.
  Receipt-time availability is recorded; missing exchange timestamps stay null.
  These observations do not reconstruct old Greeks history.

## Evidence and decision protocol

The separate SQLite archive contains the frozen manifest, input frames, quotes,
all evaluated decisions/reasons, pending/open states and closed shadow trades.
Inputs for a new bar or coverage/source change are stored in full; repeated
polls retain quotes and heartbeats without duplicating entire candle histories.
Raw broker responses, credentials and auth headers are never archived.

The first 40 valid observed sessions are development, shown in consecutive
20-session diagnostic blocks. These are chronological evaluations of frozen
rules, not a trained walk-forward optimizer. The next 20 observed sessions are
held out and their P&L is hidden by the default report. An explicit unseal only
reveals a lane's completed 20-session holdout after its final calendar day ends.
Trades spanning evaluation boundaries are excluded from that split's results.
Partial development blocks are labeled incomplete. A valid observed session
is not a claim of complete intraday coverage; inspect source errors and gaps.

Reports include win rate, average win/loss, net expectancy, profit factor,
closed-trade drawdown, per-symbol results, stressed P&L, concentration, and
leave-one-session-out sensitivity. Uncertainty resamples entire trading
sessions, never treats correlated simultaneous tickets as independent. Empty
samples remain null. Bootstrap intervals are descriptive and **do not** correct
for multiple strategy trials. The manifest registers all six books; new trials
must be counted in the later research review.

At least 200 closed trades and 60 distinct sessions with closes are preliminary
sample floors, not proof of edge. Promotion additionally requires positive
untouched-holdout expectancy, acceptable uncertainty, stressed costs, data
coverage, outlier sensitivity, drawdown, and prospective operational validation.
No code automatically authorizes promotion or changes production capital.
Raw rupees across different futures/option lot sizes are not comparable risk
budgets; inspect individual symbols before comparing controls and challengers.

## Run and inspect

From a checkout with the existing TradeBot Docker services available:

```sh
python3 tools/run_strategy_edge.py --output /absolute/separate/run-v1
python3 tools/run_strategy_edge.py --output /absolute/separate/run-v1 --watch
```

The first command is a one-shot smoke capture. Use different output directories
for smoke runs and the final frozen run. The second starts
`nomadcurie_edge_research_v1` with bounded CPU/memory, rotating logs, read-only
code/credentials, a writable research directory and a liveness health check.
It reuses the existing dependency image/network, but overrides the entrypoint:
the production server, migrations and strategy runners are never started.
Each PostgreSQL connection is server-default read-only with a bounded timeout
and a pool of one; the collector checks `transaction_read_only` before reads.
Broker access is restricted to full quotes and option-chain GETs, using the
shared low-priority rate budget. MCX instrument metadata is downloaded by GET.

```sh
docker exec nomadcurie_edge_research_v1 python -m edge_research.cli report --archive /research/edge.sqlite3
docker exec nomadcurie_edge_research_v1 python -m edge_research.cli report --archive /research/edge.sqlite3 --unseal-holdout
docker stop nomadcurie_edge_research_v1
```

Health proves that the capture loop advances, not that data is usable or that a
strategy is profitable. Check `capture_error`, source coverage and rejection
reasons. A closed NSE session is normal. Old or incomplete MCX candles stay
unavailable; the collector never repairs production history or invents fills.

## Measurement fixes accompanying this experiment

- Gann option fees use CE/PE rather than the generic OPTION label.
- Directional and Auction stores receive effective service slippage settings,
  including explicit zero overrides.
- S1 entry and exit indicator series use timestamp-filtered completed bars;
  unbucketed snapshot prices cannot act as 30-minute history. Snapshot-derived
  NSE 30-minute bars use the 09:15 session origin.
- Commodity drawdown documentation must reflect `SIGNAL_VALIDATION_UNCAPPED`:
  the entry drawdown gate can be bypassed. This change does not alter that owner
  setting, capital caps or protective exit rules.
- Unit tests default to a deliberately unreachable TEST database. Integration
  tests can explicitly select a separate database through
  `TRADEBOT_TEST_DATABASE_URL`; the application localhost database is not inherited.

These source fixes are reviewable separately from starting the isolated
collector. Deploying them into the application's divergent mounted worktree
requires a focused integration and source/runtime parity check.
