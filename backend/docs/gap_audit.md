I'll write the independent gap audit report based on the findings and adversarial verdicts provided. Let me synthesize the confirmed findings, dedupe across dimensions, and apply the revised severities.

# Independent Gap Audit — Nomad Curie Trading Platform

**Date:** 2026-06-07  **Auditor:** Lead Engineer (independent review)  **Subject:** NSE F&O algorithmic trading platform (prod: AWS EC2 `15.206.56.206`, t3.medium, 3.7 GiB RAM)

---

## 1. Executive Summary

**Overall health: AT RISK.** The platform has one internet-exploitable money-loss vector (unauthenticated API by default) that overshadows everything else, plus a cluster of confirmed reliability and Redis-leak defects that degrade silently during market hours. The adversarial verification pass was valuable: it **killed the entire "P&L sign inversion" theme** (4 findings refuted — the NSE lane is long-only and commodity state carries the correct multiplier) and several over-stated security claims, but it **confirmed the most dangerous one** — auth is off by default.

**Confirmed counts (post-verification):**
- **Blockers:** 4 (1 security, 3 reliability/streaming)
- **Highs:** 11 (after downgrades)
- **Refuted/false-positive:** 15 findings killed or reduced to none
- **Downgraded by verification:** 6 (high→medium/low)

**Top 5 risks (one line each):**
1. **API auth disabled by default** — any internet client can flip to live mode and place/cancel real Fyers orders. (BLOCKER)
2. **Redis pub/sub connection leaks** in `ws_quotes`/`ws_proposals` — exhaust the 1000-conn pool during reconnect storms → total quote-feed blackout. (BLOCKER)
3. **Background tasks never cancelled on shutdown** — orphaned market-data loops leak DB/Redis conns into the next boot; restarts hang. (BLOCKER)
4. **Stub `/health` + sole-Fyers SPOF with a 30–75s silent stale-feed window** — orchestrator thinks it's healthy while strategies trade on dead/stale ticks. (HIGH×2)
5. **`ON CONFLICT DO NOTHING` on `option_premium_candles`** — greeks-null `live_tick` rows shadow greeks-bearing `fyers_chain` rows → S1 MACD computes on biased data. (HIGH)

---

## 2. Confirmed BLOCKER / HIGH Gaps

> Refuted findings dropped. Deduped across audit dimensions (the SECRET_KEY finding appeared 3× and `.env`/`APP_WRITE_TOKEN` overlapped — collapsed below). Ordered by severity, then blast radius.

### BLOCKERS

#### B1. API authentication disabled by default — full unauthenticated trade control
- **File:** `backend/core/config.py:187` (`APP_TOKEN_AUTH_ENABLED: bool = False`) + `backend/main.py:321-339` (`require_api_token` short-circuits when guard inactive)
- **Impact (money):** With the default (not overridden in `.env`, and `docker-compose.yml` defaults it to `false`), every `/api/*` route is unauthenticated. An attacker on the internet can `POST /api/trading/mode {"mode":"live"}` (no auth) then `POST /api/trading/orders` → **real Fyers F&O orders**, `DELETE` orders, toggle the kill-switch, read all positions/P&L, reset paper accounts. Fyers is the sole live lane. This is direct, remote, real-money loss.
- **Fix [backend/infra]:** Set `APP_TOKEN_AUTH_ENABLED=true` in prod `.env` **now** and rotate `APP_WRITE_TOKEN`. Add fail-fast startup validation: raise if `APP_ENV=="production"` and auth is disabled. Enforce token on all mutating routes independently of the global flag. (Note: this is also the root cause that makes B2-adjacent finding *WS-token-unauthenticated* and the CORS finding exploitable — fix this first.)

#### B2. Redis pub/sub connection leak in `ws_quotes` on Redis unavailability
- **File:** `backend/api/websockets/ticks.py:781-787`
- **Impact:** On the Redis-down degrade path the code `return`s without `await _close_pubsub(pubsub)`. The pubsub client created at line 783 leaks. `REDIS_MAX_CONNECTIONS=1000` (config already documents a prior 2026-06-05 exhaustion event). During market-hours reconnect churn, leaked clients accumulate → `max number of clients reached` → **entire quote tape blackout** (hard failure, not graceful degrade) → can't mark positions or price exits.
- **Fix [backend]:** Call `await _close_pubsub(pubsub)` before the early `return`. Apply the same guard to `ws_depth` (`:826-836`) and `ws_ticks` (`:112-119`), which share the pattern.

