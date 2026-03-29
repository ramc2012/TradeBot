"""Sector rotation tracker and macro dashboard."""
from __future__ import annotations

import json
from datetime import datetime
from statistics import mean
from typing import Dict, List, Optional
from urllib.parse import quote

import httpx
from loguru import logger

from db.redis_client import get_redis


SECTORAL_INDICES = {
    "NIFTY_BANK": {"symbol": "NSE:NIFTYBANK-INDEX", "label": "Bank", "upstox_symbol": "NSE_INDEX|Nifty Bank"},
    "NIFTY_IT": {"symbol": "NSE:NIFTYIT-INDEX", "label": "IT", "upstox_symbol": "NSE_INDEX|Nifty IT"},
    "NIFTY_AUTO": {"symbol": "NSE:NIFTYAUTO-INDEX", "label": "Auto", "upstox_symbol": "NSE_INDEX|Nifty Auto"},
    "NIFTY_PHARMA": {"symbol": "NSE:NIFTYPHARMA-INDEX", "label": "Pharma", "upstox_symbol": "NSE_INDEX|Nifty Pharma"},
    "NIFTY_FMCG": {"symbol": "NSE:NIFTYFMCG-INDEX", "label": "FMCG", "upstox_symbol": "NSE_INDEX|Nifty FMCG"},
    "NIFTY_METAL": {"symbol": "NSE:NIFTYMETAL-INDEX", "label": "Metal", "upstox_symbol": "NSE_INDEX|Nifty Metal"},
    "NIFTY_ENERGY": {"symbol": "NSE:NIFTYENERGY-INDEX", "label": "Energy", "upstox_symbol": "NSE_INDEX|Nifty Energy"},
    "NIFTY_REALTY": {"symbol": "NSE:NIFTYREALTY-INDEX", "label": "Realty", "upstox_symbol": "NSE_INDEX|Nifty Realty"},
}

MACRO_SYMBOLS = {
    "INDIA_VIX": "NSE:INDIA_VIX-INDEX",
    "NIFTY50": "NSE:NIFTY50-INDEX",
}
UPSTOX_BENCHMARK_SYMBOL = "NSE_INDEX|Nifty 50"


