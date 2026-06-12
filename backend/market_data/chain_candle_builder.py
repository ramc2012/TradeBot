"""F1 feed — full-universe option-chain → 3m CE+PE OHLC candle builder.

This is the headline S1 (NSE premium-MACD) feed. S1 requires 3-minute OHLC for
the FULL option chain (12 ITM + ATM + 12 OTM, both CE and PE) of every F&O
underlying. Per-contract /history would be ~1.375M calls/day (14x the Fyers cap);
instead we poll Fyers `/options-chain-v3` ONCE per underlying (one call returns the
whole near-money band incl. ltp/oi/volume/bid/ask + underlying spot; greeks are
derived app-side) and roll the successive point-in-time snapshots into 3m bars.

Budget (verified): tier by kind so the per-minute governor never trips —
  INDEX  @ 60s  → proper 3-sample OHLC   (~5 names  × 375min = ~1,875 calls/day)
  STOCK  @ 180s → ~1 sample / 3m bar     (~222 names × 125    = ~27,750 calls/day)
≈ 29.6k Fyers REST/day (~30% of the 100k cap). Every call passes through the
shared FYERS_DATA_LIMITER (10/s · 200/min) inside `_get_data_json`, so the calls
self-stagger — this builder never has to sleep between names by hand.

Persistence reuses `OptionHistoryService._persist_broker_candles(source="fyers_chain")`
so the phantom-expiry gate + source tagging + ON CONFLICT dedup all apply, and the
read-path dedup prefers these greeks-bearing chain rows over greeks-null live rows.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

from loguru import logger

from core.trading_calendar import trading_calendar

BUCKET_SECONDS = 180  # 3-minute bars

# Per-kind poll cadence (seconds). INDEX gets a tighter cadence for true OHLC;
# stocks are polled at the bar width to stay inside the daily budget.
TIER_INTERVAL_SECONDS = {"INDEX": 60.0, "STOCK": 180.0}
DEFAULT_INTERVAL_SECONDS = 180.0


def _bucket_start(ts: datetime) -> datetime:
    """Floor a UTC timestamp to its 3-minute bucket start."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    epoch = int(ts.timestamp())
    floored = epoch - (epoch % BUCKET_SECONDS)
    return datetime.fromtimestamp(floored, timezone.utc)


@dataclass
class _Bar:
    bucket_start: datetime
    open: float
    high: float
    low: float
    close: float
    volume_open: float        # cumulative day-volume at bar open (for per-bar delta)
    volume_last: float
    oi: Optional[int] = None
    iv: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    underlying_price: Optional[float] = None

    def to_row(self) -> dict[str, Any]:
        vol = max(0, int(round(self.volume_last - self.volume_open)))
        return {
            "time": self.bucket_start.isoformat().replace("+00:00", "Z"),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": vol,
            "oi": self.oi,
            "iv": self.iv,
            "delta": self.delta,
            "gamma": self.gamma,
            "theta": self.theta,
            "vega": self.vega,
            "underlying_price": self.underlying_price,
        }


# key = (underlying, expiry_iso, strike, option_type) ; payload carried alongside.
ContractKey = tuple[str, str, float, str]


