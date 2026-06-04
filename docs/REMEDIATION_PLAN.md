# Nomad Curie — Platform Remediation Plan

> Consolidates the platform audit (architecture, data plumbing, execution/OMS, reliability,
> robustness, frontend, infra/observability) and the latency audit into one sequenced,
> trackable roadmap. **Strategy edge is explicitly out of scope** — this plan is about the
> machine that runs the strategies, not the strategies.

**Status:** Draft v1 · **Owner:** _you_ · **Last updated:** 2026-06-04

---

## 0. Reading guide

- **Sequenced by capital-risk, not by difficulty.** A money system survives on data integrity,
  deterministic execution, and the ability to see + survive failure — in that order.
- Phases **P0 → P4**. Each phase has **workstreams (WS-x.y)** → **tasks**. Each task carries:
  *what · why · where (files) · acceptance criteria · effort*.
- **Effort:** S = ≤1 day · M = 2–4 days · L = 1–2 weeks · XL = >2 weeks (solo-operator pace).
- Do **P0 + the P1 instrumentation** before flipping any lane to live size. P3/P4 are
  value-add, not safety.

### Golden rules (hard-won — violating these reverts your work or takes prod down)
1. **Never run heavy app-context Python inside the prod `nomadcurie_backend` container.** It
   OOMs (rc 137) → container recreate → `/app` bind-mount re-syncs from image → **silent revert**
   of edited files → leaked DB connections → pool exhaustion (cap = 25). Run heavy work in an
   **isolated sidecar** (the proven `sniper-shadow` pattern) or locally off DB-pulled data.
2. **Bind-mount edits are ephemeral.** Permanent deploy = **commit to repo + rebuild image**.
   Push-via-SSM + `docker restart` is for hotfixes only and survives only until the next recreate.
3. **After pushing code, clear `__pycache__`** before restart, or stale `.pyc` shadows new source.
4. **DB connection cap is 25.** Anything that opens many concurrent connections (or leaks them)
   saturates the pool. Right-size and monitor before adding load.

---

## P0 — Stop the bleeding · capital safety + visibility  *(~1–2 weeks)*

Cheap, safe, high-leverage. Mostly no strategy-logic changes. **This is the must-do block.**

### WS-0.1 — Market-data integrity gate
The scariest finding: garbage candles → wrong signals, invisibly. Today there is **zero validation
at ingest**; every consumer (Sniper, Gann) re-implements its own ±20%-median guard.

| Task | Detail |
|---|---|
| **0.1a Ingest validation** | Add a validation function in the candle write path ([`live_candle_store.py`](../backend/market_data/live_candle_store.py) `_flush_pending`/`_persist_*`): reject rows failing OHLC ordering (`low ≤ open,close ≤ high`), non-positive price, negative volume, symbol ∉ `fo_contract_catalog`, or price deviating >X% from session-median. **Count rejects** (metric). Effort: **M** |
| **0.1b Purge known-bad rows** | Run the `DELETE` you already scoped for the `local_csv_spot` (SENSEX values mislabeled NIFTY) and `live_tick` (BANKNIFTY price under NIFTY symbol) contamination in `underlying_spot_candles`. Effort: **S** |
| **0.1c Source-side dedup** | The ~44% duplicate-timestamp issue: enforce dedup (keep=last) at write via the existing unique constraint + `ON CONFLICT`; verify the constraint covers `(instrument_key, interval, time)`. Effort: **S** |
| **0.1d Retire scattered guards** | Once the gate is trusted, remove the per-consumer ±20% guards so cleaning lives in one place. Effort: **S** |

**Acceptance:** 0 cross-symbol-contaminated rows in a fresh scan; `ingest_rejected_total` metric exposed;
legitimate-row reject rate <0.1% (tune threshold against a clean week).

### WS-0.2 — Instrumentation (the unlock for everything else)
You cannot manage what you don't measure. **No latency or health metrics exist today.** This is
pure observation — no behavior change, safe to ship immediately, and it validates every later fix.

