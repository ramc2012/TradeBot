"""Upstox full-universe option-chain → 30m OHLC+greeks builder.

WHY THIS EXISTS
---------------
`option_premium_candles.iv` stopped being populated for equities on 2026-07-28.
Two feeds had produced it and both are gone for live use:

  * `source='upstox_expired'` — research-sync's Black-Scholes pass over ALREADY
    EXPIRED contracts. It wrote 204 underlyings on 2026-07-28 and then stopped.
    Backward-looking by construction: it can only price a contract after that
    contract has expired, so it can never be a live feed no matter how it is
    scheduled.
  * `source='fyers_chain'` — `chain_candle_builder`, this module's sibling and
    template. It is disabled (CHAIN_CANDLE_BUILDER_ENABLED=false) AND
    hard-coupled to Fyers, whose credentials no longer decrypt in this
    deployment (Fyers moved to the sibling MACD-mini project).

Everything downstream that needs implied vol went blind — most visibly
Vanguard's M2 informed-flow scanner, whose IV-spread and skew ingredients are
its whole signal.

Upstox `/option/chain` returns per-strike greeks for EQUITY options and Upstox
is the connected broker here. Verified live on 2026-08-27 during an open
session: RELIANCE 2026-09-29 returned 76 entries, 62 carrying iv with real
delta/gamma. This module turns that endpoint into the missing feed.

RELATIONSHIP TO chain_candle_builder
------------------------------------
The bar-accumulation logic is IDENTICAL and is imported, not copied:
`ChainBarAccumulator`, the bucket constants and the 30m grid phase all come
from that module. Only three things genuinely differ, and each is why this is a
separate class rather than a flag on the existing one:

  1. Upstox REQUIRES an explicit expiry date; Fyers accepts "" for front month.
     So this resolves a front expiry per underlying from fo_contract_catalog.
  2. Upstox addresses an underlying by instrument key (`NSE_EQ|INE002A01018`),
     not a Fyers symbol.
  3. THE IV UNIT DIFFERS. See below — this is the one that silently corrupts
     data if missed.

IV UNIT — the trap
------------------
Upstox reports iv as a PERCENT (27.34 for 27.34%). `option_premium_candles.iv`
is a FRACTION (0.2734) — the convention every consumer expects, and the one
`greeks_enrichment` already documents for `option_chain_snapshots`. Writing the
raw value inflates iv 100x, which would not crash anything: it would quietly
poison every z-score and percentile computed downstream. `_iv_fraction()` does
the conversion and a test pins it.

30-MINUTE GRID — the other trap
-------------------------------
NSE option 30m bars are anchored to the :15/:45 IST session grid (09:15, 09:45,
…), NOT the :00/:30 wall clock. `chain_candle_builder` encodes that as
BUCKET_PHASE_30M=900 with an explicit warning that mixing grids "interleaves two
15-min-offset grids" in the same interval='30minute' partition. This module
imports that constant rather than restating it, so the two builders can never
drift onto different grids.

SCOPE
-----
30-minute bars only. The Fyers builder also emits 3m because S1 needs it; the
consumers this feed exists for read the 30minute partition, and every extra
interval is extra write volume on a shared production table for no reader.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from typing import Any, Optional

from loguru import logger

from core.trading_calendar import trading_calendar
from market_data.chain_candle_builder import (
    BUCKET_PHASE_30M,
    BUCKET_SECONDS_30M,
    ChainBarAccumulator,
    ContractKey,
    _Bar,
)

SOURCE = "upstox_chain"
INTERVAL = "30minute"
# One poll per underlying per bar. The bar is 30 minutes wide, so a tighter
# cadence buys intra-bar OHLC detail that no consumer of this feed reads, at a
# directly proportional cost in broker calls.
POLL_INTERVAL_SECONDS = 1800.0
# How often the loop wakes to see whose turn it is. Small relative to the poll
# interval so a name is never more than this late.
CYCLE_SECONDS = 20.0


def _iv_fraction(raw: Any) -> Optional[float]:
    """Upstox percent iv (27.34) -> the fraction (0.2734) this column stores.

    Returns None for missing/zero rather than 0.0: a strike with no iv is
    genuinely unpriced, and a 0.0 would read as "vol is zero", which is a
    number, not an absence.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value / 100.0


