"""Option chain service — polls, caches in Redis, calculates PCR, max pain, IV rank."""
from __future__ import annotations
import asyncio
import json
import math
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger

from brokers.base import BrokerAdapter, OptionChain
from db.redis_client import get_redis
from market_data.symbols import to_broker_symbol


POLL_INTERVAL = 30  # seconds
OC_TTL = 60         # Redis TTL for option chain


class OptionChainService:
    """Periodically polls option chain, calculates analytics, stores in Redis."""

    def __init__(self):
        self._broker: Optional[BrokerAdapter] = None
        self._tracked: List[tuple[str, str]] = []  # (symbol, expiry) pairs
        self._task: Optional[asyncio.Task] = None

    def set_broker(self, broker: BrokerAdapter):
        self._broker = broker

    def track(self, symbol: str, expiry: str):
        if (symbol, expiry) not in self._tracked:
            self._tracked.append((symbol, expiry))

    async def start(self):
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self):
        if self._task:
            self._task.cancel()

    async def get_cached(self, symbol: str, expiry: str) -> Optional[dict]:
        redis = await get_redis()
        raw = await redis.get(f"oc:{symbol}:{expiry}")
        return json.loads(raw) if raw else None

    async def _poll_loop(self):
        while True:
            try:
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
            chain: OptionChain = await self._broker.get_option_chain(to_broker_symbol(symbol), expiry)
            analytics = self._calculate_analytics(chain)
            payload = {
                "symbol": symbol,
                "expiry": expiry,
                "spot_price": chain.spot_price,
                "timestamp": datetime.utcnow().isoformat(),
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
                    }
                    for e in chain.entries
                ],
                **analytics,
            }
            redis = await get_redis()
            await redis.set(f"oc:{symbol}:{expiry}", json.dumps(payload), ex=OC_TTL)
            logger.debug(f"[OC] Refreshed {symbol} {expiry}")
        except Exception as e:
            logger.error(f"[OC] Refresh failed {symbol}/{expiry}: {e}")

    def _calculate_analytics(self, chain: OptionChain) -> dict:
        ce_entries = [e for e in chain.entries if e.option_type == "CE"]
        pe_entries = [e for e in chain.entries if e.option_type == "PE"]

        total_ce_oi = sum(e.oi for e in ce_entries)
        total_pe_oi = sum(e.oi for e in pe_entries)
        total_ce_vol = sum(e.volume for e in ce_entries)
        total_pe_vol = sum(e.volume for e in pe_entries)

        pcr_oi = total_pe_oi / total_ce_oi if total_ce_oi > 0 else 1.0
        pcr_vol = total_pe_vol / total_ce_vol if total_ce_vol > 0 else 1.0

        max_pain = self._calculate_max_pain(chain.entries, chain.spot_price)

        # ATM IV
        atm_strike = self._get_atm_strike(chain)
        atm_iv = 0.0
        for e in chain.entries:
            if e.strike == atm_strike and e.option_type == "CE":
                atm_iv = e.iv or 0.0
                break

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
            "max_pain": max_pain,
            "atm_strike": atm_strike,
            "atm_iv": round(atm_iv, 4),
            "total_ce_oi": total_ce_oi,
            "total_pe_oi": total_pe_oi,
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
