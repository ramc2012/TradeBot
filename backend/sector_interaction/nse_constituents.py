"""NSE/Nifty sector constituent sync and runtime taxonomy overlay."""
from __future__ import annotations

import csv
import io
import asyncio
import html
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import httpx

from core.runtime_state import load_runtime_state, save_runtime_state


NSE_CONSTITUENT_STATE_KEY = "sector_interaction_nse_constituents_v1"

NSE_CONSTITUENT_SOURCES: dict[str, dict[str, str]] = {
    "nifty_auto": {"label": "Nifty Auto", "url": "https://www.niftyindices.com/IndexConstituent/ind_niftyautolist.csv"},
    "nifty_bank": {"label": "Nifty Bank", "url": "https://www.niftyindices.com/IndexConstituent/ind_niftybanklist.csv"},
    "nifty_financial_services": {"label": "Nifty Financial Services", "url": "https://www.niftyindices.com/IndexConstituent/ind_niftyfinancelist.csv"},
    "nifty_financial_services_ex_bank": {
        "label": "Nifty Financial Services Ex-Bank",
        "url": "https://www.niftyindices.com/IndexConstituent/ind_niftyfinancialservicesexbank_list.csv",
        "page_url": "https://www.niftyindices.com/indices/equity/sectoral-indices/nifty-financial--services-ex-bank",
    },
    "nifty_fmcg": {"label": "Nifty FMCG", "url": "https://www.niftyindices.com/IndexConstituent/ind_niftyfmcglist.csv"},
    "nifty_healthcare": {"label": "Nifty Healthcare", "url": "https://www.niftyindices.com/IndexConstituent/ind_niftyhealthcarelist.csv"},
    "nifty_it": {"label": "Nifty IT", "url": "https://www.niftyindices.com/IndexConstituent/ind_niftyitlist.csv"},
    "nifty_media": {"label": "Nifty Media", "url": "https://www.niftyindices.com/IndexConstituent/ind_niftymedialist.csv"},
    "nifty_metal": {"label": "Nifty Metal", "url": "https://www.niftyindices.com/IndexConstituent/ind_niftymetallist.csv"},
    "nifty_oil_gas": {"label": "Nifty Oil & Gas", "url": "https://www.niftyindices.com/IndexConstituent/ind_niftyoilgaslist.csv"},
    "nifty_pharma": {"label": "Nifty Pharma", "url": "https://www.niftyindices.com/IndexConstituent/ind_niftypharmalist.csv"},
    "nifty_private_bank": {"label": "Nifty Private Bank", "url": "https://www.niftyindices.com/IndexConstituent/ind_nifty_privatebanklist.csv"},
    "nifty_psu_bank": {"label": "Nifty PSU Bank", "url": "https://www.niftyindices.com/IndexConstituent/ind_niftypsubanklist.csv"},
    "nifty_realty": {"label": "Nifty Realty", "url": "https://www.niftyindices.com/IndexConstituent/ind_niftyrealtylist.csv"},
    "nifty_consumer_durables": {"label": "Nifty Consumer Durables", "url": "https://www.niftyindices.com/IndexConstituent/ind_niftyconsumerdurableslist.csv"},
    "nifty_india_defence": {
        "label": "Nifty India Defence",
        "url": "https://www.niftyindices.com/IndexConstituent/ind_niftyindiadefence_list.csv",
        "page_url": "https://www.niftyindices.com/indices/equity/thematic-indices/nifty-india-defence",
    },
    "nifty_energy": {"label": "Nifty Energy", "url": "https://www.niftyindices.com/IndexConstituent/ind_niftyenergylist.csv"},
}

