# Full-Stack Audit — Data Pipeline, Strategy Lanes, and Their Interactions
**Date:** 2026-07-12 (Sunday, pre "first full test" session of Monday 2026-07-13)

> **IMPLEMENTATION STATUS (2026-07-12, same day):** P0-1, P0-2, P1-A/B/C, P1-D, P1-E, P1-F, P1-G, P1-H (minimal), P1-I (caps fed), P1-J (fail-closed), P1-K, P1-P, P1-Q and several P2s (exit-mark bar-time stamp, macd_refined `fresh` guard, CBE atomic write, MCX quote limiter, expiry-weekday swap, learning-block threshold) are FIXED in the working tree with tests. **P2-2 was REFUTED**: live Upstox MCX expiry epochs verified as 23:59:59 IST (same UTC date) — the parse was already correct. Still open: P1-L (directional fill costs), P1-M cluster (live RiskManager — hard gate before live activation), P1-N (gross-vs-net honesty incl. auction costs), P1-O journal-replay, directional session_progress TZ + view tagging, commodity sync persists, open-window priority, chain-builder poll timeout, WS-reconnect depth-callback. Details in the fix log at the end of `full-stack-audit-2026-07-12` memory and the session transcript.
**Method:** six parallel deep-read audits (broker pipeline; watchlist/MI orchestration; S1/MACD lanes; directional options; commodity+auction MP/OF; execution/portfolio/risk core). Every finding was verified against current source with file:line evidence — no findings were carried from memory without re-verification. All paths relative to `backend/` unless noted.

---

## Executive summary

The July fix-set (limiter chokepoints, watchlist `wait_for(75s)` guards, premium time-budget, gap-fill cooldown-before-work, HTF gate, protective directional exits, S1 mark-staleness gate) is **genuinely in place and well built** — every claimed fix was located in code. But the audit found **2 P0s and ~17 P1s**, clustering into seven systemic themes:

1. **The un-timed-broker-await class was fixed at the crash site, not as a class.** The exact defect that caused the 07-08/09/10 zero-signal days survives in the two MI stages *before* the watchlist write (index chain refresh, expiry discovery) and in two more loops (S2 profile resolution, chain builder).
2. **One unsupervised worker can silently kill all tick/candle persistence** (`live_candle_store`) — a P0 of the same "silent zero-day" family as the Telegram bug.
3. **The Telegram zero-notification mystery is solved**: the S1 sender never checks the HTTP response; a 401 produces literally nothing, not even a log line.
4. **Paper books are not yet "honest books."** Gross-vs-net PnL divergence is systemic (S1 events/learning gross, commodity durable ledger gross, auction lane has *zero* costs, directional live fills carry no spread/slippage while its own backtest does). The "re-measure on honest books" goal is currently unachievable on 3 of 4 lanes.
5. **Fail-open on *missing* data coexists with fail-closed on *stale* data** — directional silently reverts to the measured-PF-0.2 momentum entry when the positioning row is absent; `latest_session` readiness trades yesterday's prices during a live outage; OF coverage gate passes on short history; ban-list parser can return zero bans.
6. **Dead risk machinery**: directional loss caps never receive realized PnL and the lane has no kill-switch; the live `RiskManager` feedback loop is unwired (position counter only increments → after 5 lifetime orders it rejects everything **including exits**); `broker_circuit.allow()` has zero callers; `try_fill`/brackets/trailing stops are dead code.
7. **Rollover/expiry-calendar correctness** — the commodity roll books calendar-spread basis as trading PnL and has no monotonic-expiry guard (flap-churn possible), the Upstox expiry epoch is parsed in the wrong timezone, and the auction front-month calendar has NSE/BSE expiry weekdays **inverted** (post-Sept-2025 SEBI regime: NSE=Tue, BSE=Thu).

**Time-critical for Monday 2026-07-13:** (a) the MI pre-build un-timed awaits are the same freeze class the Monday test is meant to validate against; (b) the ZINCMINI-class MCX roll window opens ~Jul-13, hitting both P1 rollover bugs.

---

## P0 findings