#### B3. Redis pub/sub connection leak in `ws_proposals` — no error handling at all
- **File:** `backend/api/websockets/ticks.py:863-868`
- **Impact:** Unlike the other handlers, `ws_proposals` does not wrap `get_redis()`/`subscribe()` in try/except. If `subscribe("proposals")` raises, the already-created pubsub leaks (the `finally` only runs once the listen loop is entered). Same pool-exhaustion path as B2; proposals carry agent order-generation signals. *(Verifier downgraded this to "high" for revised_severity but confirmed the leak; kept as blocker-tier because it shares the same pool-exhaustion blast radius as B2 — same root cause, fix together.)*
- **Fix [backend]:** `pubsub = None` before a try block; wrap Redis init; `if pubsub is not None: await _close_pubsub(pubsub)` in `finally`.

#### B4. Background tasks not cancelled in graceful shutdown
- **File:** `backend/main.py:193-294` (tasks created at 199/214/227; shutdown block 265-294 omits them)
- **Impact:** `option_ws_task`, `held_position_ws_task`, `commodity_mark_task` are created inside `try` blocks in `lifespan()` but never `.cancel()`/`await`ed at shutdown (only `research_sync_task`/`loop_lag_task` are). They keep running after `yield`, holding DB/Redis connections that leak into the next container cycle — directly feeding the documented DB `max_connections=25` exhaustion and the Redis cap. Restarts hang waiting on pending tasks. On a bind-mount + OOM-prone t3.medium this compounds the recreate/revert hazard.
- **Fix [backend]:** Declare the three tasks at `lifespan()` scope; in shutdown loop `task.cancel(); try: await task; except asyncio.CancelledError: pass`. All three loops already re-raise `CancelledError`, so this is safe.

---

### HIGHS

#### H1. Stub `/health` endpoint with zero dependency checks *(verifier confirmed; revised high)*
- **File:** `backend/main.py:466-468`
- **Impact:** `/health` returns `{"status":"ok"}` unconditionally. Docker Compose healthcheck and the deploy workflow both probe it. It reports healthy with Fyers/Redis/Postgres down. The real check at `/api/system/health` is auth-gated (unusable for LB probes). Orchestrator routes traffic to a dead-feed container → orders into a dead broker.
- **Fix [backend]:** Make `/health` do cached (1s TTL) Redis ping + DB `SELECT 1` + optional broker-session check; return 503 on any critical-dep failure. Keep `/api/system/health` for detail.

#### H2. Sole-Fyers SPOF — 30–75s silent stale-feed window before reconnect *(confirmed)*
- **File:** `backend/market_data/data_router.py:679-698` (watchdog `_watchdog_interval_seconds=30`, `_required_tick_stale_seconds=45`)
- **Impact:** On a clean Fyers WS drop, the data-quality status goes `degraded` (not `critical`), so the strategy gate does **not** block scans (`strategy_agent.py:2259` only blocks on `critical`). First reconnect attempt is up to 30+45s out. Strategies trade on stale index data and place orders that silently no-fill → phantom internal ledger vs. real Fyers account.
- **Fix [backend]:** Reduce `_required_tick_stale_seconds` to 20–25; arm the watchdog unconditionally at startup (see H3); escalate stale required-index ticks to `critical` so the gate blocks; alert eagerly via the audit bridge.

#### H3. Required-feed watchdog may never arm (startup path gap) *(related to H2; verifier confirmed the SPOF; merge)*
- **File:** `backend/market_data/data_router.py:273-287` — `start_required_feed_watchdog()` only called from `auth.py` `get_active_adapter()`.
- **Impact:** If the app boots without an interactive broker auth (token auto-restore), the watchdog is never started; a later WS drop is never auto-detected. *(Reported as medium originally; folded here because it is the enabling condition for H2.)*
- **Fix [backend]:** In `main.py` lifespan after broker selection, if an adapter exists call `await market_data_router.start_required_feed_watchdog()` unconditionally.

