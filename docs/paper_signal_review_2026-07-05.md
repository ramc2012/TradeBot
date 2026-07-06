# Paper-Mode Signal-Generation Review — 2026-07-05

Multi-agent audit of the working tree (deployed via bind-mount, including ~4.9k uncommitted lines) against the paper-mode objective: **correct signal generation per defined strategies, not P/L**. 7 subsystem maps → 6 dimension reviewers (incl. empirical DB/log evidence) → adversarial verification of every finding. 48 raw findings → 45 verified → **42 CONFIRMED** (+2 confirmed in an earlier pass and re-checked by hand). Known-fixed baseline (MI timeout fix, directional discipline fix, gap-aware candle loader, MP durable history, deliberate uncaps) was excluded and not re-reported.

Severity is judged by impact on correct signal generation in paper mode.

---

## Verdict

The platform's plumbing (supervisor, lanes, feeds) is substantially built, but **four lanes are currently not testing the strategy they were designed to test** (Gann, directional-positional, S1 on indices, macd_refined dedup), **the measurement layer that would catch this is unplugged** (audits framework never scheduled, no CI, 7 of S1's own signal tests fail in the deployed tree, coverage telemetry ephemeral), and **the database underneath everything is being OOM-killed daily** (786 crash/recovery log lines in 7 days). Efficiency issues are real but secondary — with one exception: the supervisor's head-of-line blocking structurally breaks the 60s cadence the fast lanes are tuned for.

---

## A. Strategy fidelity — lanes not running their defined strategy (HIGH)

### A1. Gann time-stop counts 60s scan cycles as "bars" — exits ~15× early ⚑ HIGH
`backend/gann_tp_delta/agent.py:565` increments `bars_held` once per `run_once` with no bar-boundary check; `time_stop_bars=26` (config.py:144, meant = 26 × 15-min bars = 6.5h) therefore fires at ~26 **minutes**. Evidence: 74/183 closes are `time_stop` at exactly `bars_held=26` (~37-minute holds). The paper record measures a 26-minute scalper, not the defined 15-min-bar Gann strategy.
**Root-cause pair:** `GANN_TP_DELTA_AUTO_INTERVAL_SECONDS` / `CBE_MARKS_*` are read via `getattr(settings, ...)` but have **no Settings field** (config has `extra="ignore"`), so env overrides are silently dropped and gann is pinned to the directional 60s interval (`market_hours_paper_supervisor.py:662`).
**Fix:** advance `bars_held` only on a new 15-min bar close (track `last_bar_time`), or convert the time stop to wall-clock; declare the missing Settings fields.

### A2. Directional-positional lane trades front-WEEKLY contracts; the validated strategy is MONTHLY ATM (DTE 8–22) ⚑ HIGH
`backend/directional_options/regime.py:83` — `preferred_expiry_kind="weekly"` on all four `trade_allowed` branches; selector uses it verbatim. Close-reason distribution: 27 `expiry_roll` vs 1 `profit_target` — the book churns weeklies instead of holding the monthly positions the BANKNIFTY OI-build-confirmation edge was validated on. The paper record cannot confirm/refute that edge.
**Fix:** when the positional view is active, force `expiry_preference='monthly'` + DTE∈[8,22] entry window matching `backtest_indices_monthly.py`.

### A3. S1 enters WEEKLY index contracts via dead-S2 watchlist rows ⚑ HIGH
`backend/paper_engine/strategy_agent.py:2489-2507` still merges S2-native weekly composite rows and **replaces** the monthly index row, though the S2 lane was removed from `_strategy_agents` (2026-06-02). `_scan_entries` has no expiry-track guard. DB evidence: S1 position `OPT:NIFTY:2026-07-07:23900:PE` (weekly; monthly = 2026-07-28) entered/closed 07-01. S1's index signal record blends two contract classes; a weekly-row cross can flip-close a monthly position.
**Fix:** stop calling `_load_strategy2_native_watchlist_rows` in the scan path (or filter to `expiry_track=='monthly'`).