@dataclass
class ChainBarAccumulator:
    """Pure 3m OHLC accumulator over option-chain snapshots.

    `update()` returns a closed bar (with its instrument_key) when a snapshot
    crosses into a new bucket; `flush(now)` force-closes every still-open bar
    whose bucket has ended (call at end of session). Pure + deterministic so it
    is unit-tested without a broker or DB.
    """

    _cur: dict[ContractKey, _Bar] = field(default_factory=dict)
    _instrument_key: dict[ContractKey, Optional[str]] = field(default_factory=dict)

    def update(
        self,
        key: ContractKey,
        ts: datetime,
        ltp: float,
        *,
        volume: float = 0.0,
        oi: Optional[int] = None,
        iv: Optional[float] = None,
        delta: Optional[float] = None,
        gamma: Optional[float] = None,
        theta: Optional[float] = None,
        vega: Optional[float] = None,
        underlying_price: Optional[float] = None,
        instrument_key: Optional[str] = None,
    ) -> Optional[tuple[ContractKey, _Bar]]:
        if ltp is None or ltp <= 0:
            return None
        bucket = _bucket_start(ts)
        self._instrument_key[key] = instrument_key or self._instrument_key.get(key)
        cur = self._cur.get(key)

        if cur is not None and cur.bucket_start == bucket:
            cur.high = max(cur.high, ltp)
            cur.low = min(cur.low, ltp)
            cur.close = ltp
            cur.volume_last = max(cur.volume_last, volume)
            if oi is not None:
                cur.oi = oi
            if iv is not None:
                cur.iv, cur.delta, cur.gamma, cur.theta, cur.vega = iv, delta, gamma, theta, vega
            if underlying_price:
                cur.underlying_price = underlying_price
            return None

        closed = (key, cur) if cur is not None and cur.bucket_start < bucket else None
        self._cur[key] = _Bar(
            bucket_start=bucket, open=ltp, high=ltp, low=ltp, close=ltp,
            volume_open=volume, volume_last=volume, oi=oi, iv=iv, delta=delta,
            gamma=gamma, theta=theta, vega=vega, underlying_price=underlying_price,
        )
        return closed

    def flush(self, now: datetime) -> list[tuple[ContractKey, _Bar]]:
        """Close every open bar whose bucket has fully elapsed before `now`."""
        cutoff = _bucket_start(now)
        out: list[tuple[ContractKey, _Bar]] = []
        for key, bar in list(self._cur.items()):
            if bar.bucket_start < cutoff:
                out.append((key, bar))
                del self._cur[key]
        return out

    def flush_all(self) -> list[tuple[ContractKey, _Bar]]:
        """Force-close EVERY open bar regardless of bucket — the session-close
        sweep, so the final (in-progress) 3m bar + the close are persisted."""
        out = list(self._cur.items())
        self._cur.clear()
        return out

    def instrument_key_for(self, key: ContractKey) -> Optional[str]:
        return self._instrument_key.get(key)


