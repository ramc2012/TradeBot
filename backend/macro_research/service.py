"""Macro research service for sector-strength and budding-theme discovery."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode
from xml.etree import ElementTree

import httpx
from loguru import logger

from .catalog import (
    BUDDING_THEMES,
    COMMODITY_WATCHLIST,
    MACRO_INDICATORS,
    SECTOR_RESEARCH_CATALOG,
    SOURCE_REGISTRY,
)

UTC = timezone.utc


@dataclass
class CacheItem:
    expires_at: float
    value: Any


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def _slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_")


class MacroResearchService:
    """Public-source enriched research engine for sector strategy agents."""

    def __init__(self) -> None:
        self._cache: dict[str, CacheItem] = {}
        self._sector_map = {sector["code"]: sector for sector in SECTOR_RESEARCH_CATALOG}
        self._theme_map = {theme["code"]: theme for theme in BUDDING_THEMES}
        self._live_macro = os.getenv("MACRO_RESEARCH_LIVE_MACRO", "1") == "1"
        self._live_commodities = os.getenv("MACRO_RESEARCH_LIVE_COMMODITIES", "1") == "1"
        self._live_trends = os.getenv("MACRO_RESEARCH_LIVE_TRENDS", "0") == "1"

    async def overview(self, refresh: bool = False) -> dict[str, Any]:
        return await self._cached(
            "overview",
            ttl=900,
            refresh=refresh,
            builder=self._build_overview,
        )

    async def sector_map(self, refresh: bool = False) -> dict[str, Any]:
        return await self._cached(
            "sector_map",
            ttl=900,
            refresh=refresh,
            builder=self._build_sector_map,
        )

    async def sector_detail(self, sector_code: str, refresh: bool = False) -> dict[str, Any]:
        code = (sector_code or "").upper()
        if code not in self._sector_map:
            code = self._resolve_sector_code(code) or "AUTO"
        payload = await self.sector_map(refresh=refresh)
        sector = next((item for item in payload["sectors"] if item["code"] == code), None)
        if not sector:
            sector = self._sector_snapshot(self._sector_map[code], {}, [], [])
        return {
            "sector": sector,
            "research_matrix": self._sector_map[code]["research_points"],
            "drivers": self._sector_map[code]["drivers"],
            "draggers": self._sector_map[code]["draggers"],
            "leaders": self._sector_map[code]["leaders"],
            "agent_uses": self._sector_map[code]["agent_uses"],
            "source_queries": {
                "news_social_proxy": self._sector_map[code]["theme_queries"],
                "frontier_research": self._sector_map[code]["frontier_queries"],
            },
            "agent_prompt": self._build_agent_prompt(self._sector_map[code]),
            "timestamp": _now_iso(),
        }

    async def budding_sectors(self, refresh: bool = False) -> dict[str, Any]:
        return await self._cached(
            "budding",
            ttl=900,
            refresh=refresh,
            builder=self._build_budding_sectors,
        )

    async def search(self, query: str, sector_code: str | None = None, limit: int = 12, refresh: bool = False) -> dict[str, Any]:
        q = (query or "").strip()
        if not q:
            q = "sector strength"
        sector_filter = self._resolve_sector_code(sector_code or "") if sector_code else None
        sector_payload = await self.sector_map(refresh=refresh)
        budding_payload = await self.budding_sectors(refresh=refresh)

        corpus: list[dict[str, Any]] = []
        for sector in sector_payload["sectors"]:
            if sector_filter and sector["code"] != sector_filter:
                continue
            catalog = self._sector_map[sector["code"]]
            corpus.append(
                {
                    "scope": "sector",
                    "sector_code": sector["code"],
                    "title": catalog["label"],
                    "summary": " ".join(catalog["drivers"] + catalog["draggers"]),
                    "tags": [sector["code"], *catalog["agent_uses"], *catalog["theme_queries"]],
                    "payload": sector,
                }
            )
            for point in catalog["research_points"]:
                corpus.append(
                    {
                        "scope": "research_point",
                        "sector_code": sector["code"],
                        "title": f"{catalog['label']} - {point['metric']}",
                        "summary": f"{point['signal']} Cadence: {point['cadence']}.",
                        "tags": [sector["code"], point["metric"], point["cadence"]],
                        "payload": point,
                    }
                )

        for theme in budding_payload["themes"]:
            if sector_filter and theme["sector_code"] != sector_filter:
                continue
            corpus.append(
                {
                    "scope": "budding_theme",
                    "sector_code": theme["sector_code"],
                    "title": theme["label"],
                    "summary": theme["why_now"],
                    "tags": [theme["code"], theme["sector_code"], *theme["watchlist"]],
                    "payload": theme,
                }
            )

        tokens = self._tokens(q)
        results = []
        for doc in corpus:
            text = " ".join([doc["title"], doc["summary"], " ".join(map(str, doc["tags"]))])
            text_tokens = self._tokens(text)
            overlap = len(tokens.intersection(text_tokens))
            exact = 1 if q.lower() in text.lower() else 0
            tag_hits = sum(1 for tag in doc["tags"] if str(tag).lower() in q.lower())
            score = overlap * 12 + exact * 35 + tag_hits * 10
            if score <= 0 and not sector_filter:
                continue
            results.append(
                {
                    "score": round(score, 2),
                    "scope": doc["scope"],
                    "sector_code": doc["sector_code"],
                    "title": doc["title"],
                    "summary": doc["summary"],
                    "tags": doc["tags"][:10],
                    "payload": doc["payload"],
                }
            )
        results.sort(key=lambda item: item["score"], reverse=True)
        return {
            "query": q,
            "sector_filter": sector_filter,
            "results": results[: max(1, min(limit, 50))],
            "result_count": len(results),
            "timestamp": _now_iso(),
        }

    def source_map(self) -> dict[str, Any]:
        return {
            "sources": SOURCE_REGISTRY,
            "routing": [
                {"question": "Numeric macro or commodity value", "route": "public data connector with fallback snapshot"},
                {"question": "Sector drivers, risks, and playbooks", "route": "research catalog plus RAG-ready text"},
                {"question": "Budding sector discovery", "route": "news trend proxy + frontier research proxy + policy/adoption catalog"},
                {"question": "Hard trading rule", "route": "deterministic risk engine, not RAG prose"},
            ],
            "timestamp": _now_iso(),
        }

    async def _build_overview(self) -> dict[str, Any]:
        macro_task = asyncio.create_task(self._macro_indicators())
        commodity_task = asyncio.create_task(self._commodity_snapshot())
        sector_task = asyncio.create_task(self._build_sector_map())
        budding_task = asyncio.create_task(self._build_budding_sectors())
        macro, commodities, sectors, budding = await asyncio.gather(
            macro_task,
            commodity_task,
            sector_task,
            budding_task,
        )
        leaders = sorted(sectors["sectors"], key=lambda item: item["health_score"], reverse=True)[:5]
        risks = sorted(sectors["sectors"], key=lambda item: item["risk_score"], reverse=True)[:5]
        return {
            "macro_indicators": macro,
            "commodities": commodities,
            "sectors": sectors["sectors"],
            "sector_leaders": leaders,
            "sector_risks": risks,
            "budding_themes": budding["themes"][:6],
            "market_read": self._macro_read(macro, commodities, leaders, risks),
            "sources": [source["id"] for source in SOURCE_REGISTRY],
            "timestamp": _now_iso(),
        }

    async def _build_sector_map(self) -> dict[str, Any]:
        macro_task = asyncio.create_task(self._macro_indicators())
        commodity_task = asyncio.create_task(self._commodity_snapshot())
        trend_tasks = [
            asyncio.create_task(self._theme_signal(sector["theme_queries"], fallback_seed=sector["base_score"]))
            for sector in SECTOR_RESEARCH_CATALOG
        ]
        macro, commodities, *trend_signals = await asyncio.gather(macro_task, commodity_task, *trend_tasks)
        sectors = [
            self._sector_snapshot(sector, macro, commodities, trend_signals[index])
            for index, sector in enumerate(SECTOR_RESEARCH_CATALOG)
        ]
        sectors.sort(key=lambda item: item["health_score"], reverse=True)
        return {
            "sectors": sectors,
            "macro_indicators": macro,
            "commodities": commodities,
            "timestamp": _now_iso(),
        }

    async def _build_budding_sectors(self) -> dict[str, Any]:
        tasks = [
            asyncio.create_task(self._budding_theme_snapshot(theme))
            for theme in BUDDING_THEMES
        ]
        themes = await asyncio.gather(*tasks)
        themes = sorted(themes, key=lambda item: item["budding_score"], reverse=True)
        return {
            "themes": themes,
            "method": {
                "news_social_weight": 0.28,
                "frontier_research_weight": 0.26,
                "catalog_policy_adoption_weight": 0.30,
                "strategy_fit_weight": 0.16,
            },
            "timestamp": _now_iso(),
        }

    async def _macro_indicators(self) -> list[dict[str, Any]]:
        tasks = [asyncio.create_task(self._world_bank_indicator(indicator)) for indicator in MACRO_INDICATORS]
        return await asyncio.gather(*tasks)

    async def _world_bank_indicator(self, indicator: dict[str, Any]) -> dict[str, Any]:
        params = {
            "format": "json",
            "per_page": "8",
        }
        url = (
            f"https://api.worldbank.org/v2/country/{indicator['country']}"
            f"/indicator/{indicator['world_bank_code']}?{urlencode(params)}"
        )
        history: list[dict[str, Any]] = []
        source = "offline_seed"
        try:
            if not self._live_macro:
                raise RuntimeError("live macro connector disabled")
            # 2026-08-02: api.worldbank.org answers in ~7-8s from this network;
            # the old 1.8s timeout meant 5 of 6 indicators fell back to their
            # 2024 offline seeds on EVERY refresh. Indicators fetch
            # concurrently and sit behind the 900s overview cache, so a longer
            # timeout costs one slow refresh per 15 minutes, not per request.
            async with httpx.AsyncClient(timeout=12.0) as client:
                response = await client.get(url)
            if response.status_code == 200:
                payload = response.json()
                rows = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
                for row in rows:
                    value = row.get("value")
                    if value is None:
                        continue
                    history.append({"date": str(row.get("date")), "value": round(_safe_float(value), 3)})
                history.sort(key=lambda item: item["date"])
                if history:
                    source = "world_bank"
        except Exception as exc:
            logger.debug(f"[MacroResearch] World Bank fetch failed for {indicator['id']}: {exc}")

        if not history:
            latest_value = _safe_float(indicator["fallback_value"])
            latest_year = str(indicator["fallback_year"])
            history = [{"date": latest_year, "value": latest_value}]
        else:
            latest_value = _safe_float(history[-1]["value"])
            latest_year = str(history[-1]["date"])

        prior = _safe_float(history[-2]["value"], latest_value) if len(history) > 1 else latest_value
        change = latest_value - prior
        good_direction = indicator["good_direction"]
        signal = "tailwind" if (change >= 0 and good_direction == "higher") or (change <= 0 and good_direction == "lower") else "headwind"
        return {
            "id": indicator["id"],
            "label": indicator["label"],
            "country": indicator["country"],
            "latest_value": round(latest_value, 3),
            "latest_year": latest_year,
            "unit": indicator["unit"],
            "change": round(change, 3),
            "signal": signal,
            "good_direction": good_direction,
            "influences": indicator["influences"],
            "history": history[-6:],
            "source": source,
            "source_url": url,
        }

    async def _commodity_snapshot(self) -> list[dict[str, Any]]:
        tasks = [asyncio.create_task(self._commodity_quote(item)) for item in COMMODITY_WATCHLIST]
        return await asyncio.gather(*tasks)

    async def _commodity_quote(self, item: dict[str, Any]) -> dict[str, Any]:
        quote = {
            "price": _safe_float(item["fallback_price"]),
            "change_pct": _safe_float(item["fallback_change_pct"]),
            "as_of": _now_iso(),
            "source": "offline_seed",
        }
        symbol = item.get("stooq_symbol")
        if symbol and self._live_commodities:
            url = f"https://stooq.com/q/l/?{urlencode({'s': symbol, 'f': 'sd2t2ohlcv', 'h': '', 'e': 'csv'})}"
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    response = await client.get(url)
                if response.status_code == 200 and "N/D" not in response.text:
                    reader = csv.DictReader(io.StringIO(response.text))
                    row = next(reader, None)
                    if row:
                        close = _safe_float(row.get("Close"), quote["price"])
                        open_px = _safe_float(row.get("Open"), close)
                        change_pct = ((close / open_px) - 1.0) * 100 if open_px else 0.0
                        quote = {
                            "price": round(close, 3),
                            "change_pct": round(change_pct, 3),
                            "as_of": f"{row.get('Date', '')}T{row.get('Time', '')}".strip("T"),
                            "source": "stooq_delayed",
                        }
            except Exception as exc:
                logger.debug(f"[MacroResearch] Stooq fetch failed for {item['code']}: {exc}")

        pressure = "rising" if quote["change_pct"] > 0.25 else "falling" if quote["change_pct"] < -0.25 else "flat"
        return {
            "code": item["code"],
            "label": item["label"],
            "unit": item["unit"],
            "price": quote["price"],
            "change_pct": quote["change_pct"],
            "pressure": pressure,
            "beneficiaries": item["beneficiaries"],
            "hurt_by_rise": item["hurt_by_rise"],
            "why": item["why"],
            "source": quote["source"],
            "as_of": quote["as_of"],
        }

    async def _theme_signal(self, queries: list[str], fallback_seed: float = 55) -> list[dict[str, Any]]:
        tasks = [asyncio.create_task(self._gdelt_signal(query, fallback_seed + index * 3)) for index, query in enumerate(queries[:3])]
        return await asyncio.gather(*tasks)

    async def _gdelt_signal(self, query: str, fallback_seed: float) -> dict[str, Any]:
        params = {
            "query": query,
            "mode": "timelinevol",
            "format": "json",
            "timespan": "7d",
        }
        url = f"https://api.gdeltproject.org/api/v2/doc/doc?{urlencode(params)}"
        fallback_strength = _clamp(38 + (fallback_seed % 41))
        if not self._live_trends:
            return {
                "query": query,
                "strength": round(fallback_strength, 2),
                "momentum": round((fallback_strength - 50) / 10, 3),
                "source": "offline_seed",
                "source_url": url,
            }
        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                response = await client.get(url)
            if response.status_code == 200:
                payload = response.json()
                timeline = payload.get("timeline") or payload.get("data") or []
                values = []
                if isinstance(timeline, list):
                    for item in timeline:
                        if isinstance(item, dict):
                            values.append(_safe_float(item.get("value") or item.get("norm"), 0.0))
                if values:
                    latest = values[-1]
                    avg = sum(values) / len(values)
                    strength = _clamp(50 + ((latest - avg) * 3))
                    return {
                        "query": query,
                        "strength": round(strength, 2),
                        "momentum": round(latest - avg, 3),
                        "source": "gdelt_doc",
                        "source_url": url,
                    }
        except Exception as exc:
            logger.debug(f"[MacroResearch] GDELT fetch failed for {query}: {exc}")

        return {
            "query": query,
            "strength": round(fallback_strength, 2),
            "momentum": round((fallback_strength - 50) / 10, 3),
            "source": "offline_seed",
            "source_url": url,
        }

    async def _frontier_signal(self, queries: list[str], fallback_seed: float = 55) -> list[dict[str, Any]]:
        tasks = [asyncio.create_task(self._arxiv_signal(query, fallback_seed + index * 5)) for index, query in enumerate(queries[:3])]
        return await asyncio.gather(*tasks)

    async def _arxiv_signal(self, query: str, fallback_seed: float) -> dict[str, Any]:
        params = {
            "search_query": f"all:{query}",
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "start": "0",
            "max_results": "5",
        }
        url = f"https://export.arxiv.org/api/query?{urlencode(params)}"
        fallback_count = int(8 + fallback_seed % 18)
        if not self._live_trends:
            return {
                "query": query,
                "paper_count": fallback_count,
                "strength": round(_clamp(35 + math.log1p(fallback_count) * 7), 2),
                "examples": [],
                "source": "offline_seed",
                "source_url": url,
            }
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(url)
            if response.status_code == 200 and response.text:
                root = ElementTree.fromstring(response.text)
                ns = {"opensearch": "http://a9.com/-/spec/opensearch/1.1/"}
                total_node = root.find("opensearch:totalResults", ns)
                total = int(total_node.text.strip()) if total_node is not None and total_node.text else fallback_count
                entries = []
                for entry in root.findall("{http://www.w3.org/2005/Atom}entry")[:3]:
                    title = entry.find("{http://www.w3.org/2005/Atom}title")
                    entries.append(" ".join((title.text or "").split()) if title is not None else query)
                return {
                    "query": query,
                    "paper_count": total,
                    "strength": round(_clamp(35 + math.log1p(total) * 7), 2),
                    "examples": entries,
                    "source": "arxiv",
                    "source_url": url,
                }
        except Exception as exc:
            logger.debug(f"[MacroResearch] arXiv fetch failed for {query}: {exc}")

        return {
            "query": query,
            "paper_count": fallback_count,
            "strength": round(_clamp(35 + math.log1p(fallback_count) * 7), 2),
            "examples": [],
            "source": "offline_seed",
            "source_url": url,
        }

    async def _budding_theme_snapshot(self, theme: dict[str, Any]) -> dict[str, Any]:
        trend_task = asyncio.create_task(self._theme_signal(theme["queries"], fallback_seed=theme["base_score"]))
        frontier_task = asyncio.create_task(self._frontier_signal(theme["frontier_queries"], fallback_seed=theme["base_score"]))
        trend_signals, frontier_signals = await asyncio.gather(trend_task, frontier_task)
        news_score = self._avg([item["strength"] for item in trend_signals])
        frontier_score = self._avg([item["strength"] for item in frontier_signals])
        policy_adoption_score = _safe_float(theme["base_score"])
        strategy_fit = 70 if len(theme["watch"]) >= 5 else 62
        score = (
            news_score * 0.28
            + frontier_score * 0.26
            + policy_adoption_score * 0.30
            + strategy_fit * 0.16
        )
        stage = "emerging"
        if score >= 76:
            stage = "confirming"
        if score >= 86:
            stage = "crowded-watch"
        return {
            "code": theme["code"],
            "label": theme["label"],
            "sector_code": theme["sector_code"],
            "budding_score": round(score, 2),
            "stage": stage,
            "why_now": theme["why_now"],
            "watchlist": theme["watch"],
            "news_social_proxy_score": round(news_score, 2),
            "frontier_research_score": round(frontier_score, 2),
            "trend_signals": trend_signals,
            "frontier_signals": frontier_signals,
            "agent_action": self._theme_agent_action(score),
        }

    def _sector_snapshot(
        self,
        sector: dict[str, Any],
        macro: list[dict[str, Any]] | dict[str, Any],
        commodities: list[dict[str, Any]],
        trend_signals: list[dict[str, Any]],
    ) -> dict[str, Any]:
        macro_list = list(macro) if isinstance(macro, list) else []
        macro_tailwinds = sum(1 for item in macro_list if item.get("signal") == "tailwind")
        macro_headwinds = sum(1 for item in macro_list if item.get("signal") == "headwind")
        trend_score = self._avg([item.get("strength", 50) for item in trend_signals]) if trend_signals else sector["base_score"]

        commodity_drag = 0.0
        commodity_tailwind = 0.0
        commodity_notes = []
        for commodity in commodities or []:
            affected = sector["code"] in commodity.get("hurt_by_rise", [])
            benefited = sector["code"] in commodity.get("beneficiaries", [])
            change_pct = abs(_safe_float(commodity.get("change_pct")))
            if affected and commodity.get("pressure") == "rising":
                commodity_drag += min(change_pct * 2.0, 8.0)
                commodity_notes.append(f"{commodity['label']} rising pressures margins")
            if affected and commodity.get("pressure") == "falling":
                commodity_tailwind += min(change_pct * 1.5, 6.0)
                commodity_notes.append(f"{commodity['label']} easing helps costs")
            if benefited and commodity.get("pressure") == "rising":
                commodity_tailwind += min(change_pct * 1.5, 6.0)
                commodity_notes.append(f"{commodity['label']} rising supports realization")

        macro_score = (macro_tailwinds - macro_headwinds) * 1.2
        health_score = _clamp(sector["base_score"] * 0.55 + trend_score * 0.32 + 10 + macro_score + commodity_tailwind - commodity_drag)
        risk_score = _clamp(38 + len(sector["draggers"]) * 2.5 + commodity_drag + max(0, macro_headwinds - macro_tailwinds) * 2.0)
        stage = "accumulating"
        if health_score >= 72:
            stage = "leading"
        elif health_score < 48:
            stage = "lagging"
        elif risk_score > 66:
            stage = "fragile"
        return {
            "code": sector["code"],
            "label": sector["label"],
            "health_score": round(health_score, 2),
            "risk_score": round(risk_score, 2),
            "stage": stage,
            "trend_score": round(trend_score, 2),
            "macro_tailwinds": macro_tailwinds,
            "macro_headwinds": macro_headwinds,
            "drivers": sector["drivers"][:4],
            "draggers": sector["draggers"][:4],
            "research_points": sector["research_points"],
            "leaders": sector["leaders"],
            "agent_uses": sector["agent_uses"],
            "commodity_notes": commodity_notes[:4],
            "trend_signals": trend_signals,
        }

    def _macro_read(
        self,
        macro: list[dict[str, Any]],
        commodities: list[dict[str, Any]],
        leaders: list[dict[str, Any]],
        risks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        tailwinds = [item for item in macro if item["signal"] == "tailwind"]
        headwinds = [item for item in macro if item["signal"] == "headwind"]
        rising_costs = [item for item in commodities if item["pressure"] == "rising"]
        return {
            "headline": self._headline(tailwinds, headwinds, rising_costs),
            "tailwind_count": len(tailwinds),
            "headwind_count": len(headwinds),
            "cost_pressure_count": len(rising_costs),
            "leading_sectors": [item["code"] for item in leaders],
            "risk_sectors": [item["code"] for item in risks],
            "agent_instruction": "Use this payload as a context gate: confirm numeric signals with sector strength, then check draggers before increasing size.",
        }

    @staticmethod
    def _headline(tailwinds: list[dict[str, Any]], headwinds: list[dict[str, Any]], rising_costs: list[dict[str, Any]]) -> str:
        if len(tailwinds) >= len(headwinds) + 2 and len(rising_costs) <= 1:
            return "Macro context is constructive for domestic cyclicals and consumption, with manageable cost pressure."
        if len(headwinds) > len(tailwinds):
            return "Macro context is mixed to defensive; use stricter confirmation on rate-sensitive and commodity-sensitive sectors."
        if len(rising_costs) >= 3:
            return "Cost pressure is elevated; prefer sectors with pricing power or commodity pass-through."
        return "Macro context is balanced; let sector trend and earnings drivers decide allocation."

    @staticmethod
    def _theme_agent_action(score: float) -> str:
        if score >= 80:
            return "Promote to active sector watchlist and require confirmation from price breadth and options positioning."
        if score >= 68:
            return "Keep in discovery watchlist; retrieve cases and news catalysts before trade selection."
        return "Monitor only; insufficient evidence for active strategy bias."

    @staticmethod
    def _build_agent_prompt(sector: dict[str, Any]) -> str:
        return (
            f"Evaluate {sector['label']} using these drivers: {', '.join(sector['drivers'][:3])}. "
            f"Reject weak signals when these draggers dominate: {', '.join(sector['draggers'][:3])}. "
            "Return sector_strength, key_evidence, invalidation, and whether the strategy signal should be upweighted, unchanged, or blocked."
        )

    async def _cached(self, key: str, ttl: int, refresh: bool, builder) -> Any:
        now = time.time()
        if not refresh:
            cached = self._cache.get(key)
            if cached and cached.expires_at > now:
                return cached.value
        value = await builder()
        self._cache[key] = CacheItem(expires_at=now + ttl, value=value)
        return value

    def _resolve_sector_code(self, raw: str) -> str | None:
        if not raw:
            return None
        normalized = raw.upper().replace(" ", "_").replace("-", "_")
        if normalized in self._sector_map:
            return normalized
        for code, sector in self._sector_map.items():
            if normalized in code or normalized in _slug(sector["label"]).upper():
                return code
        return None

    @staticmethod
    def _tokens(value: str) -> set[str]:
        return {
            token
            for token in _slug(value).split("_")
            if len(token) > 2
        }

    @staticmethod
    def _avg(values: list[Any], default: float = 50.0) -> float:
        nums = [_safe_float(value, default) for value in values if value is not None]
        return sum(nums) / len(nums) if nums else default


macro_research_service = MacroResearchService()