#### H4. `ON CONFLICT DO NOTHING` lets stale/greeks-null rows shadow fresh chain rows *(confirmed)*
- **File:** `backend/market_data/option_history.py:799`
- **Impact:** `live_tick` writes a greeks-null premium row first (every 5s flush); `fyers_chain` arrives later with full greeks but the insert is silently rejected because the key exists. Read-path `DISTINCT ON (synced_at DESC)` only ever sees the stale row. **S1 MACD computes on greeks-null premium candles** → biased signal lines, skewed entry timing; ATM watchlist greeks/IV stale. Note the spot path (`live_candle_store.py:370`) already uses the correct `DO UPDATE`.
- **Fix [backend]:** Change to `ON CONFLICT … DO UPDATE` with source-aware precedence (`fyers_chain=0 > fyers=1 > upstox=2 > live_tick=3`), `COALESCE`-ing greeks; only overwrite if incoming source ranks higher or same-tier-and-newer `synced_at`. Also covers the duplicate `live_candle_store.py:354-389` write path.

#### H5. Unhandled exceptions in background loops swallowed without alerting *(confirmed)*
- **File:** `backend/paper_engine/strategy_agent.py:1038-1048`; `option_subscription_manager.py:361-405, 654-676, 755-790`
- **Impact:** Bare `except` logs and continues; the option-pick and commodity-mark loops have **no health probe at all** (DB error / token expiry / 429 → silent skip; MCX marks freeze for 12s/cycle). Operator sees "healthy" while option legs aren't subscribed or commodity marks are stale. Strategy agent's `_last_error` surfaces only after ~5-20s health poll.
- **Fix [backend]:** Rate-limited audit alert per error-type (once/5min); log at `warning`/`exception` not `debug`; add a heartbeat/last-success timestamp for the option and commodity loops to `/api/system/health`.

#### H6. WebSocket token endpoint unauthenticated *(confirmed; gated on B1)*
- **File:** `backend/api/routers/auth.py:1464-1470`
- **Impact:** With `APP_TOKEN_AUTH_ENABLED=False` (the default), `GET /api/auth/ws-token` mints a 300s token to anyone, which then authorizes all 16 `/ws/*` streams (ticks, positions, quotes, depth, strategy-dashboard) — live market data + positions + P&L exfiltration on a 5-min refresh loop. **Resolved as a side effect of B1, but verify independently** since the WS auth path is separate from REST middleware.
- **Fix [backend]:** Once B1 is fixed the middleware covers this; additionally require `APP_WRITE_TOKEN` to mint, or mint server-side on authenticated UI load.

#### H7. Broker credentials encrypted with a key derived from a weak SECRET_KEY *(confirmed; deduped — SECRET_KEY finding appeared 3×)*
- **File:** `backend/core/config.py:186` (`SECRET_KEY="change-me-…"`) + `backend/core/security.py` (`Fernet(sha256(SECRET_KEY))`)
- **Impact:** Fernet key is `sha256(SECRET_KEY)`. The prod `.env` value is a human-readable placeholder-grade string (`nomadcurie-dev-secret-key-change-in-production`), and the hardcoded default ships in source. Anyone with source + `.env` (or just the default) decrypts `credentials.json` and the `app_runtime_state` table — Fyers PIN/tokens, Upstox analytics_token, 5Paisa password/user_key, ICICI secret. Verifier *successfully decrypted a stored Fyers field* with the derived key. **Note:** verifier refuted the related claim that SECRET_KEY enables order-forging (no JWT; writes are gated by `APP_WRITE_TOKEN`, not SECRET_KEY) — so the impact is **credential disclosure**, not auth bypass.
- **Fix [backend/infra]:** Generate a high-entropy `SECRET_KEY` at deploy (never commit); fail-fast if it contains `change-me`/`dev`; rotate it and re-encrypt all stored credentials; medium-term move to AWS KMS/Secrets Manager.