### P0-1 — `live_candle_store` worker dies permanently on the first DB error; tick + candle persistence stops silently
`market_data/live_candle_store.py:170-189` — `_worker` has no exception guard other than `CancelledError`. One transient Postgres failure kills the task; `start()` runs once at boot (`main.py:142`), nothing monitors or restarts it. Afterward `on_tick` keeps enqueuing into an unbounded queue (RSS growth), `market_ticks` inserts stop (auction OF tape freezes), live spot/option candles stop. The WS feed itself stays healthy so `data_quality_agent` sees nothing. Worse, `_persist_ticks` clears the batch **before** the INSERT (`:260`) and `_persist_candles` clears `dirty` before commit (`:296`) — the failing batch is also lost. `quote_bus._flush_loop` (`quote_bus.py:131-152`) does this correctly; this worker is the outlier.
**Fix:** per-iteration try/except + backoff, re-queue failed batches, a heartbeat metric, and a supervisor restart path.

### P0-2 — Telegram 401 invisibility: the mechanism of the three silent zero-notification days
Two independent senders:
- `paper_engine/strategy_agent.py:4724-4741` (`_send_telegram_text`, used by every S1 entry/exit alert via `strategy_agent_entries.py:1243-1249` / `strategy_agent_exits.py:520-523`) **posts and discards the response** — no status check, so a 401 is completely silent. It is also gated on `TELEGRAM_REPORTS_ENABLED` (:4728), so muting periodic reports mutes trade alerts too.
- `notifications/telegram_agent.py:120-128` logs HTTP ≥400 but returns the same `False` as dedupe/rate-limit/missing-creds — callers can't distinguish "suppressed" from "auth-dead". No retry, counter, audit event, or health state; `core/metrics.py` has no notification metric. Only manual endpoints (`api/routers/auth.py:1639-1668`) would catch it.
**Fix:** delete the bespoke S1 sender, route through `telegram_agent.send()`; track `last_success_at`/`consecutive_failures` on the singleton; on 401/403 emit one `telegram_auth_failed` audit event per day (re-entrancy-guarded); add `nomad_telegram_sends_total{result}` metric; surface health in the pre-open token-readiness sweep so a dead bot token is a pre-open actionable fact like a dead broker token.

---

## P1 findings

### Orchestration (the remaining session-freeze surface)

**P1-A — `refresh_index_option_chains`: un-timed serial broker loop + cooldown-registered-at-end.**
`market_data/market_intelligence_runtime.py:565-580` serially awaits `_refresh_cached_index_option_chain` (`:1435` — bare `adapter.get_option_chain`, no `wait_for`, up to 2 brokers × ~15 (symbol, expiry) pairs). The HTTP timeout does not bound it: each retry first blocks on an **unbounded** `LIMITER.acquire()` (worst case ~30 min on the Upstox 1800/30-min window). `_last_chain_refresh_at` is set only at the end (`:582`) — a watchdog kill never enters cooldown, so every subsequent cycle re-runs the full refresh: the identical register-cooldown-after-work bug already fixed for gap-fill (`:329-338`) was not applied here. Lands after the watchlist write but before premium top-up → the 07-07 premium-starvation class returns via a different door.

**P1-B — Expiry discovery is un-timed and one symbol's failure aborts the watchlist step.**
`market_intelligence_runtime.py:389` awaits `get_expiries(None, live_refresh=True)` with no try/except or `wait_for`, **before** the S1-critical watchlist write. Inside, `atm_watchlist.py:741` gathers ~9 representative symbols **without** `return_exceptions=True`; the DB read at `:1665` (`_load_persisted_expiries_for_symbol`, `:1753-1783`) has no try/except; the `get_option_contracts` awaits (`:1672`, `:1687`) carry the same unbounded-limiter exposure. One transient DB error → the entire watchlist step fails that cycle; one hung acquire → 300s watchdog **before** the watchlist write — the zero-signal class, one stage earlier than the fixed location. Same pattern at `atm_watchlist.py:836` (`get_watchlist` → `get_expiries` unguarded) and the forced network session validations at `:917/:919/:2749-2763` sit outside the 75s row bounds.

**P1-C — Build lock TTL (120s) ≪ build duration → overlapping full-universe builds.**
`atm_watchlist.py:38` (`DEFAULT_BUILD_LOCK_TTL=120`), set once (`:1216-1218`/`:1253-1255`), never re-armed inside `_bg_build_and_cache` (`:1060-1137`). A healthy ~200-stock build takes many minutes (0.5s inter-row sleep alone); MI force-rebuilds every 15 min after deleting cache keys (`market_intelligence_runtime.py:464-478`) → a second concurrent BG build starts over the unbuilt tail. Duplicated broker load at the worst time, plus a served-payload race (each build overwrites the cache wholesale from its private `prior` dict — fresh rows can transiently vanish).

### Broker pipeline (data integrity)

