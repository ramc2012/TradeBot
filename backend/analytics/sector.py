"""Sector rotation tracker and macro dashboard."""
from __future__ import annotations
import asyncio
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import httpx
from loguru import logger

from db.redis_client import get_redis


SECTORAL_INDICES = {
    "NIFTY_IT": "NSE:NIFTYIT-INDEX",
    "NIFTY_BANK": "NSE:BANKNIFTY-INDEX",
    "NIFTY_FMCG": "NSE:NIFTYFMCG-INDEX",
    "NIFTY_AUTO": "NSE:NIFTYAUTO-INDEX",
    "NIFTY_PHARMA": "NSE:NIFTYPHARMA-INDEX",
    "NIFTY_METAL": "NSE:NIFTYMETAL-INDEX",
    "NIFTY_ENERGY": "NSE:NIFTYENERGY-INDEX",
    "NIFTY_REALTY": "NSE:NIFTYREALTY-INDEX",
}

MACRO_SYMBOLS = {
    "INDIA_VIX": "NSE:INDIA_VIX-INDEX",
    "NIFTY50": "NSE:NIFTY50-INDEX",
}


class SectorRotationTracker:
    """Track sectoral relative strength vs Nifty 50 and macro indicators."""

    CACHE_TTL = 300  # 5 minutes

    def __init__(self):
        self._sector_data: Dict[str, List[float]] = {}  # 20-day price history
        self._nifty_data: List[float] = []

    async def get_sector_rotation(self) -> dict:
        """Return relative strength of each sector vs Nifty 50."""
        redis = await get_redis()
        cached = await redis.get("sector_rotation")
        if cached:
            return json.loads(cached)

        result = await self._calculate_relative_strength()
        await redis.set("sector_rotation", json.dumps(result), ex=self.CACHE_TTL)
        return result

    async def _calculate_relative_strength(self) -> dict:
        """Calculate 20-day relative strength for each sector."""
        try:
            # In production, fetch from broker API or NSE
            # Using placeholder data structure
            sectors = {}
            for name in SECTORAL_INDICES:
                change = 0.0  # Would fetch real 20-day change
                sectors[name] = {
                    "name": name,
                    "relative_strength": change,
                    "trend": "neutral",
                }
            return {
                "sectors": sectors,
                "timestamp": datetime.utcnow().isoformat(),
            }
        except Exception as e:
            logger.error(f"[Sector] Error calculating RS: {e}")
            return {"sectors": {}, "timestamp": datetime.utcnow().isoformat()}

    async def get_macro_dashboard(self) -> dict:
        """Fetch macro indicators: India VIX, Crude, Gold, DXY, US 10Y."""
        redis = await get_redis()
        cached = await redis.get("macro_dashboard")
        if cached:
            return json.loads(cached)

        result = {
            "india_vix": await self._get_india_vix(),
            "crude_mcx": {"price": 0, "change_pct": 0, "sparkline": []},
            "gold_mcx": {"price": 0, "change_pct": 0, "sparkline": []},
            "dxy": {"price": 0, "change_pct": 0, "sparkline": []},
            "us10y": {"price": 0, "change_pct": 0, "sparkline": []},
            "timestamp": datetime.utcnow().isoformat(),
        }
        await redis.set("macro_dashboard", json.dumps(result), ex=self.CACHE_TTL)
        return result

    async def _get_india_vix(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(
                    "https://www.nseindia.com/api/allIndices",
                    headers={"User-Agent": "Mozilla/5.0"},
                )
            data = r.json().get("data", [])
            for item in data:
                if item.get("index") == "India VIX":
                    return {
                        "price": item.get("last", 0),
                        "change_pct": item.get("percentChange", 0),
                        "sparkline": [],
                    }
        except Exception:
            pass
        return {"price": 0, "change_pct": 0, "sparkline": []}

    async def get_iv_rank(self, symbol: str) -> dict:
        """Calculate IV rank and percentile (52-week)."""
        redis = await get_redis()
        cached = await redis.get(f"iv_rank:{symbol}")
        if cached:
            return json.loads(cached)

        # Placeholder — in production, fetch historical IV from stored option_chain_snapshots
        result = {
            "symbol": symbol,
            "current_iv": 0.0,
            "iv_rank": 0.0,      # 0-100, where current IV sits in 52w range
            "iv_percentile": 0.0,
            "iv_52w_high": 0.0,
            "iv_52w_low": 0.0,
        }
        await redis.set(f"iv_rank:{symbol}", json.dumps(result), ex=300)
        return result


sector_tracker = SectorRotationTracker()