#### H8. No alerting for feed failures / strategy errors / pool exhaustion *(confirmed)*
- **File:** `backend/core/config.py:219-226` + missing hooks across data_router/strategy loops
- **Impact:** Telegram alert framework exists but is **only invoked via `record_audit_event()`**, which the hot-path failures (strategy scan crash at `strategy_agent.py:1044-1047`, order rejection, DB pool exhaustion, Redis saturation) never call. Prometheus `/metrics` exists but is **never scraped** (no scrape config, no dashboard, no rules). A scan crashes silently; positions age with no exit. Feed-staleness *does* alert (one positive).
- **Fix [backend/infra]:** Call `record_audit_event(severity=error)` in the strategy-loop except handler and on order reject / pool timeout. Set `TELEGRAM_EVENT_MIN_SEVERITY=error` in prod. (Defer Prometheus/Grafana — RAM-constrained; lightweight Telegram alerts first.)

#### H9. DB connection-pool defaults unsafe for the RAM-constrained box *(confirmed)*
- **File:** `backend/core/config.py:52-55` (defaults 0 → `db/database.py:21-22` resolves to pool 8 / overflow 8 = 16)
- **Impact:** Postgres `max_connections=25` (per memory). 16 async conns + the documented OOM-recreate connection-leak cascade → `FATAL: too many clients` → `atm_watchlist_service.get_watchlist()` fails while single-conn endpoints still 200. Morning-open concurrent scans are the trigger. *(Verifier refuted the separate "pool exhaustion is unfixed" framing — the 3→8 bump already landed — but confirmed 16 is still tight on 25 with no saturation alerting.)*
- **Fix [backend]:** Keep total ≤ ~20 (leave headroom under 25); add pool-saturation metadata to `/health`/`/api/system/health`; alert on pool timeout. Do **not** raise blindly — `max_connections=25` is the hard ceiling.

#### H10. No preflight/rollback in deploy; non-blocking `docker compose up -d` reports false success *(confirmed)*
- **File:** `.github/workflows/deploy.yml:1-50, 232, 267-289`
- **Impact:** Deploy triggers on every push to main with no `pytest`/typecheck/migration validation. `up -d` returns 0 after build regardless of runtime startup; health probes are explicitly "informational, never deploy-killing." A config typo → container crash-loops (`restart: unless-stopped`) while GitHub reports success. Rollback is manual git-revert + 2-min redeploy — costly during a fast market move. Combined with bind-mount ephemerality, recreates silently revert hand-pushed edits.
- **Fix [infra]:** Add `pytest --co` (import-sanity) + alembic dry-run preflight; make the post-deploy health probe deploy-killing with auto-revert; keep last 2 images for instant rollback; Telegram on deploy fail/timeout.

#### H11. Missing tests on money-critical paths — order reconciliation, e2e order flow, data-quality gate, expiry rollover, quote_bus *(confirmed; deduped — 5 testing findings collapsed)*
- **Files:** `live_engine/order_manager.py:60-150` (reconcile loop, out-of-order partial fills, orphan cleanup, non-atomic status updates untested); `backend/tests` (no e2e tick→order→fill→P&L); `market_data/data_quality_agent.py` (gate-blocks/unblocks/symbol-granular untested); NSE expiry rollover/auto-liquidation untested (no equivalent of the commodity `_manage_positions` stranded-position close); `market_data/quote_bus.py` (zero tests — verifier downgraded to medium).
- **Impact:** State divergence between broker and local book (risk miscalc, duplicate hedge), expired NSE positions left open with stale pricing, gate false-positive/negative under partial feed freeze — all invisible to current unit tests.
- **Fix [backend]:** Add (a) async order-manager reconcile tests incl. out-of-order fills + orphan cleanup + locked status updates; (b) `test_end_to_end_order_flow.py` with a latency-mocked broker; (c) data-quality gate block/unblock/per-symbol tests; (d) NSE `test_contract_rollover.py` incl. expiry-day auto-close; (e) `test_quote_bus.py`. **Refuted:** the portfolio P&L-sign tests were claimed missing but the math + tests already exist (long-only NSE; commodity multiplier verified) — drop that sub-item.