**P1-D — Option-chain snapshot IV unit flips with the serving broker; greeks enrichment then corrupts candle IV 100×.**
Fyers chains carry IV as a **fraction** (`brokers/fyers.py:796-831`); Upstox as a **percent** (`brokers/upstox.py:561`). `option_chain.py:218-252` persists iv raw with no source column; `greeks_enrichment.py:137-143` does `SET iv = matched.iv / 100.0` unconditionally. `OptionChainService._broker` is pinned on first acquisition (`option_chain.py:49-67`) using the circuit-reordered route (`source_policy.py:86-91`) — a session that starts during an Upstox 429-storm pins Fyers all day → every snapshot that day is fraction-unit → enrichment writes iv ≈ 0.0014 instead of 0.14, permanently (NULL-only fill), and the Redis `oc:` payload's `atm_iv` unit flips for every consumer. The post-07-07 failover machinery makes this a realistic path.
**Fix:** normalize the unit at persist time and record `iv_source`; make enrichment source-aware.

**P1-E — Mixed volume semantics in `option_premium_candles`.**
`live_candle_store.py:215,226` stores tick `volume` (Fyers `vol_traded_today` / Upstox `vtt` — session-**cumulative**) and the upsert (`:405-424`) overwrites per-bar broker rows at the same key. The ~10 WS-subscribed legs (held positions + ATM index legs — the contracts that actually trade) get late-session bars inflated ~2 orders of magnitude. `chain_candle_builder.py:73-99` does the cumulative→delta conversion correctly; this path doesn't. Read-path dedupe (`option_history.py:610-617`) only mitigates when a Fyers-keyed row exists.

**P1-F — Unpruned full-hypertable scan every 45s during market hours** (the 07-08 PG lock/OOM class).
`option_subscription_manager.py:620-625` — `WHERE timezone(...)::date = (SELECT MAX(timezone(...)::date) FROM atm_option_watchlist_snapshots)` — non-sargable filter + STABLE MAX subquery over the whole 1-day-chunked hypertable, executed every 45s by `run_held_position_subscription_loop` (`:654-677`). The repo's own sargable-band fix pattern (`strategy_agent_entries.py:397-398`) was not applied. Related un-banded restore-path queries at `strategy_agent.py:1772,1876,1975` (colder). Also `option_history.py:782-816` (`_load_snapshot_candles`) has no time floor and runs inside the concurrency-6 top-up (P2).

### S1 / MACD

**P1-G — Learning gate is a self-locking ratchet that silently re-caps the "uncapped" S1 test.**
`strategy_learning.py:141-146`: block when `closed ≥ 2` (`STRATEGY_LEARNING_MIN_TRADES=2`, `core/config.py:220`) AND `win_rate < 0.25` AND `expectancy < 0`. Two losses on a `(underlying, option_type, reason)` key — trivially likely for a −25%-stop long-premium lane — permanently blocks it; blocked keys can never book a trade to clear themselves (only the 120-day lookback ages them out). Dropped candidates (`strategy_agent_entries.py:1033`) are **not tallied** in `blocked_reasons` — invisible in zero-trade forensics. Learning also trains on **gross** pre-cost PnL (see P1-N).
**Fix:** require ≥6 closed for the *block* decision (or TTL/decay the block), tally `learning_blocked`, feed learning net PnL.

**P1-H — `ensure_recovered_state` can wipe the S1 in-memory trade ledger and resurrect zombie DB positions.**
`strategy_agent.py:1712-1717` unconditionally clears positions, events, portfolio positions, **and trade history** when recovery fires; trigger (`:1120-1132`) compares `MAX(created_at)` of qty>0 `OPT:%` rows (any day) vs newest in-memory day. Zombie qty>0 rows arise from swallowed `_persist_position` failures (`:4346-4347`; conflict-update never touches `created_at`, `:4329-4334`). A stale row from a DB hiccup at close → days later a restart rebuilds the book **from zombies** (as `phase1`/`macd_zero_cross`, `:1795-1813`), wipes lifetime history, persists the wiped state (`:2606`), and overwrites the cumulative entries counter (`:1845`). Inverse crash-window variant: entry persisted to DB but not to state JSON → orphan DB row that is never marked/exited and seeds a future resurrection.
**Fix:** merge-don't-clear; reconcile qty>0 rows at close-time with retry instead of resurrecting by `created_at::date`.

### Directional options

