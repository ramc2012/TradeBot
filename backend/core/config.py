from __future__ import annotations
from functools import lru_cache
from pathlib import Path
from typing import List
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

    # DB / Redis
    DATABASE_URL: str = "postgresql+asyncpg://nomadcurie:nomadcurie@localhost:5433/nomadcurie"
    DATABASE_POOL_SIZE: int = 0
    DATABASE_MAX_OVERFLOW: int = 0
    DATABASE_POOL_TIMEOUT_SECONDS: int = 15
    DATABASE_POOL_RECYCLE_SECONDS: int = 900
    REDIS_URL: str = "redis://localhost:6383/0"
    # Bound the Redis connection pool well under the server's maxclients (10000).
    # Unbounded, the pool grew without limit under tick-cache SET bursts (amplified
    # when the loop blocks on heavy scans) until it exhausted maxclients and tick
    # pub/sub failed (2026-06-05). Idle connections are reused, so this is headroom.
    REDIS_MAX_CONNECTIONS: int = 1000
    RESEARCH_SYNC_AUTO_ENABLED: bool = False
    RESEARCH_SYNC_EMBEDDED_ENABLED: bool = False
    # Automatic historical-data backfill (data.historical_backfill). When enabled,
    # a background loop detects missing coverage vs DEFAULT_TARGETS (spot 5Y/30m +
    # 1Y/1m, options ATM-band 5Y/30m + 1Y/1m, commodity 5Y/30m + 2Y/1m) and pulls
    # only the gaps. Idempotent + resumable. OFF by default; enable on the runtime box.
    AUTO_BACKFILL_ENABLED: bool = False
    AUTO_BACKFILL_ON_STARTUP: bool = True
    AUTO_BACKFILL_POLL_MINUTES: int = 60
    # Option contracts processed per pass (bounded + resumable across passes).
    AUTO_BACKFILL_MAX_OPTION_CONTRACTS: int = 300
    # How many recent expiries per index to backfill options for. Small = "current
    # contract" scope (fast); raise for deeper expired-options history (slow — the
    # full multi-year sweep is broker-latency-bound under live-app contention).
    AUTO_BACKFILL_OPTION_MAX_EXPIRIES: int = 2
    # MACD diffusion — hourly CE/PE-above-zero breadth snapshot (market sentiment).
    # Reads the live watchlist's per-leg MACD; seeds history from option_premium_candles.
    MACD_DIFFUSION_ENABLED: bool = True
    MACD_DIFFUSION_POLL_MINUTES: int = 60
    MACD_DIFFUSION_BACKFILL_DAYS: int = 21
    # MCX auto-rollover — keep the MP+OF agent's configured futures on their
    # current front-month so the watchlist never tracks an expired contract.
    MCX_ROLLOVER_ENABLED: bool = True
    MCX_ROLLOVER_POLL_HOURS: int = 6
    STRATEGY_SPOT_SYNC_ENABLED: bool = False
    # F1 feed — full-universe option-chain → 3m CE+PE OHLC builder (chain_candle_builder).
    # OFF by default: scales Fyers REST (~30k calls/day, governed by FYERS_DATA_LIMITER);
    # enable deliberately in prod after sign-off + a market-open verification.
    CHAIN_CANDLE_BUILDER_ENABLED: bool = False
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
    # Auto-resolve the front-month index FUTURES as the order-flow book contract
    # (futures carry real bid/ask depth + tape; the index spot does not), so the
    # auction lane can stream genuine order flow instead of bar-inference. When
    # ON, the resolved {index→front-month-future} map is pinned onto the tick-
    # capture WS so depth lands in market_ticks. Default OFF → unchanged (index
    # spot only); the contract rolls monthly so enabling needs a live session to
    # confirm the broker delivers depth, and a restart picks up the next roll.
    AUCTION_OF_AUTO_FUTURES_BOOK: bool = False
    AUCTION_OF_FUTURES_INDICES: str = "NIFTY,BANKNIFTY"
    FRACTAL_MARKET_PROFILE_AUTO_ENABLED: bool = True
    FRACTAL_MARKET_PROFILE_AUTO_INTERVAL_SECONDS: int = 300
    DIRECTIONAL_OPTIONS_AUTO_ENABLED: bool = True
    # Strategy operates on 5- and 15-minute bars. A 300s cadence misses
    # the close of a fresh 5-min bar by up to 4 minutes and produces stale
    # decisions on 15-min bars. 60s ensures every fresh bar is evaluated
    # within one cycle of its close, which the broker quote refresh rate
    # comfortably supports.
    DIRECTIONAL_OPTIONS_AUTO_INTERVAL_SECONDS: int = 60
    # ── Directional 1-2 DAY POSITIONAL MODE (master switch) ────────────────
    # When True, the directional_options lane is re-architected from a 5-min
    # intraday scalper into a multi-day positional book: open/flip decisions on
    # CLOSED 30-min bars, held-time counted in trading-SESSION bars (not
    # wall-clock, so the overnight gap can't force a day-2 close), ATR-based
    # adaptive target/stop, CONTINUOUS 1-min marking with immediate square-off on
    # a large move, >= min-DTE expiry selection, and confirmed-flip discipline
    # (the coordinated G1-G7 set from audit wf_d95b9a43-8d0). Tunables live in
    # directional_options.config DEFAULT_CONFIG['positional']. Default OFF: the
    # lane behaves exactly as the legacy 5-min engine. These changes are jointly
    # necessary, so a single master switch avoids dangerous partial states.
    # Requires a backend restart + paper walk-forward; do NOT enable mid-session.
    DIRECTIONAL_POSITIONAL_MODE_ENABLED: bool = False
    # ── Directional MULTI-FACTOR VIEW (independent of the hold-duration switch) ──
    # When True, the CE/PE direction is formed from a regime-gated, sign-constrained
    # confluence of orthogonal families — ATR-normalized trend backbone + ADX gate +
    # HTF alignment + LIVE option-chain tilts (25Δ skew, GEX size-damper, DEX/OI flow)
    # — instead of the legacy collinear 5-term price-momentum sum. The chain tilts can
    # therefore FLIP the side, not just confirm it. Uses only live, raw-bounded chain
    # values (no causal z-normalization or offline backtest validation yet — those need
    # a chain-history store, deferred). Tunables in DEFAULT_CONFIG['view']. Default OFF:
    # the lane uses the legacy momentum view. Validate by paper walk-forward before
    # flag-on; needs a backend restart. Orthogonal to DIRECTIONAL_POSITIONAL_MODE_ENABLED
    # (either can be on independently, but they're designed to run together).
    DIRECTIONAL_MULTIFACTOR_VIEW_ENABLED: bool = False
    # CBE alpha engine runs at EOD. Cadence = 1 hour: during market hours
    # the daily MACD/RSI indicators don't move (they're computed off last-bar-
    # of-day closes), so re-running intra-day is cheap and idempotent. The
    # supervisor's post-close catch-up gives the canonical EOD scan that
    # decides position changes for the next session.
    CBE_SCANNER_AUTO_ENABLED: bool = True
    CBE_SCANNER_AUTO_INTERVAL_SECONDS: int = 3600
    # MACD Refined — premium-MACD entry, low-IV gated, volume-led long-premium
    # book (separate CE/PE). The auto-runner fetches current + next monthly
    # expiry chains, persists per-contract volume/turnover, and syncs the
    # paper book. 30-min strategy → 60s cadence catches each fresh bar close.
    MACD_REFINED_AUTO_ENABLED: bool = True
    # Full F&O universe (~217 names) × current+next expiry chains per cycle is
    # broker-intensive; 300s keeps load sane and still catches every 30-min bar.
    MACD_REFINED_AUTO_INTERVAL_SECONDS: int = 300
    COMMODITY_FYERS_RATE_LIMIT_BACKOFF_SECONDS: int = 90
    COMMODITY_KILL_LOCK: bool = False
    SECTOR_INTERACTION_DURABLE_STATE_ENABLED: bool = False

    # ── Higher-timeframe (weekly/monthly) alignment gate ───────────────────
    # The commodity MP+OF desk is DIRECTIONAL + POSITIONAL: it trades in the
    # direction of the weekly+monthly value-area bias and holds for hours/days.
    # ON by default now (2026-06-24 positional redesign).
    COMMODITY_HTF_GATE_ENABLED: bool = True
    # When True, a signal that OPPOSES the higher-timeframe bias is BLOCKED
    # outright (directional desk) rather than downgraded to a counter-trend
    # scalp. Set False to fall back to the old "downgrade to scalp" behaviour.
    COMMODITY_HTF_REQUIRE_ALIGNMENT: bool = True
    # When True, a positional trade does NOT flip on a single opposite 1-min
    # MACD signal — it holds to its stop / target / runner-trail. This is the
    # core anti-churn lever that lets winners run for hours/days instead of
    # being scalped out after the 4-bar min-hold.
    COMMODITY_POSITIONAL_HOLD_ENABLED: bool = True
    # Re-entry cooldown (minutes) after ANY exit on an underlying — not just
    # stop-outs — to stop same-/next-bar churn. 0 disables.
    COMMODITY_REENTRY_COOLDOWN_MINUTES: int = 20
    # ── Index-futures MP+OF sleeve ─────────────────────────────────────────
    # When True, the commodity MP+OF agent also scans+trades NIFTY/BANKNIFTY
    # index FUTURES (NSE) through the same positional/HTF/anti-churn path,
    # branching only hours/data-source/specs on an is_index check. Default OFF:
    # requires a populated `index_futures_candles` table + live intraday writer
    # first, and must NOT be enabled around a monthly expiry roll. Blast radius
    # when off = zero (index symbols never enter the scan universe).
    COMMODITY_INDEX_FUTURES_ENABLED: bool = False
    # Refuse index-futures entries when the front-month contract is within this
    # many sessions of expiry (avoid trading the dying contract around the roll).
    COMMODITY_INDEX_FUTURES_MIN_DAYS_TO_EXPIRY: int = 2
    # Daily + per-underlying realized-loss caps. TEMPORARILY OFF (2026-06-24)
    # to exercise the full trade pipeline during infra testing. The 15%
    # catastrophe drawdown backstop and the stop/re-entry cooldowns still apply.
    # Flip back to True to restore the loss caps.
    COMMODITY_LOSS_CAPS_ENABLED: bool = False
    # Counter-bias scalp sizing as a fraction of the normal equal-notional lots.
    COMMODITY_HTF_SCALP_SIZE_FRACTION: float = 0.5
    # Target R-multiple for scalps vs positionals (positional default = today's 2R).
    COMMODITY_HTF_SCALP_TARGET_R: float = 1.0
    COMMODITY_HTF_POSITIONAL_TARGET_R: float = 2.0
    # Quick time-stop: close a scalp that hasn't hit its 1R target within this
    # many 1-min bars so a counter-trend probe never becomes a positional bag.
    COMMODITY_HTF_SCALP_MAX_HOLD_BARS: int = 6
    # Anti-churn: widen the initial futures stop off the flat 0.5% noise band.
    # Today the stop is max(atr, price*0.005); 14-bar 1-min ATR is ~0.01–0.35%
    # of price so max() collapses to a thin 0.5% band on every contract, and
    # adverse 1-min wicks knock positions out in minutes (the dominant commodity
    # churn driver). When True the floor becomes max(atr*FUTURES_ATR_STOP_MULT,
    # price*FUTURES_MIN_STOP_PCT_WIDE) — genuinely range-adaptive AND wider.
    # Default OFF (no behaviour change). NOTE: wider stops with
    # COMMODITY_LOSS_CAPS_ENABLED=False mean larger per-trade bleed is uncapped;
    # consider re-enabling the loss caps when turning this on.
    COMMODITY_STOP_WIDENING_ENABLED: bool = False
    # ── MP+OF gap-fixes (audit wf_7473d93a-46d; each default OFF, paper-validate) ──
    # R5: the lvn_fade "absorption" check is mis-defined (fires on SMALL flow +
    # MOVING price, sigma on the cumulative CVD series). When True, use the
    # textbook definition: LARGE per-bar flow that FAILS to move price, sigma on
    # the per-bar delta series. OFF preserves the (buggy) legacy behaviour.
    COMMODITY_LVN_ABSORPTION_FIX_ENABLED: bool = False
    # R0: per-symbol order-flow QUALITY gate. MCX bar-OHLCV volume is sparse on
    # illiquid names (NICKEL ~5% / GOLD ~25% of 1-min bars nonzero) so CVD reads
    # are noise there. When True, if a symbol's MP-period volume coverage is below
    # the floor, OF confirmations are DEMOTED to TPO/structure-only (breakouts/
    # migrations fire on price+IB structure; the pure-OF lvn_fade is suppressed).
    # Strictly subtractive. Coverage measured at the MP-period bar, not 1-min.
    COMMODITY_OF_QUALITY_GATE_ENABLED: bool = False
    COMMODITY_OF_MIN_VOL_COVERAGE: float = 0.70
    # R1: day-type-conditioned trigger suppression (the missing "balance-vs-trend,
    # then fade-or-follow" gate). When True, `assess_day_type` (price-vs-VA + IB
    # extension + POC migration + session CVD) classifies the day; on a trend day
    # counter-trend FADES (failed_auction/lvn_fade) are suppressed, in balance the
    # breakout triggers (open_drive/ib_break) are suppressed. Pure filter (never
    # creates entries). TPO-based, but IB/excess reads are noisy across the MCX
    # evening-open on illiquid names, so EXCLUDE_SYMBOLS skips those roots.
    COMMODITY_DAYTYPE_ENABLED: bool = False
    COMMODITY_DAYTYPE_EXCLUDE_SYMBOLS: str = "GOLD,NICKEL"
    # R4: anchor the exit target to a naked/virgin prior-session POC (week/month/
    # prev-day) when one sits between entry and the blind R-multiple target in the
    # trade direction (and >= MIN_R_FRACTION of the way there). Structure-anchored
    # exits / better R:R. Target-only (never an entry) → zero churn. TPO POC =
    # volume-free, safe on thin names.
    COMMODITY_NAKED_POC_TARGET_ENABLED: bool = False
    COMMODITY_NAKED_POC_MIN_R_FRACTION: float = 0.5

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

    # ── US market data (Alpaca primary, Finnhub supplement) ───────────────
    # Alpaca Market Data API (options + equities). Free key gives delayed
    # ("indicative") option data + IEX equities; paid gives OPRA/SIP realtime.
    ALPACA_API_KEY_ID: str = ""
    ALPACA_API_SECRET_KEY: str = ""
    ALPACA_OPTION_FEED: str = "indicative"   # indicative | opra
    ALPACA_STOCK_FEED: str = "iex"           # iex | sip
    # Finnhub — index/equity quotes + candles (option chain is premium-gated).
    FINNHUB_API_KEY: str = ""
    # US market module auto-runner (US RTH; paper).
    US_MARKET_AUTO_ENABLED: bool = True
    US_MARKET_AUTO_INTERVAL_SECONDS: int = 300

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