#### H12. Hardcoded `localhost:3000` v1 links across the v2 frontend *(confirmed; deduped — 3 findings collapsed)*
- **File:** `frontend-v2/src/components/layout/TopBar.tsx:36`, `settings/SettingsPanel.tsx:86/1632`, `strategies/.../AgentMonitor.tsx:337`, and ~11 desk components.
- **Impact:** v1 frontend was retired 2026-06-07; these links point at the user's own `localhost`, not prod. Every trader sees broken "v1" links on every page (TopBar/AgentMonitor are global and clickable). *(SettingsPanel link verifier-downgraded to low since v2 is the full replacement.)*
- **Fix [frontend]:** Replace with `NEXT_PUBLIC_V1_URL` (fallback `/`) or simply remove the dead links now that v1 is gone. One shared constant.

#### H13. Stale multi-symbol data shown as live without freshness *(confirmed)*
- **File:** `frontend-v2/src/components/strategies/directional/UniverseWatchlist.tsx:90-150`
- **Impact:** Rows show spot/regime/signal with `fresh = ds.execution_ready` only — the boolean stays true until `spot_age_seconds > 600s`. v1's `MultiSymbolWatch` checked `< 600s` explicitly; v2 dropped `spot_age_seconds` from the type. Backend caches 30s + 15s refresh, so 30–90s-old data renders as "live" green across 30–50 symbols with no per-row asOf. Trader acts on stale prices in fast markets.
- **Fix [frontend]:** Re-include `spot_age_seconds`; tighten `fresh` to `execution_ready && spot_age_seconds < 600`; add a per-row freshness/asOf indicator with an amber warning > 30s.

---

## 3. MEDIUM Gaps

| Title | File | Impact | Fix |
|---|---|---|---|
| Depth ref-count desync on failed re-arm *(confirmed, downgraded high→med)* | `market_data/data_router.py:178-183,494-537` | After a reconnect fails to re-arm a depth sub, refs desync; a second client's subscribe is a no-op → 5-level DOM unavailable for that symbol until next full reconnect (auto-recovers) | On re-arm failure, mark symbol "needs re-subscribe" so unsubscribe doesn't decrement a never-established sub; or retry w/ backoff |
| Option/commodity loops silently lock empty subs / freeze marks *(downgraded high→med)* | `option_subscription_manager.py:217-218,351-352,733-734` | Watchlist fetch failing returns `[]` but `_locked_for_date` still set → zero option subs locked for the day, no retry; MCX REST failure returns `{}` (not exception) → stale marks, no warning | Don't lock the date on empty desired set; log warning + alert when `written==0` |
| Overly permissive CORS (`allow_methods/headers=*`, regex origin) *(downgraded high→med)* | `main.py:342-349`; `docker-compose.yml` `BACKEND_CORS_ORIGIN_REGEX=^https?://...` | Wildcard methods/headers are hygiene; the regex accepting any HTTPS origin is the real issue and compounds B1 | Restrict methods to GET/POST/PUT/DELETE, headers to explicit set; replace regex with an allow-list in prod |
| Bind-mount restore failures silently suppressed *(downgraded high→med)* | `.github/workflows/deploy.yml:204-215` | `cp … 2>/dev/null || true` + unconditional `echo restored` hides partial restore of gitignored strategy-state JSON; prints success on failure | Don't suppress restore errors; fail/alert on non-zero; move durable state to DB |
| Options margin = 100% premium, charged at entry price | `paper_engine/portfolio.py:314-330,445` | Capital not released on favorable option moves (reserved_margin uses entry, not mark) — stalls new entries; UI-only (no hard loss) | Use conservative SPAN % (≈20% opt / 12% fut); read current mark in `reserved_margin()` |
| Cash-mode only blocks new BUY, not SELL | `live_engine/risk_manager.py:164-166` | In a ≥70% dead-zone, short entries still pass the gate | Drop the `action=="BUY"` filter — block all new entries in cash mode |
| Daily-loss tracking trusts upstream P&L sign | `live_engine/risk_manager.py:209-216` | If a winning short ever arrives with a wrong sign, daily-loss budget is consumed on a gain (no current sign bug found, but no invariant guard) | Assert signed P&L semantics at `on_trade_close()`; store signed + abs separately |
| `live_candle_store` TZ floor can mix naive/UTC buckets | `market_data/live_candle_store.py:120-173` | A naive (IST) broker tick floors in local context → candle boundaries shift vs RTH masks | Assert `tzinfo is not None` post-normalize; force UTC before `floor_timestamp` |
| Phantom-expiry gate covers options only; spot ticks unguarded | `market_data/live_candle_store.py:291-301` | Corrupt index instrument_key on the spot path lands unvalidated | Validate `DISPLAY_NAMES[symbol] == resolved underlying` for index spots; reject mismatch |
| OAuth state is symmetric-encrypted, not random/session-bound | `api/routers/auth.py:1323-1338` | No per-request nonce/session binding; forgeable if SECRET_KEY leaks (see H7) | Use uuid4 state stored server-side (Redis, 5-min TTL), verify+delete on callback |
| Wildcard `postMessage('*')` in OAuth callbacks | `api/routers/auth.py:1742,1832` | Connected-status message readable by any opener window | Use explicit `window.location.origin`; add `X-Frame-Options: SAMEORIGIN` |
| No CSRF protection on state-changing endpoints | `api/routers/trading.py`, `auth.py` | Compromised CORS-whitelisted origin can drive orders | SameSite=Strict cookies or double-submit CSRF token on POST/PUT/DELETE |
| WS token expiry not revalidated on reconnect | `frontend-v2/src/lib/websocket.ts:41-72,114-148` | Stale token reused on reconnect → ~30s stuck-reconnect, no quotes | Clear cached token on auth-fail; revalidate with a tighter margin |
| `ws_positions_overview` mutates cached structure in place | `api/websockets/ticks.py:456-481` | Overlay applied twice/compounds → flickering/stale P&L in UI | Deep-copy structure before `_overlay_positions_overview()` |
| `quote_bus` publish assumes str; no failure circuit-breaker | `market_data/quote_bus.py:140-148` | Redis-client version mismatch → silent total quote outage; `_pending` grows unbounded on long Redis outage | Encode bytes as needed; circuit-break + drop `_pending` after N failures |
| Migration uses f-string interpolation (pattern risk only) *(verifier refuted as exploitable)* | `db/migrations/versions/010_underlying_lot_size.py:38` | Values are static literals — not exploitable, but bad pattern | Parameterize for hygiene |

