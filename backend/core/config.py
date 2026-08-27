from __future__ import annotations
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import List
from zoneinfo import ZoneInfo
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_BACKEND_DIR = Path(__file__).resolve().parents[1]
_PROJECT_ROOT = _BACKEND_DIR.parent
FYERS_FIXED_REDIRECT_URI = "https://trade.fyers.in/api-login/redirect-uri/index.html"
UPSTOX_SANDBOX_REDIRECT_URI = "https://www.google.com"


def normalize_fyers_redirect_uri(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return FYERS_FIXED_REDIRECT_URI
    if raw.endswith("/api/auth/fyers/callback"):
        return FYERS_FIXED_REDIRECT_URI
    return raw


def normalize_upstox_redirect_uri(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return UPSTOX_SANDBOX_REDIRECT_URI
    lowered = raw.lower()
    if lowered.startswith(("http://localhost", "https://localhost", "http://127.0.0.1", "https://127.0.0.1", "http://0.0.0.0", "https://0.0.0.0")):
        return UPSTOX_SANDBOX_REDIRECT_URI
    return raw


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(
            str(_PROJECT_ROOT / ".env"),
            str(_BACKEND_DIR / ".env"),
        ),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    APP_ENV: str = "development"
    LOG_LEVEL: str = "INFO"
    BACKEND_CORS_ORIGINS: List[str] = ["http://localhost:3000"]
    BACKEND_CORS_ORIGIN_REGEX: str | None = None
    # ── Phase-1 process split (owner-approved 2026-07-18) ──────────────────
    # LANESET selects which boot plane this process runs:
    #   "all"        (DEFAULT) — byte-identical single-process boot, exactly
    #                today's behavior. Deploy-safe with no compose changes.
    #   "core"       — market-data ingest (the ONE Fyers WS via data_router,
    #                live_candle_store, quote_bus, option/held/commodity
    #                subscription managers, MI/watchlist/chain service via the
    #                supervisor's core runners), API routers + websockets,
    #                health, auth/token plumbing, data-maintenance daemons.
    #   "strategies" — the market_hours_paper_supervisor strategy runners +
    #                the two own-loop agents (S1 paper_strategy_agent,
    #                commodity_strategy_agent). NEVER opens a broker WS
    #                (data_router.subscribe is gated); reads ticks/marks from
    #                Redis tick:*/quotes:bus and PG tables. Broker REST is
    #                allowed (chains/premium).
    # Unknown values fail SAFE to "all" (single-process) with a CRITICAL log —
    # a typo must never dark a plane. See core/laneset.py.
    LANESET: str = "all"
    # Cross-process broker REST budget (Phase-1 stopgap): the in-process rate
    # limiters (brokers/rate_limiter.py) are per-process, so TWO planes could
    # jointly reach ~2× the per-token caps. Each plane scales its limiter
    # window counts by this fraction (split compose profile: core=0.6,
    # strategies=0.4). 1.0 (default) = today's exact limits, provable no-op.
    # Phase 2 replaces this with a shared Redis token bucket.
    BROKER_REST_BUDGET_FRACTION: float = 1.0

    # ── Phase-2 split hardening (2026-07-18) ──────────────────────────────────
    # ITEM 1 — Shared Redis REST token-bucket. When this process is a SPLIT half
    # (LANESET != all) and this flag is on, every broker REST acquire also draws
    # from a Redis sliding-window budget keyed per broker+span, so BOTH planes
    # jointly stay under the ONE per-TOKEN cap (Fyers 9/s+190/60s, Upstox
    # 8/s+1800/30min). Layered ON TOP of the in-process AsyncRateLimiter (which
    # still owns priority + CRITICAL/BULK class geometry). LANESET=all never
    # consults it — provable no-op. Redis unreachable → FAIL OPEN to the
    # in-process limiter (BROKER_REST_BUDGET_FRACTION stays the degraded
    # divisor). The shared layer enforces only the aggregate token count; class
    # fairness stays per-process (documented Phase-2b follow-up).
    SHARED_REST_BUDGET_ENABLED: bool = True
    SHARED_REST_BUDGET_KEY_PREFIX: str = "rlb"       # Redis key namespace
    SHARED_REST_BUDGET_MAX_WAIT_SECONDS: float = 5.0  # per-acquire ceiling before fail-open grant
    # ITEM 2 — run-once / close-position proxy (core plane → strategy plane) via
    # a Redis request/ack command channel. Only the LANESET=core plane ever
    # publishes; the strategy plane runs a consumer. LANESET=all runs the action
    # in-process exactly as today (no proxy). Bounded timeout → honest 504.
    STRATEGY_PROXY_ENABLED: bool = True
    STRATEGY_PROXY_TIMEOUT_SECONDS: float = 140.0     # ≥ commodity scan budget (120s) + headroom
    STRATEGY_PROXY_REQUEST_KEY: str = "strat:cmd:req"

    # DB / Redis
    DATABASE_URL: str = "postgresql+asyncpg://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
    DATABASE_POOL_SIZE: int = 0
    DATABASE_MAX_OVERFLOW: int = 0
    DATABASE_POOL_TIMEOUT_SECONDS: int = 15
    DATABASE_POOL_RECYCLE_SECONDS: int = 900
    # Task D (2026-07-18): default per-statement timeout applied to every NEW
    # connection of the MAIN app engine (SET statement_timeout on connect).
    # 0 disables entirely. Legitimate long-runners (backfills / aggregations)
    # must use db.database.long_query_session() instead of lowering this.
    # The standalone research_sync container calls
    # db.database.disable_default_statement_timeout() at process start, so its
    # multi-hour contract sweeps are exempt even though it shares database.py.
    DB_STATEMENT_TIMEOUT_SECONDS: int = 60
    # Negative-readiness backoff (seconds). Once a broker's saved session is
    # known expired-and-unrefreshable (SEBI daily-OAuth flow: Fyers no longer
    # issues durable refresh material, so the "refresh" attempt is pure churn —
    # a forced DB/disk credential reload + a warning line, observed ~45x/10min
    # and doubling under the LANESET process split), stop re-attempting the
    # refresh and collapse the log to ONE throttled line per this window until
    # the owner supplies a fresh token (which clears the backoff via the token
    # persistence path). Never gates the owner's manual reconnect. 0 disables.
    BROKER_NEGATIVE_READINESS_BACKOFF_SECONDS: int = 300
    REDIS_URL: str = "redis://localhost:6383/0"
    # DEPRECATED (2026-07-18): the single 1000-cap client pool is replaced by the
    # two bounded blocking pools below. Kept so stale env overrides don't crash.
    REDIS_MAX_CONNECTIONS: int = 1000
    # Redis P0 (2026-07-18): commands and pub/sub ride SEPARATE bounded
    # BlockingConnectionPools. At the cap a caller WAITS (up to the acquire
    # timeout) for a free connection instead of raising "Too many connections"
    # — the 07-17 failure mode was the CLIENT pool ceiling raising while the
    # server sat at rejected_connections=0. 200 command connections is ample
    # steady-state headroom (tick fan-out is now coalesced to ~1 pipeline per
    # 150ms); pub/sub connections are held one-per-WS-client, so 150 bounds
    # concurrent UI sockets without letting them starve command traffic.
    REDIS_COMMAND_MAX_CONNECTIONS: int = 200
    REDIS_PUBSUB_MAX_CONNECTIONS: int = 150
    REDIS_POOL_ACQUIRE_TIMEOUT_SECONDS: float = 3.0
    RESEARCH_SYNC_AUTO_ENABLED: bool = False
    RESEARCH_SYNC_EMBEDDED_ENABLED: bool = False
    STRATEGY_SPOT_SYNC_ENABLED: bool = False
    # Greeks enrichment — stamp real broker greeks onto greeks-null index option
    # candles by copying from option_chain_snapshots (the live chain service already
    # persists per-strike broker greeks for the tracked index expiries). Restores
    # what the dead 2026-06-23 Fyers greeks writer used to fill, with zero extra
    # broker load. Index-band + source in ('fyers','upstox') only; iv scaled
    # percent -> fraction. Cheap in steady state; gated ON.
    GREEKS_ENRICHMENT_ENABLED: bool = True
    GREEKS_ENRICHMENT_POLL_MINUTES: int = 10
    # 1 (was 3): with Timescale compression live (2026-07-07), a 3-day lookback
    # reaches into COMPRESSED chunks and blows the tuple-decompression DML limit
    # (15.17M vs 100k on 2026-07-08). 1 day stays inside uncompressed chunks;
    # older gaps go through the manual backfill path instead.
    GREEKS_ENRICHMENT_LOOKBACK_DAYS: int = 1
    # MACD diffusion — hourly CE/PE-above-zero breadth snapshot (market sentiment).
    # Reads the live watchlist's per-leg MACD; seeds history from option_premium_candles.
    MACD_DIFFUSION_ENABLED: bool = True
    MACD_DIFFUSION_POLL_MINUTES: int = 60
    MACD_DIFFUSION_BACKFILL_DAYS: int = 21
    # MACD Refined — premium-MACD entry, low-IV gated, volume-led long-premium
    # book (separate CE/PE). The auto-runner fetches current + next monthly
    # expiry chains, persists per-contract volume/turnover, and syncs the
    # paper book. 30-min strategy → 60s cadence catches each fresh bar close.
    MACD_REFINED_AUTO_ENABLED: bool = True
    # Full F&O universe (~217 names) × current+next expiry chains is a bounded,
    # broker-throttled sweep. Match its cadence to the 30-minute signal bar so
    # completed cycles do not immediately start another redundant sweep.
    MACD_REFINED_AUTO_INTERVAL_SECONDS: int = 1800
    # Seconds-cadence protective-exit heartbeat for OPEN macd_refined positions
    # (owner directive 2026-07-17: "position updates must be in SECONDS"). The
    # 1800s decision cycle above sets the SIDE; this separate lightweight pass
    # re-marks held positions off the REAL-TIME plane (Fyers-WS tick buffer →
    # Redis tick:{symbol} → shared oc: chain cache — never a broker REST /
    # route_order fetch) and fires ONLY the existing stop/target/trail/window
    # exits, so a breached stop is caught in seconds instead of waiting up to
    # 30 minutes. Mirrors CBE_MARKS_REFRESH_INTERVAL_SECONDS.
    MACD_REFINED_MARKS_REFRESH_INTERVAL_SECONDS: int = 45
    # Per-session data-audit re-run (audit 2026-07-18): the data-sufficiency
    # sweep was manual-only and its report went stale for days. When True the
    # india live cycle kicks ONE background data_audit per session, gated to
    # >=14:45 IST so the full-universe chain sweep never competes with the
    # open-window broker budget.
    MACD_REFINED_DATA_AUDIT_PER_SESSION: bool = True
    # NSE MACD Strategy 1 (macd_strategy) max simultaneous positions. Default
    # 1000 = effectively NO cap — the strategy trades the full ATM watchlist
    # (~216 contracts) one position per underlying-side. (The shared live-risk
    # MAX_SIMULTANEOUS_POSITIONS=5 and Strategy 2's cap are untouched.) Set a
    # finite number here to re-cap later.
    MACD_STRATEGY_MAX_POSITIONS: int = 1000
    # Signal-generation test mode (paper Strategy 1): pin the position-sizing
    # base to max(total_equity, initial_capital) so capital depletion or a
    # drawdown can never SHRINK a signal's position — every signal converts to a
    # full-size trade and signal→trade reconciles ~1:1. Paper engine sizing
    # only; the live-risk capital/sizing path is untouched. There is no hard
    # cash/margin reject in the paper book, so this is the only place capital
    # could otherwise cap trading. Set False to restore equity-tracking sizing.
    MACD_STRATEGY_UNCAPPED_CAPITAL: bool = True
    # ═══════════════════════════════════════════════════════════════════════
    # SIGNAL-VALIDATION MODE — OWNER DIRECTIVE 2026-07-17: "we are currently
    # validating signals, hence no limit on loss/capital — allow lanes to
    # trade fully as per strategy."  PAPER LANES ONLY.
    #
    # When True, every PAPER lane's CAPITAL / LOSS / DRAWDOWN /
    # CIRCUIT-BREAKER **entry** block is bypassed (mirrors S1's
    # MACD_STRATEGY_UNCAPPED_CAPITAL pin above) so every strategy signal
    # converts to a paper trade and signal→trade reconciles ~1:1:
    #   * macd_refined            — sizing base pinned to starting equity;
    #                               paper-book cash gate allows negative
    #                               available cash; drawdown/win-rate kill
    #                               switch REPORTS but no longer pauses entries
    #   * directional_options     — daily/weekly loss caps skipped; the
    #                               fail-closed decline when the loss-cap DB
    #                               fetch fails is skipped too (pointless with
    #                               caps off)
    #   * commodity agent         — 15% drawdown entry block skipped
    #                               (stop/re-entry cooldowns + operator kill
    #                               switch still honored)
    #   * institutional_convergence — 2-consecutive-loss / −3%-day circuit
    #                               breaker REPORTS but does not lock entries
    #                               (NSE + MCX books)
    #   * auction_intelligence    — governor margin/symbol/correlated exposure
    #                               caps, daily-loss cap and per-agent drawdown
    #                               cap skipped (paper mode only)
    #
    # KEPT under the flag: ALL strategy gates (regime, liquidity/turnover
    # floors, MP/OF gates, RL policy act/skip, RAG) and ALL protective EXITS
    # (stops, targets, trailing, squareoff, expiry/window exits) — validation
    # needs honest exits. The live_engine / risk_manager live-order path is
    # UNTOUCHED. Set False to restore every capital/loss/drawdown risk gate.
    # ═══════════════════════════════════════════════════════════════════════
    SIGNAL_VALIDATION_UNCAPPED: bool = True
    # Per-cycle time budget (seconds) for the ATM premium-candle top-up inside
    # refresh_nse_runtime. The recorded set spans ~4k contracts (ATM + the
    # extended 10-strike window across ~217 names); at 0.1s/contract a full
    # serial pass exceeds the supervisor's 300s runner timeout and gets the
    # whole market_intelligence runner killed. Bounding the top-up to this
    # budget (current ATM picks every cycle, extended window round-robin)
    # keeps the runner well under 300s so signal generation actually runs.
    MARKET_INTELLIGENCE_PREMIUM_BUDGET_SECONDS: int = 90
    # Premium top-up cooldown (seconds), decoupled from the 60s supervisor
    # interval. The top-up is data-coverage-only (3-min bars) and was running
    # ~every cycle, monopolizing the shared DB pool / broker limiter and
    # starving the actual strategy lanes (fractal/auction/commodity all began
    # timing out at 300s while completing in <6s when they could run). 180s
    # matches the 3-min bar cadence — one refresh per bar is plenty — and
    # frees ~2/3 of cycles for the strategy lanes.
    MARKET_INTELLIGENCE_PREMIUM_COOLDOWN_SECONDS: int = 180
    # Per-call timeout (seconds) for each load_candles top-up. The budget
    # above is only checked between calls, so one hung broker fetch can
    # overrun it (observed: 189s vs a 150s budget; a hung call near the
    # boundary blew the 300s runner timeout). Capping each call makes the
    # budget enforceable; a contract that times out is skipped and retried
    # on a later cycle.
    MARKET_INTELLIGENCE_PREMIUM_CALL_TIMEOUT_SECONDS: int = 8
    # Premium top-up concurrency: how many load_candles top-ups run in parallel
    # per batch. Serially, a rate-limiting broker (Fyers 429) hangs each fetch
    # to its call timeout, so a 150s budget covered only ~18 of ~434 priority
    # contracts and the rest of the stock universe's snapshots FROZE mid-session
    # (observed 2026-07-07: 211/216 names stopped updating, so S1 never saw
    # later zero-crosses). Batching lets DB-fresh reads return instantly in
    # parallel while only genuinely-stale contracts queue on the shared broker
    # limiter — multiplying coverage per cycle at the same budget. The limiter
    # still caps real broker calls, so this does not worsen the 429s.
    MARKET_INTELLIGENCE_PREMIUM_CONCURRENCY: int = 6
    # Demote the premium top-up's Pass-1 to DB-ONLY (no per-contract broker
    # fetch), relying on chain_candle_builder for the broad 3m/30m option bars.
    # ONLY takes effect when CHAIN_CANDLE_BUILDER_ENABLED is also True (so the
    # replacement is guaranteed running). Default False = today's broker-fetch
    # behaviour. Flip on AFTER a market-open verification that the builder covers
    # the universe (GET /api/system/rate-budget → chain_builder.last_cycle).
    MARKET_INTELLIGENCE_PREMIUM_TOPUP_GAPS_ONLY: bool = False
    # F1 feed — full-universe option-chain → 3m CE+PE OHLC builder (chain_candle_builder).
    # OFF by default: scales Fyers REST (~30k calls/day, governed by FYERS_DATA_LIMITER);
    # enable deliberately in prod after sign-off + a market-open verification.
    CHAIN_CANDLE_BUILDER_ENABLED: bool = False
    # Also emit 30-minute fyers_chain bars from the chain builder (not just 3m).
    # S1's entry MACD reads the interval='30minute' partition of
    # option_premium_candles; the builder only wrote 3m, so nothing populated the
    # series S1 actually trades off. When True the builder rolls a parallel 30m
    # accumulator so enabling it genuinely feeds S1 (WS-first chain design P2).
    # Only meaningful when CHAIN_CANDLE_BUILDER_ENABLED is also True.
    CHAIN_CANDLE_BUILDER_EMIT_30M: bool = True
    # Upstox full-universe option-chain -> 30m OHLC+greeks builder
    # (market_data/upstox_chain_builder.py). This is the LIVE replacement for
    # the equity `iv` that died on 2026-07-28: `upstox_expired` only prices
    # already-expired contracts and `fyers_chain` needs Fyers, whose credentials
    # no longer decrypt in this deployment. Costs ~213 Upstox chain calls per
    # 30-minute bar (~2.8k/session), governed by the shared upstox limiter as
    # CLASS_BULK. OFF by default: enable deliberately after a market-open
    # verification via GET /api/system/upstox-chain-builder.
    UPSTOX_CHAIN_BUILDER_ENABLED: bool = False
    # WS-first chain design — phase-P0 empirical probe. OFF by default; enable for
    # ONE live session to confirm Upstox WS greeks/iv payload, Fyers oi/pdoi
    # cadence, and the Upstox iv unit via GET /api/diagnostics/ws-chain-probe.
    WS_CHAIN_PROBE_ENABLED: bool = False
    # Broker circuit breaker — skip a broker's data REST for a cooldown after
    # sustained 429s/errors and prefer the healthy broker for chain failover.
    # FAIL-OPEN: only trips on sustained failure; informs routing, never hard-
    # blocks. Surfaced at GET /api/system/rate-budget → circuit.
    BROKER_CIRCUIT_ENABLED: bool = True
    # ── Per-lane broker routing (owner directive 2026-07-17) ──────────────────
    # Route SLOW lanes (30m: S1 MACD, MACD Refined, CBE, Gann) to Upstox for
    # their spot/chain/premium REST, and FAST lanes (3m + tick: directional,
    # auction, convergence, MP+OF) to Fyers (WS ticks + Fyers REST chains).
    # Mechanism: a `lane_broker_profile` contextvar (brokers/rate_limiter.py) set
    # once at runner dispatch reorders the source_policy.route_order failover list
    # (SLOW → upstox-first, FAST → fyers-first). Reorder ONLY — never drops a
    # source, so failover is preserved and a broker circuit-OPEN still wins over
    # the profile. DEFAULT OFF ⇒ route_order returns the identical global order
    # (provable no-op). Commodity MCX contract/quote resolution stays Upstox-only
    # regardless of profile (upstox_commodity.py, forced-hybrid — see exc-a).
    LANE_BROKER_ROUTING_ENABLED: bool = False
    # Per-cadence-group kill switches ("work independently"): dark an entire
    # broker-dependent group in one move when that broker degrades, without
    # touching each lane's own *_AUTO_ENABLED flag. Orthogonal to the routing
    # flag (they gate SCHEDULING, not broker choice). Default True = no effect.
    SLOW_LANES_ENABLED: bool = True
    FAST_LANES_ENABLED: bool = True
    # Lane profile for the commodity strategy agent's self-owned loop (it runs
    # outside supervisor dispatch, so it wires the lane seam itself — audit
    # 2026-07-18). Whether the MCX lane is tagged slow/fast is an OWNER decision:
    # MCX contract/quote resolution is forced-hybrid (Upstox-only regardless of
    # profile), so "default" (= global broker order, never darkened by either
    # cadence kill switch) is the safe out-of-the-box value.
    COMMODITY_AGENT_BROKER_PROFILE: str = "default"
    # Option-flow watchdog: fast lanes now depend on the Fyers tick path, and WS
    # Fyers-format option ticks have never persisted (dormant defect). This
    # watchdog flags zero option-bucket persists over a window during market
    # hours while options are subscribed, so the defect becoming load-bearing is
    # visible. Detection/telemetry only; default OFF (opt-in).
    OPTION_FLOW_WATCHDOG_ENABLED: bool = False
    OPTION_FLOW_WATCHDOG_INTERVAL_SECONDS: int = 60
    OPTION_FLOW_WATCHDOG_STALE_SECONDS: int = 300
    # ── Expiry policy + pre-open MACD ATM watchlist (owner spec 2026-07-20) ──
    # All FOUR default OFF: staged tonight, flipped one per session so each is
    # independently provable and independently reversible.
    #
    # EXPIRY_POLICY_ENABLED — route analysis.instruments' expiry math through
    # core.expiry_policy (calendar-driven, ONE holiday table shared with
    # core/trading_calendar) instead of the private _KNOWN_HOLIDAYS set. OFF ⇒
    # the legacy table answers, byte-identically to today. NOTE: the legacy
    # table is WRONG for NSE 2026-03 / 2026-11 and SENSEX 2026-03 / 2026-05, so
    # OFF is acceptable as a same-day revert, not as a resting state.
    EXPIRY_POLICY_ENABLED: bool = False
    # Validate the calendar ladder against the broker ONCE pre-market (and on
    # demand). Inert while EXPIRY_POLICY_ENABLED is False.
    EXPIRY_POLICY_VALIDATE_ON_OPEN: bool = True
    # Stock delivery-risk roll: watchlist moves to the next monthly when <= N
    # TRADING days remain on the active one. Indian single-stock options are
    # PHYSICALLY settled — this exists to avoid compulsory delivery. Owner spec
    # says five days; the legacy value was 3 (agent/strategy_config
    # MIN_TTE_DAYS_STOCK, which remains the separate ENTRY gate).
    EXPIRY_POLICY_STOCK_ROLL_TRADING_DAYS: int = 5
    # ── Held-position compulsory closure (owner rule, 2026-07-21) ──────────
    # The 5TD roll above moves the WATCHLIST. It says nothing about positions
    # already OPEN on the near expiry — those keep their own contract past the
    # roll and are COMPULSORILY CLOSED once <= N trading days remain, because
    # a physically-settled single-stock option must never reach expiry.
    # Boundary semantics are stated exactly in core/expiry_policy.py: closure
    # fires on the first session with trading_days_until(expiry) <= N, i.e. N
    # sessions still remain AFTER the closing session (2026-07-28 ⇒ 2026-07-24).
    # Requires BOTH EXPIRY_POLICY_ENABLED and this flag; OFF ⇒ the exit cascade
    # is byte-identical to today (window_end remains the terminal exit).
    EXPIRY_POLICY_FORCED_CLOSE_ENABLED: bool = False
    EXPIRY_POLICY_FORCED_CLOSE_TRADING_DAYS: int = 2
    # Indices are CASH settled — there is no delivery to be compelled into, so
    # the stock rationale does not carry over and this defaults to 0 = DISABLED.
    # A SEPARATE knob on purpose: enabling it for indices would be an expiry-day
    # gamma/liquidity argument, a different decision, and must never happen as a
    # side effect of tuning the stock number.
    EXPIRY_POLICY_INDEX_FORCED_CLOSE_TRADING_DAYS: int = 0
    # MACD_PREOPEN_WATCHLIST_ENABLED — build the ATM strike ladder ONCE
    # pre-market from settled/pre-open prices and FREEZE it for the session
    # (no intraday re-racing). Removes the moving-ATM defect class that causes
    # option-premium chart gaps. OFF ⇒ _build_row re-picks strikes per cycle,
    # exactly as today.
    MACD_PREOPEN_WATCHLIST_ENABLED: bool = False
    # PREOPEN_SPOT_SNAPSHOT_ENABLED — owner spec 2026-07-27: persist a durable
    # per-(session, underlying) record of the NSE cash pre-open call auction
    # (equilibrium price, matched quantity, gap vs prior close, auction book
    # imbalance) plus an ACTIVENESS flag. Observation only: it reads ticks
    # already in Postgres after the 09:00-09:15 window closes, makes no broker
    # call, and touches no strategy math. Default OFF so the owner enables it
    # deliberately; see market_data/preopen_spot.py for the definition.
    PREOPEN_SPOT_SNAPSHOT_ENABLED: bool = False
    # Pre-open sample window (IST). NSE's call auction runs 09:00-09:08; we
    # sample at 09:04+ so the book has actually built (sampling at 09:00 mostly
    # returns a carried previous close).
    MACD_PREOPEN_WINDOW_START: str = "09:04"
    MACD_PREOPEN_WINDOW_END: str = "09:14"
    # MACD_STICKY_STRIKES_ENABLED — once a position is open on a strike, that
    # strike PERSISTS in the watchlist until the position closes, regardless of
    # spot drift; after closure the next strike is re-picked from the spot AT
    # THAT TIME. Restart-safe (open positions live in Postgres agent_positions
    # and the macd_refined paper JSON, never in memory).
    MACD_STICKY_STRIKES_ENABLED: bool = False
    # MACD_LIQUID_STRIKE_SELECTION_ENABLED — owner-directed removal of the
    # ITM/OTM bias in _select_liquid_atm_strikes. Today CE is hard-anchored at
    # or ABOVE spot and PE at or BELOW spot ("natural directional convexity").
    # Owner: "new ATM CE/PE does not mean exact atm strike. it is next or just
    # below spot liquid contract" — free pick by LIQUIDITY, no ITM/OTM bias.
    # ON  ⇒ each side ranks a 3-wide window SPANNING spot and may land ITM.
    # OFF ⇒ the legacy asymmetric anchor answers, byte-identically to today.
    MACD_LIQUID_STRIKE_SELECTION_ENABLED: bool = False
    # Liquidity floors. Precedent: market_data/option_subscription_manager.py
    # (MIN_OI_LOTS=1000, MIN_PRIOR_VOLUME_LOTS=100). Single stocks are not
    # comparable to indices, so they get their own (lower) floors.
    MACD_STRIKE_MIN_OI_INDEX: float = 1000.0
    MACD_STRIKE_MIN_PRIOR_VOLUME_INDEX: float = 100.0
    MACD_STRIKE_MIN_OI_STOCK: float = 200.0
    MACD_STRIKE_MIN_PRIOR_VOLUME_STOCK: float = 25.0
    # Relative bid/ask spread veto. bid/ask exist on the in-memory chain object
    # (brokers/base.py OptionChainEntry) but are NOT persisted in
    # option_chain_snapshots, so this is a VETO applied when both sides of the
    # book are populated and skipped (labelled "spread_untestable") when they
    # are not. We have NO L2 depth and NO aggressor tags — no depth-based
    # liquidity measure is invented here.
    MACD_STRIKE_MAX_REL_SPREAD: float = 0.05
    # History warm-up. Bar counts are DERIVED from the MACD params, not guessed
    # (see market_data/macd_watchlist.py::warmup_requirement).
    MACD_WARMUP_ENABLED: bool = False
    # Half-width of the strike band pre-warmed pre-open so a mid-session re-pick
    # after a position closes is already warm (0 broker calls, 0 latency).
    #
    # SET TO 1, NOT 2, and the numbers are the reason. The selector picks from a
    # THREE-strike window spanning spot (_spot_spanning_window), so:
    #   band=1 → 3 strikes x 2 sides x 216 underlyings = 1,296 fetches ≈ 7.6 min
    #            at 0.35s pacing — fits the pre-09:00 dead zone, and covers every
    #            strike the selector can ever choose.
    #   band=2 → 5 strikes x 2 sides                   = 2,160 fetches ≈ 12.6 min,
    #            of which 864 are for strikes OUTSIDE the selection window and
    #            therefore unreachable. Pure waste of broker budget.
    # Neither width fits the ten-minute 09:04-09:14 pre-open window, which is
    # why warm-up is phase 1 and the strike pick is phase 2.
    MACD_WARMUP_BAND_STRIKES: int = 1
    MACD_WARMUP_PACING_SECONDS: float = 0.35
    # Post-close F&O stock spot sweep. The only LIVE writer of stock 30m spot is
    # data/upstox_research_sync.py with spot_limit=25, so it reaches 25 of ~211
    # names per pass (07-16: 124 names, 07-17: 20). Every full-coverage day so
    # far came from a MANUAL backfill, so the gap re-opened each evening. This
    # runner sweeps the whole F&O stock universe ONCE after the close, under the
    # BULK broker class, so it never competes with live decision traffic.
    STOCK_SPOT_SWEEP_ENABLED: bool = True
    # Intervals to sweep. 30minute is the positional/decision grid; 3minute is
    # the fast-lane grid and is even thinner today (22 names 07-15, ZERO 07-16,
    # 67 07-17 — all live_tick/aggregate, no durable writer).
    STOCK_SPOT_SWEEP_INTERVALS: str = "30minute,3minute"
    # Trailing sessions re-fetched each run. >1 self-heals a missed evening
    # (holiday, restart, dead token) without a manual backfill. Bounded.
    STOCK_SPOT_SWEEP_DAYS: int = 3
    # Hard ceiling on symbols per run (0 = the whole catalog). Safety valve.
    STOCK_SPOT_SWEEP_MAX_SYMBOLS: int = 0
    # Pacing between per-symbol broker calls (~3 req/s — well inside Fyers
    # 10/s + 200/min, and BULK is capped at 25% of the shared budget anyway).
    STOCK_SPOT_SWEEP_SLEEP_SECONDS: float = 0.35
    # Pre-open broker token readiness sweep (07:00-09:20 IST, NSE session days):
    # validates Fyers (auto-refresh via saved refresh token + PIN when the daily
    # access token is dead) + checks Upstox expiry, and logs/alerts BEFORE open
    # so a dead token is an actionable pre-open fact, not a mid-session surprise.
    TOKEN_READINESS_AUTO_ENABLED: bool = True
    # Phase 6 — Fyers v3 TBT 50-level depth socket (FyersTbtSocket). OFF by default:
    # requires the PAID TBT entitlement on the Fyers app. When True, /ws/depth routes a
    # focused symbol through the TBT socket (50 levels + per-level order counts + seqNo)
    # instead of the 5-level DataSocket DepthUpdate. Enable only after confirming
    # entitlement + a market-open verification; falls back to 5-level on any TBT error.
    FYERS_TBT_DEPTH_ENABLED: bool = False
    # Phase-2 sector coverage: stream the full ~206 F&O sector constituents on the
    # broker WS (for sector-network live quotes). OFF by default — +40-90 MB backend
    # RSS (live_candle_store per-symbol OHLC) on a 3.7 GiB box. Enable only after v1
    # is retired and RSS headroom is confirmed; sector INDICES stream regardless.
    STOCK_WS_SUBSCRIPTIONS_ENABLED: bool = False
    # Legacy global bypass flag — used as a fallback when the per-lane
    # flags aren't set. Kept for backward compatibility with the prod .env;
    # new code should consult NSE_S1_/NSE_S2_BYPASS_MARKET_PROFILE_GATE.
    NSE_STRATEGY_BYPASS_MARKET_PROFILE_GATE: bool = False
    # Per-lane bypass flags. S1 (monthly options, 30m MACD) is currently
    # kept on bypass to preserve paper-book continuity; S2 transitioned to
    # the MP+OF engine where the gate is the signal itself, so its bypass
    # is vestigial but kept for emergency rollback.
    NSE_S1_BYPASS_MARKET_PROFILE_GATE: bool = True
    NSE_S2_BYPASS_MARKET_PROFILE_GATE: bool = False
    # When true, S2 evaluates the MP+OF engine (the same one driving the
    # commodity desk) on 1-min index spot before falling back to the legacy
    # 15-min option-premium MACD path. The MP+OF path emits BUY/SELL plus
    # the standard mp_* fields, which the lane maps to ATM CE/PE on the
    # weekly+monthly expiry tracks declared in strategy2_mp_of.S2_EXPIRY_ROUTING.
    NSE_S2_USE_MP_OF_ENGINE: bool = True
    # Attribution honesty for S2 when the MP+OF engine is silent (insufficient
    # 1-min history / no prior session). "named_fallback" (default) preserves
    # the current trade BEHAVIOR — the legacy MACD gate still fires — but the
    # emitted signal is explicitly labelled `s2_macd_fallback` so the paper
    # journal records which engine actually fired (never an unlabelled MP_OF).
    # "block" is an owner opt-in that skips the trade instead (labelled
    # `s2_blocked`). Only "block" changes WHEN S2 trades.
    NSE_S2_ON_INSUFFICIENT_MP: str = "named_fallback"  # "named_fallback" | "block"
    PAPER_TRADING_ONLY: bool = False
    PAPER_RUNTIME_PREWARM_ENABLED: bool = True
    MARKET_HOURS_PAPER_SUPERVISOR_ENABLED: bool = True
    MARKET_HOURS_SUPERVISOR_LOOP_SECONDS: int = 15
    # WS-0.5a — hard ceiling on a single runner's callback so one hung scan can't
    # stall the gather (and every other lane) indefinitely. Deliberately generous:
    # it catches true hangs, not slow-but-working scans. Tighten per-lane once the
    # WS-0.2 nomad_scan_duration_seconds p99 is known. Per-runner override:
    # RunnerConfig.timeout_seconds.
    MARKET_HOURS_SUPERVISOR_RUNNER_TIMEOUT_SECONDS: int = 300
    MARKET_INTELLIGENCE_AUTO_ENABLED: bool = True
    MARKET_INTELLIGENCE_REFRESH_INTERVAL_SECONDS: int = 60
    MARKET_INTELLIGENCE_GAP_FILL_LOOKBACK_DAYS: int = 10
    MARKET_INTELLIGENCE_FULL_WATCHLIST_REFRESH_MINUTES: int = 15
    MARKET_INTELLIGENCE_STRATEGY_LOCAL_ONLY: bool = False
    STRATEGY_LEARNING_ENABLED: bool = True
    STRATEGY_LEARNING_LOOKBACK_DAYS: int = 120
    # Bring online faster — 2 closed trades is enough to start ranking
    # (default was 3 which on S1's 5-trades-a-day pace took weeks to
    # accumulate per (underlying, option_type, signal_reason) tuple).
    STRATEGY_LEARNING_MIN_TRADES: int = 2
    # Once a tuple has hit MIN_TRADES *and* shows <25% win rate with
    # negative expectancy, the learning gate refuses fresh entries for
    # it. Without this flag, learning was purely a size/confidence
    # advisor and never actually filtered bad-history setups out.
    STRATEGY_LEARNING_BLOCK_ENTRIES_ENABLED: bool = True
    # The BLOCK decision needs more evidence than scoring: blocking on 2
    # closed trades made 2 early losses a PERMANENT ratchet (blocked keys
    # can never trade their way back inside the lookback window).
    STRATEGY_LEARNING_BLOCK_MIN_TRADES: int = 6
    # S1 (NSE ATM MACD) re-entry path. When True, an underlying whose
    # 30-min MACD is already above zero (CE) or below zero (PE) can fire
    # a new entry on a fresh 15-min MACD zero-cross. Catches intraday
    # pullback re-entries within an established higher-TF trend,
    # especially after a prior position closed via stop/target. Set to
    # False to revert to the strict "30-min cross only" behavior.
    NSE_S1_ALLOW_15M_REENTRY: bool = True
    # Per-purpose adapter routing. The goal is to distribute load between the
    # two live brokers so neither hits 429 first and a single rate-limit storm
    # doesn't cascade across desks. Rationale per purpose:
    #
    #   live_ticks      Fyers' websocket is more responsive and exposes L2 +
    #                   aggressor-side data Upstox doesn't. Fyers first.
    #   market_profile  Needs continuous tick stream for the value-area engine.
    #                   Fyers preferred; postgres covers offline windows.
    #   order_flow      Needs depth + aggressor flags only Fyers exposes.
    #   option_chain    Upstox first — its chain endpoint has higher quota and
    #                   richer contract metadata. Fyers reserved for the few
    #                   purposes where it's uniquely capable (ticks / MP / OF)
    #                   so the chain polls don't deplete Fyers' rate budget at
    #                   startup. Catalog/postgres fallback if both brokers fail.
    #   historical      DB-cached candles served first; Upstox analytics token
    #                   has the deepest history; live Upstox + Fyers as backup.
    #   analytics       DB first; brokers only as a backfill for missing rows.
    MARKET_DATA_LIVE_TICK_ORDER: str = "fyers,upstox"
    MARKET_DATA_MARKET_PROFILE_ORDER: str = "fyers,postgres,upstox"
    MARKET_DATA_ORDER_FLOW_ORDER: str = "fyers,upstox"
    MARKET_DATA_OPTION_CHAIN_ORDER: str = "upstox,fyers,catalog"
    MARKET_DATA_HISTORICAL_ORDER: str = "postgres,upstox_analytics,upstox,fyers"
    MARKET_DATA_ANALYTICS_ORDER: str = "postgres,upstox_analytics,upstox"
    DATA_QUALITY_SCAN_GATE_ENABLED: bool = True
    AUCTION_INTELLIGENCE_AUTO_ENABLED: bool = True
    AUCTION_INTELLIGENCE_AUTO_INTERVAL_SECONDS: int = 180
    INSTITUTIONAL_CONVERGENCE_AUTO_ENABLED: bool = True
    # FAST-lane cadence (timeframe policy 2026-07-15): the convergence rules
    # evaluate closed 3-minute bars, so scans align to 3m bar closes (180s)
    # instead of re-running mid-bar every 60s.
    INSTITUTIONAL_CONVERGENCE_AUTO_INTERVAL_SECONDS: int = 180
    INSTITUTIONAL_CONVERGENCE_INDEX_SYMBOLS: str = "NIFTY,BANKNIFTY"
    INSTITUTIONAL_CONVERGENCE_STOCK_COUNT: int = 10
    INSTITUTIONAL_CONVERGENCE_SETUP_WINDOW_BARS: int = 5
    INSTITUTIONAL_CONVERGENCE_MIN_CONFIRMATIONS: int = 2
    INSTITUTIONAL_CONVERGENCE_MAX_CHASE_ATR: float = 0.5
    INSTITUTIONAL_CONVERGENCE_MIN_REWARD_RISK: float = 1.5
    # Commodity (MCX) variant of the convergence lane: same rules engine on
    # the active front-month futures, evening-session square-off (23:15), no
    # VIX / noon-quarantine gates (those are NSE-session concepts).
    INSTITUTIONAL_CONVERGENCE_COMMODITY_ENABLED: bool = True
    # 3-minute bars → 180s scans aligned to bar closes (timeframe policy).
    INSTITUTIONAL_CONVERGENCE_COMMODITY_INTERVAL_SECONDS: int = 180
    INSTITUTIONAL_CONVERGENCE_COMMODITY_SYMBOLS: str = "GOLD,SILVERM,CRUDEOIL,NATURALGAS,COPPER,ALUMINI,ZINCMINI,NICKEL"
    # Auction-Intelligence COMMODITY sleeve (2026-07-16): the same MP+order-flow
    # auction machinery (market profile from the unified 1-minute MCX store
    # aggregated to 30-minute auction bars + real MCX tick-tape order flow) run
    # over a small set of liquid MCX roots during the EVENING/extended MCX
    # session (09:00-23:30) when NSE is closed. Trades the ACTIVE front-month
    # futures directly (no options remap) into a SEPARATE paper book so the NSE
    # index auction book and the commodity book never collide. Default roots are
    # the most liquid three; widen via env. Interval mirrors the NSE lane.
    AUCTION_INTELLIGENCE_COMMODITY_ENABLED: bool = True
    AUCTION_INTELLIGENCE_COMMODITY_INTERVAL_SECONDS: int = 180
    AUCTION_INTELLIGENCE_COMMODITY_SYMBOLS: str = "GOLD,SILVERM,CRUDEOIL"
    # Real order-flow book source (2026-06-03). Maps an index app-symbol to the
    # market_ticks symbol whose REAL order book feeds auction-intelligence order
    # flow — a front-month futures or ATM option contract, which (unlike the
    # order-book-less index) carries genuine bid/ask sizes + tape. Format:
    #   "NSE:NIFTY50-INDEX=NSE:NIFTY26JUNFUT,BSE:SENSEX-INDEX=BSE:SENSEX26JUNFUT"
    # EMPTY = OFF → the desk stays on the legacy bar-inference path (unchanged
    # behaviour). When set, those book symbols are also pinned onto the WS
    # capture set so their ticks land in market_ticks. Flag-gated so it can be
    # enabled + verified during a live RTH session without risking the default.
    AUCTION_OF_BOOK_SYMBOLS: str = ""
    # Automatically map supported indices to the current Fyers front-month
    # futures contract. Explicit AUCTION_OF_BOOK_SYMBOLS entries override the
    # generated value per index.
    AUCTION_OF_BOOK_AUTO_ENABLED: bool = True
    # FMP lane parked out of production 2026-07-07 (owner: "remove FMP + sniper,
    # revisit later; preserve the work"). Runner registers with enabled=False so
    # the supervisor filters it out of every scheduling pass; the fmp_service
    # singleton + read-only router/WS stay importable. Flip back to True to revive.
    FRACTAL_MARKET_PROFILE_AUTO_ENABLED: bool = False
    FRACTAL_MARKET_PROFILE_AUTO_INTERVAL_SECONDS: int = 300
    DIRECTIONAL_OPTIONS_AUTO_ENABLED: bool = True
    # FAST-lane cadence (timeframe policy 2026-07-15): the strategy's default
    # timeframe is now 3-minute bars (5m/15m stay selectable via the API), so
    # the runner fires every 180s — aligned to 3m bar closes. Every fresh bar
    # is evaluated exactly once per close instead of re-scanning mid-bar.
    DIRECTIONAL_OPTIONS_AUTO_INTERVAL_SECONDS: int = 180
    # POSITIONAL options strategy (2026-06-28): the researched directional edge —
    # multi-day hold, MONTHLY ATM contract, HTF-direction backbone CONFIRMED by
    # option positioning (oi_build / PCR from directional_positioning_daily), with
    # a single position per underlying and a 30% hard stop. Default OFF; when on,
    # predict() uses the positioning-confirmed positional view instead of the
    # legacy momentum sum. Edge measured small + cost-sensitive on indices, so
    # this is a forward PAPER A/B. IV state SIZES the entry, never vetoes it
    # (2026-07-17 owner directive) — signals.compute_iv_sizing_factor scales
    # the base risk budget from d_atm_iv / ATM-IV percentile; the former
    # d_atm_iv>=0 hard gate is retired. pcr_low/high = the call-heavy /
    # put-heavy confirmation thresholds for CE / PE.
    DIRECTIONAL_POSITIONAL_OPTIONS_ENABLED: bool = False
    DIRECTIONAL_POSITIONAL_PCR_LOW: float = 0.9
    DIRECTIONAL_POSITIONAL_PCR_HIGH: float = 1.2
    DIRECTIONAL_POSITIONAL_STOP_PCT: float = 0.30
    # Positioning feed must be fresh to take NEW positional entries: decline new
    # trades when the latest directional_positioning_daily row lags the most
    # recent finalized NSE session by more than this many sessions (held
    # positions still exit on stop/target/DTE). Paired with the daily refresh
    # runner below so it rarely trips in steady state.
    DIRECTIONAL_POSITIONAL_MAX_STALE_SESSIONS: int = 1
    # Validated contract window for positional entries (backtest_indices_monthly:
    # MONTHLY ATM, DTE 8-22). The selector filters to this window whenever the
    # signal is positional; the regime's weekly preference applies only to the
    # legacy intraday view.
    DIRECTIONAL_POSITIONAL_DTE_MIN: int = 8
    DIRECTIONAL_POSITIONAL_DTE_MAX: int = 22
    # Once-per-session post-close refresh of directional_positioning_daily.
    DIRECTIONAL_POSITIONING_REFRESH_INTERVAL_SECONDS: int = 3600
    # ── NIFTY-50 stock expansion of the directional universe (2026-07-17) ──
    # Owner: "include Nifty 50 stocks — it lacks proper signal generation."
    # Indices (NIFTY/BANKNIFTY/SENSEX) keep the positional-confirmation path;
    # stocks route through the standard signal engine and are hard-guarded by
    # per-symbol data readiness (fresh spot bars + live ATM watchlist rows).
    # Single-flag revert: set False to restore the 3-index universe.
    DIRECTIONAL_INCLUDE_STOCK_UNIVERSE: bool = True
    # Per-cycle stock scan load bounds: the runner scans indices serially,
    # then a ROTATING batch of ready stocks under a concurrency semaphore
    # with a per-symbol wait_for. Full NIFTY-50 rotation completes in
    # ceil(ready/batch) cycles (2 cycles at the defaults) — bounded worst
    # case ≈ 3×75s (indices) + ceil(25/5)×30s (stocks) ≈ 375s < the 600s
    # runner timeout, and typically well under one 180s cadence interval.
    # (2026-07-20: the stock bound moved 20s→30s below; this budget line was
    # left stale by that change and is corrected here. Re-derive it whenever
    # DIRECTIONAL_STOCK_SYMBOL_TIMEOUT_SECONDS or the batch size moves.)
    DIRECTIONAL_STOCK_SCAN_CONCURRENCY: int = 5
    # A cold but healthy stock snapshot measured 18-20s under concurrent lane
    # load. Keep a small completion margin; deeper stages retain their own
    # fail-closed broker/RAG bounds.
    DIRECTIONAL_STOCK_SYMBOL_TIMEOUT_SECONDS: float = 30.0
    DIRECTIONAL_STOCK_BATCH_SIZE: int = 25
    DIRECTIONAL_INDEX_SYMBOL_TIMEOUT_SECONDS: float = 75.0
    # Honesty bound for stock option quotes: the stock ATM watchlist rows
    # refresh via the round-robin premium top-up (~minutes per name, unlike
    # the ~35s index refresh). A stock is only evaluated when its freshest
    # watchlist row is younger than this; older quotes = skip-and-report
    # (no fail-open entries against stale premiums).
    DIRECTIONAL_STOCK_WATCHLIST_MAX_AGE_SECONDS: int = 1200
    # Just-in-time watchlist refresh for the directional stock batch
    # (2026-07-17): the BG universe build rotates all ~217 F&O names over
    # HOURS, so stock snapshot rows aged far past the 1200s bound on day one
    # (telemetry: ready=0, option_quotes_stale_9000-12000s on all 50 names
    # while their spot streams were current). The runner now refreshes ONLY
    # its ~25-name cycle batch just-in-time (CLASS_STANDARD, chain-semaphore
    # admitted, hard budget) and re-checks honesty afterwards — refresh
    # failures stay skipped, never fail-open. Rows younger than the REFRESH
    # age are not re-fetched (they're already honest enough to trade).
    DIRECTIONAL_STOCK_WATCHLIST_REFRESH_AGE_SECONDS: float = 180.0
    DIRECTIONAL_STOCK_WATCHLIST_REFRESH_BUDGET_SECONDS: float = 90.0
    DIRECTIONAL_STOCK_WATCHLIST_REFRESH_CONCURRENCY: int = 3
    # ── Directional anti-churn re-entry cooldowns (2026-07-17) ─────────────
    # OWNER DIRECTIVE (~13:40 IST): "uncap signals, no hard gate. but see
    # that the lane has sane strategy instead of just opening and closing
    # posiitons." Signal generation stays UNCAPPED — every cycle journals a
    # signal; discipline lives in the EXECUTION layer only. After a close on
    # an underlying, NEW entries on that same underlying wait out a cooldown
    # (proposals during the window are journaled as status=cooldown_skip with
    # seconds remaining — visible, never silently dropped, and never counted
    # as a policy act). Flat/flip/target/DTE closes use the FLAT window
    # (900s = 5 bars at the 3m cadence); a stop-out means the thesis was
    # FALSIFIED, so immediate re-entry is churn — it waits the longer STOP
    # window. Protective exits (stop/target/DTE/expiry) are NEVER delayed by
    # cooldowns. A confirmed close-and-reverse flip is one atomic decision
    # and opens its reverse leg in the same cycle (the 2-cycle flip
    # confirmation in paper.py is what stops single-cycle whipsaws).
    DIRECTIONAL_REENTRY_COOLDOWN_FLAT_SECONDS: float = 900.0
    DIRECTIONAL_REENTRY_COOLDOWN_STOP_SECONDS: float = 1800.0
    # CBE alpha engine runs at EOD. Cadence = 1 hour: during market hours
    # the daily MACD/RSI indicators use completed sessions only, so re-running
    # intra-day is idempotent. A post-close catch-up after the ingestion grace
    # period gives the canonical EOD scan for the next session.
    CBE_SCANNER_AUTO_ENABLED: bool = True
    CBE_SCANNER_AUTO_INTERVAL_SECONDS: int = 3600
    # Declared as real Settings fields (extra="ignore" silently drops env
    # overrides for knobs read only via getattr). cbe_marks refreshes paper
    # marks; gann runs its own 60s paper cycle on 15-min signal bars.
    CBE_MARKS_REFRESH_INTERVAL_SECONDS: int = 300
    # PARKED 2026-07-21 (owner call): default False stops the supervisor runner
    # from ever cycling the Gann lane. Code, tables, tests and research stay in
    # the repo untouched; flip this to re-activate (registry execution_mode in
    # core/lane_registry.py should be flipped back to "paper" at the same time).
    GANN_TP_DELTA_AUTO_ENABLED: bool = False
    GANN_TP_DELTA_AUTO_INTERVAL_SECONDS: int = 60
    # S1: a held option mark older than this during NSE hours is stale (contract
    # fell off the ATM rotation / broker refresh not landing) → skip price-based
    # exits that cycle so a frozen mark can't misfire hard_stop/macd_reversal.
    MACD_STRATEGY_MARK_STALE_SECONDS: int = 1200
    # S1: minimum hold before macd_reversal_30m may fire (one 30m bar). Entry
    # uses a synthetic forming bar while the exit MACD is persisted-close only;
    # this stops the reversal exit from churning a position out of its entry bar.
    MACD_STRATEGY_REVERSAL_MIN_HOLD_SECONDS: int = 1800
    # Lane signal-correctness audit (audits framework) — post-close once-daily
    # replay-parity / gate-attribution / reconciliation for every registered
    # lane, so live signals are mechanically checked against the strategy
    # definition instead of only via P&L. Persists to lane_audit.
    LANE_AUDIT_ENABLED: bool = True
    LANE_AUDIT_INTERVAL_SECONDS: int = 3600
    COMMODITY_FYERS_RATE_LIMIT_BACKOFF_SECONDS: int = 90
    COMMODITY_KILL_LOCK: bool = False
    # ── Commodity MP+OF module (restored from feat 2026-06-30) ─────────────
    # Durable commodity MP history: write-once backfill from the MCX 1-min spot
    # store + post-close/gap repair; profiles at the per-instrument coarse value
    # tick so the live HTF gate reads non-degenerate value areas.
    COMMODITY_MP_HISTORY_AUTO_ENABLED: bool = True
    COMMODITY_MP_HISTORY_AUTO_INTERVAL_SECONDS: int = 21600  # 6h — boot + post-close
    COMMODITY_MP_HISTORY_BACKFILL_SESSIONS: int = 90
    # Higher-timeframe (weekly/monthly) alignment gate. The commodity MP+OF desk
    # is DIRECTIONAL + POSITIONAL: trades the weekly+monthly value-area bias and
    # holds for hours/days. ON by default (2026-06-24 positional redesign).
    COMMODITY_HTF_GATE_ENABLED: bool = True
    # When True a signal OPPOSING the HTF bias is BLOCKED outright (not downgraded
    # to a counter-trend scalp).
    COMMODITY_HTF_REQUIRE_ALIGNMENT: bool = True
    # Positional trade does NOT flip on a single opposite 1-min MACD — holds to
    # stop/target/runner-trail (core anti-churn).
    COMMODITY_POSITIONAL_HOLD_ENABLED: bool = True
    # Re-entry cooldown (minutes) after ANY exit on an underlying. 0 disables.
    COMMODITY_REENTRY_COOLDOWN_MINUTES: int = 20
    # A protective stop invalidates that exact MP thesis (root + direction +
    # setup) for the rest of the MCX session. A new session rebuilds value/IB
    # and therefore permits a genuinely new auction thesis.
    COMMODITY_SETUP_STOP_LOCK_ENABLED: bool = True
    # Index-futures MP+OF sleeve. Default OFF (needs index_futures_candles
    # populated + a live writer; never enable around a monthly expiry roll).
    COMMODITY_INDEX_FUTURES_ENABLED: bool = False
    COMMODITY_INDEX_FUTURES_MIN_DAYS_TO_EXPIRY: int = 2
    # Daily + per-underlying realized-loss caps remain deferred. Structural
    # stops, risk sizing, setup invalidation and the portfolio DD backstop stay
    # active; do not let a calendar cap override the market-led position state.
    COMMODITY_LOSS_CAPS_ENABLED: bool = False
    # Risk-size from the structural invalidation stop. 0.25% of the ₹50L paper
    # book = ₹12.5K maximum planned risk per position. If one exchange lot alone
    # exceeds the budget, the setup is skipped rather than using a fake tight SL.
    COMMODITY_RISK_PER_TRADE_PCT: float = 0.0025
    # Responsive MP trades target POC/another supplied structure. Do not enter
    # when that structural target offers less than 1R from the invalidation stop.
    COMMODITY_MIN_STRUCTURE_TARGET_R: float = 1.0
    # Responsive auction trades (failed auction / LVN fade) are the scalp
    # sleeve.  They may occupy at most 20% of a rolling entry sample; the cap
    # never creates a scalp when no valid responsive setup exists.
    COMMODITY_SCALP_MAX_TRADE_SHARE: float = 0.20
    COMMODITY_SCALP_MIX_LOOKBACK: int = 20
    # Scalp sizing fraction; scalp vs positional target R; scalp time-stop
    # (1-min bars).
    COMMODITY_HTF_SCALP_SIZE_FRACTION: float = 0.5
    COMMODITY_HTF_SCALP_TARGET_R: float = 1.0
    COMMODITY_HTF_POSITIONAL_TARGET_R: float = 2.0
    COMMODITY_HTF_SCALP_MAX_HOLD_BARS: int = 6
    # Range-adaptive + wider futures stop off the flat 0.5% noise band. OFF.
    COMMODITY_STOP_WIDENING_ENABLED: bool = False
    # Textbook MP+OF context: absorption means aggressive flow without price
    # progress; OF-dependent setups require usable volume; day type controls
    # initiative vs responsive entries; targets use prior POC magnets; CVD is
    # normalized to each instrument's own historical MP-period distribution.
    COMMODITY_LVN_ABSORPTION_FIX_ENABLED: bool = True
    COMMODITY_OF_QUALITY_GATE_ENABLED: bool = True
    COMMODITY_OF_MIN_VOL_COVERAGE: float = 0.70
    COMMODITY_DAYTYPE_ENABLED: bool = True
    COMMODITY_DAYTYPE_EXCLUDE_SYMBOLS: str = ""
    COMMODITY_NAKED_POC_TARGET_ENABLED: bool = True
    COMMODITY_NAKED_POC_MIN_R_FRACTION: float = 0.5
    COMMODITY_VOL_BASELINE_ENABLED: bool = True
    # Initiative entries need at least modest directionally-aligned pressure
    # relative to the instrument's own median 15-minute volume.
    COMMODITY_MIN_OF_PRESSURE_RATIO: float = 0.25
    # Cost-robust setup isolated by the causal ATR walk-forward: accept only a
    # trend-day IB break that is not late relative to POC and whose existing
    # planned invalidation has enough room for a volatility expansion. The ATR
    # thresholds are entry filters; they never widen a stop or impose a hold.
    COMMODITY_HIGH_CONVICTION_SETUP_ENABLED: bool = True
    COMMODITY_HIGH_CONVICTION_MAX_POC_DISTANCE_ATR: float = 3.0
    COMMODITY_HIGH_CONVICTION_MIN_STOP_DISTANCE_ATR: float = 3.0
    SECTOR_INTERACTION_DURABLE_STATE_ENABLED: bool = False
    # Post-close (once/session) sector-interaction ingestion runner: IN public
    # open-data collectors + live-market sector observations + NSE constituent
    # CSV sync. Fills the durable ingestion store toward the 24-distinct-date
    # runtime handoff that flips /api/sector-interaction/signals off synthetic.
    SECTOR_INGESTION_AUTO_ENABLED: bool = True

    # Security
    SECRET_KEY: str = "change-me-to-a-random-secret-key"
    APP_TOKEN_AUTH_ENABLED: bool = False
    APP_WRITE_TOKEN: str = ""

    # Fyers
    FYERS_APP_ID: str = ""
    FYERS_SECRET: str = ""
    FYERS_REDIRECT_URI: str = FYERS_FIXED_REDIRECT_URI
    FYERS_PIN: str = ""

    # Upstox
    UPSTOX_API_KEY: str = ""
    UPSTOX_SECRET: str = ""
    UPSTOX_REDIRECT_URI: str = UPSTOX_SANDBOX_REDIRECT_URI
    UPSTOX_ANALYTICS_TOKEN: str = ""

    # 5Paisa
    FIVEPAISA_APP_NAME: str = ""
    FIVEPAISA_APP_SOURCE: str = ""
    FIVEPAISA_USER_ID: str = ""      # client code (e.g. NL0BYabni01)
    FIVEPAISA_EMAIL: str = ""        # registered email address (for TOTP login)
    FIVEPAISA_PASSWORD: str = ""     # account password / TPIN
    FIVEPAISA_USER_KEY: str = ""
    FIVEPAISA_ENCRYPTION_KEY: str = ""

    # ICICI Direct Breeze
    ICICI_BREEZE_API_KEY: str = ""
    ICICI_BREEZE_SECRET: str = ""

    # Anthropic
    ANTHROPIC_API_KEY: str = ""

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""
    TELEGRAM_REPORTS_ENABLED: bool = False
    TELEGRAM_REPORT_INTERVAL: str = "1h"
    TELEGRAM_EVENT_ALERTS_ENABLED: bool = True
    TELEGRAM_EVENT_MIN_SEVERITY: str = "warning"
    TELEGRAM_RATE_LIMIT_PER_MINUTE: int = 12

    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors(cls, v):
        if isinstance(v, str):
            import json
            return json.loads(v)
        return v

    @field_validator("BACKEND_CORS_ORIGIN_REGEX", mode="before")
    @classmethod
    def parse_optional_regex(cls, v):
        if v is None:
            return None
        value = str(v).strip()
        return value or None

    @field_validator("FYERS_REDIRECT_URI", mode="before")
    @classmethod
    def normalize_fyers_redirect(cls, v):
        return normalize_fyers_redirect_uri(v)

    @field_validator("UPSTOX_REDIRECT_URI", mode="before")
    @classmethod
    def normalize_upstox_redirect(cls, v):
        return normalize_upstox_redirect_uri(v)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


def auction_of_book_symbols() -> dict[str, str]:
    """Parse AUCTION_OF_BOOK_SYMBOLS into {index_app_symbol: book_market_ticks_symbol}.

    Format: "NSE:NIFTY50-INDEX=NSE:NIFTY26JUNFUT,BSE:SENSEX-INDEX=BSE:SENSEX26JUNFUT".
    Empty / malformed entries are skipped; empty string → {} (feature OFF).
    """
    out: dict[str, str] = {}
    for pair in str(settings.AUCTION_OF_BOOK_SYMBOLS or "").split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        key, value = key.strip(), value.strip()
        if key and value:
            out[key] = value
    return out


def auction_front_month_book_symbols(as_of: date | None = None) -> dict[str, str]:
    """Return explicit book mappings plus calendar-rolled Fyers futures."""
    explicit = auction_of_book_symbols()
    if not settings.AUCTION_OF_BOOK_AUTO_ENABLED:
        return explicit

    from data.index_futures_backfill import fyers_front_month_symbol

    contract_date = as_of or datetime.now(ZoneInfo("Asia/Kolkata")).date()
    generated = {
        "NSE:NIFTY50-INDEX": fyers_front_month_symbol("NIFTY", contract_date),
        "NSE:BANKNIFTY-INDEX": fyers_front_month_symbol("BANKNIFTY", contract_date),
        "BSE:SENSEX-INDEX": fyers_front_month_symbol("SENSEX", contract_date),
    }
    generated.update(explicit)
    return generated
