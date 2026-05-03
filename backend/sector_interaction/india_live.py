"""Live India sector taxonomy and F&O/ATM watchlist aggregation."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import text

from db.database import AsyncSessionLocal
from sector_interaction.nse_constituents import nse_constituent_service


NSE_SECTOR_INDICES: dict[str, dict[str, str]] = {
    "nifty_auto": {"label": "Nifty Auto", "category": "Automobile"},
    "nifty_bank": {"label": "Nifty Bank", "category": "Banking"},
    "nifty_financial_services": {"label": "Nifty Financial Services", "category": "Financial Services"},
    "nifty_financial_services_ex_bank": {"label": "Nifty Financial Services Ex-Bank", "category": "Financial Services"},
    "nifty_fmcg": {"label": "Nifty FMCG", "category": "FMCG"},
    "nifty_healthcare": {"label": "Nifty Healthcare", "category": "Healthcare"},
    "nifty_it": {"label": "Nifty IT", "category": "Information Technology"},
    "nifty_media": {"label": "Nifty Media", "category": "Media"},
    "nifty_metal": {"label": "Nifty Metal", "category": "Metals"},
    "nifty_oil_gas": {"label": "Nifty Oil & Gas", "category": "Oil & Gas"},
    "nifty_pharma": {"label": "Nifty Pharma", "category": "Pharmaceuticals"},
    "nifty_private_bank": {"label": "Nifty Private Bank", "category": "Banking"},
    "nifty_psu_bank": {"label": "Nifty PSU Bank", "category": "Banking"},
    "nifty_realty": {"label": "Nifty Realty", "category": "Real Estate"},
    "nifty_consumer_durables": {"label": "Nifty Consumer Durables", "category": "Consumer Durables"},
    "nifty_capital_markets": {"label": "Nifty Capital Markets", "category": "Capital Markets"},
    "nifty_india_defence": {"label": "Nifty India Defence", "category": "Defence"},
    "nifty_energy": {"label": "Nifty Energy", "category": "Energy"},
}


AGGREGATE_SECTOR_MEMBERS: dict[str, tuple[str, ...]] = {
    "nifty_bank": ("nifty_private_bank", "nifty_psu_bank"),
    "nifty_financial_services_ex_bank": ("nifty_financial_services", "nifty_capital_markets"),
    "nifty_healthcare": ("nifty_healthcare", "nifty_pharma"),
}


SECTOR_LABELS: dict[str, str] = {
    **{key: value["label"] for key, value in NSE_SECTOR_INDICES.items()},
    "nifty_industrials": "Industrials",
    "nifty_chemicals": "Chemicals",
    "nifty_consumer_services": "Consumer Services",
    "nifty_power": "Power",
    "nifty_telecom": "Telecom",
    "nifty_logistics": "Logistics",
    "nifty_cement": "Cement",
    "nifty_textiles": "Textiles",
    "nifty_other": "Other / Unmapped",
}


SYMBOL_SECTOR_OVERRIDES: dict[str, str] = {
    # Indices
    "NIFTY": "nifty_other",
    "BANKNIFTY": "nifty_bank",
    "FINNIFTY": "nifty_financial_services",
    "MIDCPNIFTY": "nifty_other",
    "SENSEX": "nifty_other",
    "BANKEX": "nifty_bank",
    # Auto
    "ASHOKLEY": "nifty_auto",
    "BAJAJ-AUTO": "nifty_auto",
    "BHARATFORG": "nifty_auto",
    "BOSCHLTD": "nifty_auto",
    "EICHERMOT": "nifty_auto",
    "EXIDEIND": "nifty_auto",
    "FORCEMOT": "nifty_auto",
    "HEROMOTOCO": "nifty_auto",
    "HYUNDAI": "nifty_auto",
    "M&M": "nifty_auto",
    "MARUTI": "nifty_auto",
    "MOTHERSON": "nifty_auto",
    "SONACOMS": "nifty_auto",
    "TATATECH": "nifty_auto",
    "TIINDIA": "nifty_auto",
    "TMPV": "nifty_auto",
    "TVSMOTOR": "nifty_auto",
    "UNOMINDA": "nifty_auto",
    # Banks and financials
    "360ONE": "nifty_financial_services",
    "ABCAPITAL": "nifty_financial_services",
    "ANGELONE": "nifty_capital_markets",
    "AUBANK": "nifty_private_bank",
    "AXISBANK": "nifty_private_bank",
    "BAJAJFINSV": "nifty_financial_services",
    "BAJAJHLDNG": "nifty_financial_services",
    "BAJFINANCE": "nifty_financial_services",
    "BANDHANBNK": "nifty_private_bank",
    "BANKBARODA": "nifty_psu_bank",
    "BANKINDIA": "nifty_psu_bank",
    "BSE": "nifty_capital_markets",
    "CAMS": "nifty_capital_markets",
    "CANBK": "nifty_psu_bank",
    "CDSL": "nifty_capital_markets",
    "CHOLAFIN": "nifty_financial_services",
    "FEDERALBNK": "nifty_private_bank",
    "HDFCAMC": "nifty_financial_services",
    "HDFCBANK": "nifty_private_bank",
    "HDFCLIFE": "nifty_financial_services",
    "HUDCO": "nifty_financial_services",
    "ICICIBANK": "nifty_private_bank",
    "ICICIGI": "nifty_financial_services",
    "ICICIPRULI": "nifty_financial_services",
    "IDFCFIRSTB": "nifty_private_bank",
    "IEX": "nifty_capital_markets",
    "INDIANB": "nifty_psu_bank",
    "INDUSINDBK": "nifty_private_bank",
    "IRFC": "nifty_financial_services",
    "JIOFIN": "nifty_financial_services",
    "KFINTECH": "nifty_capital_markets",
    "KOTAKBANK": "nifty_private_bank",
    "LICHSGFIN": "nifty_financial_services",
    "LICI": "nifty_financial_services",
    "LTF": "nifty_financial_services",
    "MANAPPURAM": "nifty_financial_services",
    "MCX": "nifty_capital_markets",
    "MFSL": "nifty_financial_services",
    "MOTILALOFS": "nifty_capital_markets",
    "MUTHOOTFIN": "nifty_financial_services",
    "NAM-INDIA": "nifty_financial_services",
    "NUVAMA": "nifty_capital_markets",
    "PAYTM": "nifty_financial_services",
    "PFC": "nifty_financial_services",
    "PNB": "nifty_psu_bank",
    "PNBHOUSING": "nifty_financial_services",
    "POLICYBZR": "nifty_financial_services",
    "RBLBANK": "nifty_private_bank",
    "RECLTD": "nifty_financial_services",
    "SAMMAANCAP": "nifty_financial_services",
    "SBICARD": "nifty_financial_services",
    "SBILIFE": "nifty_financial_services",
    "SBIN": "nifty_psu_bank",
    "SHRIRAMFIN": "nifty_financial_services",
    "UNIONBANK": "nifty_psu_bank",
    "YESBANK": "nifty_private_bank",
    # IT
    "COFORGE": "nifty_it",
    "HCLTECH": "nifty_it",
    "INFY": "nifty_it",
    "KPITTECH": "nifty_it",
    "LTM": "nifty_it",
    "MPHASIS": "nifty_it",
    "OFSS": "nifty_it",
    "PERSISTENT": "nifty_it",
    "TATAELXSI": "nifty_it",
    "TCS": "nifty_it",
    "TECHM": "nifty_it",
    "WIPRO": "nifty_it",
    # Healthcare and pharma
    "ALKEM": "nifty_pharma",
    "APOLLOHOSP": "nifty_healthcare",
    "AUROPHARMA": "nifty_pharma",
    "BIOCON": "nifty_pharma",
    "CIPLA": "nifty_pharma",
    "DIVISLAB": "nifty_pharma",
    "DRREDDY": "nifty_pharma",
    "FORTIS": "nifty_healthcare",
    "GLENMARK": "nifty_pharma",
    "LAURUSLABS": "nifty_pharma",
    "LUPIN": "nifty_pharma",
    "MANKIND": "nifty_pharma",
    "MAXHEALTH": "nifty_healthcare",
    "PPLPHARMA": "nifty_pharma",
    "SUNPHARMA": "nifty_pharma",
    "TORNTPHARM": "nifty_pharma",
    "ZYDUSLIFE": "nifty_pharma",
    # FMCG, consumer and durables
    "AMBER": "nifty_consumer_durables",
    "ASIANPAINT": "nifty_consumer_durables",
    "BLUESTARCO": "nifty_consumer_durables",
    "BRITANNIA": "nifty_fmcg",
    "COLPAL": "nifty_fmcg",
    "CROMPTON": "nifty_consumer_durables",
    "DABUR": "nifty_fmcg",
    "DIXON": "nifty_consumer_durables",
    "DMART": "nifty_consumer_services",
    "GODFRYPHLP": "nifty_fmcg",
    "GODREJCP": "nifty_fmcg",
    "HAVELLS": "nifty_consumer_durables",
    "HINDUNILVR": "nifty_fmcg",
    "ITC": "nifty_fmcg",
    "JUBLFOOD": "nifty_consumer_services",
    "KALYANKJIL": "nifty_consumer_durables",
    "MARICO": "nifty_fmcg",
    "NESTLEIND": "nifty_fmcg",
    "NYKAA": "nifty_consumer_services",
    "PAGEIND": "nifty_textiles",
    "PATANJALI": "nifty_fmcg",
    "PIDILITIND": "nifty_consumer_durables",
    "SWIGGY": "nifty_consumer_services",
    "TATACONSUM": "nifty_fmcg",
    "TITAN": "nifty_consumer_durables",
    "TRENT": "nifty_consumer_services",
    "UNITDSPR": "nifty_fmcg",
    "VBL": "nifty_fmcg",
    "VMM": "nifty_consumer_services",
    "VOLTAS": "nifty_consumer_durables",
    # Metals, materials and chemicals
    "APLAPOLLO": "nifty_metal",
    "AMBUJACEM": "nifty_cement",
    "ASTRAL": "nifty_chemicals",
    "DALBHARAT": "nifty_cement",
    "GRASIM": "nifty_cement",
    "HINDALCO": "nifty_metal",
    "HINDZINC": "nifty_metal",
    "JINDALSTEL": "nifty_metal",
    "JSWSTEEL": "nifty_metal",
    "NATIONALUM": "nifty_metal",
    "NMDC": "nifty_metal",
    "PIIND": "nifty_chemicals",
    "SAIL": "nifty_metal",
    "SHREECEM": "nifty_cement",
    "SRF": "nifty_chemicals",
    "SUPREMEIND": "nifty_chemicals",
    "TATASTEEL": "nifty_metal",
    "ULTRACEMCO": "nifty_cement",
    "UPL": "nifty_chemicals",
    "VEDL": "nifty_metal",
    # Oil, gas, energy and power
    "ADANIENSOL": "nifty_power",
    "ADANIGREEN": "nifty_energy",
    "ADANIPOWER": "nifty_power",
    "BPCL": "nifty_oil_gas",
    "COALINDIA": "nifty_energy",
    "GAIL": "nifty_oil_gas",
    "HINDPETRO": "nifty_oil_gas",
    "IOC": "nifty_oil_gas",
    "IREDA": "nifty_energy",
    "JSWENERGY": "nifty_power",
    "NHPC": "nifty_power",
    "NTPC": "nifty_power",
    "OIL": "nifty_oil_gas",
    "ONGC": "nifty_oil_gas",
    "PETRONET": "nifty_oil_gas",
    "POWERGRID": "nifty_power",
    "RELIANCE": "nifty_oil_gas",
    "SUZLON": "nifty_energy",
    "TATAPOWER": "nifty_power",
    "TORNTPOWER": "nifty_power",
    "WAAREEENER": "nifty_energy",
    # Industrials, defence, infra, logistics
    "ABB": "nifty_industrials",
    "ADANIENT": "nifty_industrials",
    "ADANIPORTS": "nifty_logistics",
    "BDL": "nifty_india_defence",
    "BEL": "nifty_india_defence",
    "BHEL": "nifty_industrials",
    "CGPOWER": "nifty_industrials",
    "COCHINSHIP": "nifty_india_defence",
    "CONCOR": "nifty_logistics",
    "CUMMINSIND": "nifty_industrials",
    "DELHIVERY": "nifty_logistics",
    "GMR AIRPORT": "nifty_logistics",
    "GMRAIRPORT": "nifty_logistics",
    "HAL": "nifty_india_defence",
    "INDIGO": "nifty_logistics",
    "INOXWIND": "nifty_energy",
    "KAYNES": "nifty_industrials",
    "KEI": "nifty_industrials",
    "LT": "nifty_industrials",
    "MAZDOCK": "nifty_india_defence",
    "NBCC": "nifty_industrials",
    "PGEL": "nifty_industrials",
    "POLYCAB": "nifty_industrials",
    "POWERINDIA": "nifty_industrials",
    "PREMIERENE": "nifty_energy",
    "RVNL": "nifty_industrials",
    "SIEMENS": "nifty_industrials",
    "SOLARINDS": "nifty_india_defence",
    # Realty
    "DLF": "nifty_realty",
    "GODREJPROP": "nifty_realty",
    "LODHA": "nifty_realty",
    "OBEROIRLTY": "nifty_realty",
    "PHOENIXLTD": "nifty_realty",
    "PRESTIGE": "nifty_realty",
    # Telecom/media
    "BHARTIARTL": "nifty_telecom",
    "IDEA": "nifty_telecom",
    "INDUSTOWER": "nifty_telecom",
    "NAUKRI": "nifty_consumer_services",
    "ETERNAL": "nifty_consumer_services",
    "INDHOTEL": "nifty_consumer_services",
}


def sector_for_symbol(symbol: str) -> str:
    normalized = str(symbol or "").strip().upper()
    official_sector = nse_constituent_service.sector_for_symbol(normalized)
    if official_sector:
        return official_sector
    return SYMBOL_SECTOR_OVERRIDES.get(normalized, "nifty_other")


def sector_label(sector_key: str) -> str:
    return SECTOR_LABELS.get(sector_key, sector_key.replace("_", " ").title())


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _signed_log_signal(value: float, scale: float = 100.0, cap: float = 5.0) -> float:
    scaled = abs(value) / max(scale, 1.0)
    signal = min(math.log1p(scaled), cap)
    return signal if value >= 0 else -signal


def _iv_state(avg_iv: float) -> str:
    threshold = 45.0 if avg_iv > 3.0 else 0.45
    return "elevated" if avg_iv >= threshold else "normal"


class IndiaLiveSectorService:
    async def overview(self) -> dict[str, Any]:
        rows = await self._load_live_rows()
        grouped = self._group_rows(rows, include_aggregates=True)
        sectors = [self._sector_summary(key, items) for key, items in grouped.items()]
        sectors.sort(key=lambda row: row["leadership_score"], reverse=True)
        for index, row in enumerate(sectors, start=1):
            row["rank"] = index
        return {
            "country": "IN",
            "default_country": "IN",
            "source_mode": "live_fno_atm_watchlist",
            "as_of": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "nse_constituent_status": nse_constituent_service.status(),
            "nse_sector_indices": [
                {"key": key, **value}
                for key, value in NSE_SECTOR_INDICES.items()
            ],
            "universe": {
                "symbols": len({row["symbol"] for row in rows}),
                "stocks": len({row["symbol"] for row in rows if row["kind"] == "STOCK"}),
                "indices": len({row["symbol"] for row in rows if row["kind"] == "INDEX"}),
                "mapped": len({row["symbol"] for row in rows if row["kind"] == "STOCK" and sector_for_symbol(row["symbol"]) != "nifty_other"}),
                "unmapped": sorted({row["symbol"] for row in rows if sector_for_symbol(row["symbol"]) == "nifty_other" and row["kind"] == "STOCK"}),
            },
            "sectors": sectors,
            "rrg": self._rrg_payload(sectors),
            "notes": [
                "No synthetic sector values are used in this live overview.",
                "Leadership score uses latest ATM watchlist change, OI change, volume breadth, IV, and constituent breadth.",
                "Sector-interaction VAR edges require a real sector-index history feed; until connected, edges are reported as unavailable rather than simulated.",
            ],
        }

    async def sector_detail(self, sector_key: str) -> dict[str, Any]:
        normalized = str(sector_key or "").strip().lower().replace("-", "_")
        rows = await self._load_live_rows()
        grouped = self._group_rows(rows, include_aggregates=True)
        items = grouped.get(normalized, [])
        summaries = [self._sector_summary(key, value) for key, value in grouped.items()]
        summaries.sort(key=lambda row: row["leadership_score"], reverse=True)
        rank_by_sector = {row["sector_key"]: index + 1 for index, row in enumerate(summaries)}
        constituents = sorted(
            items,
            key=lambda row: row["leadership_score"],
            reverse=True,
        )
        return {
            "country": "IN",
            "sector_key": normalized,
            "sector": sector_label(normalized),
            "source_mode": "live_fno_atm_watchlist",
            "rank": rank_by_sector.get(normalized),
            "sector_count": len(summaries),
            "summary": self._sector_summary(normalized, items) if items else None,
            "constituents": constituents,
            "parameters": self._sector_parameters(items),
            "performance_cycle": self._performance_cycle(self._sector_summary(normalized, items) if items else None),
            "alt_data": self._sector_alt_data(normalized, items),
            "relative_position": [
                {
                    "sector_key": row["sector_key"],
                    "sector": row["sector"],
                    "rank": rank_by_sector[row["sector_key"]],
                    "leadership_score": row["leadership_score"],
                    "quadrant": row["rrg_quadrant"],
                }
                for row in summaries
            ],
        }

    async def market_intelligence_payload(self) -> dict[str, Any]:
        overview = await self.overview()
        try:
            from sector_interaction.real_history import real_sector_history_service

            real_model = await real_sector_history_service.india_model(periods=160, max_lag=2, alpha=0.05)
            real_model_summary = {
                "source_mode": real_model.get("source_mode"),
                "periods": real_model.get("periods"),
                "sector_count": len(real_model.get("sectors") or []),
                "edge_count": len((real_model.get("network") or {}).get("edges") or []),
                "top_edges": ((real_model.get("network") or {}).get("edges") or [])[:5],
                "top_leaders": ((real_model.get("rankings") or {}).get("leaders") or [])[:5],
                "real_data_contract": real_model.get("real_data_contract"),
            }
        except Exception as exc:
            real_model_summary = {
                "source_mode": "error",
                "error": str(exc),
            }
        return {
            "module": "sector_interaction",
            "source_mode": overview["source_mode"],
            "universe": overview["universe"],
            "nse_constituent_status": overview.get("nse_constituent_status"),
            "top_sectors": overview["sectors"][:5],
            "lagging_sectors": list(reversed(overview["sectors"][-5:])),
            "rrg": overview["rrg"],
            "real_model": real_model_summary,
        }

    async def signals_payload(self) -> dict[str, Any]:
        overview = await self.overview()
        sectors = list(overview.get("sectors") or [])
        metric_specs = [
            ("india_live_leadership_score", "Live sector leadership score", "Live market structure", "daily", "leadership_score", 1.0),
            ("india_live_price_momentum", "Live sector price momentum", "Live market structure", "daily", "avg_change_pct", 0.48),
            ("india_live_option_oi_momentum", "Live option OI momentum", "Live options flow", "daily", "avg_oi_change_pct", 0.32),
            ("india_live_iv_pressure", "Live IV pressure", "Live options flow", "daily", "avg_iv", -0.2),
        ]
        zscores = {
            metric_key: self._metric_zscores(sectors, metric_key)
            for _, _, _, _, metric_key, _ in metric_specs
        }
        count = max(1, len(sectors))
        lower_cutoff = max(1, count - max(1, count // 3) + 1)
        rankings = []
        for index, sector in enumerate(sectors, start=1):
            drivers = []
            score = _float(sector.get("leadership_score"))
            for code, label, category, _cadence, metric_key, weight in metric_specs:
                latest_z = zscores[metric_key].get(str(sector.get("sector")), 0.0)
                drivers.append(
                    {
                        "indicator": label,
                        "category": category,
                        "contribution": round(latest_z * weight, 4),
                        "latest_z": round(latest_z, 4),
                    }
                )
            drivers.sort(key=lambda row: abs(_float(row["contribution"])), reverse=True)
            stance = "overweight" if index <= max(1, count // 3) else "underweight" if index >= lower_cutoff else "neutral"
            rankings.append(
                {
                    "sector": sector.get("sector"),
                    "score": round(score, 4),
                    "rank": index,
                    "change": round(_float(sector.get("momentum")), 4),
                    "stance": stance,
                    "top_drivers": drivers,
                }
            )
        indicator_latest = []
        for code, label, category, cadence, metric_key, weight in metric_specs:
            values = list(zscores[metric_key].values())
            latest_z = max(values, key=lambda value: abs(value)) if values else 0.0
            mapped = {
                str(sector.get("sector")): weight
                for sector in sectors
            }
            indicator_latest.append(
                {
                    "code": code,
                    "label": label,
                    "category": category,
                    "latest_z": round(latest_z, 4),
                    "quality_score": 0.82 if code == "india_live_leadership_score" else 0.76,
                    "source_status": "internal_live_market_data",
                    "cadence": cadence,
                    "signal_state": "positive" if latest_z > 0.25 else "negative" if latest_z < -0.25 else "neutral",
                    "mapped_sectors": mapped,
                }
            )
        try:
            from sector_interaction.ingestion import sector_ingestion_store

            observed_dates = len(
                {
                    str(row.get("date"))
                    for row in sector_ingestion_store.load_observations("IN", limit=20_000)
                    if row.get("date") and not self._is_deprecated_runtime_stub(row)
                }
            )
        except Exception:
            observed_dates = 0
        return {
            "country": "IN",
            "label": "India",
            "as_of": str(overview.get("as_of") or "")[:10],
            "source_mode": "live_fno_atm_watchlist",
            "runtime_handoff": {
                "active": True,
                "observed_dates": max(1, observed_dates),
                "required_dates": 1,
                "indicator_count": len(indicator_latest),
                "source": "live India F&O/ATM sector snapshot",
                "reason": "India route uses live market-derived sector leadership instead of synthetic fallback.",
            },
            "rankings": rankings,
            "indicator_latest": indicator_latest,
            "alerts": [
                {
                    "sector": row["sector"],
                    "severity": "high" if abs(_float(row["score"])) >= 10 else "medium",
                    "message": f"{row['sector']} live {row['stance']} signal at {row['score']:+.2f}",
                }
                for row in rankings
                if row["stance"] != "neutral"
            ][:8],
            "method": "India signal ranking uses live F&O/ATM watchlist leadership, price breadth, option OI, IV pressure, RSI and RRG state. No synthetic India signal values are used.",
        }

    async def extended_network_payload(self, *, periods: int = 160, max_lag: int = 2, alpha: float = 0.05) -> dict[str, Any]:
        overview = await self.overview()
        from sector_interaction.real_history import real_sector_history_service

        real_model = await real_sector_history_service.india_model(
            periods=periods,
            max_lag=max_lag,
            alpha=alpha,
            timeframe="daily",
        )
        sector_edges = list((real_model.get("network") or {}).get("edges") or [])
        sectors = list(overview.get("sectors") or [])
        sector_nodes = [
            {
                "id": row.get("sector"),
                "label": row.get("sector"),
                "node_type": "sector",
                "category": row.get("rrg_quadrant") or "sector_return",
            }
            for row in sectors
        ]
        indicator_specs = [
            ("india_live_leadership_score", "Live sector leadership score", "Live market structure", "leadership_score", 1.0),
            ("india_live_price_momentum", "Live sector price momentum", "Live market structure", "avg_change_pct", 0.48),
            ("india_live_option_oi_momentum", "Live option OI momentum", "Live options flow", "avg_oi_change_pct", 0.32),
            ("india_live_iv_pressure", "Live IV pressure", "Live options flow", "avg_iv", -0.2),
        ]
        indicator_nodes = [
            {
                "id": code,
                "label": label,
                "node_type": "indicator",
                "category": category,
                "quality_score": 0.82 if code == "india_live_leadership_score" else 0.76,
                "source_status": "internal_live_market_data",
            }
            for code, label, category, _metric_key, _weight in indicator_specs
        ]
        indicator_edges = []
        for code, label, category, metric_key, weight in indicator_specs:
            zscores = self._metric_zscores(sectors, metric_key)
            ranked = sorted(sectors, key=lambda row: abs(zscores.get(str(row.get("sector")), 0.0)), reverse=True)
            for sector in ranked[:6]:
                sector_label_value = str(sector.get("sector"))
                zscore = zscores.get(sector_label_value, 0.0)
                if abs(zscore) < 0.35:
                    continue
                indicator_edges.append(
                    {
                        "source": code,
                        "target": sector_label_value,
                        "p_value": None,
                        "weight": round(min(4.0, max(0.35, abs(zscore))), 4),
                        "lag": 0,
                        "relationship": f"{label} -> {sector_label_value}",
                        "source_type": "indicator",
                        "target_type": "sector",
                        "category": category,
                        "configured_exposure": weight,
                        "edge_kind": "live_snapshot_exposure",
                    }
                )
        return {
            "country": "IN",
            "label": "India",
            "source_mode": "real_sector_history_plus_live_market_indicators",
            "runtime_handoff": {
                "active": True,
                "observed_dates": 1,
                "required_dates": 1,
                "indicator_count": len(indicator_nodes),
                "source": "live India F&O/ATM sector snapshot",
                "reason": "Live indicator edges are current exposure links; Granger sector edges use real sector-index history when available.",
            },
            "selected_lag": real_model.get("selected_lag") or 0,
            "alpha": alpha,
            "nodes": sector_nodes + indicator_nodes,
            "edges": sector_edges[:18] + indicator_edges,
            "indicator_edges": indicator_edges,
            "sector_edges": sector_edges,
            "summary": {
                "sector_edge_count": len(sector_edges),
                "indicator_edge_count": len(indicator_edges),
                "indicator_count": len(indicator_nodes),
                "sector_count": len(sector_nodes),
            },
        }

    async def validation_payload(self) -> dict[str, Any]:
        overview = await self.overview()
        try:
            from sector_interaction.ingestion import sector_ingestion_store

            observed_dates = len(
                {
                    str(row.get("date"))
                    for row in sector_ingestion_store.load_observations("IN", limit=20_000)
                    if row.get("date") and not self._is_deprecated_runtime_stub(row)
                }
            )
        except Exception:
            observed_dates = 1
        real_validation = await self._real_runtime_validation_payload(observed_dates=observed_dates)
        if real_validation is not None:
            return real_validation
        pending_reason = (
            "Backtest is pending until enough aligned real sector-index monthly return windows are available."
            if observed_dates >= 24
            else "Backtest is intentionally disabled until enough dated live/public sector observations are stored."
        )
        return {
            "country": "IN",
            "label": "India",
            "source_mode": "validation_pending_live_india_history",
            "runtime_handoff": {
                "active": False,
                "observed_dates": observed_dates,
                "required_dates": 24,
                "indicator_count": 4,
                "source": "live India F&O/ATM sector snapshot plus public-source ingestion store",
                "reason": pending_reason,
            },
            "summary": {
                "observations": 0,
                "cumulative_return_pct": 0.0,
                "average_monthly_return_pct": 0.0,
                "hit_rate_pct": 0.0,
                "information_ratio": 0.0,
                "max_drawdown_pct": 0.0,
            },
            "equity_curve": [],
            "recent_windows": [],
            "method": f"Validation pending. Latest live India snapshot is {overview.get('as_of')}; {pending_reason} No synthetic India backtest is shown.",
        }

    async def _real_runtime_validation_payload(self, *, observed_dates: int) -> dict[str, Any] | None:
        if observed_dates < 24:
            return None
        try:
            from sector_interaction.real_history import real_sector_history_service
            from sector_interaction.service import COUNTRIES, sector_interaction_service

            config = COUNTRIES["IN"]
            indicators = sector_interaction_service._runtime_indicator_frame(config)
            if indicators is None or len(indicators.index) < 24:
                return None
            returns, source, detail, _close_counts = await real_sector_history_service._load_india_returns(
                periods=500,
                timeframe="daily",
            )
            if returns is None or returns.empty:
                return None
            monthly_returns = (1.0 + returns).resample("ME").prod() - 1.0
            monthly_indicators = indicators.resample("ME").last().ffill()
            common_dates = monthly_returns.index.intersection(monthly_indicators.index)
            common_sectors = [sector for sector in config.sectors if sector in monthly_returns.columns]
            if len(common_dates) < 12 or len(common_sectors) < 4:
                return None
            monthly_returns = monthly_returns.loc[common_dates, common_sectors]
            monthly_indicators = monthly_indicators.loc[common_dates]
            signal_frame = sector_interaction_service._sector_signal_frame(config, monthly_indicators)
            signal_frame = signal_frame.reindex(columns=common_sectors).shift(1)
            rows: list[dict[str, Any]] = []
            strategy_returns: list[float] = []
            for timestamp in common_dates[1:]:
                scores = signal_frame.loc[timestamp].dropna()
                if scores.empty:
                    continue
                top_count = max(1, min(3, len(scores) // 3))
                leaders = list(scores.sort_values(ascending=False).head(top_count).index)
                laggards = list(scores.sort_values(ascending=True).head(top_count).index)
                leader_return = float(monthly_returns.loc[timestamp, leaders].mean())
                laggard_return = float(monthly_returns.loc[timestamp, laggards].mean())
                long_short = leader_return - laggard_return
                strategy_returns.append(long_short)
                rows.append(
                    {
                        "date": timestamp.date().isoformat(),
                        "leaders": leaders,
                        "laggards": laggards,
                        "leader_return": round(leader_return, 5),
                        "laggard_return": round(laggard_return, 5),
                        "long_short_return": round(long_short, 5),
                        "cumulative_return": round(float(np.prod([1.0 + value for value in strategy_returns]) - 1.0), 5),
                    }
                )
            if not rows:
                return None
            returns_array = np.asarray(strategy_returns, dtype=float)
            cumulative = float(np.prod(1.0 + returns_array) - 1.0) if len(returns_array) else 0.0
            avg = float(np.mean(returns_array)) if len(returns_array) else 0.0
            stdev = float(np.std(returns_array, ddof=1)) if len(returns_array) > 1 else 0.0
            information_ratio = (avg / stdev) * np.sqrt(12.0) if stdev > 0 else 0.0
            hit_rate = float(np.mean(returns_array > 0.0)) if len(returns_array) else 0.0
            return {
                "country": "IN",
                "label": "India",
                "source_mode": "real_sector_returns_runtime_indicators",
                "runtime_handoff": {
                    "active": True,
                    "observed_dates": observed_dates,
                    "required_dates": 24,
                    "indicator_count": len(monthly_indicators.columns),
                    "source": "durable India public/live ingestion store",
                    "reason": "Runtime India indicators are aligned against real sector-index returns.",
                },
                "summary": {
                    "observations": len(rows),
                    "cumulative_return_pct": round(cumulative * 100.0, 2),
                    "average_monthly_return_pct": round(avg * 100.0, 2),
                    "hit_rate_pct": round(hit_rate * 100.0, 2),
                    "information_ratio": round(float(information_ratio), 2),
                    "max_drawdown_pct": round(sector_interaction_service._max_drawdown([row["cumulative_return"] for row in rows]) * 100.0, 2),
                },
                "equity_curve": [{"date": row["date"], "value": round(1.0 + row["cumulative_return"], 5)} for row in rows],
                "recent_windows": rows[-12:],
                "method": f"Real India validation: runtime public/live sector indicators versus real monthly sector-index returns from {source}. {detail or ''}".strip(),
            }
        except Exception:
            return None

    async def report_payload(self) -> dict[str, Any]:
        signals = await self.signals_payload()
        market = await self.market_intelligence_payload()
        top_overweights = [row for row in signals["rankings"] if row["stance"] == "overweight"][:3]
        top_underweights = [row for row in signals["rankings"] if row["stance"] == "underweight"][:3]
        leader = top_overweights[0] if top_overweights else (signals["rankings"][0] if signals["rankings"] else {})
        laggard = top_underweights[0] if top_underweights else (signals["rankings"][-1] if signals["rankings"] else {})
        return {
            "country": "IN",
            "label": "India",
            "as_of": signals["as_of"],
            "source_mode": "live_fno_atm_watchlist",
            "headline": f"India sector leadership: {leader.get('sector', '--')} leads while {laggard.get('sector', '--')} lags.",
            "summary_bullets": [
                f"Top live sector: {leader.get('sector', '--')} at {leader.get('score', 0):+.2f}.",
                f"Weakest live sector: {laggard.get('sector', '--')} at {laggard.get('score', 0):+.2f}.",
                f"Live F&O universe contains {market.get('universe', {}).get('stocks', 0)} stocks across mapped NSE sectors.",
                f"Real VAR source mode: {(market.get('real_model') or {}).get('source_mode', 'unknown')}.",
            ],
            "top_overweights": top_overweights,
            "top_underweights": top_underweights,
            "strongest_indicator_edges": [],
            "risk_flags": [
                "India report uses live sector snapshots; historical validation activates only after enough dated observations are stored.",
                "External alt-data connectors such as UPI, GST/VAHAN, pharma exports and realty registrations still require source-specific collectors.",
            ],
            "next_actions": [
                "Schedule daily India live-market ingestion after market close.",
                "Backfill real NSE sector-index history for robust VAR and validation windows.",
                "Promote source-specific India open-data collectors after schema and compliance checks.",
            ],
            "disclaimer": "India report contains live market-derived sector rankings only. Synthetic India signal and backtest fallbacks are disabled.",
        }

    def _metric_zscores(self, sectors: list[dict[str, Any]], metric_key: str) -> dict[str, float]:
        values = [_float(row.get(metric_key)) for row in sectors]
        if not values:
            return {}
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / max(1, len(values) - 1)
        stdev = math.sqrt(variance)
        if stdev <= 1e-9:
            return {str(row.get("sector")): 0.0 for row in sectors}
        return {
            str(row.get("sector")): (value - mean) / stdev
            for row, value in zip(sectors, values)
        }

    def _is_deprecated_runtime_stub(self, row: dict[str, Any]) -> bool:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        return str(metadata.get("mode") or "") in {"open_data_runtime_stub", "prototype_runtime_stub"}

    async def _load_live_rows(self) -> list[dict[str, Any]]:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    """
                    WITH latest AS (
                        SELECT DISTINCT ON (underlying, option_type)
                            underlying, kind, expiry, strike, option_type, time,
                            underlying_price, ltp, change_pct, oi_change_pct,
                            volume, iv, rsi, macd_histogram
                        FROM atm_option_watchlist_snapshots
                        ORDER BY underlying, option_type, time DESC
                    ),
                    symbols AS (
                        SELECT symbol, kind, lot_size
                        FROM fo_underlying_catalog
                    )
                    SELECT
                        symbols.symbol,
                        symbols.kind,
                        symbols.lot_size,
                        MAX(latest.time) AS latest_time,
                        MAX(latest.underlying_price) AS underlying_price,
                        AVG(latest.change_pct) AS avg_change_pct,
                        AVG(latest.oi_change_pct) AS avg_oi_change_pct,
                        SUM(latest.volume) AS total_volume,
                        AVG(latest.iv) AS avg_iv,
                        AVG(latest.rsi) AS avg_rsi,
                        AVG(latest.macd_histogram) AS avg_macd_histogram
                    FROM symbols
                    LEFT JOIN latest ON latest.underlying = symbols.symbol
                    GROUP BY symbols.symbol, symbols.kind, symbols.lot_size
                    ORDER BY symbols.symbol
                    """
                )
            )
            records = result.mappings().all()
        rows = []
        for row in records:
            symbol = str(row["symbol"])
            sector_key = sector_for_symbol(symbol)
            change_pct = _float(row.get("avg_change_pct"))
            oi_change_pct = _float(row.get("avg_oi_change_pct"))
            volume = _float(row.get("total_volume"))
            iv = _float(row.get("avg_iv"))
            rsi = _float(row.get("avg_rsi"))
            macd = _float(row.get("avg_macd_histogram"))
            oi_signal = _signed_log_signal(oi_change_pct)
            volume_signal = min(math.log1p(volume) / 6.0, 3.0)
            leadership = round(
                (change_pct * 0.45)
                + (oi_signal * 0.85)
                + (volume_signal * 0.35)
                + (macd * 0.65)
                + ((rsi - 50.0) * 0.02),
                4,
            )
            rows.append(
                {
                    "symbol": symbol,
                    "kind": str(row["kind"]),
                    "sector_key": sector_key,
                    "sector": sector_label(sector_key),
                    "lot_size": row.get("lot_size"),
                    "latest_time": row.get("latest_time").isoformat() if row.get("latest_time") else None,
                    "underlying_price": round(_float(row.get("underlying_price")), 2),
                    "change_pct": round(change_pct, 3),
                    "oi_change_pct": round(oi_change_pct, 3),
                    "oi_signal": round(oi_signal, 4),
                    "volume": int(volume),
                    "iv": round(iv, 4),
                    "rsi": round(rsi, 2),
                    "macd_histogram": round(macd, 5),
                    "leadership_score": leadership,
                }
            )
        return rows

    def _group_rows(self, rows: list[dict[str, Any]], *, include_aggregates: bool = False) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if row["kind"] != "STOCK":
                continue
            grouped[str(row["sector_key"])].append(row)
        if include_aggregates:
            base_grouped = {key: list(value) for key, value in grouped.items()}
            for aggregate_key, member_keys in AGGREGATE_SECTOR_MEMBERS.items():
                aggregate_items: list[dict[str, Any]] = []
                for member_key in member_keys:
                    aggregate_items.extend(base_grouped.get(member_key, []))
                if aggregate_items:
                    grouped[aggregate_key] = [
                        {
                            **item,
                            "sector_key": aggregate_key,
                            "sector": sector_label(aggregate_key),
                        }
                        for item in aggregate_items
                    ]
        return dict(grouped)

    def _sector_summary(self, sector_key: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        if not items:
            return {
                "sector_key": sector_key,
                "sector": sector_label(sector_key),
                "constituents": 0,
                "leadership_score": 0.0,
                "relative_strength": 0.0,
                "momentum": 0.0,
                "rrg_quadrant": "lagging",
            }
        stock_items = [item for item in items if item["kind"] == "STOCK"] or items
        avg_score = sum(_float(item["leadership_score"]) for item in stock_items) / len(stock_items)
        avg_change = sum(_float(item["change_pct"]) for item in stock_items) / len(stock_items)
        avg_oi = sum(_float(item["oi_change_pct"]) for item in stock_items) / len(stock_items)
        avg_oi_signal = sum(_float(item.get("oi_signal")) for item in stock_items) / len(stock_items)
        avg_iv = sum(_float(item["iv"]) for item in stock_items) / len(stock_items)
        rel_strength = round(avg_change + avg_score * 0.15, 4)
        momentum = round(avg_oi_signal + avg_score * 0.1, 4)
        return {
            "sector_key": sector_key,
            "sector": sector_label(sector_key),
            "constituents": len(stock_items),
            "leadership_score": round(avg_score, 4),
            "relative_strength": rel_strength,
            "momentum": momentum,
            "avg_change_pct": round(avg_change, 3),
            "avg_oi_change_pct": round(avg_oi, 3),
            "avg_iv": round(avg_iv, 4),
            "leaders": sorted(stock_items, key=lambda row: row["leadership_score"], reverse=True)[:5],
            "laggards": sorted(stock_items, key=lambda row: row["leadership_score"])[:5],
            "rrg_quadrant": self._rrg_quadrant(rel_strength, momentum),
        }

    def _rrg_payload(self, sectors: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "sector_key": row["sector_key"],
                "sector": row["sector"],
                "x": row["relative_strength"],
                "y": row["momentum"],
                "quadrant": row["rrg_quadrant"],
                "leadership_score": row["leadership_score"],
            }
            for row in sectors
        ]

    def _rrg_quadrant(self, relative_strength: float, momentum: float) -> str:
        if relative_strength >= 0 and momentum >= 0:
            return "leading"
        if relative_strength >= 0 and momentum < 0:
            return "weakening"
        if relative_strength < 0 and momentum >= 0:
            return "improving"
        return "lagging"

    def _sector_parameters(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not items:
            return []
        stock_items = [item for item in items if item["kind"] == "STOCK"] or items
        positive_count = sum(1 for item in stock_items if _float(item["change_pct"]) > 0)
        high_oi_count = sum(1 for item in stock_items if _float(item.get("oi_signal")) > 0.25)
        avg_rsi = sum(_float(item["rsi"]) for item in stock_items) / len(stock_items)
        avg_volume = sum(_float(item["volume"]) for item in stock_items) / len(stock_items)
        avg_iv = sum(_float(item["iv"]) for item in stock_items) / len(stock_items)
        return [
            {
                "code": "price_breadth",
                "label": "Price Breadth",
                "value": round((positive_count / len(stock_items)) * 100, 2),
                "unit": "%",
                "state": "constructive" if positive_count >= len(stock_items) / 2 else "weak",
            },
            {
                "code": "oi_breadth",
                "label": "OI Breadth",
                "value": round((high_oi_count / len(stock_items)) * 100, 2),
                "unit": "%",
                "state": "expanding" if high_oi_count >= len(stock_items) / 2 else "quiet",
            },
            {
                "code": "avg_rsi",
                "label": "Average RSI",
                "value": round(avg_rsi, 2),
                "unit": "",
                "state": "strong" if avg_rsi >= 58 else "oversold" if avg_rsi <= 42 else "neutral",
            },
            {
                "code": "avg_iv",
                "label": "Average IV",
                "value": round(avg_iv, 4),
                "unit": "",
                "state": _iv_state(avg_iv),
            },
            {
                "code": "avg_volume",
                "label": "Average Option Volume",
                "value": round(avg_volume, 0),
                "unit": "contracts",
                "state": "active" if avg_volume >= 100_000 else "thin",
            },
        ]

    def _performance_cycle(self, summary: dict[str, Any] | None) -> dict[str, Any]:
        phases = [
            {
                "phase": "improving",
                "label": "Improving",
                "description": "Momentum is recovering before relative strength fully confirms.",
            },
            {
                "phase": "leading",
                "label": "Leading",
                "description": "Relative strength and momentum are both positive.",
            },
            {
                "phase": "weakening",
                "label": "Weakening",
                "description": "Relative strength remains positive but momentum is fading.",
            },
            {
                "phase": "lagging",
                "label": "Lagging",
                "description": "Relative strength and momentum are both negative.",
            },
        ]
        current_phase = str((summary or {}).get("rrg_quadrant") or "lagging")
        current_index = next((index for index, phase in enumerate(phases) if phase["phase"] == current_phase), 3)
        rel_strength = _float((summary or {}).get("relative_strength"))
        momentum = _float((summary or {}).get("momentum"))
        score = _float((summary or {}).get("leadership_score"))
        next_phase = phases[(current_index + 1) % len(phases)]["phase"]
        return {
            "method": "RRG-style cycle from live relative strength and momentum; no simulated history is used.",
            "current_phase": current_phase,
            "current_phase_index": current_index,
            "next_phase_to_watch": next_phase,
            "cycle_score": round(score, 4),
            "relative_strength": rel_strength,
            "momentum": momentum,
            "phases": phases,
            "interpretation": self._cycle_interpretation(current_phase, rel_strength, momentum),
        }

    def _cycle_interpretation(self, phase: str, relative_strength: float, momentum: float) -> str:
        if phase == "leading":
            return "Sector leadership is confirmed; monitor for momentum rollover before reducing bias."
        if phase == "improving":
            return "Sector is rotating up from a weak base; require price breadth confirmation."
        if phase == "weakening":
            return "Sector still has relative strength, but fresh longs need stricter timing."
        return "Sector is lagging; prefer defensive sizing until momentum improves."

    def _sector_alt_data(self, sector_key: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        stock_items = [item for item in items if item["kind"] == "STOCK"] or items
        if stock_items:
            positive_count = sum(1 for item in stock_items if _float(item["change_pct"]) > 0)
            high_oi_count = sum(1 for item in stock_items if _float(item.get("oi_signal")) > 0.25)
            avg_volume = sum(_float(item["volume"]) for item in stock_items) / len(stock_items)
            avg_iv = sum(_float(item["iv"]) for item in stock_items) / len(stock_items)
            avg_rsi = sum(_float(item["rsi"]) for item in stock_items) / len(stock_items)
            option_flow_state = "expanding" if high_oi_count >= len(stock_items) / 2 else "quiet"
            price_breadth = round((positive_count / len(stock_items)) * 100, 2)
            oi_breadth = round((high_oi_count / len(stock_items)) * 100, 2)
        else:
            avg_volume = avg_iv = avg_rsi = price_breadth = oi_breadth = 0.0
            option_flow_state = "empty"
        nse_status = nse_constituent_service.status()
        official_row = next(
            (row for row in nse_status.get("sectors", []) if row.get("sector_key") == sector_key),
            None,
        )
        rows = [
            {
                "name": "ATM option-flow breadth",
                "status": "live_atm_watchlist",
                "value": oi_breadth,
                "unit": "%",
                "state": option_flow_state,
                "detail": "Share of mapped F&O constituents with expanding option open-interest signal.",
            },
            {
                "name": "Price breadth",
                "status": "live_underlying_snapshot",
                "value": price_breadth,
                "unit": "%",
                "state": "constructive" if price_breadth >= 50 else "weak",
                "detail": "Share of mapped constituents trading positive in the latest ATM watchlist snapshot.",
            },
            {
                "name": "Average option volume",
                "status": "live_atm_watchlist",
                "value": round(avg_volume, 0),
                "unit": "contracts",
                "state": "active" if avg_volume >= 100_000 else "thin",
                "detail": "Average latest CE/PE ATM option volume across sector constituents.",
            },
            {
                "name": "Average IV",
                "status": "live_atm_watchlist",
                "value": round(avg_iv, 4),
                "unit": "",
                "state": _iv_state(avg_iv),
                "detail": "Average latest ATM implied-volatility proxy across mapped constituents.",
            },
            {
                "name": "Average RSI",
                "status": "live_underlying_snapshot",
                "value": round(avg_rsi, 2),
                "unit": "",
                "state": "strong" if avg_rsi >= 58 else "oversold" if avg_rsi <= 42 else "neutral",
                "detail": "Average latest RSI from the sector's F&O/ATM constituent set.",
            },
            {
                "name": "Official NSE constituent coverage",
                "status": "official_niftyindices_overlay" if official_row else "static_fallback_mapping",
                "value": int((official_row or {}).get("constituents") or len(stock_items)),
                "unit": "symbols",
                "state": "official" if official_row else "fallback",
                "detail": "Official Nifty sector CSV membership is used before static fallback mappings.",
            },
        ]
        proxy_map = {
            "nifty_bank": ["RBI credit/card data", "deposit/credit growth", "bond-yield curve"],
            "nifty_auto": ["VAHAN registrations", "GST auto collections", "fuel-price pressure"],
            "nifty_it": ["IT hiring momentum", "US tech spend proxy", "INR/USD sensitivity"],
            "nifty_pharma": ["export velocity", "USFDA event monitor", "API/raw-material pressure"],
            "nifty_fmcg": ["rural demand", "FMCG search basket", "input commodity pressure"],
            "nifty_oil_gas": ["crude/import pressure", "EIA inventory", "refining margin proxy"],
            "nifty_realty": ["registration pulse", "mortgage rate pressure", "urban footfall"],
        }
        rows.extend(
            {
                "name": item,
                "status": "planned_source_connector",
                "value": None,
                "unit": "",
                "state": "pending",
                "detail": "Source-specific India connector still pending access, schema, and validation.",
            }
            for item in proxy_map.get(sector_key, ["sector-specific news", "approved source metric"])
        )
        return rows


india_live_sector_service = IndiaLiveSectorService()