---

## 4. LOW / Hygiene

- `StrategyPosition.return_pct` returns 0.0 silently on `entry_price<=0` — log a warning so malformed positions surface (`strategy_agent_state.py:59-62`).
- Order-adoption second-sight returns early without refreshing fill data (`order_manager.py:299-344`) — update on change, not silent return.
- `guard_ohlc` fixed 20% band, not symbol-aware — mild contamination within ±20% of session median passes (`analysis/safe_candles.py:25-66`).
- `chain_candle_builder` passes empty `already_in_db` — redundant insert attempts (harmless under DO NOTHING; moot once H4 lands) (`chain_candle_builder.py:243-273`).
- `self._latest_spot` only populated for `DISPLAY_NAMES` indices → commodity/non-index option `underlying_price` is None (`live_candle_store.py:174-175`).
- `quote_bus._latest` never pruned — unbounded but negligible (~30–300 KB); verifier refuted the crash claim (`list(dict.values())` is GIL-atomic). Add TTL eviction for hygiene.
- `useDepth` doesn't clear `book` on socket close → stale ladder for a frame on fast symbol switch (`useDepth.ts:29-45`).
- QuoteGrid flashes green/red on coalesced quotes identically to live — add a `·c` coalesced indicator (`QuoteGrid.tsx:53`).
- Broker token suffix logged at INFO (`auth.py:1800,1170,2048`) — log success/failure only.
- Missing a11y attributes on SVG charts/tables/canvas (SectorNetwork, MarketProfileChart, CandleChart, tables) — `role="img"`/`aria-label`/`scope="col"`.
- Missing error/loading states in `UniverseWatchlist`; missing inline error boundaries around chart components.

---

## 5. Refuted / False Positives (killed by the verification pass)