### A4. S1 zero-cross dedupe re-arms every 60s — intra-bar entry/exit ping-pong ⚑ HIGH
`strategy_agent_entries.py:860` keys `processed_signals` on the **per-minute snapshot timestamp** instead of the 30m bucket (which is computed and carried but unused). After any intra-bar exit (hard stop, `macd_reversal_30m`, flip), the still-standing forming-bar cross re-enters the SAME 30m bar; signal counts and strategy_learning stats get inflated with duplicates of one cross.
**Fix:** key on `latest_macd_bucket` — one entry per underlying:side:kind per bucket.

### A5. macd_refined cross dedup compares mixed-timezone timestamp STRINGS ⚑ HIGH
`backend/macd_refined/live.py:608` — `str(t) > last_sig` where the parquet-resample path yields `+00:00` strings and the broker-history fallback yields `+05:30` strings. When a contract's series source flips intra-day (thin parquet), lexical comparison mis-orders bars → genuine fresh crosses silently dropped for the rest of the session, or already-signalled crosses re-fired. The lane exists precisely to measure these signals.
**Fix:** compare tz-normalized `pd.Timestamp` (UTC ISO); migrate `signal_state.json` once at load.

### A6. S1 signal-flip closes the old position BEFORE downstream gates and even under the kill switch — MEDIUM
`strategy_agent_entries.py:797` closes the opposite leg, then missing-LTP/IV-sanity/dedupe/MP-gate/learning gates can `continue` without re-entry, and the kill-switch check is only at :986. A gated flip leaves the book flat with a misleading `signal_flip_to_*` close; flips still close positions while the kill switch forbids acting.
**Fix:** approve the replacement entry (all gates incl. kill switch) first; only then close; else record `flip_blocked`.

### A7. S1 has NO expiry/window-end exit — MEDIUM
`strategy_agent_exits.py:23` — cascade is hard_stop / target / macd_reversal only (`window_end` deleted 06-02 as "over-firing"); entries enforce T-0/3-day-expiry skips but exits are asymmetric. DB: 8/45 closed S1 positions closed only *inside* the physical-delivery window. With the 1000-position uncap, zombie holds pollute the per-signal outcome record.
**Fix:** forced `window_end` close at `expiry-7d` (matching entry window math) or an expiry-1d hard exit.

### A8. S1 entry and exit compute the "same" 30m MACD from two different series — MEDIUM
Entry: watchlist snapshot closes + synthetic forming bar (atm_watchlist.py:1902-1916, entries.py:316-358). Exit: independent recompute from `load_candles` closes, no synthetic bar (`strategy_agent_exits.py:75-133`). Entry can fire on a synthetic-bar cross the exit series doesn't show → `macd_reversal_30m` closes within a cycle (no minimum hold). Combined with A4 this is the churn loop.
**Fix:** one authoritative series builder for the lane.