SECTOR_PRIORITY = (
    "nifty_private_bank",
    "nifty_psu_bank",
    "nifty_financial_services_ex_bank",
    "nifty_capital_markets",
    "nifty_consumer_durables",
    "nifty_healthcare",
    "nifty_pharma",
    "nifty_oil_gas",
    "nifty_energy",
    "nifty_auto",
    "nifty_fmcg",
    "nifty_it",
    "nifty_media",
    "nifty_metal",
    "nifty_realty",
    "nifty_india_defence",
    "nifty_financial_services",
    "nifty_bank",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class NSEConstituentService:
    def status(self) -> dict[str, Any]:
        payload, updated_at = load_runtime_state(NSE_CONSTITUENT_STATE_KEY)
        payload = payload if isinstance(payload, dict) else {}
        sectors = dict(payload.get("sectors") or {})
        return {
            "state_key": NSE_CONSTITUENT_STATE_KEY,
            "updated_at": updated_at.isoformat() if updated_at else payload.get("synced_at"),
            "source": "niftyindices_official_csv",
            "sector_count": len(sectors),
            "symbol_count": len(payload.get("symbol_to_sectors") or {}),
            "successful_sources": payload.get("successful_sources") or [],
            "failed_sources": payload.get("failed_sources") or [],
            "sectors": [
                {
                    "sector_key": key,
                    "label": row.get("label"),
                    "constituents": len(row.get("symbols") or []),
                    "source_url": row.get("source_url"),
                }
                for key, row in sorted(sectors.items())
            ],
            "runtime_overlay_active": bool(sectors),
        }

    def sector_for_symbol(self, symbol: str) -> str | None:
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            return None
        payload, _ = load_runtime_state(NSE_CONSTITUENT_STATE_KEY)
        if not isinstance(payload, dict):
            return None
        sectors = list((payload.get("symbol_to_sectors") or {}).get(normalized) or [])
        if not sectors:
            return None
        for sector in SECTOR_PRIORITY:
            if sector in sectors:
                return sector
        return str(sectors[0])

    async def sync(self, *, timeout_seconds: float = 8.0) -> dict[str, Any]:
        sectors: dict[str, dict[str, Any]] = {}
        symbol_to_sectors: dict[str, list[str]] = {}
        successful_sources: list[dict[str, Any]] = []
        failed_sources: list[dict[str, Any]] = []
        headers = {
            "User-Agent": "Mozilla/5.0 sector-interaction-sync/1.0",
            "Accept": "text/csv,*/*",
            "Referer": "https://www.niftyindices.com/",
        }

        async def fetch_one(client: httpx.AsyncClient, sector_key: str, source: dict[str, str]) -> dict[str, Any]:
            errors: list[str] = []

            async def fetch_csv(csv_url: str) -> dict[str, Any] | None:
                try:
                    response = await client.get(csv_url)
                    if response.status_code != 200:
                        raise RuntimeError(f"HTTP {response.status_code}")
                    symbols = self._parse_symbols(response.text)
                    if not symbols:
                        raise RuntimeError("no symbols parsed")
                    return {
                        "ok": True,
                        "sector_key": sector_key,
                        "label": source["label"],
                        "source_url": csv_url,
                        "symbols": symbols,
                    }
                except Exception as exc:
                    errors.append(f"{csv_url}: {exc}")
                    return None

            try:
                csv_result = await fetch_csv(source["url"])
                if csv_result is not None:
                    return csv_result
                page_url = source.get("page_url")
                if page_url:
                    page_response = await client.get(page_url)
                    if page_response.status_code != 200:
                        raise RuntimeError(f"{page_url}: HTTP {page_response.status_code}")
                    for candidate_url in self._extract_constituent_urls(page_response.text, page_url):
                        csv_result = await fetch_csv(candidate_url)
                        if csv_result is not None:
                            return csv_result
                raise RuntimeError("; ".join(errors[-3:]) or "no symbols parsed")
            except Exception as exc:
                return {
                    "ok": False,
                    "sector_key": sector_key,
                    "label": source["label"],
                    "source_url": source["url"],
                    "error": str(exc),
                }

        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True, headers=headers) as client:
            results = await asyncio.gather(
                *(
                    fetch_one(client, sector_key, source)
                    for sector_key, source in NSE_CONSTITUENT_SOURCES.items()
                )
            )
            for result in results:
                sector_key = str(result["sector_key"])
                if result.get("ok"):
                    symbols = list(result.get("symbols") or [])
                    sectors[sector_key] = {
                        "label": result["label"],
                        "symbols": symbols,
                        "source_url": result["source_url"],
                    }
                    for symbol in symbols:
                        symbol_to_sectors.setdefault(symbol, [])
                        if sector_key not in symbol_to_sectors[symbol]:
                            symbol_to_sectors[symbol].append(sector_key)
                    successful_sources.append(
                        {
                            "sector_key": sector_key,
                            "label": result["label"],
                            "symbols": len(symbols),
                        }
                    )
                else:
                    failed_sources.append(
                        {
                            "sector_key": sector_key,
                            "label": result["label"],
                            "source_url": result["source_url"],
                            "error": result.get("error") or "unknown error",
                        }
                    )
        if not sectors:
            return {
                "stored": False,
                "synced_at": _now_iso(),
                "successful_sources": successful_sources,
                "failed_sources": failed_sources,
                "message": "No official NSE constituent sources were loaded; existing runtime state was preserved.",
            }
        payload = {
            "synced_at": _now_iso(),
            "source": "niftyindices_official_csv",
            "sectors": sectors,
            "symbol_to_sectors": symbol_to_sectors,
            "successful_sources": successful_sources,
            "failed_sources": failed_sources,
        }
        updated_at = save_runtime_state(NSE_CONSTITUENT_STATE_KEY, payload)
        return {
            "stored": updated_at is not None,
            "updated_at": updated_at.isoformat() if updated_at else None,
            **payload,
        }

    def _parse_symbols(self, text: str) -> list[str]:
        if "Symbol" not in text or "Company Name" not in text:
            return []
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
        symbols = []
        for row in reader:
            symbol = str(row.get("Symbol") or "").strip().upper()
            if symbol and symbol not in symbols:
                symbols.append(symbol)
        return symbols

    def _extract_constituent_urls(self, page_text: str, page_url: str) -> list[str]:
        candidates = re.findall(
            r"""(?:href=)?["']?([^"'<>\s]*IndexConstituent/[^"'<>\s]+?\.csv)["']?""",
            page_text,
            flags=re.IGNORECASE,
        )
        urls: list[str] = []
        for raw in candidates:
            cleaned = html.unescape(str(raw or "").strip())
            if not cleaned:
                continue
            absolute = urljoin(page_url, cleaned)
            if absolute not in urls:
                urls.append(absolute)
        return urls


nse_constituent_service = NSEConstituentService()