| Task | Detail |
|---|---|
| **0.2a `/metrics` endpoint** | Add `prometheus-client`; expose counters/histograms. Effort: **S** |
| **0.2b Hop timestamps** | Stamp `broker_ts → ingest_ts → publish_ts` (tick-age), `scan_start/scan_end` per lane, `order_send→ack` RTT, `decision_ts→fill_confirmed_ts`. Effort: **M** |
| **0.2c Event-loop lag monitor** | Schedule a 100ms heartbeat; record actual-vs-expected drift as a histogram. This directly exposes the data-plane/compute-plane contention (latency #1). Effort: **S** |
| **0.2d Structured logging** | Add request/trace IDs to `loguru` sink so async flows are correlatable. Effort: **S** |

**Acceptance:** Grafana/any scraper shows p50/p95/p99 for tick-age, scan-duration, order-RTT, loop-lag.

### WS-0.3 — Alerting (stop flying blind)
`TELEGRAM_BOT_TOKEN` is configured but **unused**. Wire it.

- **0.3a** Push alerts on: feed stale > budget, reconciliation break, DB pool > 80%, runner error,
  **OOM/container-recreate detected** (watch `Created` timestamp / restart count), token-expiry approaching.
  Respect `TELEGRAM_RATE_LIMIT_PER_MINUTE = 12`. Effort: **M**
- **Acceptance:** killing the feed in a test fires a Telegram alert in <60s.

### WS-0.4 — Execution determinism & safety
Only strictly required before live size, but the footguns are cheap to close now.

| Task | Detail |
|---|---|
| **0.4a `client_order_id` + idempotent retry** | Add `client_order_id` to `OrderRequest`/`OrderResponse` ([`brokers/base.py`](../backend/brokers/base.py)); dedupe on it (not the 5s symbol+action window in [`order_manager.py`](../backend/live_engine/order_manager.py)). Effort: **M** |
| **0.4b Bounded order timeout + retry** | Replace the 10–15s `httpx` timeout / no-retry with 2–3s timeout + bounded retry w/ backoff, keyed by `client_order_id` so retries can't double-fill. Effort: **M** |
| **0.4c Fix or disable 5Paisa** | [`fivepaisa.py`](../backend/brokers/fivepaisa.py) hardcodes `ScripCode=0` (always rejects) and `modify_order` is a stub. Implement symbol→ScripCode mapping **or** remove it from the selectable broker list so it can't be chosen. Effort: **S–M** |

**Acceptance:** a forced network timeout during placement results in **exactly one** broker order (verified in trade book).

### WS-0.5 — Reliability quick fixes (anti-deadlock)
- **0.5a** Per-runner `asyncio.timeout()` for the directional/FMP/market-intel runners in
  [`market_hours_paper_supervisor.py`](../backend/core/market_hours_paper_supervisor.py) (CBE/Gann already have them) — one hung call must not stall the `gather`. Effort: **S**
- **0.5b** Timeout on `/api/system/health` per-check (explains the 4–8s health latency: the broker
  network check dominates behind a 5s cache with no per-check bound). Return cached on slow check. Effort: **S**
- **0.5c** Startup `lifespan` timeout wrapper so a hanging agent start can't block the process from
  becoming healthy ([`main.py`](../backend/main.py) lines ~119–142). Effort: **S**

### WS-0.6 — Housekeeping
- **0.6a** Delete `diag.py`/`diag2.py`/`diag3.py`/`*_probe.py`/`tok_test.py` from `backend/` root (131 lines of scaffolding) or move to `scripts/`. Effort: **S**

---

## P1 — See + survive · the nervous & immune system  *(~2–4 weeks)*

### WS-1.1 — Bulkhead the data plane from the compute plane  ⭐ highest-impact
**The single most important fix.** Tick ingest, Redis publish, WS push, and all heavy analytics
share one event loop in one process — so a fat scan **freezes live marks and every WS client** for
its duration (supervisor timeouts of 120–180s prove scans can run that long), and is the root of
the OOM cascade.

| Task | Detail |
|---|---|
| **1.1a Offload CPU-bound work** | Force all analytics/feature/MP/RL/backtest compute into `run_in_executor`/`ProcessPoolExecutor` so it never blocks the loop. Effort: **M** |
| **1.1b Isolate the data plane** | Move tick-ingest + publish (and ideally each heavy lane) into a **separate worker/sidecar** — reuse the proven `sniper-shadow` isolation pattern (own container, `/opt`-mounted, reads DB directly, zero prod-backend load). Effort: **L** |

**Acceptance (SLO):** loop-lag p99 < 50ms even during the heaviest scan; tick-age p95 unchanged while a backtest runs.

### WS-1.2 — Active reconciliation + event-driven fills
Replace the **passive 30s reconcile poll** ([`order_manager.py`](../backend/live_engine/order_manager.py) `RECONCILE_INTERVAL = 30`) that only logs mismatches.

- **1.2a** On mismatch: adopt broker-only orders, correct internal state, **alert** on breaks. Effort: **M**
- **1.2b** Add partial-fill modeling (qty+price array, not a scalar `fill_price`). Effort: **M**
- **1.2c** Subscribe to broker **order postback/webhook** for event-driven fill confirmation (drops fill-confirm latency from ≤30s to ~real-time). Effort: **M**

**Acceptance:** crash-after-placement test recovers the true position on restart; fill-confirm p95 < 2s.

### WS-1.3 — Feed resilience
- **1.3a** WS **heartbeat ping**; shrink `_required_tick_stale_seconds` from **90s → ~10–15s** for liquid indices in RTH ([`data_router.py`](../backend/market_data/data_router.py)). Effort: **S**
- **1.3b** Exponential backoff **+ jitter** on reconnect (replace fixed 60s). Effort: **S**
- **1.3c** Redis **reconnect loop** (today a Redis outage degrades to 1Hz polling permanently until manual restart). Effort: **S**

**Acceptance:** dead-feed detection < 15s; simulated Redis bounce auto-recovers pub/sub.

### WS-1.4 — Paper fidelity (makes research transfer honestly)
Paper P&L overstates live because [`order_book.py`](../backend/paper_engine/order_book.py) models only a flat 5bps slippage.

- **1.4a** Add full cost model: brokerage, **STT**, exchange txn charges, SEBI fee, GST, stamp duty,
  + realistic slippage and intraday **theta** decay between ticks. Effort: **M**
- **Acceptance:** paper vs live P&L on a replayed day diverge < 5%.

### WS-1.5 — Connection pooling & DB headroom
- **1.5a** Redis **`ConnectionPool`** (today a single shared connection serializes pub/sub + cache + cross-process reads — [`redis_client.py`](../backend/db/redis_client.py)). Effort: **S**
- **1.5b** Right-size DB pool, **raise `max_connections`** above 25, fix the leak-on-OOM path; add pool-saturation metric. Effort: **S–M**
- **1.5c** Add a **read replica** for analytics so scans don't compete with the data plane for connections. Effort: **M** (infra)

---

## P2 — Durability & DR · survive the box dying  *(~1–2 weeks)*

### WS-2.1 — Backups & recovery
- **2.1a** Move runtime-state backups **off `/tmp`** (ephemeral tmpfs — lost on reboot) **to S3**. Effort: **S**
- **2.1b** Enable **DB PITR / automated snapshots**; document **RTO/RPO** and test a restore. Effort: **M**

**Acceptance:** RPO ≤ 1h, RTO ≤ 1h, restore rehearsed end-to-end.

### WS-2.2 — Kill the persistence footguns
- **2.2a** Move `runtime/` + `credentials.json` onto a **named volume**, off the `/app` bind mount, so container recreates stop reverting state. Effort: **M**
- **2.2b** Establish "permanent = baked into image" as the deploy norm (commit + rebuild), reserving SSM-push for hotfixes. Document it. Effort: **S**

### WS-2.3 — Secrets
- **2.3a** Move secrets to **AWS SSM Parameter Store / Secrets Manager**; remove plaintext `.env` from the box; add **rotation**. (`.env` is correctly gitignored and was never committed — this is about at-rest plaintext + rotation, not a leak.) Effort: **M**

### WS-2.4 — Environments & safe deploys
- **2.4a** Add a **staging environment** (today main → prod with no gate). Effort: **M**
- **2.4b** Add **resource limits** (mem/cpu) to compose services so one runaway can't OOM the host. Effort: **S**
- **2.4c** Add **migration locking** so backend + research-sync can't run `alembic upgrade` concurrently. Effort: **S**

### WS-2.5 — Infrastructure as code
- **2.5a** Terraform the EC2 box + SG + volumes + IAM (currently hand-built, non-reproducible). Effort: **L**

---

## P3 — Latency & execution maturity  *(~2–3 weeks; after P0/P1 instrumentation proves the numbers)*

### WS-3.1 — Event-driven hot lanes
- **3.1a** Trigger the order-flow / auction-intelligence lanes on **bar-close events** instead of the fixed **180s** poll (`AUCTION_INTELLIGENCE_AUTO_INTERVAL_SECONDS`); drop the 15s supervisor floor for reactive lanes. Effort: **M**

### WS-3.2 — Execution quality
- **3.2a** Pre-trade **spread/liquidity check** + limit-vs-market logic; optional order **slicing** for size. Effort: **M–L**

### WS-3.3 — Real market depth
- **3.3a** Capture **MBP-10 / order-book depth** (where the broker allows) to feed the order-flow lanes that are currently starved on top-of-book + tick-time buy/sell totals. Effort: **L**

### WS-3.4 — Transaction Cost Analysis
- **3.4a** TCA module: arrival-price slippage, fill quality, by-lane — the live-vs-paper truth meter. Effort: **M**

### WS-3.5 — Misc latency trims
- **3.5a** SPAN/margin pre-trade check (today margin is estimated). **3.5b** Candle flush → 1s for the handful of hot symbols (from 5s). **3.5c** Cap the aggressive frontend polls (`refetchInterval: 5000` on the analysis page). Effort: **S** each.

---

## P4 — Terminal-grade features & architecture cleanup  *(backlog / ongoing)*

- **WS-4.1** Finish the **v1→v2 frontend migration** and retire v1 — stop paying double maintenance for one product. Effort: **L**
- **WS-4.2** Order/exec **blotter + trade ticket + manual override + alerts center** UI (today read-mostly console + kill-switch). Effort: **L**
- **WS-4.3** Charting → **`lightweight-charts`**; table **virtualization**; multi-pane workspace. Effort: **L**
- **WS-4.4** Refactor god-modules (`strategy_agent.py` 5.2k lines → entry/exit/lifecycle; `atm_watchlist.py` 3k); add **API versioning**. Effort: **L**
- **WS-4.5** **Multi-account / portfolio-level risk**; **immutable order audit log** (compliance). Effort: **L**

---

## Quick-win sprint (first ~5 days — do these now)

1. **0.2a–c** Instrumentation: `/metrics` + loop-lag monitor + hop timestamps (safe, unlocks everything).
2. **0.3a** Telegram alerts on feed-stale / reconcile-break / OOM-recreate.
3. **0.1a–b** Ingest validation gate + purge known-bad rows.
4. **0.4a** `client_order_id` idempotency.
5. **0.5a–c** Per-runner + health + startup timeouts.
6. **0.6a** Delete diag/probe scaffolding.

All are S/M, low-risk, and high-leverage. Most are pure additions (instrumentation, validation,
timeouts) that don't touch strategy logic.

---

## Definition of done — target SLOs

| Dimension | Target |
|---|---|
| Data integrity | 0 contaminated rows; legit-reject < 0.1%; reject metric live |
| Loop-lag | p99 < 50ms during heaviest scan |
| Tick-age | p95 within broker feed budget; alert on breach |
| Order RTT | p95 < 1s; **exactly-once** placement under retry |
| Fill confirmation | p95 < 2s (event-driven, not 30s poll) |
| Feed-death detection | < 15s |
| Alert MTTR (detection→notify) | < 60s |
| DR | RPO ≤ 1h, RTO ≤ 1h, restore tested |
| Paper vs live P&L | divergence < 5% |

---

## Effort summary

| Phase | Theme | Rough effort | Gate before live size? |
|---|---|---|---|
| P0 | Capital safety + visibility | 1–2 wks | **Yes (all)** |
| P1 | See + survive | 2–4 wks | **Yes (1.1 bulkhead, 1.2 reconciliation, 1.4 paper costs)** |
| P2 | Durability & DR | 1–2 wks | Strongly recommended |
| P3 | Latency & execution maturity | 2–3 wks | Value-add |
| P4 | Terminal features & cleanup | ongoing | Value-add |

---

## Notes & corrections carried from the audits
- **Migration "006 collision" is a non-issue** — the two `006_*` files have distinct Alembic
  `revision` ids forming a proper chain (`006_analytics_tables_plain_pg` → `006_validation_runs`).
  Alembic keys on the revision string, not the filename prefix. Not a deploy blocker.
- **Secrets are not leaked to git** — `.env` is gitignored and never committed. The P2 secrets work
  is about at-rest plaintext on the box + rotation, not a leak.
- This is a **retail positional/swing F&O system on broker REST/WS** — not HFT. The network is not
  the bottleneck; **compute blocking the data plane (WS-1.1)** and the **unsafe/slow order
  round-trip (WS-0.4/1.2)** are. Tune to that yardstick.