- **P&L sign-inversion theme (4 findings — `strategy_agent_state.py`, `live_marks.py`, `commodity_strategy_agent.py`, `strategy_agent.py` exit logic):** REFUTED. `StrategyPosition` has **no `action` field**; the NSE lane is **long-only** (PE bets are long puts, never short calls); commodity P&L uses `CommodityPositionState` which **does** apply `mult = 1 if BUY else -1` (test-verified); the Strategy-2 exit code is **dead** (deleted per user instruction). The `force_long=True` overlay is architecturally guaranteed for NSE; commodities route separately with the correct `side_field`.
- **SQL injection in Strategy API (`strategy.py:239`):** REFUTED. `table` is sourced only from a hardcoded tuple; the endpoint takes no user params.
- **SQL injection in migration 010:** REFUTED as exploitable — values are static literals (kept as a low hygiene note).
- **`.env` committed / secrets in git history:** REFUTED. `.env` is gitignored from the first commit; `git log --all -- .env` is empty; not in remote; `.env` values are dev placeholders. Real creds live in Fernet-encrypted `credentials.json` (also gitignored) + DB. *(The encryption-key weakness is real — see H7 — but the "credentials in git" claim is false.)*
- **`APP_WRITE_TOKEN` / weak-SECRET-KEY → order forgery:** REFUTED. No JWT in the codebase; writes are gated by `APP_WRITE_TOKEN` (a proper random value), not SECRET_KEY. SECRET_KEY impact is credential disclosure (H7), and the token is inert while auth is disabled (B1 is the real exposure).
- **DB pool "exhaustion unfixed":** REFUTED as framed — the 3→8/8 bump already landed; residual tightness retained as H9.
- **`quote_bus.snapshot_frame()` concurrent-modification crash:** REFUTED — `list(dict.values())` is GIL-atomic; unbounded growth is negligible (low hygiene note).
- **`_close_pubsub` missing on listen()-loop exception path (ws_quotes/ws_depth):** REFUTED — nested try/finally guarantees `_close_pubsub` runs and `aclose()` releases the conn before the generator's finally; `_close_pubsub` can't raise. *(The Redis-down early-return path B2/B3 is the real leak.)*
- **Frontend RAF lifecycle leak (`useQuoteStore`):** REFUTED — single-threaded JS; `stopRaf()` cancels synchronously; listeners deleted before any orphaned tick; Set iteration is delete-safe.
- **Portfolio P&L tests "missing":** REFUTED — 49 P&L/portfolio/trade tests exist incl. short-position sign, IST bucketing, charges (dropped from H11).
- **CORS as a high:** DOWNGRADED to medium — wildcard methods/headers are hygiene; the regex origin is the real (medium) issue; root cause is B1.

---

## 6. Prioritized Remediation Plan

Weighed against: t3.medium 3.7 GiB ceiling, Postgres `max_connections=25`, bind-mount edits are ephemeral (recreate reverts), no heavy-exec in prod container, **sole Fyers live lane**.

### NOW (today — money/exposure; all low-RAM, source-only changes that must be baked into the image, not just bind-mount pushed)
1. **[infra]** Set `APP_TOKEN_AUTH_ENABLED=true` + rotate `APP_WRITE_TOKEN` in prod `.env`; restart backend. **(B1, H6 — single biggest risk.)**
2. **[backend]** Add `_close_pubsub` on the Redis-down early-return in `ws_quotes`, `ws_depth`, `ws_ticks`; wrap `ws_proposals` init. **(B2, B3 — quote-feed survival.)**
3. **[backend]** Cancel/await the 3 lifespan tasks on shutdown. **(B4 — restart safety + conn leak.)**
4. **[infra]** Generate a strong `SECRET_KEY`, fail-fast if it contains `change-me`/`dev`; rotate + re-encrypt stored credentials. **(H7.)**
5. **[backend]** Add startup validation: refuse to boot in `production` with auth disabled or default SECRET_KEY. **(B1+H7 guardrail.)**

> Commit all NOW items to the repo and **rebuild the image** — bind-mount-only pushes revert on the next OOM/recreate (documented hazard). These are tiny diffs; no backtest/heavy-exec involved.

