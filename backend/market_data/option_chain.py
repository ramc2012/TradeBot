"""Option chain service — polls, caches in Redis, calculates PCR, max pain, IV rank."""
from __future__ import annotations
import asyncio
import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import text

from brokers.base import BrokerAdapter, OptionChain
from db.database import AsyncSessionLocal
from db.redis_client import get_redis
from market_data.symbols import to_broker_symbol, to_fyers_symbol


POLL_INTERVAL = 30   # seconds — Redis cache refresh cadence
OC_TTL = 60          # Redis TTL for option chain
# Durable-history persistence cadence (option_chain_snapshots). Coarser than
# the 30s Redis poll so the DB doesn't take ~80 rows × N chains every 30s, but
# fine-grained enough for intraday PCR/max-pain/GEX time-series + pre-market
# analysis. 120s ≈ 190 snapshots/contract/session.
PERSIST_INTERVAL = 120


async def _catalog_underlying_key(symbol: str) -> Optional[str]:
    normalized = str(symbol or "").strip().upper()
    if not normalized or "|" in normalized or ":" in normalized:
        return None
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    SELECT underlying_key
                    FROM fo_underlying_catalog
                    WHERE symbol = :symbol
                      AND underlying_key IS NOT NULL
                    LIMIT 1
                    """
                ),
                {"symbol": normalized},
            )
            row = result.first()
            value = str(row.underlying_key or "").strip() if row else ""
            return value or None
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[OC] catalog underlying-key lookup failed for {normalized}: {exc}")
        return None


async def _chain_lookup_symbol(symbol: str, broker_name: str) -> str:
    """Return the broker-specific option-chain lookup key for indices/stocks."""
    if broker_name == "fyers":
        lookup = to_fyers_symbol(symbol)
        if lookup == symbol and "|" not in lookup and ":" not in lookup:
            # Fyers option-chain stock symbols use the cash-market ticker form.
            return f"NSE:{lookup.upper()}-EQ"
        return lookup

    lookup = to_broker_symbol(symbol)
    if lookup == symbol and "|" not in lookup and ":" not in lookup:
        return await _catalog_underlying_key(symbol) or lookup
    return lookup


class OptionChainService:
    """Periodically polls option chain, calculates analytics, stores in Redis."""

    def __init__(self):
        self._broker: Optional[BrokerAdapter] = None
        self._tracked: List[tuple[str, str]] = []  # (symbol, expiry) pairs
        self._task: Optional[asyncio.Task] = None
        # Last DB-persist time per (symbol, expiry) — throttles the durable
        # option_chain_snapshots write to PERSIST_INTERVAL.
        self._last_persist: Dict[tuple[str, str], datetime] = {}

    def set_broker(self, broker: BrokerAdapter):
        self._broker = broker

    def track(self, symbol: str, expiry: str):
        if (symbol, expiry) not in self._tracked:
            self._tracked.append((symbol, expiry))

    async def start(self):
        self._task = asyncio.create_task(self._poll_loop())

    async def ensure_running(self) -> None:
        """Acquire a broker (if missing) and start the poll loop (if not
        running). Idempotent — safe to call repeatedly. Lets callers that want
        a chain *guaranteed* tracked + persisted (e.g. the session-open pick
        registering the active index expiries) get the loop up without
        depending on a desk having started it first."""
        if self._broker is None:
            try:
                from api.routers.market import _get_market_adapter
                adapter, _ = await _get_market_adapter()
                if adapter is not None:
                    self.set_broker(adapter)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[OC] ensure_running broker acquire failed: {exc}")
        if self._task is None or self._task.done():
            try:
                await self.start()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[OC] ensure_running start failed: {exc}")

    async def stop(self):
        if self._task:
            self._task.cancel()

    async def get_cached(self, symbol: str, expiry: str) -> Optional[dict]:
        redis = await get_redis()
        raw = await redis.get(f"oc:{symbol}:{expiry}")
        return json.loads(raw) if raw else None

    async def cached_expiries(self, symbol: str) -> list[str]:
        """Return expiries currently present in Redis for one app symbol."""
        redis = await get_redis()
        prefix = f"oc:{symbol}:"
        expiries: set[str] = set()
        try:
            async for key in redis.scan_iter(match=f"{prefix}*"):
                raw = str(key)
                if raw.startswith(prefix):
                    expiry = raw[len(prefix):]
                    if expiry:
                        expiries.add(expiry)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[OC] cached-expiry scan failed for {symbol}: {exc}")
        return sorted(expiries)

    async def known_expiries(self, symbol: str) -> list[str]:
        """Return tracked + cached expiries, nearest first."""
        tracked = {
            expiry
            for tracked_symbol, expiry in self._tracked
            if tracked_symbol == symbol and expiry
        }
        cached = set(await self.cached_expiries(symbol))
        return sorted(tracked | cached)

    async def _poll_loop(self):
        while True:
            try:
                # ONE-DATA-ONE-COMPUTE: pin the canonical chain writer to the
                # option_chain-routed adapter (Upstox-first → streamed greeks) on
                # every cycle. This defeats last-writer-wins set_broker() flipping
                # the source to a non-greeks broker, and keeps this the SOLE writer
                # of oc:{symbol}:{expiry} that all consumers read via get_cached().
                # NON-BLOCKING: only consider already-connected adapters — never call
                # ensure_*_session() here (it can hang ~30s on a degraded session and
                # would stall the whole poll loop, staling every tracked chain).
                try:
                    from api.routers.auth import get_active_adapter
                    from market_data.source_policy import route_order
                    for _src in route_order("option_chain"):
                        if _src in ("upstox", "fyers"):
                            _a = get_active_adapter(_src)
                            if _a is not None:
                                self._broker = _a
                                break
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[OC] poll routing resolve failed: {exc}")
                for symbol, expiry in self._tracked:
                    await self._refresh(symbol, expiry)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[OC] Poll error: {e}")
            await asyncio.sleep(POLL_INTERVAL)

    async def _refresh(self, symbol: str, expiry: str):
        if not self._broker:
            return
        try:
            broker_name = getattr(self._broker, "broker_name", "")
            lookup_symbol = await _chain_lookup_symbol(symbol, broker_name)
            chain: OptionChain = await self._broker.get_option_chain(lookup_symbol, expiry)
            analytics = self._calculate_analytics(chain)
            payload = {
                "symbol": symbol,
                "expiry": expiry,
                "spot_price": chain.spot_price,
                "timestamp": datetime.utcnow().isoformat(),
                "source": getattr(self._broker, "broker_name", "unknown"),
                "entries": [
                    {
                        "strike": e.strike,
                        "option_type": e.option_type,
                        "ltp": e.ltp,
                        "oi": e.oi,
                        "volume": e.volume,
                        "bid": e.bid,
                        "ask": e.ask,
                        "iv": e.iv,
                        "delta": e.delta,
                        "gamma": e.gamma,
                        "theta": e.theta,
                        "vega": e.vega,
                        "prev_oi": e.prev_oi,
                        "prev_close": e.prev_close,
                        "oi_change": round(float(e.oi) - float(e.prev_oi or 0.0), 2),
                        "oi_change_pct": round(
                            ((float(e.oi) - float(e.prev_oi or 0.0)) / float(e.prev_oi or 1.0)) * 100.0,
                            2,
                        ) if e.prev_oi else None,
                        "ltp_change": round(float(e.ltp) - float(e.prev_close or 0.0), 2),
                        "ltp_change_pct": round(
                            ((float(e.ltp) - float(e.prev_close or 0.0)) / float(e.prev_close or 1.0)) * 100.0,
                            2,
                        ) if e.prev_close else None,
                        "instrument_key": e.instrument_key,
                    }
                    for e in chain.entries
                ],
                **analytics,
            }
            redis = await get_redis()
            await redis.set(f"oc:{symbol}:{expiry}", json.dumps(payload), ex=OC_TTL)
            logger.debug(f"[OC] Refreshed {symbol} {expiry}")

            # Durable persistence for analysis (2026-06-04). The Redis cache is
            # ephemeral (60s TTL) — it vanishes after market close, so there was
            # no chain history in the DB for pre-market reads or PCR/OI/GEX
            # time-series. Persist the full chain to option_chain_snapshots,
            # throttled to PERSIST_INTERVAL. Best-effort: a DB error here must
            # never break the live cache / poll loop.
            now = datetime.now(timezone.utc)
            last = self._last_persist.get((symbol, expiry))
            if last is None or (now - last).total_seconds() >= PERSIST_INTERVAL:
                try:
                    await self._persist_snapshot(symbol, expiry, chain, now)
                    self._last_persist[(symbol, expiry)] = now
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[OC] persist failed {symbol}/{expiry}: {exc}")
        except Exception as e:
            logger.error(f"[OC] Refresh failed {symbol}/{expiry}: {e}")

    async def _persist_snapshot(
        self, symbol: str, expiry: str, chain: OptionChain, ts: datetime
    ) -> None:
        """Write the full chain to option_chain_snapshots (TimescaleDB hypertable).

        One row per strike×side: time, symbol, expiry, strike, option_type,
        ltp, oi, volume, iv, delta, gamma, theta, vega — a durable, queryable
        history for PCR / max-pain / GEX-DEX time-series and pre-market
        analysis. Short-lived session (mindful of the small connection pool).
        """
        rows = [
            {
                "time": ts,
                "symbol": symbol,
                "expiry": str(expiry),
                "strike": float(e.strike),
                "option_type": e.option_type,
                "ltp": float(e.ltp) if e.ltp is not None else None,
                "oi": int(e.oi or 0),
                "volume": int(e.volume or 0),
                "iv": float(e.iv) if e.iv is not None else None,
                "delta": float(e.delta) if e.delta is not None else None,
                "gamma": float(e.gamma) if e.gamma is not None else None,
                "theta": float(e.theta) if e.theta is not None else None,
                "vega": float(e.vega) if e.vega is not None else None,
            }
            for e in chain.entries
        ]
        if not rows:
            return
        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO option_chain_snapshots
                        (time, symbol, expiry, strike, option_type, ltp, oi,
                         volume, iv, delta, gamma, theta, vega)
                    VALUES
                        (:time, :symbol, :expiry, :strike, :option_type, :ltp, :oi,
                         :volume, :iv, :delta, :gamma, :theta, :vega)
                    """
                ),
                rows,
            )
            await session.commit()

    def _calculate_analytics(self, chain: OptionChain) -> dict:
        ce_entries = [e for e in chain.entries if e.option_type == "CE"]
        pe_entries = [e for e in chain.entries if e.option_type == "PE"]

        total_ce_oi = sum(e.oi for e in ce_entries)
        total_pe_oi = sum(e.oi for e in pe_entries)
        total_ce_prev_oi = sum(float(e.prev_oi or 0.0) for e in ce_entries)
        total_pe_prev_oi = sum(float(e.prev_oi or 0.0) for e in pe_entries)
        total_ce_vol = sum(e.volume for e in ce_entries)
        total_pe_vol = sum(e.volume for e in pe_entries)

        pcr_oi = total_pe_oi / total_ce_oi if total_ce_oi > 0 else 1.0
        pcr_vol = total_pe_vol / total_ce_vol if total_ce_vol > 0 else 1.0
        prev_pcr_oi = (
            total_pe_prev_oi / total_ce_prev_oi if total_ce_prev_oi > 0 else None
        )

        max_pain = self._calculate_max_pain(chain.entries, chain.spot_price)

        # ATM IV
        atm_strike = self._get_atm_strike(chain)
        atm_iv = 0.0
        atm_call = None
        atm_put = None
        for e in chain.entries:
            if e.strike == atm_strike and e.option_type == "CE":
                atm_iv = e.iv or 0.0
                atm_call = e
            if e.strike == atm_strike and e.option_type == "PE":
                atm_put = e

        # Gamma exposure per strike
        gamma_exposure = {}
        for e in chain.entries:
            key = str(e.strike)
            ge = gamma_exposure.get(key, 0.0)
            sign = 1 if e.option_type == "CE" else -1
            gamma_exposure[key] = ge + sign * (e.gamma or 0) * e.oi * chain.spot_price

        return {
            "pcr_oi": round(pcr_oi, 4),
            "pcr_volume": round(pcr_vol, 4),
            "pcr_prev_oi": round(prev_pcr_oi, 4) if prev_pcr_oi is not None else None,
            "pcr_oi_change": round(pcr_oi - prev_pcr_oi, 4) if prev_pcr_oi is not None else None,
            "max_pain": max_pain,
            "atm_strike": atm_strike,
            "atm_iv": round(atm_iv, 4),
            "total_ce_oi": total_ce_oi,
            "total_pe_oi": total_pe_oi,
            "total_ce_prev_oi": round(total_ce_prev_oi, 2),
            "total_pe_prev_oi": round(total_pe_prev_oi, 2),
            "total_ce_oi_change": round(total_ce_oi - total_ce_prev_oi, 2),
            "total_pe_oi_change": round(total_pe_oi - total_pe_prev_oi, 2),
            "total_ce_volume": total_ce_vol,
            "total_pe_volume": total_pe_vol,
            "atm_call_ltp_change": round(float(atm_call.ltp) - float(atm_call.prev_close or 0.0), 2)
            if atm_call and atm_call.prev_close
            else None,
            "atm_call_ltp_change_pct": round(
                ((float(atm_call.ltp) - float(atm_call.prev_close or 0.0)) / float(atm_call.prev_close or 1.0)) * 100.0,
                2,
            ) if atm_call and atm_call.prev_close else None,
            "atm_put_ltp_change": round(float(atm_put.ltp) - float(atm_put.prev_close or 0.0), 2)
            if atm_put and atm_put.prev_close
            else None,
            "atm_put_ltp_change_pct": round(
                ((float(atm_put.ltp) - float(atm_put.prev_close or 0.0)) / float(atm_put.prev_close or 1.0)) * 100.0,
                2,
            ) if atm_put and atm_put.prev_close else None,
            "atm_call_oi_change": round(float(atm_call.oi) - float(atm_call.prev_oi or 0.0), 2)
            if atm_call
            else None,
            "atm_put_oi_change": round(float(atm_put.oi) - float(atm_put.prev_oi or 0.0), 2)
            if atm_put
            else None,
            "gamma_exposure": gamma_exposure,
        }

    def _get_atm_strike(self, chain: OptionChain) -> float:
        if not chain.entries:
            return 0.0
        strikes = sorted(set(e.strike for e in chain.entries))
        return min(strikes, key=lambda s: abs(s - chain.spot_price))

    def _calculate_max_pain(self, entries: list, spot: float) -> float:
        """Max pain = strike where total option buyers lose most."""
        from collections import defaultdict
        strikes = sorted(set(e.strike for e in entries))
        oi_by_strike: dict = defaultdict(lambda: {"CE": 0, "PE": 0})
        for e in entries:
            oi_by_strike[e.strike][e.option_type] += e.oi

        pain = {}
        for exp_strike in strikes:
            total = 0.0
            for k, oi in oi_by_strike.items():
                # CE loss at expiry_price < strike → intrinsic 0
                ce_loss = max(0, exp_strike - k) * oi["CE"]
                pe_loss = max(0, k - exp_strike) * oi["PE"]
                total += ce_loss + pe_loss
            pain[exp_strike] = total

        return min(pain, key=pain.get) if pain else spot


option_chain_service = OptionChainService()