class ChainCandleBuilder:
    """Polls the full F&O universe and builds 3m CE+PE OHLC into option_premium_candles."""

    def __init__(self):
        self._acc = ChainBarAccumulator()
        self._next_due: dict[str, float] = {}   # symbol -> monotonic time it may next be polled
        self._task: Optional[asyncio.Task] = None
        self._running = False

    # ── universe / broker resolution (reuse the ATM watchlist + Fyers adapter) ──
    async def _universe(self) -> list[Any]:
        from market_data.atm_watchlist import ATMWatchlistService
        return await ATMWatchlistService()._load_underlyings()

    @staticmethod
    def _fyers_symbol(meta: Any) -> str:
        from market_data.atm_watchlist import ATMWatchlistService
        return ATMWatchlistService._to_fyers_symbol(meta)

    async def _fyers_adapter(self):
        from api.routers.auth import ensure_fyers_session, get_active_adapter
        adapter = get_active_adapter("fyers")
        if adapter is None and await ensure_fyers_session(force_validate=True):
            adapter = get_active_adapter("fyers")
        return adapter

    # ── one poll cycle ─────────────────────────────────────────────────────────
    async def poll_once(self, *, now_mono: Optional[float] = None, force: bool = False) -> dict[str, int]:
        """Poll every underlying that is due, accumulate, and persist closed bars.

        ``force=True`` ignores the per-symbol cadence and polls the whole universe
        once — used for the session-close sweep so the final tick lands on every
        name's last bar.
        """
        import time

        now_mono = time.monotonic() if now_mono is None else now_mono
        adapter = await self._fyers_adapter()
        if adapter is None:
            logger.warning("[chain-builder] no active Fyers session; skipping poll")
            return {"polled": 0, "persisted": 0, "skipped": 0}

        universe = await self._universe()
        polled = persisted = skipped = 0

        for meta in universe:
            interval = TIER_INTERVAL_SECONDS.get(getattr(meta, "kind", ""), DEFAULT_INTERVAL_SECONDS)
            due_at = self._next_due.get(meta.symbol, 0.0)
            if not force and now_mono < due_at:
                skipped += 1
                continue
            self._next_due[meta.symbol] = now_mono + interval

            try:
                # Empty expiry → Fyers returns the NEAREST-expiry band (front month).
                chain = await adapter.get_option_chain(self._fyers_symbol(meta), "")
            except Exception as exc:  # noqa: BLE001 - isolate one bad name from the sweep
                logger.debug(f"[chain-builder] {meta.symbol} chain fetch failed: {exc}")
                continue
            polled += 1
            ts = datetime.now(timezone.utc)
            closed = self._ingest_chain(meta.symbol, chain, ts)
            persisted += await self._persist(closed)

        return {"polled": polled, "persisted": persisted, "skipped": skipped}

    def _ingest_chain(self, underlying: str, chain: Any, ts: datetime) -> list[tuple[ContractKey, _Bar]]:
        closed: list[tuple[ContractKey, _Bar]] = []
        expiry_iso = str(getattr(chain, "expiry", "") or "")
        spot = float(getattr(chain, "spot_price", 0) or 0)
        for e in getattr(chain, "entries", []) or []:
            otype = str(getattr(e, "option_type", "")).upper()
            if otype not in {"CE", "PE"}:
                continue
            key: ContractKey = (underlying, expiry_iso, float(getattr(e, "strike", 0) or 0), otype)
            result = self._acc.update(
                key, ts, float(getattr(e, "ltp", 0) or 0),
                volume=float(getattr(e, "volume", 0) or 0),
                oi=int(getattr(e, "oi", 0) or 0),
                iv=getattr(e, "iv", None), delta=getattr(e, "delta", None),
                gamma=getattr(e, "gamma", None), theta=getattr(e, "theta", None),
                vega=getattr(e, "vega", None), underlying_price=spot or None,
                instrument_key=getattr(e, "instrument_key", None),
            )
            if result is not None:
                closed.append(result)
        return closed

    async def _persist(self, closed: list[tuple[ContractKey, _Bar]]) -> int:
        if not closed:
            return 0
        from market_data.option_history import OptionHistoryService
        svc = OptionHistoryService()
        count = 0
        for key, bar in closed:
            underlying, expiry_iso, strike, otype = key
            instrument_key = self._acc.instrument_key_for(key)
            if not instrument_key or not expiry_iso:
                continue
            try:
                expiry_dt = date.fromisoformat(expiry_iso)
            except ValueError:
                continue
            try:
                await svc._persist_broker_candles(
                    rows=[bar.to_row()],
                    underlying=underlying,
                    expiry=expiry_dt,
                    strike=strike,
                    option_type=otype,
                    instrument_key=instrument_key,
                    interval="3minute",
                    already_in_db=set(),
                    source="fyers_chain",
                )
                count += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[chain-builder] persist failed {underlying} {strike}{otype}: {exc}")
        return count

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def _run_loop(self, *, cycle_seconds: float = 15.0) -> None:
        self._running = True
        was_open = False
        logger.info("[chain-builder] started full-universe 3m chain → option_premium_candles")
        try:
            while self._running:
                # Holiday/weekend/after-hours gate: the chain only moves while NSE
                # is open. When closed, idle to the next session (capped 5 min)
                # instead of polling the full option universe every 15s round the
                # clock — this is the dominant off-hours Fyers REST saver.
                if not trading_calendar.is_exchange_open("NSE"):
                    if was_open:
                        # Market JUST closed. Without this, the final 3m bar
                        # (15:27–15:30) and the close are dropped — it never
                        # reaches a later bucket before the gate idles the loop,
                        # so the feed stops at ~15:24. Take one forced final tick
                        # and force-close every open bar.
                        try:
                            await self.poll_once(force=True)
                            n = await self._persist(self._acc.flush_all())
                            logger.info(f"[chain-builder] session-close flush persisted {n} closing bars")
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(f"[chain-builder] session-close flush failed: {exc}")
                        was_open = False
                    try:
                        nxt = trading_calendar.next_exchange_open("NSE")
                        now = datetime.now(nxt.tzinfo) if nxt.tzinfo else datetime.now()
                        idle = max(cycle_seconds, min((nxt - now).total_seconds(), 300.0))
                    except Exception:
                        idle = 300.0
                    await asyncio.sleep(idle)
                    continue
                was_open = True
                try:
                    stats = await self.poll_once()
                    if stats["persisted"]:
                        logger.debug(f"[chain-builder] cycle {stats}")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[chain-builder] cycle error: {exc}")
                await asyncio.sleep(cycle_seconds)
        finally:
            # Flush any open bars on shutdown.
            try:
                await self._persist(self._acc.flush(datetime.now(timezone.utc)))
            except Exception:
                pass

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()


# Process singleton.
chain_candle_builder = ChainCandleBuilder()
