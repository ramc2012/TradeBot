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
    FRACTAL_MARKET_PROFILE_AUTO_ENABLED: bool = True
    FRACTAL_MARKET_PROFILE_AUTO_INTERVAL_SECONDS: int = 300
    DIRECTIONAL_OPTIONS_AUTO_ENABLED: bool = True
    # Strategy operates on 5- and 15-minute bars. A 300s cadence misses
    # the close of a fresh 5-min bar by up to 4 minutes and produces stale
    # decisions on 15-min bars. 60s ensures every fresh bar is evaluated
    # within one cycle of its close, which the broker quote refresh rate
    # comfortably supports.
    DIRECTIONAL_OPTIONS_AUTO_INTERVAL_SECONDS: int = 60
    # POSITIONAL options strategy (2026-06-28): the researched directional edge —
    # multi-day hold, MONTHLY ATM contract, HTF-direction backbone CONFIRMED by
    # option positioning (oi_build / PCR from directional_positioning_daily), with
    # a single position per underlying and a 30% hard stop. Default OFF; when on,
    # predict() uses the positioning-confirmed positional view instead of the
    # legacy momentum sum. Edge measured small + cost-sensitive on indices, so
    # this is a forward PAPER A/B. Vol gate (d_atm_iv>=0) sourced live where
    # history is null. pcr_low/high = the call-heavy / put-heavy confirmation
    # thresholds for CE / PE.
    DIRECTIONAL_POSITIONAL_OPTIONS_ENABLED: bool = False
    DIRECTIONAL_POSITIONAL_PCR_LOW: float = 0.9
    DIRECTIONAL_POSITIONAL_PCR_HIGH: float = 1.2
    DIRECTIONAL_POSITIONAL_STOP_PCT: float = 0.30
    # CBE alpha engine runs at EOD. Cadence = 1 hour: during market hours
    # the daily MACD/RSI indicators use completed sessions only, so re-running
    # intra-day is idempotent. A post-close catch-up after the ingestion grace
    # period gives the canonical EOD scan for the next session.
    CBE_SCANNER_AUTO_ENABLED: bool = True
    CBE_SCANNER_AUTO_INTERVAL_SECONDS: int = 3600
    COMMODITY_FYERS_RATE_LIMIT_BACKOFF_SECONDS: int = 90
    COMMODITY_KILL_LOCK: bool = False
    SECTOR_INTERACTION_DURABLE_STATE_ENABLED: bool = False

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