### NEXT (this week — silent reliability/data-integrity)
6. **[backend]** Real `/health` (cached DB/Redis/broker checks, 503 on failure). **(H1.)**
7. **[backend]** Arm watchdog at startup; cut `_required_tick_stale_seconds` to 20–25; make stale required-index → `critical` so the gate blocks; alert eagerly. **(H2, H3.)**
8. **[backend]** `option_premium_candles` → source-aware `ON CONFLICT DO UPDATE`. **(H4 — fixes S1 MACD data quality.)**
9. **[backend]** Wire `record_audit_event(error)` + Telegram on strategy-loop crash, order reject, pool/Redis saturation; `TELEGRAM_EVENT_MIN_SEVERITY=error`; add heartbeat for option/commodity loops. **(H5, H8.)**
10. **[infra]** Deploy preflight (`pytest --co` + alembic dry-run); make health probe deploy-killing with auto-revert; keep last 2 images; Telegram on deploy fail. **(H10.)**
11. **[frontend]** Remove/parameterize `localhost:3000` v1 links; add `spot_age_seconds` freshness to `UniverseWatchlist`. **(H12, H13.)** *(Frontend is image-baked — rebuild + `next build`.)*

### LATER (hardening, lower urgency)
12. **[backend]** Order-manager reconcile + e2e + data-quality-gate + NSE expiry-rollover + quote_bus tests. **(H11.)**
13. **[backend]** Depth ref-count re-arm fix; option/commodity empty-lock guard; TZ floor assertion; spot phantom-expiry gate; cash-mode block-all; margin SPAN%. **(MEDIUMs.)**
14. **[backend/infra]** CORS allow-list (drop regex), CSRF, OAuth random state, postMessage origin, stop logging token suffixes. **(MEDIUMs — partly mooted once B1 holds.)**
15. **[infra]** Move durable strategy state to DB (kill bind-mount-restore fragility); medium-term creds → AWS Secrets Manager/KMS. Defer Prometheus/Grafana — RAM-constrained; Telegram alerting covers the gap for now.

---

## 7. Coverage Gaps in THIS Audit (honest limits of a static read)

- **No live-market validation.** The SPOF stale-feed window (H2), watchdog timing, and the option/commodity silent-lock paths (H5/medium) were reasoned from code, not observed during an actual Fyers WS drop. Real reconnect behavior and the data-quality gate's true block/unblock latency need a market-hours observation or a fault-injection harness.
- **No load/concurrency test.** The DB-pool tightness (H9, 16 vs 25), Redis-leak exhaustion rate (B2/B3), and morning-open scan contention are quantified by reasoning only. A controlled load test (all lanes scanning + WS reconnect churn) is required to confirm the breaking point and the t3.medium RAM headroom. **Do not run this against prod** (heavy-exec OOM hazard) — use a local stack or off-prod sidecar.
- **No security pentest.** B1's exploitability is confirmed by code, but I did **not** probe the live prod URL — whether an AWS SG/ALB or WAF sits in front of `15.206.56.206` is unknown and could partially mitigate (or not). The CORS regex, CSRF, and OAuth-state findings need an active test from an external origin to confirm real reachability.
- **Prod config drift unverifiable statically.** Whether `APP_TOKEN_AUTH_ENABLED`, `SECRET_KEY`, and pool sizes are actually overridden in the *running* prod `.env` (vs. the repo defaults) can only be confirmed by inspecting the live environment. This audit assumes worst-case (defaults in effect).
- **Frontend runtime behavior.** The stale-data-as-live (H13), RAF/socket lifecycle (refuted), and QuoteGrid coalesced-flash issues were read, not run. Confirming the actual rendered freshness and any ghost-render leaks needs the app open in a browser under a live tick stream.
- **Migration/DB state.** The `underlying_spot_candles` contamination and `option_premium_candles` shadowing are inferred from write/read code; the *current magnitude* of contaminated/duplicate rows in the live TimescaleDB was not measured (would need a read-only DB probe).
- **No git-history secret scan beyond `.env`.** I relied on the verifier's `git log` checks for `.env`; a full historical secret scan (e.g. trufflehog across all blobs) was not performed.