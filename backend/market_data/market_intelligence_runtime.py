"""Central market-intelligence refresh and local-runtime reads for strategies."""
from __future__ import annotations

import asyncio
import csv
import gzip
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from loguru import logger
from sqlalchemy import text

from api.routers.auth import ensure_fyers_session, ensure_upstox_session, get_active_adapter
from db.redis_client import get_redis
from core.config import settings
from core.trading_calendar import trading_calendar
from db.database import AsyncSessionLocal
from market_data.option_chain import OC_TTL, option_chain_service
from market_data.symbols import to_broker_symbol, to_fyers_symbol
from macro_research import macro_research_service
from sector_interaction.india_live import india_live_sector_service


UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
SESSION_OPEN = time(9, 15)
NSE_INDEX_SCOPE = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX")
STRATEGY_LIVE_WATCHLIST_MAX_AGE_SECONDS = 10 * 60
STRATEGY_EXECUTION_MAX_WATCHLIST_AGE_SECONDS = 36 * 60 * 60
STRATEGY_MIN_LATEST_UNDERLYINGS = 50
APP_SYMBOLS = {
    "NIFTY": "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:BANKNIFTY-INDEX",
    "FINNIFTY": "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
    "SENSEX": "BSE:SENSEX-INDEX",
}


def _drop_contaminated_spot_rows(rows: list[dict[str, Any]], *, band: float = 0.5) -> list[dict[str, Any]]:
    """Drop cross-symbol-contaminated OHLC rows from a spot-history payload.

    The underlying_spot_candles feed occasionally carries a garbage print (e.g. a
    NIFTY minute whose high is ~54k while close ~23.4k). Such a row explodes any
    downstream TPO price-ladder build (low->high in tick steps) into thousands of
    levels, and the GIL-bound profile compute then stalls the event loop. Reject
    rows whose O/H/L/C falls outside +/-band of the median close. Over a 10-day
    index window even a wide ``band`` (default 50%) only ever trims contamination.
    """
    if len(rows) < 3:
        return rows
    closes = sorted(float(r["close"]) for r in rows if r.get("close") and float(r["close"]) > 0)
    if not closes:
        return rows
    med = closes[len(closes) // 2]
    if med <= 0:
        return rows
    lo, hi = med * (1.0 - band), med * (1.0 + band)
    clean: list[dict[str, Any]] = []
    for r in rows:
        c = float(r.get("close") or 0.0)
        if c <= 0 or not (lo <= c <= hi):
            continue
        h = float(r.get("high") or 0.0)
        l = float(r.get("low") or 0.0)
        o = float(r.get("open") or 0.0)
        if (h > 0 and h > hi) or (l > 0 and l < lo) or (o > 0 and not (lo <= o <= hi)):
            continue
        clean.append(r)
    return clean if clean else rows


def _runtime_root() -> Path:
    return Path(__file__).resolve().parents[1] / "runtime" / "index_analytics_data"


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _strategy_readiness_fields(
    *,
    watchlist_rows_today: int,
    watchlist_rows_latest: int,
    watchlist_age_seconds: Optional[float],
    market_open: bool = False,
) -> dict[str, Any]:
    today_session_ready = watchlist_rows_today >= STRATEGY_MIN_LATEST_UNDERLYINGS
    today_ready = (
        today_session_ready
        and watchlist_age_seconds is not None
        and watchlist_age_seconds <= STRATEGY_LIVE_WATCHLIST_MAX_AGE_SECONDS
    )
    latest_session_ready = watchlist_rows_latest >= STRATEGY_MIN_LATEST_UNDERLYINGS
    latest_session_execution_ready = (
        latest_session_ready
        and watchlist_age_seconds is not None
        and watchlist_age_seconds <= STRATEGY_EXECUTION_MAX_WATCHLIST_AGE_SECONDS
    )
    readiness_mode = "live" if today_ready else "latest_session" if latest_session_ready else "missing"
    if today_ready:
        execution_mode = "live"
    elif market_open and watchlist_rows_today and not today_session_ready:
        execution_mode = "partial_live_session"
    elif market_open and watchlist_rows_today:
        execution_mode = "stale_live_session"
    elif market_open and latest_session_ready:
        execution_mode = "missing_live_session"
    elif latest_session_execution_ready:
        execution_mode = "latest_session"
    elif latest_session_ready and watchlist_age_seconds is not None:
        execution_mode = "stale_latest_session"
    elif latest_session_ready:
        execution_mode = "catalog_only"
    else:
        execution_mode = "missing"
    execution_ready = today_ready or (latest_session_execution_ready and not market_open)
    return {
        "ready": today_ready or latest_session_ready,
        "execution_ready": execution_ready,
        "readiness_mode": readiness_mode,
        "execution_mode": execution_mode,
        "market_open": market_open,
        "today_session_ready": today_session_ready,
        "latest_session_ready": latest_session_ready,
        "max_live_age_seconds": STRATEGY_LIVE_WATCHLIST_MAX_AGE_SECONDS,
        "max_execution_age_seconds": STRATEGY_EXECUTION_MAX_WATCHLIST_AGE_SECONDS,
    }


def _index_spot_readiness_fields(
    per_symbol: dict[str, str],
    *,
    market_open: bool,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    if not market_open:
        return {
            "index_spot_ready": True,
            "index_spot_missing": [],
            "index_spot_stale": {},
            "max_index_spot_age_seconds": STRATEGY_LIVE_WATCHLIST_MAX_AGE_SECONDS,
        }

    now = now_utc or datetime.now(UTC)
    missing: list[str] = []
    stale: dict[str, float] = {}
    for symbol in NSE_INDEX_SCOPE:
        latest_time = per_symbol.get(symbol)
        if not latest_time:
            missing.append(symbol)
            continue
        try:
            age_seconds = max(0.0, (now - _parse_time(latest_time)).total_seconds())
        except Exception:
            missing.append(symbol)
            continue
        if age_seconds > STRATEGY_LIVE_WATCHLIST_MAX_AGE_SECONDS:
            stale[symbol] = age_seconds

    return {
        "index_spot_ready": not missing and not stale,
        "index_spot_missing": missing,
        "index_spot_stale": stale,
        "max_index_spot_age_seconds": STRATEGY_LIVE_WATCHLIST_MAX_AGE_SECONDS,
    }


class MarketIntelligenceRuntime:
    def __init__(self) -> None:
        self._last_full_watchlist_refresh_at: datetime | None = None
        self._last_chain_refresh_at: datetime | None = None
        self._last_premium_refresh_at: datetime | None = None
        self._last_learning_refresh_at: datetime | None = None
        # Per-(underlying, option_type) set of instrument_keys that have
        # been ATM at any point during the current session. Lets the
        # periodic refresh continue to top up prior-ATM strikes even
        # after spot has moved enough to roll the live ATM — without
        # this, every strike that drops off the watchlist gets stuck
        # on whatever bars it had at the moment of the roll.
        self._session_atm_seen: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        self._session_atm_seen_date: date | None = None

    async def load_local_spot_rows(
        self,
        symbol_code: str,
        *,
        lookback_days: int = 10,
    ) -> tuple[list[dict[str, Any]], str, str]:
        from_time = datetime.combine(
            date.today() - timedelta(days=max(int(lookback_days), 1)),
            time.min,
            tzinfo=IST,
        ).astimezone(UTC)
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    SELECT time, open, high, low, close, volume
                    FROM underlying_spot_candles
                    WHERE underlying = :underlying
                      AND interval = '1minute'
                      AND time >= :from_time
                    ORDER BY time ASC
                    """
                ),
                {"underlying": symbol_code.upper(), "from_time": from_time},
            )
            rows = result.mappings().all()

        payload = [
            {
                "time": _parse_time(row["time"]).isoformat(),
                "open": float(row["open"] or row["close"] or 0.0),
                "high": float(row["high"] or row["close"] or 0.0),
                "low": float(row["low"] or row["close"] or 0.0),
                "close": float(row["close"] or 0.0),
                "volume": float(row["volume"] or 0.0),
            }
            for row in rows
        ]
        payload = _drop_contaminated_spot_rows(payload)
        if payload:
            return payload, "timescaledb_spot_1minute", symbol_code.upper()

        csv_path = _runtime_root() / "spot" / f"underlying={symbol_code.upper()}" / "1minute.csv.gz"
        if not csv_path.exists():
            return [], "none", symbol_code.upper()

        cutoff_date = (datetime.now(IST) - timedelta(days=max(int(lookback_days), 1))).date()
        local_rows: list[dict[str, Any]] = []
        with gzip.open(csv_path, "rt", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                try:
                    timestamp = _parse_time(row["time"]).astimezone(IST)
                except Exception:
                    continue
                if timestamp.date() < cutoff_date:
                    continue
                local_rows.append(
                    {
                        "time": timestamp.astimezone(UTC).isoformat(),
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row.get("volume") or 0.0),
                    }
                )
        return local_rows, "local_csv_spot", csv_path.name

    async def gap_fill_spot_history(
        self,
        *,
        symbols: Optional[list[str]] = None,
        lookback_days: int = 10,
    ) -> dict[str, Any]:
        from auction_intelligence.live import _fetch_recent_minute_rows

        requested = [str(symbol).upper() for symbol in (symbols or list(NSE_INDEX_SCOPE))]
        results: list[dict[str, Any]] = []
        stored_total = 0

        for symbol in requested:
            try:
                rows, source, history_symbol = await _fetch_recent_minute_rows(
                    symbol,
                    lookback_days=lookback_days,
                    allow_live_broker_refresh=True,
                )
                stored = await self._upsert_spot_rows(
                    symbol_code=symbol,
                    history_symbol=history_symbol,
                    rows=rows,
                    source=source,
                )
                stored_total += stored
                results.append(
                    {
                        "symbol_code": symbol,
                        "source": source,
                        "history_symbol": history_symbol,
                        "rows_seen": len(rows),
                        "rows_stored": stored,
                    }
                )
            except Exception as exc:
                logger.warning(f"[MarketIntelligence] Spot gap-fill failed for {symbol}: {exc}")
                results.append(
                    {
                        "symbol_code": symbol,
                        "source": "error",
                        "error": str(exc),
                        "rows_seen": 0,
                        "rows_stored": 0,
                    }
                )
            await asyncio.sleep(0.1)

        return {
            "symbols_requested": requested,
            "stored_total": stored_total,
            "results": results,
        }

    async def refresh_nse_watchlists(self) -> dict[str, Any]:
        from market_data.atm_watchlist import atm_watchlist_service

        now = datetime.now(IST)
        expiry_payload = await atm_watchlist_service.get_expiries(None, live_refresh=True)
        stock_monthly_expiry = str(expiry_payload.get("stock_monthly_expiry") or "").strip()
        index_expiries = sorted(
            {
                str(expiry)
                for expiry in dict(expiry_payload.get("index_monthlies") or {}).values()
                if str(expiry or "").strip()
            }
        )
        full_refresh_due = self._last_full_watchlist_refresh_at is None or (
            now - self._last_full_watchlist_refresh_at
        ).total_seconds() >= max(int(settings.MARKET_INTELLIGENCE_FULL_WATCHLIST_REFRESH_MINUTES), 1) * 60

        # DB-state override: if today's STOCK coverage in
        # atm_option_watchlist_snapshots is below the last-session count, the
        # in-memory timer is lying (stale Redis cache, prior crash, container
        # restart, etc.). Force a full refresh until the stock universe
        # actually populates for today. Without this, S1 keeps scanning 215
        # rows where 208 are Friday's snapshots and MACD never crosses.
        if not full_refresh_due:
            try:
                today_start = datetime.combine(now.date(), time.min, tzinfo=IST).astimezone(UTC)
                async with AsyncSessionLocal() as session:
                    stock_today = await session.scalar(
                        text(
                            """
                            SELECT COUNT(DISTINCT underlying)
                            FROM atm_option_watchlist_snapshots
                            WHERE kind = 'STOCK'
                              AND time >= :today_start
                            """
                        ),
                        {"today_start": today_start},
                    )
                    stock_latest = await session.scalar(
                        text(
                            """
                            SELECT COUNT(DISTINCT underlying)
                            FROM atm_option_watchlist_snapshots
                            WHERE kind = 'STOCK'
                            """
                        ),
                    )
                stock_today = int(stock_today or 0)
                stock_latest = int(stock_latest or 0)
                if stock_latest > 0 and stock_today < int(stock_latest * 0.5):
                    logger.info(
                        "[MarketIntelligence] forcing full watchlist refresh: "
                        "stocks today={today} vs latest_session={latest}",
                        today=stock_today,
                        latest=stock_latest,
                    )
                    full_refresh_due = True
                    self._last_full_watchlist_refresh_at = None
            except Exception as exc:
                logger.warning(
                    "[MarketIntelligence] stock freshness probe failed: {}", exc
                )
        watchlist_requests: list[tuple[str | None, list[str] | None]] = []
        if full_refresh_due:
            full_universe_expiry = stock_monthly_expiry or str(expiry_payload.get("monthly_expiry") or "").strip()
            if full_universe_expiry:
                watchlist_requests.append((full_universe_expiry, None))
                # Bust the shared-universe Redis cache so the live_refresh
                # rebuild actually iterates the stock universe instead of
                # returning the cached blob. Without this, a stale "ready"
                # payload satisfies _watchlist_rows_are_fresh() and the BG
                # build for stocks never fires.
                try:
                    redis = await get_redis()
                    cache_keys = [
                        f"atm_watchlist:v12:live:{full_universe_expiry}:all",
                        f"atm_watchlist:partial:v12:live:{full_universe_expiry}:all",
                    ]
                    for key in cache_keys:
                        await redis.delete(key)
                except Exception as exc:
                    logger.warning(
                        "[MarketIntelligence] failed to invalidate full-universe cache: {}",
                        exc,
                    )
        for expiry in index_expiries:
            watchlist_requests.append((expiry, list(NSE_INDEX_SCOPE)))
        if not index_expiries:
            watchlist_requests.append((None, list(NSE_INDEX_SCOPE)))
        payloads = await asyncio.gather(
            *(
                atm_watchlist_service.get_watchlist(
                    expiry=expiry,
                    symbols=symbols,
                    live_refresh=True,
                    # Force a true rebuild for the full-universe call so the
                    # stale-row filter at line 864 actually runs. Index-scope
                    # calls keep the cache fast path.
                    force_rebuild=(symbols is None),
                )
                for expiry, symbols in watchlist_requests
            ),
            return_exceptions=True,
        )

        results: list[dict[str, Any]] = []
        for (expiry, symbols), payload in zip(watchlist_requests, payloads):
            if isinstance(payload, Exception):
                results.append(
                    {
                        "expiry": expiry,
                        "symbols": symbols or [],
                        "status": "error",
                        "detail": str(payload),
                        "rows": 0,
                    }
                )
                continue
            results.append(
                {
                    "expiry": expiry,
                    "symbols": symbols or [],
                    "status": str(payload.get("build_status") or "ready"),
                    "detail": payload.get("detail"),
                    "rows": int((payload.get("summary") or {}).get("total_rows") or 0),
                }
            )

        if full_refresh_due and any(item.get("symbols") == [] and item.get("status") in {"ready", "building"} for item in results):
            self._last_full_watchlist_refresh_at = now

        return {
            "monthly_expiries": index_expiries,
            "stock_monthly_expiry": stock_monthly_expiry or None,
            "full_refresh_due": full_refresh_due,
            "requests": results,
        }

    async def refresh_index_option_chains(self) -> dict[str, Any]:
        now = datetime.now(IST)
        cooldown_seconds = max(int(settings.MARKET_INTELLIGENCE_REFRESH_INTERVAL_SECONDS), 30)
        if self._last_chain_refresh_at is not None:
            elapsed = (now - self._last_chain_refresh_at).total_seconds()
            if elapsed < cooldown_seconds:
                return {
                    "status": "cooldown",
                    "source": "cached",
                    "requests": [],
                    "last_refresh_at": self._last_chain_refresh_at.isoformat(),
                    "cooldown_seconds": cooldown_seconds,
                }

        fyers_adapter = get_active_adapter("fyers")
        if fyers_adapter is None and await ensure_fyers_session(force_validate=False):
            fyers_adapter = get_active_adapter("fyers")
        upstox_adapter = get_active_adapter("upstox")
        if upstox_adapter is None and await ensure_upstox_session(force_validate=False):
            upstox_adapter = get_active_adapter("upstox")

        if fyers_adapter is None and upstox_adapter is None:
            return {
                "status": "offline",
                "source": "none",
                "requests": [],
            }

        requests = await self._load_chain_refresh_candidates()
        results: list[dict[str, Any]] = []
        for symbol_code, expiry_iso in requests:
            result = await self._refresh_cached_index_option_chain(
                symbol_code,
                expiry_iso,
                upstox_adapter=upstox_adapter,
                fyers_adapter=fyers_adapter,
            )
            if result.get("status") == "error":
                logger.warning(
                    "[MarketIntelligence] Option-chain refresh failed for "
                    f"{symbol_code} {expiry_iso}: {result.get('detail')}"
                )
            results.append(result)
            await asyncio.sleep(0.1)

        self._last_chain_refresh_at = datetime.now(IST)
        return {
            "status": "ok",
            "source": "+".join(
                [
                    name
                    for name, adapter in (("upstox", upstox_adapter), ("fyers", fyers_adapter))
                    if adapter is not None
                ]
            ) or "none",
            "last_refresh_at": self._last_chain_refresh_at.isoformat(),
            "requests": results,
        }

    async def refresh_atm_premium_candles(self) -> dict[str, Any]:
        """Top up `option_premium_candles` for every ATM contract in the live
        watchlist, at 3-minute granularity.

        Why 3-minute, not 30-minute: 3-min bars give AI / profile / order-flow
        modules 10× the granularity for free (Upstox 1-min source aggregated
        to 3-min inside option_history_service). S1's MACD strategy still
        reads 30-min via its own on-demand load_candles call — those land
        in the same table tagged with `interval='30minute'` so the two
        timeframes coexist without interference.

        Coverage problem this fixes: the live tick aggregator only writes
        premiums for ~10 actively-subscribed instruments (indices +
        commodities + currently-held stock options). For the other ~190
        stocks in the F&O universe, the premium table used to sit on the
        09:15 open bar all day. This periodic refresh tops up the full
        10-strike window (3 ITM + 1 ATM + 6 OTM per side) so when the
        intraday ATM rolls, neighbour-strike history is already there.

        Idempotent — bars already in DB don't re-write. Cooldown matches
        the supervisor interval so the rest of the session sees fresh
        3-min closes within a couple of minutes of each new bar landing
        on Upstox.
        """
        now = datetime.now(IST)
        # Bar cadence is 30 min, but we want to catch the new bar within
        # 60-90s of its close. A 60s cooldown matches the supervisor's
        # cycle interval — every run advances at most one new bar per
        # contract, which is what we want.
        cooldown_seconds = max(int(settings.MARKET_INTELLIGENCE_REFRESH_INTERVAL_SECONDS), 30)
        if self._last_premium_refresh_at is not None:
            elapsed = (now - self._last_premium_refresh_at).total_seconds()
            if elapsed < cooldown_seconds:
                return {
                    "status": "cooldown",
                    "last_refresh_at": self._last_premium_refresh_at.isoformat(),
                    "cooldown_seconds": cooldown_seconds,
                }

        # Only refresh during NSE market hours — otherwise we're spending
        # API budget for no benefit (no new bars are landing on Upstox).
        market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
        market_close = now.replace(hour=15, minute=35, second=0, microsecond=0)
        if now < market_open or now > market_close:
            self._last_premium_refresh_at = now
            return {
                "status": "market_closed",
                "last_refresh_at": now.isoformat(),
            }

        # Pull current ATM watchlist directly — the source of truth for
        # which strikes/expiries we should be feeding the strategy.
        try:
            from market_data.atm_watchlist import atm_watchlist_service
            from market_data.option_history import option_history_service
        except Exception as exc:  # noqa: BLE001
            return {"status": "import_error", "error": str(exc)}

        try:
            watchlist = await atm_watchlist_service.get_watchlist(live_refresh=False)
        except Exception as exc:  # noqa: BLE001
            return {"status": "watchlist_error", "error": str(exc)}

        rows = list(watchlist.get("rows") or [])

        # Roll the session-seen registry at session boundary.
        today = now.date()
        if self._session_atm_seen_date != today:
            self._session_atm_seen = {}
            self._session_atm_seen_date = today

        # First pass — record every strike the agent might need.
        # Two sources feed the session_atm_seen registry:
        #
        #   1. The watchlist's current ATM picks (`row.ce` / `row.pe`) —
        #      these are what trading actually uses *right now*.
        #   2. The pre-computed `extended_strikes` window (3 ITM + 1 ATM
        #      + 6 OTM per side) — pre-warms neighbours so when the
        #      intraday ATM rolls, history is already there. Watchlist
        #      and trade execution remain anchored on the ATM pick;
        #      this is *data coverage only*.
        for row in rows:
            expiry_iso = str(row.get("expiry") or "").strip()
            underlying = str(row.get("underlying") or "").strip().upper()
            if not expiry_iso or not underlying:
                continue
            try:
                expiry = date.fromisoformat(expiry_iso)
            except ValueError:
                continue

            # Source 1 — the watchlist's current ATM picks
            for side_key in ("ce", "pe"):
                side = row.get(side_key) or {}
                strike = side.get("strike")
                instrument_key = str(side.get("instrument_key") or "").strip()
                if strike is None or not instrument_key:
                    continue
                bucket = self._session_atm_seen.setdefault(
                    (underlying, side_key.upper()), {}
                )
                bucket[instrument_key] = {
                    "underlying": underlying,
                    "option_type": side_key.upper(),
                    "strike": float(strike),
                    "expiry": expiry,
                    "instrument_key": instrument_key,
                }

            # Source 2 — the extended 10-strike window per side.
            extended = row.get("extended_strikes") or {}
            for side_label in ("CE", "PE"):
                for ext in extended.get(side_label) or []:
                    instrument_key = str(ext.get("instrument_key") or "").strip()
                    strike = ext.get("strike")
                    if strike is None or not instrument_key:
                        continue
                    bucket = self._session_atm_seen.setdefault(
                        (underlying, side_label), {}
                    )
                    bucket[instrument_key] = {
                        "underlying": underlying,
                        "option_type": side_label,
                        "strike": float(strike),
                        "expiry": expiry,
                        "instrument_key": instrument_key,
                    }

        # Refresh every recorded ATM strike — current AND historical for
        # today. A 30-min top-up call is cheap when the contract already
        # has bars (broker only returns the missing tail).
        ok = 0
        skipped = 0
        errors = 0
        # Spread broker calls across the cycle so we don't hammer Upstox
        # for a multi-thousand-contract burst — 100ms pause gives the
        # native rate limiter headroom even when the extended 10-strike
        # window expands the set to ~4k contracts.
        per_call_pause = 0.1
        refresh_targets: list[dict[str, Any]] = [
            contract
            for bucket in self._session_atm_seen.values()
            for contract in bucket.values()
        ]
        for contract in refresh_targets:
            try:
                # 3-minute granularity. The service aggregates from
                # Upstox 1-min source and persists at `interval='3minute'`.
                # limit=160 covers ~8 hours of session (160 × 3 min) so
                # the first cycle after market open backfills the whole
                # session in one call; subsequent cycles incrementally
                # add only the new bars.
                await option_history_service.load_candles(
                    underlying=contract["underlying"],
                    expiry=contract["expiry"],
                    strike=contract["strike"],
                    option_type=contract["option_type"],
                    instrument_key=contract["instrument_key"],
                    interval="3minute",
                    limit=160,
                    allow_broker_refresh=True,
                )
                ok += 1
            except Exception as exc:  # noqa: BLE001
                errors += 1
                logger.debug(
                    f"[MarketIntelligence] premium refresh failed for "
                    f"{contract['underlying']} {contract['option_type']} "
                    f"{contract['strike']}: {exc}"
                )
            await asyncio.sleep(per_call_pause)

        self._last_premium_refresh_at = datetime.now(IST)
        session_strike_count = sum(
            len(bucket) for bucket in self._session_atm_seen.values()
        )
        return {
            "status": "ok",
            "interval": "3minute",
            "last_refresh_at": self._last_premium_refresh_at.isoformat(),
            "rows_in_watchlist": len(rows),
            "session_atm_strikes_tracked": session_strike_count,
            "refreshed": ok,
            "skipped": skipped,
            "errors": errors,
            "elapsed_seconds": round(
                (self._last_premium_refresh_at - now).total_seconds(), 2
            ),
        }

    async def refresh_learning_scores(self) -> dict[str, Any]:
        """Recompute the strategy-learning scores from `agent_signals` and
        `agent_positions` so S1/S2 entry decisions see updated win-rate /
        expectancy / size_multiplier per (underlying, option_type, signal_reason).

        The infrastructure (StrategyLearningService) was fully built but
        never invoked periodically — it only refreshed on a manual
        `/api/strategy/learning-refresh` POST. Result: the in-memory
        score cache stayed empty, S1/S2 ran without learning signal, and
        chronically-losing setups kept firing. Hooking refresh into the
        MI cycle turns the system from "passive recorder" into "active
        learner".

        Cooldown: 5 minutes. Learning scores aggregate trades over many
        days, so per-minute refresh adds nothing — but 5-min gives
        intraday closed trades a chance to influence the next entry.
        """
        now = datetime.now(IST)
        cooldown_seconds = 300  # 5 min
        if self._last_learning_refresh_at is not None:
            elapsed = (now - self._last_learning_refresh_at).total_seconds()
            if elapsed < cooldown_seconds:
                return {
                    "status": "cooldown",
                    "last_refresh_at": self._last_learning_refresh_at.isoformat(),
                    "cooldown_seconds": cooldown_seconds,
                }

        try:
            from paper_engine.strategy_learning import strategy_learning_service
        except Exception as exc:  # noqa: BLE001
            return {"status": "import_error", "error": str(exc)}

        try:
            result = await strategy_learning_service.refresh_scores()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[MarketIntelligence] Learning refresh failed: {exc}")
            return {"status": "error", "error": str(exc)}

        self._last_learning_refresh_at = datetime.now(IST)
        # Compact summary — the full score dict can be hundreds of entries.
        score_count = 0
        try:
            score_count = int(result.get("score_count") or len(result.get("scores") or {}))
        except Exception:
            pass
        return {
            "status": "ok",
            "last_refresh_at": self._last_learning_refresh_at.isoformat(),
            "score_count": score_count,
            "elapsed_seconds": round(
                (self._last_learning_refresh_at - now).total_seconds(), 2
            ),
        }

    async def refresh_nse_runtime(self) -> dict[str, Any]:
        spot_gap_fill = await self.gap_fill_spot_history(
            symbols=list(NSE_INDEX_SCOPE),
            lookback_days=max(int(settings.MARKET_INTELLIGENCE_GAP_FILL_LOOKBACK_DAYS), 1),
        )
        watchlists = await self.refresh_nse_watchlists()
        option_chains = await self.refresh_index_option_chains()
        # Top up 30m option premium candles across the full ATM watchlist
        # so S1's MACD scan sees fresh bars throughout the session.
        # Failure here MUST NOT abort the cycle — strategy fall-back paths
        # use whatever DB has.
        try:
            premium_refresh = await self.refresh_atm_premium_candles()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[MarketIntelligence] Option premium refresh failed: {exc}")
            premium_refresh = {"status": "error", "error": str(exc)}
        # Strategy-learning refresh — recomputes per-(underlying, option_type,
        # signal_reason) win-rate / expectancy / size-multiplier so S1/S2's
        # entry decision sees fresh learning signal. Has its own 5-min
        # cooldown; failure here is non-fatal.
        try:
            learning_refresh = await self.refresh_learning_scores()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[MarketIntelligence] Learning refresh failed: {exc}")
            learning_refresh = {"status": "error", "error": str(exc)}
        try:
            sector_interaction = await india_live_sector_service.market_intelligence_payload()
        except Exception as exc:
            logger.warning(f"[MarketIntelligence] Sector interaction refresh failed: {exc}")
            sector_interaction = {
                "module": "sector_interaction",
                "source_mode": "error",
                "error": str(exc),
            }
        try:
            macro_research = await macro_research_service.overview(refresh=False)
        except Exception as exc:
            logger.warning(f"[MarketIntelligence] Macro research refresh failed: {exc}")
            macro_research = {
                "module": "macro_research",
                "source_mode": "error",
                "error": str(exc),
            }
        return {
            "spot_gap_fill": spot_gap_fill,
            "watchlists": watchlists,
            "option_chains": option_chains,
            "premium_refresh": premium_refresh,
            "learning_refresh": learning_refresh,
            "sector_interaction": sector_interaction,
            "macro_research": macro_research,
        }

    async def get_strategy_health(self) -> dict[str, Any]:
        now = datetime.now(IST)
        today_start = datetime.combine(now.date(), time.min, tzinfo=IST).astimezone(UTC)
        tomorrow_start = today_start + timedelta(days=1)
        async with AsyncSessionLocal() as session:
            watchlist_time = await session.scalar(
                text(
                    """
                    SELECT MAX(time)
                    FROM atm_option_watchlist_snapshots
                    WHERE expiry >= CURRENT_DATE
                    """
                ),
            )
            if watchlist_time is None:
                watchlist_time = await session.scalar(
                    text("SELECT MAX(time) FROM atm_option_watchlist_snapshots")
                )

            today_stats = await session.execute(
                text(
                    """
                    SELECT
                        COUNT(DISTINCT underlying)::INT AS underlyings,
                        COUNT(DISTINCT underlying) FILTER (WHERE option_type = 'CE' AND ltp IS NOT NULL)::INT AS ce_ready,
                        COUNT(DISTINCT underlying) FILTER (WHERE option_type = 'PE' AND ltp IS NOT NULL)::INT AS pe_ready,
                        COUNT(DISTINCT underlying) FILTER (WHERE kind = 'STOCK')::INT AS stocks,
                        COUNT(DISTINCT underlying) FILTER (WHERE kind = 'INDEX')::INT AS indices
                    FROM atm_option_watchlist_snapshots
                    WHERE time >= :today_start
                      AND time < :tomorrow_start
                      AND expiry >= CURRENT_DATE
                    """
                ),
                {"today_start": today_start, "tomorrow_start": tomorrow_start},
            )
            today_row = today_stats.mappings().first() or {}

            latest_row: dict[str, Any] = {}
            latest_session_start: datetime | None = None
            latest_session_end: datetime | None = None
            if watchlist_time is not None:
                latest_ist = _parse_time(watchlist_time).astimezone(IST)
                latest_session_start = datetime.combine(latest_ist.date(), time.min, tzinfo=IST).astimezone(UTC)
                latest_session_end = latest_session_start + timedelta(days=1)
                latest_stats = await session.execute(
                    text(
                        """
                        SELECT
                            COUNT(DISTINCT underlying)::INT AS underlyings,
                            COUNT(DISTINCT underlying) FILTER (WHERE option_type = 'CE' AND ltp IS NOT NULL)::INT AS ce_ready,
                            COUNT(DISTINCT underlying) FILTER (WHERE option_type = 'PE' AND ltp IS NOT NULL)::INT AS pe_ready,
                            COUNT(DISTINCT underlying) FILTER (WHERE kind = 'STOCK')::INT AS stocks,
                            COUNT(DISTINCT underlying) FILTER (WHERE kind = 'INDEX')::INT AS indices
                        FROM atm_option_watchlist_snapshots
                        WHERE time >= :session_start
                          AND time < :session_end
                          AND expiry >= CURRENT_DATE
                        """
                    ),
                    {"session_start": latest_session_start, "session_end": latest_session_end},
                )
                latest_row = dict(latest_stats.mappings().first() or {})

            premium_row: dict[str, Any] = {}
            if int(latest_row.get("underlyings") or 0) < STRATEGY_MIN_LATEST_UNDERLYINGS:
                premium_stats = await session.execute(
                    text(
                        """
                        WITH latest AS (
                            SELECT DISTINCT ON (underlying, option_type)
                                time,
                                underlying,
                                option_type,
                                close AS ltp
                            FROM option_premium_candles
                            WHERE expiry >= CURRENT_DATE
                              AND option_type IN ('CE', 'PE')
                              AND close IS NOT NULL
                            ORDER BY underlying, option_type, time DESC
                        )
                        SELECT
                            MAX(time) AS latest_time,
                            COUNT(DISTINCT underlying)::INT AS underlyings,
                            COUNT(DISTINCT underlying) FILTER (WHERE option_type = 'CE' AND ltp IS NOT NULL)::INT AS ce_ready,
                            COUNT(DISTINCT underlying) FILTER (WHERE option_type = 'PE' AND ltp IS NOT NULL)::INT AS pe_ready
                        FROM latest
                        """
                    )
                )
                premium_row = dict(premium_stats.mappings().first() or {})
                premium_time = premium_row.get("latest_time")
                premium_is_newer = (
                    premium_time is not None
                    and (
                        watchlist_time is None
                        or _parse_time(premium_time) > _parse_time(watchlist_time)
                    )
                )
                if (
                    int(premium_row.get("underlyings") or 0) > int(latest_row.get("underlyings") or 0)
                    and (watchlist_time is None or premium_is_newer)
                ):
                    latest_row = {
                        **latest_row,
                        **premium_row,
                        "stocks": max(int(latest_row.get("stocks") or 0), int(premium_row.get("underlyings") or 0) - 7),
                        "indices": max(int(latest_row.get("indices") or 0), 7),
                    }
                    if premium_row.get("latest_time") is not None:
                        watchlist_time = premium_row.get("latest_time")
                        latest_ist = _parse_time(watchlist_time).astimezone(IST)
                        latest_session_start = datetime.combine(latest_ist.date(), time.min, tzinfo=IST).astimezone(UTC)
                        latest_session_end = latest_session_start + timedelta(days=1)

            if int(latest_row.get("underlyings") or 0) < STRATEGY_MIN_LATEST_UNDERLYINGS:
                catalog_stats = await session.execute(
                    text(
                        """
                        WITH eligible AS (
                            SELECT DISTINCT catalog.underlying, catalog.option_type
                            FROM fo_contract_catalog catalog
                            WHERE catalog.expiry >= CURRENT_DATE
                              AND catalog.option_type IN ('CE', 'PE')
                              AND catalog.instrument_key IS NOT NULL
                        )
                        SELECT
                            COUNT(DISTINCT underlying)::INT AS underlyings,
                            COUNT(DISTINCT underlying) FILTER (WHERE option_type = 'CE')::INT AS ce_ready,
                            COUNT(DISTINCT underlying) FILTER (WHERE option_type = 'PE')::INT AS pe_ready
                        FROM eligible
                        """
                    )
                )
                catalog_row = dict(catalog_stats.mappings().first() or {})
                if int(catalog_row.get("underlyings") or 0) > int(latest_row.get("underlyings") or 0):
                    latest_row = {
                        **latest_row,
                        **catalog_row,
                        "stocks": max(int(latest_row.get("stocks") or 0), int(catalog_row.get("underlyings") or 0) - 7),
                        "indices": max(int(latest_row.get("indices") or 0), 7),
                    }

            spot_rows = await session.execute(
                text(
                    """
                    SELECT underlying, MAX(time) AS latest_time
                    FROM underlying_spot_candles
                    WHERE interval = '1minute'
                      AND time >= NOW() - INTERVAL '10 days'
                    GROUP BY underlying
                    """
                ),
            )
            per_symbol = {
                str(row.underlying): _parse_time(row.latest_time).isoformat()
                for row in spot_rows.fetchall()
                if row.latest_time is not None
            }

        latest_watchlist_iso = _parse_time(watchlist_time).isoformat() if watchlist_time is not None else None
        watchlist_age_seconds: Optional[float] = None
        if watchlist_time is not None:
            watchlist_age_seconds = max(
                0.0,
                (datetime.now(UTC) - _parse_time(watchlist_time)).total_seconds(),
            )

        watchlist_rows_today = int(today_row.get("underlyings") or 0)
        watchlist_rows_latest = int(latest_row.get("underlyings") or 0)
        market_open = trading_calendar.is_exchange_open("NSE", now)
        readiness = _strategy_readiness_fields(
            watchlist_rows_today=watchlist_rows_today,
            watchlist_rows_latest=watchlist_rows_latest,
            watchlist_age_seconds=watchlist_age_seconds,
            market_open=market_open,
        )
        index_spot_readiness = _index_spot_readiness_fields(
            per_symbol,
            market_open=market_open,
        )
        if readiness.get("execution_ready") and not index_spot_readiness.get("index_spot_ready"):
            readiness = {
                **readiness,
                "execution_ready": False,
                "execution_mode": (
                    "shared_spot_missing"
                    if index_spot_readiness.get("index_spot_missing")
                    else "shared_spot_stale"
                ),
            }
        return {
            **readiness,
            **index_spot_readiness,
            "watchlist_rows_today": watchlist_rows_today,
            "watchlist_rows_latest": watchlist_rows_latest,
            "latest_ce_ready": int(latest_row.get("ce_ready") or 0),
            "latest_pe_ready": int(latest_row.get("pe_ready") or 0),
            "latest_stock_underlyings": int(latest_row.get("stocks") or 0),
            "latest_index_underlyings": int(latest_row.get("indices") or 0),
            "today_ce_ready": int(today_row.get("ce_ready") or 0),
            "today_pe_ready": int(today_row.get("pe_ready") or 0),
            "latest_watchlist_session": (
                latest_session_start.astimezone(IST).date().isoformat()
                if latest_session_start is not None
                else None
            ),
            "latest_watchlist_time": latest_watchlist_iso,
            "watchlist_age_seconds": watchlist_age_seconds,
            "latest_spot_rows": per_symbol,
        }

    async def _load_chain_refresh_candidates(self) -> list[tuple[str, str]]:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    SELECT underlying, expiry
                    FROM (
                        SELECT
                            underlying,
                            expiry,
                            ROW_NUMBER() OVER (
                                PARTITION BY underlying
                                ORDER BY expiry ASC
                            ) AS rn
                        FROM (
                            SELECT DISTINCT underlying, expiry
                            FROM fo_contract_catalog
                            WHERE underlying = ANY(:underlyings)
                              AND expiry >= CURRENT_DATE
                        ) ladder
                    ) ranked
                    WHERE rn <= 3
                    ORDER BY underlying ASC, expiry ASC
                    """
                ),
                {"underlyings": list(NSE_INDEX_SCOPE)},
            )
            rows = result.fetchall()
        return [
            (str(row.underlying), row.expiry.isoformat())
            for row in rows
            if getattr(row, "expiry", None) is not None
        ]

    async def _upsert_spot_rows(
        self,
        *,
        symbol_code: str,
        history_symbol: str,
        rows: list[dict[str, Any]],
        source: str,
    ) -> int:
        payload = []
        for row in rows:
            try:
                timestamp = _parse_time(row["time"])
            except Exception:
                continue
            payload.append(
                {
                    "time": timestamp,
                    "instrument_key": history_symbol,
                    "underlying": symbol_code.upper(),
                    "interval": "1minute",
                    "open": float(row.get("open", row.get("close") or 0.0) or 0.0),
                    "high": float(row.get("high", row.get("close") or 0.0) or 0.0),
                    "low": float(row.get("low", row.get("close") or 0.0) or 0.0),
                    "close": float(row.get("close") or 0.0),
                    "volume": int(float(row.get("volume") or 0.0)),
                    "oi": int(float(row.get("oi") or 0.0)),
                    "source": source,
                }
            )
        if not payload:
            return 0

        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO underlying_spot_candles (
                        time, instrument_key, underlying, interval, open, high,
                        low, close, volume, oi, source, synced_at
                    ) VALUES (
                        :time, :instrument_key, :underlying, :interval, :open, :high,
                        :low, :close, :volume, :oi, :source, NOW()
                    )
                    ON CONFLICT (instrument_key, interval, time) DO UPDATE
                    SET open = EXCLUDED.open,
                        high = EXCLUDED.high,
                        low = EXCLUDED.low,
                        close = EXCLUDED.close,
                        volume = EXCLUDED.volume,
                        oi = EXCLUDED.oi,
                        source = EXCLUDED.source,
                        synced_at = NOW()
                    """
                ),
                payload,
            )
            await session.commit()
        return len(payload)

    async def _refresh_cached_index_option_chain(
        self,
        symbol_code: str,
        expiry_iso: str,
        *,
        upstox_adapter: Any | None,
        fyers_adapter: Any | None,
    ) -> dict[str, Any]:
        app_symbol = APP_SYMBOLS.get(symbol_code)
        if not app_symbol:
            return {
                "symbol_code": symbol_code,
                "expiry": expiry_iso,
                "status": "skipped",
                "detail": "unknown app symbol",
            }

        errors: list[str] = []
        for source, adapter, lookup_symbol in (
            ("upstox", upstox_adapter, to_broker_symbol(app_symbol)),
            ("fyers", fyers_adapter, to_fyers_symbol(app_symbol)),
        ):
            if adapter is None:
                continue
            try:
                chain = await adapter.get_option_chain(lookup_symbol, expiry_iso)
            except Exception as exc:
                errors.append(f"{source}: {exc}")
                continue
            if not getattr(chain, "entries", None):
                errors.append(f"{source}: empty chain")
                continue
            await self._cache_option_chain_payload(
                app_symbol=app_symbol,
                expiry_iso=expiry_iso,
                chain=chain,
                source=source,
            )
            return {
                "symbol_code": symbol_code,
                "expiry": expiry_iso,
                "status": "refreshed",
                "source": source,
            }

        return {
            "symbol_code": symbol_code,
            "expiry": expiry_iso,
            "status": "error",
            "detail": " | ".join(errors) if errors else "no broker available",
        }

    async def _cache_option_chain_payload(
        self,
        *,
        app_symbol: str,
        expiry_iso: str,
        chain: Any,
        source: str,
    ) -> None:
        # CPU-bulkhead offload: option-chain analytics (PCR/OI sums, per-strike
        # gamma-exposure loop, O(strikes^2) max-pain loop) is pure sync compute
        # over the just-fetched `chain`. Run it on a worker thread so it does not
        # block the event loop during the per-index refresh loop. Behaviour is
        # identical — same pure function, same inputs/outputs.
        analytics = await asyncio.to_thread(option_chain_service._calculate_analytics, chain)
        payload = {
            "symbol": app_symbol,
            "expiry": expiry_iso,
            "spot_price": float(chain.spot_price or 0.0),
            "timestamp": datetime.now(UTC).isoformat(),
            "source": source,
            "entries": [
                {
                    "strike": entry.strike,
                    "option_type": entry.option_type,
                    "ltp": entry.ltp,
                    "oi": entry.oi,
                    "volume": entry.volume,
                    "bid": entry.bid,
                    "ask": entry.ask,
                    "iv": entry.iv,
                    "delta": entry.delta,
                    "gamma": entry.gamma,
                    "theta": entry.theta,
                    "vega": entry.vega,
                    "prev_oi": entry.prev_oi,
                    "prev_close": entry.prev_close,
                    "oi_change": round(float(entry.oi) - float(entry.prev_oi or 0.0), 2),
                    "oi_change_pct": round(
                        ((float(entry.oi) - float(entry.prev_oi or 0.0)) / float(entry.prev_oi or 1.0)) * 100.0,
                        2,
                    ) if entry.prev_oi else None,
                    "ltp_change": round(float(entry.ltp) - float(entry.prev_close or 0.0), 2),
                    "ltp_change_pct": round(
                        ((float(entry.ltp) - float(entry.prev_close or 0.0)) / float(entry.prev_close or 1.0)) * 100.0,
                        2,
                    ) if entry.prev_close else None,
                    "instrument_key": entry.instrument_key,
                }
                for entry in chain.entries
            ],
            **analytics,
        }
        redis = await get_redis()
        await redis.set(f"oc:{app_symbol}:{expiry_iso}", json.dumps(payload), ex=OC_TTL)


market_intelligence_runtime = MarketIntelligenceRuntime()