class SectorRotationTracker:
    """Track live sector strength and build a simple RRG against NIFTY 50."""

    CACHE_TTL = 15
    HISTORY_LIMIT = 96
    RRG_TAIL = 8

    def __init__(self):
        self._price_history: Dict[str, List[tuple[str, float]]] = {}
        self._baseline_price: Dict[str, float] = {}

    async def get_sector_rotation(self) -> dict:
        redis = await get_redis()
        cached = await redis.get("sector_rotation")
        if cached:
            return json.loads(cached)

        result = await self._calculate_relative_strength()
        await redis.set("sector_rotation", json.dumps(result), ex=self.CACHE_TTL)
        return result

    async def _calculate_relative_strength(self) -> dict:
        quotes, source, detail = await self._fetch_sector_quotes()
        if not quotes:
            return {
                "benchmark": None,
                "watchlist": [],
                "rrg": {"benchmark_symbol": MACRO_SYMBOLS["NIFTY50"], "points": [], "quadrant_counts": {}},
                "sectors": {},
                "source": source,
                "detail": detail,
                "timestamp": datetime.utcnow().isoformat(),
            }

        timestamp = datetime.utcnow().isoformat()
        self._record_snapshot(quotes, timestamp)
        benchmark_symbol = MACRO_SYMBOLS["NIFTY50"]
        benchmark_price = float(quotes.get(benchmark_symbol, 0.0) or 0.0)
        benchmark_history = self._get_price_series(benchmark_symbol)

        benchmark_change_pct = self._tracked_change_pct(benchmark_symbol, benchmark_history)
        benchmark = {
            "symbol": benchmark_symbol,
            "name": "NIFTY 50",
            "price": round(benchmark_price, 2),
            "tracked_change_pct": round(benchmark_change_pct, 2),
            "samples": len(benchmark_history),
        }

        watchlist = []
        rrg_points = []
        for code, meta in SECTORAL_INDICES.items():
            symbol = meta["symbol"]
            price_history = self._get_price_series(symbol)
            current_price = float(quotes.get(symbol, 0.0) or 0.0)
            tracked_change_pct = self._tracked_change_pct(symbol, price_history)
            relative_strength_pct = tracked_change_pct - benchmark_change_pct
            rrg_series = self._build_rrg_series(symbol, benchmark_symbol, price_history, benchmark_history)
            ratio = rrg_series[-1]["ratio"] if rrg_series else 100.0
            momentum = rrg_series[-1]["momentum"] if rrg_series else 100.0
            quadrant = self._quadrant(ratio, momentum)
            trend = self._trend_label(ratio, momentum)

            entry = {
                "code": code,
                "name": meta["label"],
                "symbol": symbol,
                "price": round(current_price, 2),
                "tracked_change_pct": round(tracked_change_pct, 2),
                "relative_strength_pct": round(relative_strength_pct, 2),
                "rrg_ratio": round(ratio, 2),
                "rrg_momentum": round(momentum, 2),
                "quadrant": quadrant,
                "trend": trend,
                "samples": len(price_history),
            }
            watchlist.append(entry)
            rrg_points.append(
                {
                    **entry,
                    "trail": rrg_series[-self.RRG_TAIL:],
                }
            )

        watchlist.sort(
            key=lambda row: (row["quadrant"] != "leading", -row["relative_strength_pct"], -row["rrg_momentum"])
        )
        quadrant_counts: Dict[str, int] = {}
        for point in rrg_points:
            quadrant_counts[point["quadrant"]] = quadrant_counts.get(point["quadrant"], 0) + 1

        return {
            "benchmark": benchmark,
            "watchlist": watchlist,
            "rrg": {
                "benchmark_symbol": benchmark_symbol,
                "points": rrg_points,
                "quadrant_counts": quadrant_counts,
            },
            "sectors": {entry["code"]: entry for entry in watchlist},
            "source": source,
            "detail": detail,
            "timestamp": timestamp,
        }

    async def _fetch_sector_quotes(self) -> tuple[dict[str, float], str, Optional[str]]:
        from api.routers.auth import get_active_adapter
        from api.routers.auth import get_broker_token

        fyers_adapter = get_active_adapter("fyers")
        symbols = [MACRO_SYMBOLS["NIFTY50"], *[meta["symbol"] for meta in SECTORAL_INDICES.values()]]

        if fyers_adapter and hasattr(fyers_adapter, "get_ltp"):
            try:
                quotes = await fyers_adapter.get_ltp(symbols)
                return quotes, "fyers", None
            except Exception as exc:
                logger.warning(f"[Sector] Fyers quote fetch failed: {exc}")

        upstox_token = get_broker_token("upstox")
        if upstox_token:
            quotes = await self._fetch_upstox_quotes(upstox_token)
            if quotes:
                await self._ensure_upstox_baselines(upstox_token)
                return quotes, "upstox", None

        return {}, "none", "Connect Fyers or Upstox to populate the sector watchlist and RRG."

    async def _fetch_upstox_quotes(self, access_token: str) -> dict[str, float]:
        upstox_to_app = {meta["upstox_symbol"]: meta["symbol"] for meta in SECTORAL_INDICES.values()}
        upstox_to_app[UPSTOX_BENCHMARK_SYMBOL] = MACRO_SYMBOLS["NIFTY50"]
        joined = ",".join(upstox_to_app.keys())
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://api.upstox.com/v2/market-quote/ltp",
                params={"instrument_key": joined},
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
        if response.status_code != 200:
            return {}
        payload = response.json().get("data", {})
        quotes: dict[str, float] = {}
        for item in payload.values():
            upstox_symbol = item.get("instrument_token")
            app_symbol = upstox_to_app.get(upstox_symbol)
            if app_symbol:
                quotes[app_symbol] = float(item.get("last_price", 0.0) or 0.0)
        return quotes

    async def _ensure_upstox_baselines(self, access_token: str):
        required = {
            MACRO_SYMBOLS["NIFTY50"]: UPSTOX_BENCHMARK_SYMBOL,
            **{meta["symbol"]: meta["upstox_symbol"] for meta in SECTORAL_INDICES.values()},
        }
        missing = {app_symbol: upstox_symbol for app_symbol, upstox_symbol in required.items() if app_symbol not in self._baseline_price}
        if not missing:
            return

        async with httpx.AsyncClient(timeout=10.0) as client:
            for app_symbol, upstox_symbol in missing.items():
                url = (
                    "https://api.upstox.com/v2/historical-candle/"
                    f"{quote(upstox_symbol, safe='')}/day/{datetime.utcnow().date().isoformat()}/"
                    f"{(datetime.utcnow().date()).replace(day=1).isoformat()}"
                )
                response = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                )
                if response.status_code != 200:
                    continue
                candles = response.json().get("data", {}).get("candles", [])
                if len(candles) >= 2:
                    self._baseline_price[app_symbol] = float(candles[1][4])
                elif candles:
                    self._baseline_price[app_symbol] = float(candles[0][1])

    def _record_snapshot(self, quotes: dict[str, float], timestamp: str):
        for symbol, price in quotes.items():
            if not price:
                continue
            history = self._price_history.setdefault(symbol, [])
            if history and abs(history[-1][1] - float(price)) < 1e-9:
                continue
            history.append((timestamp, float(price)))
            self._price_history[symbol] = history[-self.HISTORY_LIMIT:]

    def _get_price_series(self, symbol: str) -> List[float]:
        return [price for _, price in self._price_history.get(symbol, []) if price > 0]

    def _tracked_change_pct(self, symbol: str, series: List[float]) -> float:
        baseline = self._baseline_price.get(symbol)
        if baseline and series:
            return ((series[-1] / baseline) - 1.0) * 100.0
        if len(series) < 2 or not series[0]:
            return 0.0
        return ((series[-1] / series[0]) - 1.0) * 100.0

    def _build_rrg_series(
        self,
        symbol: str,
        benchmark_symbol: str,
        sector_series: List[float],
        benchmark_series: List[float],
    ) -> List[dict]:
        length = min(len(sector_series), len(benchmark_series))
        if length == 0:
            return []
        sector = sector_series[-length:]
        benchmark = benchmark_series[-length:]
        ratios = [s / b for s, b in zip(sector, benchmark) if b]
        baseline_sector = self._baseline_price.get(symbol)
        baseline_benchmark = self._baseline_price.get(benchmark_symbol)
        if baseline_sector and baseline_benchmark:
            baseline_ratio = baseline_sector / baseline_benchmark
            if not ratios or abs(ratios[0] - baseline_ratio) > 1e-9:
                ratios = [baseline_ratio, *ratios]
        if not ratios:
            return []

        lookback = min(10, len(ratios))
        series = []
        for idx, ratio in enumerate(ratios):
            recent = ratios[max(0, idx - lookback + 1): idx + 1]
            avg_ratio = mean(recent) if recent else ratio
            ratio_value = 100.0 if avg_ratio == 0 else 100.0 + ((ratio / avg_ratio) - 1.0) * 100.0
            prev_ratio = ratios[max(0, idx - 3)]
            momentum_value = 100.0 if prev_ratio == 0 else 100.0 + ((ratio / prev_ratio) - 1.0) * 100.0
            series.append({"ratio": ratio_value, "momentum": momentum_value})
        return series

    @staticmethod
    def _quadrant(ratio: float, momentum: float) -> str:
        if ratio >= 100 and momentum >= 100:
            return "leading"
        if ratio < 100 and momentum >= 100:
            return "improving"
        if ratio >= 100 and momentum < 100:
            return "weakening"
        return "lagging"

    @staticmethod
    def _trend_label(ratio: float, momentum: float) -> str:
        if ratio >= 100 and momentum >= 100:
            return "outperforming"
        if ratio < 100 and momentum >= 100:
            return "improving"
        if ratio >= 100 and momentum < 100:
            return "rolling-over"
        return "underperforming"

    async def get_macro_dashboard(self) -> dict:
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
        await redis.set("macro_dashboard", json.dumps(result), ex=300)
        return result

    async def _get_india_vix(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(
                    "https://www.nseindia.com/api/allIndices",
                    headers={"User-Agent": "Mozilla/5.0"},
                )
            data = response.json().get("data", [])
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
        redis = await get_redis()
        cached = await redis.get(f"iv_rank:{symbol}")
        if cached:
            return json.loads(cached)

        result = {
            "symbol": symbol,
            "current_iv": 0.0,
            "iv_rank": 0.0,
            "iv_percentile": 0.0,
            "iv_52w_high": 0.0,
            "iv_52w_low": 0.0,
        }
        await redis.set(f"iv_rank:{symbol}", json.dumps(result), ex=300)
        return result


sector_tracker = SectorRotationTracker()