class UpstoxChainBuilder:
    """Polls the F&O universe on Upstox and writes 30m CE+PE bars WITH greeks."""

    def __init__(self) -> None:
        self._acc = ChainBarAccumulator(
            bucket_seconds=BUCKET_SECONDS_30M, phase_offset_seconds=BUCKET_PHASE_30M
        )
        self._next_due: dict[str, float] = {}
        self._universe: list[tuple[str, str, date]] = []   # (symbol, instrument_key, front_expiry)
        self._universe_day: Optional[date] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_stats: dict[str, Any] = {}

    # ── universe ────────────────────────────────────────────────────────────
    async def _load_universe(self) -> list[tuple[str, str, date]]:
        """(app symbol, Upstox underlying key, front expiry) for every F&O name.

        Cached per calendar day: the front expiry only changes at a monthly
        roll, and re-querying it every 20-second cycle would be pure noise
        against a shared database.
        """
        today = datetime.now(timezone.utc).date()
        if self._universe and self._universe_day == today:
            return self._universe

        from sqlalchemy import text

        from db.database import AsyncSessionLocal

        # Front expiry comes from fo_contract_catalog (the Upstox instrument
        # master, 213 underlyings) rather than fo_expiry_catalog, which only
        # carries 9 names and would silently cover 4% of the universe.
        sql = """
            SELECT c.underlying, u.underlying_key, MIN(c.expiry) AS front_expiry
            FROM fo_contract_catalog c
            JOIN fo_underlying_catalog u ON u.symbol = c.underlying
            WHERE c.expiry >= CURRENT_DATE
              AND u.underlying_key IS NOT NULL
            GROUP BY c.underlying, u.underlying_key
            ORDER BY c.underlying
        """
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(text(sql))).all()

        self._universe = [(r[0], r[1], r[2]) for r in rows]
        self._universe_day = today
        logger.info(f"[upstox-chain] universe: {len(self._universe)} underlyings for {today}")
        return self._universe

    @staticmethod
    async def _adapter():
        from api.routers.auth import get_active_adapter

        return get_active_adapter("upstox")

    # ── one poll cycle ──────────────────────────────────────────────────────
    async def poll_once(self, *, now_mono: Optional[float] = None) -> dict[str, Any]:
        import time

        now_mono = time.monotonic() if now_mono is None else now_mono
        adapter = await self._adapter()
        if adapter is None:
            logger.warning("[upstox-chain] no active Upstox session; skipping poll")
            return {"polled": 0, "contracts_submitted": 0, "skipped": 0, "universe": 0}

        universe = await self._load_universe()
        polled = submitted = skipped = failed = 0

        for symbol, underlying_key, front_expiry in universe:
            if now_mono < self._next_due.get(symbol, 0.0):
                skipped += 1
                continue
            self._next_due[symbol] = now_mono + POLL_INTERVAL_SECONDS

            try:
                # Same governance as the Fyers builder: a broad-universe sweep
                # is BULK class, so it is hard-capped as a share of the broker
                # budget and yields instantly to queued critical work (live
                # marks, session health) rather than starving them.
                from brokers.rate_limiter import (
                    CLASS_BULK,
                    PRIORITY_CHAIN_BUILDER,
                    broker_class,
                    broker_priority,
                )

                with broker_priority(PRIORITY_CHAIN_BUILDER), broker_class(CLASS_BULK):
                    chain = await adapter.get_option_chain(underlying_key, front_expiry.isoformat())
            except Exception as exc:  # noqa: BLE001 — isolate one bad name from the sweep
                failed += 1
                logger.debug(f"[upstox-chain] {symbol} chain fetch failed: {exc}")
                continue

            polled += 1
            closed = self._ingest_chain(symbol, front_expiry, chain, datetime.now(timezone.utc))
            submitted += await self._persist(closed)

        stats = {
            "polled": polled,
            # Contracts submitted to the writer, NOT rows confirmed in the
            # table — see _persist's docstring for why the difference matters.
            "contracts_submitted": submitted,
            "skipped": skipped,
            "failed": failed,
            "universe": len(universe),
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        if polled:
            self._last_stats = stats
        return stats

    def _ingest_chain(
        self, underlying: str, expiry: date, chain: Any, ts: datetime
    ) -> list[tuple[ContractKey, _Bar]]:
        """Fold one chain snapshot into the 30m accumulator.

        The expiry is taken from what we ASKED for, not from the response:
        Upstox echoes the request and an empty/differently-formatted echo would
        otherwise produce a contract key that never matches the next snapshot,
        silently opening a new bar every poll.
        """
        closed: list[tuple[ContractKey, _Bar]] = []
        expiry_iso = expiry.isoformat()
        spot = float(getattr(chain, "spot_price", 0) or 0)

        for entry in getattr(chain, "entries", []) or []:
            option_type = str(getattr(entry, "option_type", "")).upper()
            if option_type not in {"CE", "PE"}:
                continue
            ltp = float(getattr(entry, "ltp", 0) or 0)
            if ltp <= 0:
                continue  # unquoted strike — accumulating it would fabricate a bar
            key: ContractKey = (
                underlying,
                expiry_iso,
                float(getattr(entry, "strike", 0) or 0),
                option_type,
            )
            result = self._acc.update(
                key,
                ts,
                ltp,
                volume=float(getattr(entry, "volume", 0) or 0),
                oi=int(getattr(entry, "oi", 0) or 0),
                iv=_iv_fraction(getattr(entry, "iv", None)),
                delta=getattr(entry, "delta", None),
                gamma=getattr(entry, "gamma", None),
                theta=getattr(entry, "theta", None),
                vega=getattr(entry, "vega", None),
                underlying_price=spot or None,
                instrument_key=getattr(entry, "instrument_key", None),
            )
            if result is not None:
                closed.append(result)
        return closed

    async def _persist(self, closed: list[tuple[ContractKey, _Bar]]) -> int:
        """Returns CONTRACTS SUBMITTED without error — deliberately not called
        `rows written`, because it is not that.

        `_persist_broker_candles` drops rows silently at its own write
        chokepoint (the phantom-contract gate), and returns nothing, so the
        true landed-row count is not observable from here. Measured on the
        first live run: 73 submissions produced 71 rows. Naming this
        `persisted` and reporting it as rows would overstate the feed's
        coverage by exactly the amount that was rejected — the failure mode
        being that a partially-rejected sweep looks complete.
        """
        if not closed:
            return 0
        from market_data.option_history import OptionHistoryService

        service = OptionHistoryService()
        count = 0
        for key, bar in closed:
            underlying, expiry_iso, strike, option_type = key
            instrument_key = self._acc.instrument_key_for(key)
            if not instrument_key:
                continue
            try:
                expiry_dt = date.fromisoformat(expiry_iso)
            except ValueError:
                continue
            try:
                await service._persist_broker_candles(
                    rows=[bar.to_row()],
                    underlying=underlying,
                    expiry=expiry_dt,
                    strike=strike,
                    option_type=option_type,
                    instrument_key=instrument_key,
                    interval=INTERVAL,
                    already_in_db=set(),
                    source=SOURCE,
                )
                count += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    f"[upstox-chain] persist failed {underlying} {strike}{option_type}: {exc}"
                )
        return count

    def status(self) -> dict[str, Any]:
        from core.config import settings

        return {
            "running": self._running,
            "enabled": bool(getattr(settings, "UPSTOX_CHAIN_BUILDER_ENABLED", False)),
            "source": SOURCE,
            "interval": INTERVAL,
            "universe": len(self._universe),
            "last_cycle": self._last_stats,
        }

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def _run_loop(self) -> None:
        self._running = True
        logger.info(f"[upstox-chain] started full-universe {INTERVAL} chain → option_premium_candles")
        try:
            while self._running:
                if not trading_calendar.is_exchange_open("NSE"):
                    # The chain only moves while NSE is open. Idle to the next
                    # session instead of sweeping ~213 names round the clock.
                    try:
                        nxt = trading_calendar.next_exchange_open("NSE")
                        now = datetime.now(nxt.tzinfo) if nxt.tzinfo else datetime.now()
                        idle = max(CYCLE_SECONDS, min((nxt - now).total_seconds(), 300.0))
                    except Exception:
                        idle = 300.0
                    await asyncio.sleep(idle)
                    continue
                try:
                    stats = await self.poll_once()
                    if stats.get("contracts_submitted"):
                        logger.info(f"[upstox-chain] cycle {stats}")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[upstox-chain] cycle error: {exc}")
                await asyncio.sleep(CYCLE_SECONDS)
        finally:
            # Flush open bars on shutdown so a restart does not silently drop
            # the partially-accumulated bar.
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


# Process singleton, matching chain_candle_builder's convention.
upstox_chain_builder = UpstoxChainBuilder()
