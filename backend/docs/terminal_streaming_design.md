# Implementation status (2026-06-07)

**BUILT + build-verified (tsc 0 errors · py_compile · coalescer unit test · WS wiring smoke).
NOT deployed (FE image-baked, BE bind-mount); live-verify at next market open.**

Phase 0/1 [backend, DONE]: `market_data/quote_bus.py` (150ms last-write-wins coalescer, taps
`data_router.register_global_callback`, publishes compact frames to Redis `quotes:bus`) +
`/ws/quotes` event-driven endpoint (`api/websockets/ticks.py::ws_quotes`, `listen()` pattern — NO
asyncio.sleep floor; snapshot-replay on connect; `_close_pubsub` leak guard) + started in `main.py`
lifespan + route registered. Unit test: 4 ticks → 1 frame, last-write-wins, `c:1` flag, index field-omit.
Phase 3 [backend, DONE]: `ws_positions_overview` refactored event-driven — structure gather on a 2s
heartbeat (DB load unchanged) split from a cheap live-mark overlay re-applied within <=0.4s of ANY tick
(subscribes `quotes:bus`); identical frames de-duped (ignoring fetchedAt); degrades to a 2s timer if
Redis is down. Open-position P&L now sub-second instead of 2s.
Phase 4 [backend+frontend, DONE — plumbing; depth VALUES need a live market]: Fyers `_handle_message`
routes DepthUpdate vs SymbolUpdate on one socket; `_handle_depth` parses 5-level ladder; `data_router`
`_on_depth`/`_publish_depth` (Redis `depth:{symbol}`) + ref-counted `subscribe_depth`/`unsubscribe_depth`
(incremental on the live client, re-armed on resubscribe); `/ws/depth/{symbol:path}` endpoint
(listen()-driven). Frontend: `createDepthSocket`, `useDepth`, `<DepthLadder>` (5-level, bar-width ∝ size).
UI [frontend, DONE]: `<TerminalPanel>` = auto-populating `<QuoteGrid>` (from `useKnownSymbols` — no
hardcoded symbol keys) + click-to-focus `<DepthLadder>` + `<QuoteConnectionBadge>`. Wired as a
**"Terminal" tab on the NSE desk** (2nd tab; Positions stays default).
Phase 2 [frontend, DONE]: `lib/websocket.ts::createQuotesSocket`; `hooks/useQuoteStore.ts` (module store
OUTSIDE React + single shared socket + rAF drain + `useQuote(symbol)` via useSyncExternalStore =
cell-isolated re-render + `useQuotesConnection`); `hooks/usePriceFlash.ts`; `components/terminal/`
QuoteGrid (per-row symbol subscription + green/red flash) + LiveMarkBadge/QuoteConnectionBadge (honesty).

TO-BUILD: Phase 5 FyersOrderSocket live fills (needs live broker + a real fill); Phase 6 TBT 50-level
(GATED on confirming paid entitlement). Phases 1-4 + Terminal UI DONE (build-verified). LIVE-VERIFY at
next market open: tape latency, depth values flow for tradables (indices have no depth), sub-second P&L.

---

# Nomad Curie — Terminal-Grade Low-Latency Streaming Design

Status: design / lead-architect proposal. Grounds every recommendation in the three investigations + verified source (`api/websockets/ticks.py`, `market_data/data_router.py`, `brokers/fyers.py`, `main.py`). Scope is the **live lane** (Fyers v3 WS), not the historical/candle path.

---

## 1. Target — what "terminal-grade" means here