**P1-I — Daily/weekly loss caps are dead code; the lane has no kill-switch.**
`risk.py:41-100` implements caps but `daily_realized`/`weekly_realized` default 0.0 and no live caller passes them (`service.py:412-418`, `:692-698`); the backtester does (`backtest.py:180-186`) — backtest has protection the live lane lacks. No kill-switch exists in the lane (`macd_refined/risk.py:68` and auction's governor have one; `paper_bootstrap` switches cover S1/commodity only). Combined with no re-entry cooldown after a stop (`paper.py:446-466` checks only *currently open* positions), a grinding session can stop-out → re-enter → stop-out every 60s cycle with nothing halting it.

**P1-J — Silent fail-open to the measured-PF-0.2 legacy momentum entry.**
`service.py:513-517`: any exception from `positioning_feed.latest()` **or a simply-absent row** yields `positioning=None` → `signals.py:52` → legacy momentum path (`signals.py:84-101`, floor 0.001, no regime gate). Stale rows correctly fail closed (`positioning_feed.py:230-266`); missing rows fail open to the worst-measured strategy in the lane. SENSEX (thin 30-min premium coverage → `compute_and_store` writes nothing, `positioning_feed.py:123-124`) likely trades legacy momentum every cycle while NIFTY/BANKNIFTY trade positional — a silent mixed A/B, and journal rows carry no `positional` flag to tell them apart (only the position dict, `paper.py:522`).

**P1-K — `latest_session` readiness bypasses both staleness gates during live-session outages.**
`service.py:1052-1068`: `execution_ready` is true when `using_latest_session` regardless of watchlist/spot age; the MI runtime sets that mode whenever today's watchlist isn't ready but yesterday's rows exist (`market_intelligence_runtime.py:112`), independent of market-open state. Contract snapshots have no lower time bound (`data.py:194-216`). A full-session watchlist outage (exactly the 07-08/09/10 event class) → lane opens paper positions "filled" at **yesterday 15:29 LTPs** against today's live spot — fabricated PnL polluting the paper record and the policy's reward signal.
**Fix:** `using_latest_session` must not satisfy `execution_ready` while the exchange is open.

**P1-L — Execution realism: fills at raw possibly-minutes-old LTP, zero spread/slippage.**
Entry = watchlist snapshot `ltp` unadjusted (`paper.py:259,536-537`); exit = mark LTP unadjusted (`:575-580,593`). The configured `entry_slippage_pct=0.0075`/`exit_slippage_pct=0.006` and the candidate's `spread_pct` are applied **only in the backtester** (`backtest.py:283-289`). Quote staleness tolerated to 600s (`config.py:161`) + 30s snapshot cache + 60s runner → entries recorded at stale last-trade prices in the direction of momentum (systematically favorable). No real bid/ask exists anywhere — `spread_pct` is synthetic (`selector.py:653-659`). Each round trip is flattered ~1.5-2.5% of premium vs the lane's own backtest cost model, on a lane whose open question is precisely "is the edge above costs."

### Live engine (must land before any live activation)

**P1-M — Live `RiskManager` feedback loop unwired; bricks itself after 5 orders, including exits.**
Zero production callers of `on_trade_close`/`update_equity`/`update_dead_zone` → daily-loss halt, consecutive-stop pause, drawdown sizing, dead-zone mode all dead (`risk_manager.py:210-228,111-135`). `on_position_opened` fires on every placement (`order_manager.py:149`) and nothing decrements; `reset_daily` doesn't reset the counter → after 5 lifetime orders every order is rejected — **including exits** (`:168-169`). Also P1: risk checks are vacuous for MARKET orders — `order_manager.py:118` `price = order_req.price or 0` skips max-loss and zeroes concentration checks; `total_capital` defaults ₹10L (`risk_manager.py:147`). Kill switch is wiped by a paper→live mode toggle (`api/routers/trading.py:283` builds a fresh manager) and `reset_kill_switch` has no callers; the `/api/trading/kill-switch` endpoint halts only NSE paper + live — no global halt across commodity/CBE/gann/directional/auction books.

**P1-N (cluster) — Gross-vs-net PnL divergence across every operator-facing surface.**
- S1: events/Telegram/`agent_signals`/learning get gross unslipped PnL (`strategy_agent_exits.py:485,497-523,580-600`) while the portfolio books net-of-charges off slipped fills (`portfolio.py:158-167`).
- Commodity: durable `paper_trade_book` and audit events record gross (`commodity_strategy_agent.py:3418,3474-3475`) vs net book; the ledger that survives resets can never reconcile to the book. S1 writes **nothing** to the durable trade book (sole caller is the commodity agent).
- Auction: zero costs and zero spread in the live paper path (`auction_intelligence/paper/book.py:607-608`; only offline Gate B applies costs) — with flip/dedupe/expiry churn this flatters exactly the trade profile the lane produces.

### CBE

**P1-O — Torn write silently erases the entire CBE book.**
`cbe_scanner/paper.py:1008-1010` writes `paper_positions.json` non-atomically; `_load_positions` (:989-1002) catches `JSONDecodeError` and returns a **fresh empty book** — no error, no rebuild-from-journal despite `paper_journal.jsonl` having every event. File-only store (no Postgres copy). Kill mid-write → next scan starts from ₹0 history and re-opens the whole watchlist on a fresh capital base.
**Fix:** tmp+rename (pattern already correct in `gann_tp_delta/agent.py:809-814` and `directional_options/policy.py:584-586`) + journal-replay recovery.

### Commodity rollover (roll window opens ~Mon 2026-07-13)

**P1-P — Contract roll books the calendar-spread basis as trading PnL.**
`commodity_strategy_agent.py:3273-3362` (uncommitted): the old contract is closed at the **new** contract's price (`:3281` → `:3306-3312`); the old contract's own LTP is never fetched. In contango a BUY books phantom profit ≈ full basis × lot (ZINCMINI ~₹3.5k/lot; NATURALGAS seasonal spreads far worse), signed by side × curve shape → systematically distorts per-setup expectancy. The roll also resets `entered_at`/`peak_price` (`:3354-3357`). The new test checks stop/target geometry only — it cannot see this.
**Fix:** exit the old leg at the old contract's price (it still trades — roll fires a session early); book the basis into a separate `rollover_basis` field. Add a closed-leg PnL assertion to `tests/test_commodity_contract_rollover.py`.

**P1-Q — Backward-roll churn: no monotonic-expiry guard.**
`_reconcile_futures_rollovers` (`:3259-3271`) rolls on any symbol mismatch; nothing checks the target expiry is **later**. Enablers: `_load_mcx_instruments` (`upstox_commodity.py:194-212`) returns `[]` on fetch failure even when a stale cache exists → resolver `None` → fallback to the **configured (older)** symbol (`:1411`); the old contract still trades during the 1-2 day roll window. An instruments-CDN flap mid-day → roll JUL→JUN→JUL: each flap is a full phantom round trip (slippage ×2 + charges + basis flip).
**Fix:** serve the stale instrument cache on failure; refuse rolls into expiry ≤ current (expiry available in `active_row["contract_expiry"]`).

---

## P2 findings (grouped)

### Orchestration / scheduling
- **Watchdog vs sum-of-inner-budgets mismatch** in the MI runner: worst-case inner phases (seed 75s + gap-fill 120s + chains unbounded + premium 98s + un-timed learning/sector/macro) exceed the 300s ceiling; no shared cycle deadline (`market_intelligence_runtime.py`, `core/config.py:209`). Kill mid-premium redoes the same slice next cycle.
- **Open-window priority fix not built** (the chain-builder blocker): only two call sites set a broker priority (chain builder=5 OFF, premium top-up BULK=10); the watchlist build and everything else run at `PRIORITY_NORMAL=0` — at 09:15 the build shares FIFO pro-rata with macd_refined's ~434-call Fyers chain sweep (`macd_refined/live.py:375-406`, ≥2.3 min of the 190/min budget). Recommended: `broker_priority(PRIORITY_HIGH)` around the MI watchlist build; demote the macd_refined sweep. Keeping the chain builder OFF remains correct posture.
- **`get_watchlist_for_strategy`**: un-timed serial per-symbol resolver + forced network session validation on a `live_refresh=False` call (`atm_watchlist.py:2930,2933-2958`; S2 caller contains exceptions, not hangs).
- **Chain builder `poll_once`**: un-timed chain fetch in a ~227-name serial loop, no loop watchdog — a hang wedges the builder forever with `_running=True` (`chain_candle_builder.py:229-246,342-366`). MI's freshness-gated demotion contains the blast radius. Must fix before the flag ever turns on.
- **Cursors/session-registry process-local** (`market_intelligence_runtime.py:192,199,207`) — restart resets fairness to head-of-list; DB-restored rows carry no `extended_strikes` (`atm_watchlist.py:2051-2065`) so the extended top-up target set collapses after a mid-session restart.

### Data pipeline
- **Watchdog-forced WS reconnect** rebuilds the Fyers socket **without** depth + reconnect callbacks (`data_router.py:729-748` vs correct `:170-175`; `fyers.py:515` nulls the depth callback) — re-exposure to the 07-08 blind-tape mechanism if the periodic resync loop isn't healthy.
- **New Upstox raw-httpx limiter bypass (post-dates the 07-07 fix):** `upstox_commodity.py:357-368` — bare client, no limiter/429-retry/circuit, polled every **12s** while MCX is open with positions on.
- **`broker_circuit` half-decorative:** `allow()`/HALF_OPEN has zero callers; the only consumer is chain route-order at acquisition time, and `OptionChainService` pins its broker (`option_chain.py:49-67`) → mid-session token death = errors every 30s, no failover.
- **Trading/account REST endpoints bypass limiters** (fyers/upstox profile/positions/orders/funds — bare clients, unguarded `r.json()` on several), sharing the same broker budgets unmetered.

### S1 / MACD
- **No MACD warmup floor on the S1 snapshot source**: `latest_macd_rsi` computes from 20 closes (`analytics/technicals.py:158-165`); signal line meaningful only from bar 34; `MACD_MIN_BARS=35` enforced on S2/replay/macd_refined/15m paths but **not** at S1 snapshot consumption (`strategy_agent_entries.py:638-694`) → warmup-artifact entries on fresh contracts; sliding 80-bar window makes prev/curr MACD different-vintage values.
- **Intrabar entries, no symmetric unwind**: entry on the synthetic forming bar (`atm_watchlist.py:1953-1966`); if the cross fails at bar close, the completed series never shows a down-cross → no momentum exit; only hard stop / window_end. Same class worse on the 15m re-entry path (in-progress bucket, `strategy_agent_entries.py:276-280`).
- **Exit-mark fallback forges freshness**: candle-close fallback stamps `price_updated_at = now()` (`strategy_agent_exits.py:131-140`) → staleness gate bypassed exactly in degraded sessions (empty quote map on mid-loop DB exception, `:415-416`); the forged timestamp then defeats the anti-regression check (`:314-316`). Fix: stamp from the candle's own bar time.
- **Dark S2 runtime**: S2 removed from agents but persisted S2 positions still deserialize and count in equity; `_manage_strategy2_exits` never runs → immortal orphans (`strategy_agent.py:681-692,843-902,2854`).
- **macd_refined**: `_marks_for_open` computes `fresh` but `_manage` never reads it — hard stop/targets/trailing fire on frozen parquet marks (`macd_refined/live.py:743-761`, `paper.py:353-440`); dedupe consumes a cross before gates decide (`live.py:641-645`) — transient infra noise permanently eats the entry.

### Directional
- Stops evaluate **and fill** on marks of unbounded age; missing marks silently freeze `ret` at the last value (no stop until feed recovery) — no `mark_time` check anywhere (`paper.py:314-324,575-580`; `data.py:352-427` has no time filter).
- `session_progress` computed against IST constants on naive-UTC frames → ~0 all day live: the late-session expiry blocker (`ai_model.py:115-119`) **can never fire** (will buy an expiring weekly at 15:25 on expiry day); timing penalties skewed (`features.py:23-25,77-81`).
- `rv_percentile` lookahead persists in the backtest/workspace path (whole-frame min-max, `features.py:158-163`; live last-row is causal); `workspace()` `lru_cache`d forever (`service.py:113`).
- Positional confirmation deployed on NIFTY + SENSEX where research measured no/inverted edge (BANKNIFTY-specific per 2026-06-28 research); positional entries priced with intraday sleeve math (15-45 min horizon for a multi-day hold, `signals.py:106-132`, `selector.py:265-267`).

### Commodity / auction
- **Upstox expiry epoch parsed UTC** (`upstox_commodity.py:46-51`) — IST-midnight epochs yield expiry−1 (roll a session earlier than documented; widens the backward-roll window); month-code edge for 1st-of-month expiries → wrong Fyers symbol. Test fixtures use UTC-midnight so CI can't see it — verify one live master row.
- **Auction front-month expiry weekdays inverted** (`data/index_futures_backfill.py:140-148`: NIFTY/BNF=Thu, SENSEX=Tue; current regime is the opposite per `atm_watchlist.py:43-45`) — drives the new `auction_front_month_book_symbols()` OF subscriptions: ~2 sessions/month subscribing an expired future → silent degrade to `bar_inference`, the exact pre-fix behavior. Tests only probe dates where the conventions agree.
- **Commodity loss caps OFF** (`COMMODITY_LOSS_CAPS_ENABLED=False`, `core/config.py:371`, "TEMP for infra testing") — contradicts remembered risk posture; re-enable or record the decision.
- **Rolls execute while MCX is closed at stale marks** (`:2732`), stamping cooldowns off-session.
- `previous_day`/naked-POC/HTF daily-fallback not roll-clipped (old-contract levels the session after a roll, `commodity_profile_store.py:388-398`); roll-gap segmentation false-positives on limit-move days (clips HTF composites when context matters most, `:336-358`); stale-"ready" watchlist row can open at morning price with yesterday's ATR/stops (`:2583-2633`, no bar-time freshness guard on `_open_new_futures_positions`).
- S2 index MP+OF likely permanently `of_degraded` if index spot rows carry volume=0 (coverage <0.70 blocks all four triggers → silent MACD fallback despite the flag; verify) (`strategy2_mp_of.py:567+`, `commodity_mp_signal.py:1077-1141`).

### Execution / durability
- **Instant same-mark fills everywhere; `try_fill`/brackets dead code** — manual paper LIMIT/SL orders sit OPEN forever (`order_book.py:101-195`, `api/routers/trading.py:345-359`); your own adversarial research showed 1-bar fill lag flips the directional lane net-negative — the live paper books carry that optimism.
- **Fill→persist crash window** (state blob persisted once per scan in `finally`; per-fill DB rows survive → phantom reconciliation breaks) (`strategy_agent.py:2606`).
- **Non-atomic writes** on S1 state file, commodity config, recorder snapshot, calendar, CBE book (`strategy_agent_state.py:157`, `commodity_strategy_agent.py:477`, `paper_trade_recorder.py:167`, `trading_calendar.py:211`).
- **Commodity `_persist_state` is a blocking sync psycopg2 call on the event loop** at ~18 sites, serializing an unbounded equity-curve/trade-history payload every scan (`commodity_strategy_agent.py:1280,2085,2877,2987`; no trim in `base_strategy_agent.py:181-185`) — the loop-wedge + WAL-pressure class already paid for. S1 got this right (`_apersist_state` → `to_thread`).
- **Shared runtime-state pool `maxconn=2`** — pool exhaustion silently drops persists (warning + return None, `core/runtime_state.py:62,332-336`).
- Cross-lane couplings are the shared chokepoints (global limiters, class-level `Semaphore(2)` on chains, the maxconn=2 pool, single loop + sync persists) — **no shared cash between lanes** (verified).

---

## P3 (selected; full lists in the per-agent reports)
S1: flip_bonus slice off-by-N once re-capped; `prev==0` cross-boundary parity drift between replay and live; zero-cash restore falls back to full initial capital; `MACD_STRATEGY_UNCAPPED_CAPITAL` applied in shared `_open_position` (S2 would inherit if re-enabled); DEFAULT_LOT_SIZE=1 fallback trades micro-positions; indicators cache keyed on last-bar-time only (stale MACD after mid-history backfill). Directional: dead `trail_giveback_pct`/`DIRECTIONAL_POSITIONAL_STOP_PCT` knobs; static equity anchor for sizing (risk doesn't shrink in drawdown); `mark_time` forged on chain-cache overlay; partial-bar signal evaluation; DTE guard integer-compare fires only on expiry day. Pipeline: `oi_change` = full OI when prev missing; permanent metadata negative-cache; receive-time tick stamps; Upstox 200-non-JSON → silent `{}`. Portfolio: `day_pnl` includes lifetime open MTM on positional books; UTC `date.today()` Sharpe window; recorder `unrealized_open` last-position-only; charges dropped from restored trade history; partial-close brokerage double-billed; opposing-close qty overrun; no zero-premium guard in `_fill_market`. Commodity/auction: OF coverage fails open <15 bars; `/paper-proposal` body path bypasses the session guard; headerless ban-list parse can fail open to zero bans; MCX 23:55 winter close unmodeled; process-local OF subscription state; `market_profile.py` global 0.5 tick (SENSEX noise POC); post-roll pressure-gate suppression. Orchestration: seed timeouts swallowed silently; seed 75s budget includes semaphore queue time; one poison symbol keeps the build incomplete forever (respawn ~every minute); dead `skipped` counter; weekend live_refresh purges the board against a closed market.

---

## Verified good (prior fixes confirmed in current code)
- Upstox/Fyers `_get_data_json` chokepoints (limiter + Retry-After 429 backoff + circuit recording); fair-share aging limiter with 5s wakeup backstop.
- Watchlist BG 75s row bound + both seed gathers `wait_for`+`return_exceptions`; per-row failure isolation; single-side chain guard.
- Gap-fill: cooldown-before-work, outer `wait_for(120s)`, S1 watchlist write ordered first.
- Premium top-up: budget checked before each batch (now 90s/180s cooldown — deliberate retune), 8s per-call `wait_for`, concurrency 6, dual fairness cursors (no off-by-one), coverage-gated chain-builder demotion failing safe.
- Chain builder OFF (config default + .env + main.py gate).
- Supervisor: 300s default watchdog with per-lane overrides, loop-crash auto-restart bounded, dispatch-only scheduler.
- `load_candles`: 180d floor, cross-broker DISTINCT-ON dedupe, true-instant tz canonicalization, RTH filter, TTL-bounded gap backfill. Fyers WS resubscribe-on-reconnect + required-feed watchdog + teardown kill.
- S1: bucket-dedupe, weekly-row handling, deferred flip-close, window-end backstop, mark-staleness gate (with the P2 fallback hole), macd_refined tz-dedupe, uncap correctly scoped to paper S1 (live cap 5 / S2 cap 4 untouched), macd_reversal min-hold, PE cross convention consistent.
- Directional: positional hold-through-flat/flip, 30%/45%/DTE protective pass (runs on degraded cycles), stale-positioning fail-closed, daily refresh runner, after-hours entry guard, one-position-per-symbol, frozen-mark chain-cache fallback, transaction charges on close, DB-durable book, flat-confirmation + min-hold anti-churn.
- Commodity/auction: HTF gate ON and hard-blocking with causal bias; lvn_fade absorption corrected; OF coverage gate ON at 0.70; per-instrument MP tick in live+backfill; contract-scoped weekly/monthly aggregation; MP engine math (VA 70%, POC ties, IB, degenerate profiles) sound; MCX cost model realistic and netted on both booking paths; eviction-proof cooldowns; auction session-guard commit sound where it applies; `fo_risk` zip/headerless parsing correct with tests green.
- Portfolio: lifetime-vs-today split fixed; reward denominator fixed (size-normalized R); CBE exposure-vs-return labeling + 2026-06-29 fix-set all present; guaranteed EOD pass wired; no shared cash across lanes; commodity `book_close` self-heal idempotent.

---

## Recommended fix sequence

**Before Monday's open (small, surgical):**
1. **P0-2 Telegram** — route S1 alerts through `telegram_agent.send()`, add 401 audit-event + health; owner's stated top priority, ~1 file.
2. **P0-1 live_candle_store** — try/except per flush iteration + re-queue + restart/heartbeat.
3. **P1-A/P1-B** — `wait_for(~20s)` per chain-refresh call + cooldown-before-work (mirror the gap-fill fix); `return_exceptions=True` on the expiries gather + try/except around the `:389` call and the `:1665` DB read. These close the two remaining doors to the exact freeze class Monday is testing.
4. **P1-P/P1-Q** — old-leg exit price + monotonic-expiry guard + stale-instrument-cache fallback (roll window opens Monday).

**This week:**
5. P1-D IV unit normalization + source column (stops ongoing permanent corruption).
6. P1-F sargable band on the 45s subscription query (+ snapshot-candle floor).
7. P1-G learning-ratchet threshold/TTL + tally; P1-H non-destructive recovery.
8. P1-I/J/K directional: feed the loss caps, add a kill-switch, fail-closed on missing positioning (or tag journal rows), block `latest_session` readiness while the exchange is open.
9. P1-C build-lock re-arm; P1-E volume delta conversion on the live-tick path.
10. P1-O CBE tmp+rename + journal replay.

**Before any live activation (hard gate):** P1-M cluster — wire `on_trade_close`/`update_equity` from reconcile FILLED transitions, thread real price/capital into `check_order`, decrement on close, persist/restore the kill switch across mode toggles.

**Before the next "re-measure on honest books" pass:** P1-N cost-honesty (auction costs, net PnL in durable ledgers + events + learning, S1 into `paper_trade_book`), P1-L directional fill haircuts, P2 macd_refined `fresh` flag, S1 exit-mark bar-time stamping. Until these land, cross-lane paper PnL comparisons are not apples-to-apples.

**Structural (schedule):** open-window `broker_priority(PRIORITY_HIGH)` for the watchlist build + demote the macd_refined sweep (unblocks the chain builder); MI cycle-level shared deadline; commodity async persist + payload trim; runtime-state pool sizing; expiry-calendar single source of truth (`fo_expiry_catalog`) for both the auction front-month and Upstox epoch parsing.