### A9. S1 relative-IV sizing is dead code — MEDIUM
`strategy_agent_entries.py:842` reads `snapshot_state.get("market_iv_pct")` but snapshot_state is keyed by instrument_key and **no producer of market_iv exists anywhere in backend/** → always None → sizing and the journaled `iv_status`/`iv_size_note` (consumed by strategy_learning) computed vs a hardcoded 22% constant. Vol-spike days mislabel everything `iv_spread_extreme` at 0.25× size; per-signal attribution is systematically wrong.
**Fix:** compute a real market-IV reference per cycle (median watchlist ATM IV) and pass it explicitly.

### A10. Two zero-cross detectors with OPPOSITE PE rules still coexist — MEDIUM
`strategy_agent.py:300` `detect_macd_zero_cross` still encodes the inverted PE down-cross that entries.py fixed; it still runs on S2 signal-audit surfaces (closed-market prepared context, recovery replay via `_persist_agent_signal_observation`). Audit numbers disagree with the live rule; if S2 is ever re-enabled it trades the inverted side on day one.
**Fix:** single shared cross helper; regression test asserting CE and PE both use the premium up-cross.

### A11. macd_refined `iv_rank` "252-session" window is actually ~19 sessions — LOW
`live.py:623` ranks over 252 per-cycle capture snapshots (~13/session at 1800s cadence). Signal acceptance unaffected (`iv_gate_enabled=False`), but every journaled `iv_zone` label for the cheap-vs-rich mapping study is wrong.
**Fix:** one IV observation per session before `.tail(252)`; record window size in the journal row.

### A12. CBE "unclassified" pass-2 names are structurally untradeable — MEDIUM
`cbe_scanner/alpha_engine.py:302` tags pass-2 F&O names `stock_quadrant="unclassified"`, but `_bias_from_signals` requires quadrant ∈ {leading,improving} / {lagging,weakening} and paper.py skips non-bullish/bearish rows → 17 names (~8% of universe) are fetched and scored hourly yet can never signal, contradicting the "still tradeable" comment. Silent coverage hole.
**Fix:** neutral-permissive quadrant with score haircut, or exclude before scoring + surface `unclassified_excluded` in telemetry.

### A13. FMP order-flow confirmation runs on fabricated inputs — MEDIUM
`fractal_market_profile/service.py:1781` + `config.py:24`. Index quote history carries no real book (0 of 206,336 NIFTY50-INDEX tick rows in 5d have bid>0) — bid/ask are synthesized as LTP±tick with size 1.0 yet labeled `source="market_ticks"`, `tick_ready=true`; CRUDEOIL is pinned to hardcoded expired `MCX:CRUDEOIL26JUNFUT` (0 tick rows ever). Signal confidence gets ±0.08/−0.05 adjustments around the 0.55 actionability threshold from noise dressed as confirmation.
**Fix:** derive CRUDEOIL symbol via `resolve_upstox_mcx_future`; tag synthetic books and zero their confidence contribution.

---

## B. Data reliability

### B1. Postgres OOM-kill storm — ongoing ⚑ HIGH
786 crash/recovery lines in `nomadcurie_db` logs over 7 days, latest **2026-07-05 13:44 UTC** ("server process terminated by signal 9" → recovery mode). The research-sync daemon runs the full pipeline every ~30 min around the clock (14,831 option candles stored on a closed Saturday run) against 4.26M-row option_candles / 4.63M spot / 1.33M chain-metric hypertables. Any lane's query can die mid-cycle during recovery windows; every runner shares this DB.
**Fix:** find the memory hog (likely large sorts/aggregates from research-sync + unbounded queries in B/C below); set container memory limits + `work_mem` sanity; consider scheduling research-sync outside NSE hours; add pg crash alerting.

### B2. Directional staleness gate bypassed by mere row presence — HIGH
`directional_options/service.py:579-591` flips `execution_ready=True` and clears `degraded_reason` whenever `list_live_contract_snapshots` returns rows; the query (`data.py:210-215`) has **no lower time bound**, so day-old rows re-enable entries during exactly the intraday feed outages the 600s gate was built for. Regression-shaped hole in the measurement-integrity layer.
**Fix:** require `now - max(snapshot time) <= stale_watchlist_seconds` before overriding.

### B3. Positional side selector runs on a frozen, wrongly-sourced spot series — MEDIUM (live-broken now)
`directional_options/positioning_feed.py:111` — the sole side selector (EMA20>EMA50) is computed from spot scraped off arbitrary `option_premium_candles.underlying_price` rows with gap-days dropped. **Live failure: underlying_price stopped populating 2026-06-22, freezing the EMA at 06-19 while NIFTY rallied ~1%; htf_up=False (PE side) is being served as fresh.** The date-based staleness check and the NULL-passing vol gate both fail open around it.
**Fix:** source the EMA backbone from `underlying_spot_candles` daily closes; make `_positioning_is_stale` fail closed.

### B4. Positional "mandatory" long-premium vol gate is a silent no-op — MEDIUM
`directional_options/signals.py:73` — gate passes whenever `d_atm_iv` is NULL; the "enforced upstream" fallback claimed in the comment does not exist. Currently the latest positioning row for **all 8 underlyings has NULL d_atm_iv** → the gate is a permanent no-op in the running deployment. Research (06-28) measured high/rising IV-pct as the strongest negative conditioner for long premium (IC −0.305).
**Fix:** treat None as gate-fail, or implement the promised live ATM-IV check; log the deciding branch.

### B5. Directional stop/target marks have unbounded age — MEDIUM
`directional_options/data.py:377` + `paper.py:314-341`: `latest_local_option_mark` age is never checked; a stale watchlist-snapshot row shadows both fallbacks (chain cache consulted only when premium is None). The code's own comment concedes "no fresh mark → latest≈entry → ret≈0 → no false trigger" — i.e. **the 30%/45% protective exits are structurally unable to fire exactly when data degrades**.
**Fix:** carry mark age end-to-end; max-age gate (~15 min RTH) before stop/target evaluation; journal `mark_stale` instead of silently evaluating ret≈0. (Same pattern for S1 exits: `strategy_agent_exits.py:282`, no recency bound on the mark query — LOW since exits do broker-refresh per cycle.)

### B6. `option_premium_candles` has 4 independent writers with conflicting upsert semantics — MEDIUM
`market_data/option_history.py:602-607` — the read-side source-precedence CASE is blind to two live sources and ranks greeks-bearing `upstox_expired` rows below greeks-null `upstox` rows, contradicting its stated intent. S1's 30m MACD closes, macd_refined warm-up, and the positioning feed all read through this CASE; the winning close can silently flip source at shared timestamps, and per-bar vs cumulative volume semantics are mixed.
**Fix:** one choke-point writer (shared upsert helper, one conflict policy); `source_rank` column stamped at write time.

### B7. FO risk ingest (MWPL / ban list) permanently failing — LOW
Research-sync logs: all NSE archive URLs 404 on every run (`mwpl=0 ban=0 errors=2`). Any ban-list-aware gating is running on empty data. Fix the URLs or disable with an alert.

---

## C. Efficiency (computation & speed)

### C1. Supervisor head-of-line blocking — one slow runner freezes every 60s lane ⚑ HIGH
`market_hours_paper_supervisor.py:794-810` — `run_due_once` awaits MI serially, then `asyncio.gather`s the rest, and the loop cannot re-schedule until the **slowest** due runner finishes. macd_refined measured ~533s warm (1200s ceiling), due every 1800s → every 30 in-session minutes, market_intelligence/directional/gann miss ~8 consecutive 60s cycles. The 60s cadence the lanes are tuned to is structurally violated ~12×/session.
**Fix:** launch runners as `asyncio.create_task` (the `running` flag already prevents overlap) so the scheduling pass returns immediately — or at minimum exclude runners with `timeout_seconds > loop_seconds` from the awaited gather.

### C2. MI premium top-up is strictly serial under its budget — MEDIUM
`market_intelligence_runtime.py:770+` — priority pass iterates ~434 contracts one `await _topup()` at a time (+pause) under the 150s budget; slow/dead calls eat 8s each, so per-cycle coverage is throughput-limited. The budget fix (baseline) made it *safe*; concurrency (small `asyncio.Semaphore(3-5)` batches within the same budget and limiter) would multiply covered contracts per cycle.

### C3. Commodity agent runs the full prep pipeline every 30s around the clock — MEDIUM
`commodity_strategy_agent.py:2590` — broker LTP + 2-day 1-min history for 8 symbols + MP rebuild + ~848KB state persist every 30s **while MCX is closed** (verified live on a closed Sunday). Burns the shared Upstox limiter budget (8/s, 1800/30min) that option-history backfills and MI top-up need at the open.
**Fix:** widen closed-market sleep to 300–900s; cache per-(symbol,session); skip persist when unchanged.

### C4. Auction lane computes the full analyze pipeline twice per symbol per 180s cycle — LOW
`auction_intelligence/automation.py:209` (+ live.py:472): shadow snapshot and paper book each run `analyze_with_options` (~90s/cycle total). Reuse the AnalysisBundle.

### C5. S1 rebuilds a 7-day all-instrument 15m MACD from raw snapshots every 60s — LOW
`strategy_agent_entries.py:208` — ~68k rows refetched/resampled per minute inside S1's own loop (sleep runs *after* completion, so slow rebuilds stretch the effective scan interval). Cache per-instrument 15m series and append incrementally.

### C6. S1 exit loop is N+1 per open position — LOW
`strategy_agent_exits.py:75` — per-position UNION quote query (no time bound) + broker-refresh-capable `load_candles`, sequential, and the stale-today refresh retry has no TTL (a failing contract retries every cycle on the shared limiter). Matters at uncapped position counts. Batch with `DISTINCT ON ... WHERE (...) IN (...)` + time lower bound + retry TTL.

### C7. Small hot-loop leaks — LOW
- Unbounded `COUNT(DISTINCT underlying)` over the full snapshots hypertable every MI cycle (`market_intelligence_runtime.py:381`, growing ~7k rows/day, runs inside the runner every other lane waits on).
- ATM expiries Redis cache is dead on the live path — payload never contains `rows`, so the freshness check always fails and the ladder rebuilds ~2×/min (`atm_watchlist.py:684`).
- macd_refined does synchronous pandas parquet read/concat/write for 217 names on the shared event loop (`live.py:355`) — wrap in `asyncio.to_thread`, append instead of rewrite.
- FMP parses its entire 39.5MB `paper_journal.jsonl` to return 8 records, 4×/300s cycle (`fractal_market_profile/paper.py:708`) — tail-read + rotate.

---

## D. Unnecessary duplication (drift risks)

| # | Duplication | Files | Drift consequence |
|---|---|---|---|
| D1 | Two MACD zero-cross detectors, opposite PE rules | `strategy_agent.py:300` vs `strategy_agent_entries.py:607` | audit/replay surfaces disagree with live rule; S2 re-enable = inverted side (see A10) |
| D2 | Two 30m-MACD series pipelines within S1 (entry vs exit) | `atm_watchlist.py:1902` vs `strategy_agent_exits.py:133` | entry/exit disagree on MACD sign → same-bar churn (see A8) |
| D3 | Directional scoring: research vs live are separate ~170-line copies | `selector.py:382` vs `selector.py:571` | workspace/backtester validates the **legacy momentum view** (PF 0.2) while paper trades the positional view — research output no longer describes live signals |
| D4 | Redis watchlist cache version literal `v12` duplicated | `market_intelligence_runtime.py:418` vs `atm_watchlist.py:39` | next version bump silently kills MI cache invalidation → frozen 217-name watchlist again |
| D5 | Supervisor cadence knobs: `getattr` literals with no Settings fields | `market_hours_paper_supervisor.py:662` | env overrides silently dropped; gann pinned to 60s (fuels A1) |
| D6 | Gann paper book writes `close_reason`, journal/API reads `exit_reason` | `gann_tp_delta/agent.py:555` vs `service.py:172` | all 183 closed trades show `exit_reason=None` — exit-discipline unauditable (one-line fix) |
| D7 | `option_premium_candles`: 4 writers, per-lane conflict policies | `option_history.py:602` et al. | see B6 |

---

## E. Objective gaps — the platform can't currently *judge* signal correctness

This is the largest gap relative to the stated purpose. Paper mode's product is a trustworthy signal record; today:

1. **The purpose-built audit framework is unplugged.** `backend/audits` (replay parity, gate attribution, trade reconciliation, edge persistence — exactly the paper-mode objective made executable) has **no scheduler or caller anywhere**, is CLI-only, and registers only S1 (1 of 10 lanes) (`audits/lanes/__init__.py:3`). → Add a post-close supervisor runner (`post_close_force_daily=True`) running `lane_audit` for every registered lane; surface `overall_status` in `/api/system/health`; write auditors for commodity, macd_refined, directional next.
2. **No CI, and the deployed tree fails 7 of S1's own signal tests** — reproduced inside the live container (`pytest`: 7 failed / 32 passed; core MACD entry-path test dies on a stale stub, `fake_snapshot_state() got an unexpected keyword argument 'bucket_minutes'`). Bind-mount deployment ships uncommitted changes with zero test gate. → Fix the 7 stale tests; add a minimal pre-restart test gate (even a make target on the Mac mini).
3. **Backtest-vs-paper parity is unimplemented** — `_invariant_backtest_parity` unconditionally returns `"na"` (`audits/lanes/s1_atm_30m_macd.py:358`); no code computes live-vs-backtest signal-rate drift for any lane. This is PROJECT_STATUS's own outstanding checklist item and the central paper-mode question.
4. **Coverage / why-no-trade telemetry is ephemeral** — auction gate breakdowns go to logger+memory; S1's `scanned_rows` is dropped from the status payload; MI audit events record "scanned 0 symbol(s)" on success (`automation.py:257`). The 06-29 starvation (14-16/217 names scanned for days) is exactly the class of regression this hides. → Persist a compact per-cycle coverage row (lane, ts, universe, evaluated, per-gate blocks) to `agent_audit_events`.
5. **No alerting when a lane goes silent; a supervisor-loop crash stops all 10 lanes with zero notification** (`market_hours_paper_supervisor.py:713` re-raises; no done-callback/restart; pull-based health can't flag succeeded-then-silent). → done-callback restart + Telegram alert; independent per-runner staleness watchdog (`now - last_success_at > k*interval` during that runner's hours).
6. **Runner health not persisted across restarts**; `healthy_runner_count` counts never-ran/disabled runners as healthy (`supervisor.py:81`) — post-restart status (10/10 healthy, all timestamps None) is indistinguishable from steady state.
7. **Stated-purpose docs are stale**: README/PROJECT_STATUS (2026-03-28) describe none of the 10 running lanes — there is no written spec to judge most lanes' signals against (lane READMEs/config comments partially fill in).

---

## Priority sequence

**P0 — signals are wrong today (small, surgical fixes):**
1. Gann `bars_held` per-bar (or wall-clock time stop) + declare missing Settings fields (A1/D5)
2. Directional-positional: force monthly expiry + DTE window (A2); fix frozen positioning spot backbone — live-broken since 06-22 (B3); staleness override needs a freshness bound (B2); vol gate fail-closed on NULL (B4)
3. S1: bucket-keyed cross dedupe (A4); stop merging dead-S2 weekly rows (A3)
4. macd_refined: tz-normalized cross dedupe (A5)
5. Supervisor: fire runners as tasks — unblock the 60s lanes (C1)
6. Postgres OOM: diagnose + contain (B1)

**P1 — make the paper record trustworthy:**
7. Mark-staleness gates on directional + S1 exits (B5); S1 window-end exit (A7); flip-after-gates (A6)
8. Schedule the audits framework post-close for S1; add coverage-row persistence; silent-lane watchdog (E1/E4/E5)
9. Fix the 7 failing S1 tests + minimal test gate before container restarts (E2)
10. Real market-IV producer for S1 sizing/labels (A9); single MACD series per lane (A8/D2); single cross detector (A10/D1)

**P2 — efficiency and hygiene:**
11. Concurrent MI top-up within budget (C2); commodity closed-market cooldown (C3); auction bundle reuse (C4); S1 15m cache + batched exits (C5/C6); cache/version/literal cleanups (D4, C7); gann exit_reason one-liner (D6); premium-candles single writer (B6/D7)

---

*Method: Workflow run `wf_509fd859-97f` (4 resumes across session-limit windows), 7 mappers + 6 finders + 45 adversarial verifiers + completeness critic; every finding verified against the working tree and/or live DB (read-only). 2 critic candidates remain unverified and were dropped. Full machine-readable findings: session scratchpad `review_result.json`.*