A focused live cockpit for the **index + ATM-option + held-leg universe** (~5 indices + ~30 ATM contracts + held legs = ~50–100 symbols, well under Fyers' 5000/key cap), delivering:

- **Per-tick, event-driven push.** A price change leaves the Fyers socket and lands in the browser DOM in **<200–500ms glass-to-glass** (p95). No `asyncio.sleep` floor in the live path. The current `ws_ticks` 1.0s `get_message` timeout and the 2–15s `_stream_snapshot` cadence are *replaced* for live data, not tuned.
- **Live quote grid** — multi-symbol table, one row per symbol, cell-isolated updates (LTP / Δ / Δ% / bid / ask / spread / vol / OI), no full-table re-render on each tick.
- **Price flash** — green/red background pulse on LTP change, auto-fade ~150ms, direction-aware. Currently entirely **MISSING** (frontend investigation: commodity row renders static text + a "live" dot only).
- **Depth ladder (DOM)** — 5-level bid/ask × (price, size, order-count) side panel per selected symbol. Currently **NOT IMPLEMENTED** (no bid/ask in `TickPayload`).
- **Live position marks** — open-position P&L re-marks on each leg tick, not on the 2s `positions-overview` / 60s agent scan cadence. The hot-cache + `get_live_mark` plumbing already exists (`data_router.py:454`); we wire it to push.
- **Honesty** — a coalesced frame is labeled as such (`coalesced:true`, `as_of` ts). A stale feed shows stale, never a frozen tick masquerading as live (the `get_live_mark` `max_age_seconds` guard already enforces this; UI must surface it).

Explicit **non-goals for v1**: the 50-level TBT/DOM socket (paid entitlement, unconfirmed — see §2), full L2 order-flow on the desk, MCX (no Fyers WS for MCX; stays on the 12s poll).

---

## 2. Fyers v3 WS usage

Three sockets exist (research area `fyers-v3-ws`). We use exactly what each job needs.

### 2.1 Quote tape — `FyersDataSocket`, `SymbolUpdate`, `litemode=False` (KEEP)
- **Mode:** `subscribe(symbols=[...], data_type="SymbolUpdate")` — current call at `brokers/fyers.py:505`. **Do not switch to litemode.** Litemode is LTP+symbol+type only (3 fields); the terminal needs `bid_price/ask_price/bid_size/ask_size/tot_buy_qty/tot_sell_qty/OI/vol/OHLC` — all of which `SymbolUpdate` (`data_val`, 23 fields) carries as **singular top-of-book**. `brokers/fyers.py:519` already reads these correct singular keys.
- **Top-of-book is free in SymbolUpdate.** This gives us bid/ask/spread for the quote grid and a *1-level* "mini ladder" with **zero new subscription**. Note: litemode and depth are mutually exclusive on this socket — you cannot get LTP-lite *and* depth in one sub, so full mode is the only viable choice for a terminal.
- **Indices carry no depth/vol/OI** (`INDEX_DEPTH_ERROR_MESSAGE`); index rows show LTP/OHLC only. UI must not render empty bid/ask cells for index rows as "0".

### 2.2 5-level depth ladder — `FyersDataSocket`, `DepthUpdate` (ADD, per-symbol on demand)
- For the selected symbol's full DOM panel, open a **second subscription** in `DepthUpdate` mode → `bid_price1..5 / ask_price1..5 / bid_size1..5 / ask_size1..5 / bid_order1..5 / ask_order1..5` (32 fields, `map.json depthvalue`). 5 levels × (price, size, order-count) per side.
- **Subscribe only the focused symbol(s)** (the one(s) the trader has a ladder open for), not the whole universe — depth frames are ~6× the field count of a quote and we don't need a ladder on 100 symbols at once.
- Indices are rejected for depth — guard before subscribing.

### 2.3 Live fills — `FyersOrderSocket` (ADD, replaces REST poll)
- `order_ws` at `wss://socket.fyers.in/trade/v3`, JSON, `subscribe("OnOrders,OnTrades,OnPositions,OnGeneral")`. Today `brokers/fyers.py` polls `/orders`, `/positions`, `/tradebook` via REST. Wiring the order socket gives **sub-second fill/position updates** and removes that REST burden (relevant to live mode only; paper mode unaffected).
- This is the cleanest single-source-of-truth for "a position just changed" → the event that should invalidate the positions payload (see §3.4).

### 2.4 Subscription sharding within the 5000/key cap
- Cap is **5000 unique symbols per API key** (server- AND client-enforced; `data_ws.py self.symbol_limit=5000`), not the obsolete "50/connection". Our ~100-symbol universe is trivially within it.
- Use the SDK **channel** arg (`subscribe(..., channel=N)`, default 11; channels 1..30 pre-allocated) to **group** by purpose: e.g. channel 11 = indices, 12 = ATM options, 13 = held legs. This enables `channel_pause`/`channel_resume` (op `cp`/`cr`) to throttle a group without tearing down the socket — useful when the desk tab is backgrounded.
- The SDK auto-chunks large subscribes (1500/0.5s). At our size this never triggers. **For new intraday symbols, prefer `unsubscribe()` of stale legs over opening a parallel key** (Fyers' explicit guidance).

### 2.5 Reconnect / heartbeat (KEEP SDK behavior — do not reinvent)
- Keep `reconnect=True`. SDK does capped back-off (max 50 attempts, +5s every 5), **re-sends mode msg + replays subscriptions** on reconnect, and sends a **10s heartbeat ping itself** — do NOT add our own ping. `data_router.py` watchdog (30s, 45s-stale → reconnect) stays as the outer safety net.
- Token rotates at the 08:50 IST SEBI re-auth; socket must reconnect with the fresh `APP_ID:access_token`. `TOKEN_EXPIRED=-99` surfaces on the socket — handle it as a reconnect trigger.

### 2.6 UNCONFIRMED / deferred
- **50-level TBT (`FyersTbtSocket`, protobuf, `versova`)** — the genuine terminal DOM (50 levels + per-level order counts + tbq/tsq + sequence_no + snapshot/diff). We are NOT using it. It is a **paid TBT entitlement** — confirm entitlement before any build. **Deferred to a v2** behind the 5-level ladder.
- **Latency claims:** Fyers' primary source only documents **sub-50ms order execution** — that is *order*, not *market-data*, latency. The "TBT <10ms / 1000+ updates/sec" figure is a **third-party aggregator, unverified by Fyers**. Our <500ms budget (§5) is set conservatively and must be measured live, not assumed.
- **WS data latency itself is unquantified by any primary Fyers source** — treat §5 broker-hop numbers as estimates to validate at first market open.

---

## 3. Backend architecture — event-driven fan-out

**Core change:** the live path becomes **tick callback → Redis pub/sub → WS forwards on message arrival** (the `ws_proposals` `async for message in pubsub.listen()` pattern at `ticks.py:683`), NOT `asyncio.sleep`. The snapshot loops (`_stream_snapshot`) remain for genuinely slow channels (system-health, strategy-overview) but are removed from the price/positions live path.

### 3.1 Redis channels (the contract)

| Channel | Producer | Payload | Consumer |
|---|---|---|---|
| `ticks:{symbol}` | `data_router._publish_tick` (exists) | compact quote (below) | `/ws/quotes` fan-out |
| `quotes:bus` (NEW) | NEW coalescer task | array of changed quotes, one frame per coalesce window | `/ws/quotes` (multi-symbol) |
| `depth:{symbol}` (NEW) | NEW depth callback in `brokers/fyers.py` | 5-level ladder | `/ws/depth/{symbol}` |
| `positions:changed` (NEW) | order socket (`OnPositions/OnTrades`) + agent close | empty signal / position id | positions payload invalidation |

Keep the existing `tick:{symbol}` last-value hot-cache (`SET`, 300s TTL) untouched — late subscribers and cross-process marks depend on it (`data_router.py:439`, `get_live_mark`).

### 3.2 Compact tick payload

`_publish_tick` already emits a good shape (`data_router.py:420`). Tighten for frame size — short keys, drop nulls, numbers not strings:

```json
{"s":"NSE:NIFTY50-INDEX","p":23412.5,"b":23412.0,"a":23413.0,"bz":50,"az":75,
 "v":1234567,"oi":0,"t":1733556000123,"c":0}   // c=coalesced flag
```

Index rows omit `b/a/bz/az/oi`. Frontend maps short keys once. (Keep the verbose `tick:{symbol}` hot-cache shape as-is for backward compat with `get_live_mark` / snapshot fallbacks.)

### 3.3 The coalescer (frame-storm guard) — NEW `market_data/quote_bus.py`

A single asyncio task per process:
1. Subscribes to all `ticks:*` (or registers a `data_router` global callback — cheaper, no Redis hop; `data_router._global_callbacks` already exists, dispatched at `data_router.py:401`).
2. Accumulates **latest** tick per symbol into a dict (last-write-wins — we want the freshest value, not every intermediate).
3. Every **flush window (configurable, default 150ms)** publishes one `quotes:bus` frame = array of changed symbols since last flush, then clears.
4. If a symbol's value is unchanged since last flush, it is omitted.

This caps outbound frames at ~6–7/s regardless of tick rate, kills the re-render storm at the source, and keeps worst-case added latency = one flush window (150ms). **Coalesced frames carry `c:1` so the UI never presents a 150ms-batched frame as a raw tick** (honesty requirement §7).

> Rationale: pub/sub fan-out per raw tick to N browser sockets, each doing its own `send_text`, is the frame-storm vector. One coalesced multi-symbol frame fanned to N sockets is O(N) sends/window, not O(N×ticks).

### 3.4 New WS endpoints — `api/websockets/ticks.py` + `main.py`

- **`/ws/quotes`** (multi-symbol, NEW). Client sends a subscribe msg `{"symbols":[...]}` after connect; server forwards every `quotes:bus` frame filtered to the client's symbol set. Uses `async for message in pubsub.listen()` (the proposals pattern), **no `asyncio.sleep`, no `get_message` timeout**. On first subscribe, immediately replays the `tick:{symbol}` hot-cache for each requested symbol so the grid paints instantly (no blank-until-next-tick). Reuses `_accept_authenticated_socket` + `_close_pubsub` (the leak guard at `ticks.py:142` is mandatory — keep it).
- **`/ws/depth/{symbol}`** (NEW). On connect → trigger a `DepthUpdate` subscription for that symbol (ref-counted; unsubscribe when the last client drops), forward `depth:{symbol}` frames via `listen()`.
- **`/ws/positions-overview`** → convert from 2s `_stream_snapshot` to **invalidate-on-event**: subscribe `positions:changed` AND a 150ms-coalesced leg-tick signal; rebuild the payload (still using `overlay_live_marks` / `get_live_mark`) only when something changed, with a slow 2s heartbeat fallback for safety. Keep `ws_positions` (paper, `ticks.py:233`) but drive its loop off the same event instead of `asyncio.sleep(1)`.
- Register all in `main.py` next to the existing `@app.websocket("/ws/...")` block (`main.py:351–421`).

### 3.5 Where the universe is subscribed (unchanged ownership)
- `market_data/option_subscription_manager.py` keeps choosing ATM contracts (09:05 daily pick) and held-leg refresh (45s). It already feeds `data_router`'s subscription set — `quote_bus` consumes whatever `data_router` is subscribed to, so **no change to symbol selection**. We only add: register the `quote_bus` coalescer as a `data_router` global callback at startup, and add the optional `DepthUpdate` sub path in `brokers/fyers.py` (`subscribe_websocket` gains a `depth_symbols` arg → second `client.subscribe(..., data_type="DepthUpdate")`).

### 3.6 File-level summary
- `brokers/fyers.py` — add `DepthUpdate` subscribe path + depth `_handle_depth` callback → `data_router`; (live mode) add `FyersOrderSocket` for fills.
- `market_data/data_router.py` — add `_handle_depth` → publish `depth:{symbol}`; expose a depth subscribe/unsubscribe (ref-counted). Tick path unchanged.
- `market_data/quote_bus.py` (NEW) — the 150ms coalescer → `quotes:bus`.
- `api/websockets/ticks.py` — `ws_quotes`, `ws_depth`; rework `positions-overview` to event-driven.
- `main.py` — register `/ws/quotes`, `/ws/depth/{symbol}`; start `quote_bus` task in lifespan.

---

## 4. Frontend architecture — tick store outside React + one multiplexed socket

**Core change:** stop one-socket-per-symbol (`createTickSocket`, `websocket.ts:159`) for the terminal. Open **one** `/ws/quotes` socket, push ticks into a **store outside React render**, components subscribe by symbol, renders are **rAF-batched**.

### 4.1 Single multiplexed socket — `lib/websocket.ts`
- Add `createQuotesSocket()` wrapping the proven `createReconnectingSocket` (`websocket.ts:94`) → `/ws/quotes`. On open, send `{symbols:[...]}`. On reconnect, the existing backoff + token-refresh handles auth; resend the symbol set.
- Add `createDepthSocket(symbol)` → `/ws/depth/{symbol}` for the focused ladder only.

### 4.2 Tick store (the key new primitive) — NEW `hooks/useQuoteStore.ts`
- A module-level store (Zustand or a tiny custom `Map<symbol, Quote>` + listener set) **outside React state**. The socket's `onMessage` writes into this Map — **does not call `setState`**.
- A single **rAF loop** drains "dirty symbols" once per frame (≤60fps) and notifies only the subscribers of changed symbols. This is the frontend half of the frame-storm guard: backend coalesces to ~150ms frames, frontend coalesces all symbol updates per animation frame → at most 60 renders/s per *subscribed cell*, not per tick.
- `useQuote(symbol)` hook → subscribes a component to one symbol; re-renders only that cell/row when that symbol changes. This gives cell-isolation the current full-row-on-any-change tables lack (frontend investigation: `UniverseWatchlist`, `InstrumentRow`).

### 4.3 Terminal components (build from existing `desk-ui` primitives)
- **`<QuoteGrid>`** — virtualized (react-window) for 50+ rows; each cell uses `useQuote`. Reuses `formatNumber/formatPct/formatSigned` (`desk-ui/formatters.ts`).
- **`<PriceFlash>`** / `usePriceFlash(value)` — compares prev vs new, applies `bg-accent-green/red` for ~150ms then fades. Add a `transition` utility (currently none in `desk-ui`). The direction comes from the store, not a timer.
- **`<DepthLadder symbol>`** — 5-level two-sided ladder fed by `createDepthSocket`; bar-width ∝ size, order-count column. Side panel on the focused symbol.
- **`<LiveMarkBadge>`** — per-symbol freshness: `as_of` ts vs now (refresh ~250ms); green=live / amber=stale / red=offline. Reuses `StatusBadge` (`desk-ui/StatusBadge.tsx`). Surfaces the `c:1` coalesced flag so the trader knows it's a 150ms frame, not a raw print.
- Positions table consumes `/ws/positions-overview` (now event-driven) via the existing `useLiveSnapshotQuery` → `setQueryData` path; no structural change, just faster upstream.

### 4.4 File-level summary
- `lib/websocket.ts` — `createQuotesSocket`, `createDepthSocket`.
- `hooks/useQuoteStore.ts` (NEW) — store + rAF drain + `useQuote(symbol)`.
- `hooks/usePriceFlash.ts` (NEW), `components/terminal/QuoteGrid.tsx`, `DepthLadder.tsx`, `LiveMarkBadge.tsx` (NEW).
- Keep `useTickStream` (`hooks/useTickStream.ts`) for legacy single-symbol consumers; migrate desks to `useQuote` incrementally.

---

## 5. Latency budget (target p95 <500ms; aim ~250ms typical)

| Hop | Estimate | Notes / where ms go |
|---|---|---|
| Exchange → Fyers WS → backend socket | ~10–100ms wire | **Unquantified by primary Fyers source** — measure live. Event-driven diff push (changed fields only), no SDK throttle. |
| `_on_tick` parse + dispatch | ~1–5ms | synchronous on Fyers thread (`data_router.py:353`). |
| Coalescer flush window | **0–150ms** | the deliberate cost; last-write-wins. Tunable 100–250ms. |
| Redis PUBLISH `quotes:bus` + fan-out | ~1–5ms | async, fire-and-forget; one frame to N sockets. |
| Backend WS `send_text` per client | ~1–2ms | `listen()`-driven, **no `asyncio.sleep`** (vs today's 1000ms `get_message` worst case / 2–15s snapshot floor). |
| Network backend → browser | ~20–80ms | EC2 Mumbai → client; dominated by client RTT. |
| Frontend store write + rAF render | ~0–16ms | one animation frame; cell-isolated. |
| **Total (typical / p95)** | **~60–250ms / ~450ms** | Within budget *if* broker hop is at the low end. |

**Where to cut if over budget:** drop coalesce window to 100ms (−50ms, more frames); skip the Redis hop for `quotes:bus` by having `quote_bus` push directly into an in-process asyncio broadcast (single backend process — viable, removes ~5ms + a Redis dependency, but loses cross-process fan-out; keep Redis for now for the multi-worker safety). The single biggest *current* win is simply removing the 1000ms/2–15s timer floors — that alone takes us from 1–8s to sub-300ms.

---

## 6. Build phases (smallest-first; prod constraints baked in)

Prod constraints (from memory): **FE is image-baked** (changes need rebuild or copy-in + `npx next build`); **BE `/app` is an ephemeral bind-mount** → every change must be committed to repo (the only durable deploy) and may be hot-pushed for testing; **never heavy-exec the prod backend container** (OOM → recreate → revert + DB-pool leak); **WS adds 0 REST budget** (token bootstrap is the only REST cost).

| Phase | Lane | Deliverable | Verifiable now? |
|---|---|---|---|
| **0. Instrument** | [backend] | Already partially present (`observe_tick`, `data_router.py:358`). Add per-hop timing on the coalescer + WS send. | ✅ build/compile + unit |
| **1. Coalescer + `/ws/quotes`** | [backend] | `quote_bus.py` (150ms, last-write-wins), `ws_quotes` via `listen()`, hot-cache replay on connect, register in `main.py` lifespan. | ✅ compile + a fake-tick unit test (publish synthetic ticks, assert one coalesced frame). Full validation **needs feed-on**. |
| **2. Quote store + grid + flash** | [frontend] | `useQuoteStore`/`useQuote`, `createQuotesSocket`, `<QuoteGrid>` + `usePriceFlash`. Point at staging `/ws/quotes`. | ✅ compile + render with mocked socket frames; visual flash testable offline. |
| **3. Positions → event-driven** | [backend] | `positions:changed` + leg-tick invalidation; rebuild on event w/ 2s heartbeat fallback. | ✅ compile; **needs feed-on** for true latency. |
| **4. 5-level depth ladder** | [backend]+[frontend] | `DepthUpdate` sub path (`brokers/fyers.py`), `depth:{symbol}`, `/ws/depth`, `<DepthLadder>`. | partial: compile ✅; ladder values **need feed-on** (depth only flows on a live tradable). |
| **5. Order socket (live fills)** | [backend] | `FyersOrderSocket` → `positions:changed`; remove REST poll in live mode. | compile ✅; **needs live broker session + a real fill** to validate. Behind live-mode flag. |
| **6. (v2, gated) TBT 50-level** | [backend]+[frontend] | only after **confirming TBT entitlement**. Separate protobuf socket. | ❌ blocked on entitlement confirmation. |

Each backend phase: commit to repo (durable) → optional hot-push to bind-mount for live test → verify prod `/health` 200 + Fyers socket `StartedAt` unchanged. Each FE phase: build locally, then image rebuild OR copy-in + `npx next build` to land on prod.

---

## 7. Risks & mitigations

- **Re-render storms** — mitigated on both ends: backend 150ms coalesce (caps frames), frontend store-outside-React + rAF drain + cell-isolated `useQuote` (caps renders). Without *both*, a fast tape (8 symbols × 5+/s observed today) janks. Virtualize the grid for 50+ rows.
- **Redis availability / pool** — pub/sub is the live spine; `tick:{symbol}` cache writes once exhausted Redis maxclients (memory: pool now bounded at 1000, `_close_pubsub` leak-guard at `ticks.py:142` is mandatory on every new endpoint). Mitigation: keep the snapshot/hot-cache **fallback** path (`_stream_tick_snapshot_fallback`) so a Redis outage degrades to polling, not blackout; consider the in-process broadcast escape hatch (§5) to remove Redis from the critical hop if pool pressure returns.
- **Fyers WS reconnect / dedup** — SDK replays subscriptions on reconnect (good) but a reconnect storm re-runs REST token resolution (the only REST cost). Don't reconnect-loop. On reconnect there may be a duplicate snapshot burst → the coalescer's last-write-wins naturally dedups within a window; the WS `last_payload != current` check dedups identical frames.
- **Snapshot-vs-tick honesty** — a coalesced 150ms frame must NOT masquerade as a raw print. Enforced by the `c:1` flag end-to-end and the `LiveMarkBadge` surfacing it + `as_of`. The `get_live_mark` `max_age_seconds=30` guard (`data_router.py:458`) stays — a dead feed marks positions `None`/stale, never a frozen LTP shown as live. UI shows amber/red, never silently green.
- **Single live lane (Fyers SPOF)** — one Fyers session feeds everything; if it drops, the whole terminal goes stale. The 30s/45s-stale watchdog + capped-backoff reconnect (`data_router.py`) is the only recovery. Document that there is no redundant feed; the UI must make a dead feed loud (offline badge), and strategy agents already short-circuit on `data_quality_agent` staleness. A second-broker failover lane is out of scope but worth a future ADR.
- **Depth/index edge cases** — never request `DepthUpdate` for an index (rejected). Never render index bid/ask cells as `0`. Depth sub is ref-counted — a leaked ref keeps an unneeded subscription burning a slot (harmless at our scale, but unsubscribe on last client).
- **TBT assumption risk** — building toward 50-level depth before confirming the paid entitlement wastes effort and the latency premise (<10ms) is third-party/unverified. Phase 6 is explicitly gated.

---

### Key file references
- Backend WS / patterns: `/Users/chinnadurairamachandran/Claude Projects/TradingBot/nomad-curie/backend/api/websockets/ticks.py` (`ws_proposals` `listen()` pattern at :683; `_stream_snapshot` timer at :192; `_close_pubsub` leak-guard at :142)
- Tick fan-out: `/Users/chinnadurairamachandran/Claude Projects/TradingBot/nomad-curie/backend/market_data/data_router.py` (`_on_tick` :353, `_publish_tick` :416, `get_live_mark` :454, `_global_callbacks` :401)
- Broker subscribe: `/Users/chinnadurairamachandran/Claude Projects/TradingBot/nomad-curie/backend/brokers/fyers.py` (`subscribe_websocket` :485, `SymbolUpdate` :505, top-of-book read :519)
- Route registration: `/Users/chinnadurairamachandran/Claude Projects/TradingBot/nomad-curie/backend/main.py` (:351–421)
- Frontend socket layer: `/Users/chinnadurairamachandran/Claude Projects/TradingBot/nomad-curie/frontend-v2/src/lib/websocket.ts` (`createReconnectingSocket` :94, `createTickSocket` :159)
- New files: `backend/market_data/quote_bus.py`, `frontend-v2/src/hooks/useQuoteStore.ts`, `frontend-v2/src/hooks/usePriceFlash.ts`, `frontend-v2/src/components/terminal/{QuoteGrid,DepthLadder,LiveMarkBadge}.tsx`
