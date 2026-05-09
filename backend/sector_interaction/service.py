"""VAR-style sector interaction engine for US and India sector indices.

US research endpoints still expose deterministic synthetic samples when no
licensed index data is present. India API routes are wired to live F&O/ATM
sector snapshots and real sector-index history adapters, with synthetic India
fallbacks disabled at the router layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import f as f_dist

from sector_interaction.ingestion import CollectorRun, SectorObservation, sector_ingestion_store


US_SECTORS = [
    "Technology",
    "Healthcare",
    "Consumer Discretionary",
    "Consumer Staples",
    "Energy",
    "Financials",
    "Industrials",
    "Materials",
    "Communication Services",
    "Utilities",
    "Real Estate",
]

INDIA_SECTORS = [
    "Nifty Auto",
    "Nifty Bank",
    "Nifty Financial Services",
    "Nifty FMCG",
    "Nifty IT",
    "Nifty Media",
    "Nifty Metal",
    "Nifty Pharma",
    "Nifty Realty",
]

RUNTIME_SIGNAL_MIN_DATES = 24
RUNTIME_NETWORK_MIN_DATES = 48
RUNTIME_BACKTEST_MIN_DATES = 60


@dataclass(frozen=True)
class CountryConfig:
    code: str
    label: str
    sectors: list[str]
    seed: int
    source_note: str


@dataclass(frozen=True)
class IndicatorConfig:
    code: str
    label: str
    category: str
    cadence: str
    source_status: str
    quality_score: float
    lead_months: int
    sector_weights: dict[str, float]
    production_source: str
    metric_definition: str


COUNTRIES = {
    "US": CountryConfig(
        code="US",
        label="United States",
        sectors=US_SECTORS,
        seed=811,
        source_note="Synthetic monthly S&P 500 sector-style returns. Replace with licensed sector index or ETF return data in production.",
    ),
    "IN": CountryConfig(
        code="IN",
        label="India",
        sectors=INDIA_SECTORS,
        seed=1947,
        source_note="India endpoints use live F&O/ATM sector snapshots and real NSE sector-index history where available; synthetic India fallbacks are disabled in the API.",
    ),
}


INDICATORS_BY_COUNTRY: dict[str, list[IndicatorConfig]] = {
    "US": [
        IndicatorConfig("search_cloud_ai", "Cloud and AI search momentum", "Search and attention", "weekly", "prototype", 0.72, 1, {"Technology": 0.9, "Communication Services": 0.35}, "Google Trends / YouTube Data API", "Normalised search growth for cloud, AI infrastructure, GPUs, and enterprise software."),
        IndicatorConfig("ev_attention", "EV and charging attention", "Search and attention", "weekly", "prototype", 0.64, 1, {"Consumer Discretionary": 0.7, "Materials": 0.35, "Energy": -0.2}, "Google Trends / approved category feeds", "Search and marketplace rank momentum for EVs, charging, batteries, and related products."),
        IndicatorConfig("card_discretionary_spend", "Discretionary card spend growth", "Transactions and payments", "monthly", "licensed_required", 0.78, 1, {"Consumer Discretionary": 0.85, "Financials": 0.2}, "Aggregated card-spend reports", "Month-on-month and year-on-year discretionary spending growth."),
        IndicatorConfig("energy_inventory_pressure", "Energy inventory pressure", "Macro and commodities", "weekly", "open_data", 0.69, 1, {"Energy": -0.75, "Materials": -0.18, "Industrials": -0.12}, "EIA inventory and production statistics", "Inventory surprise and production pressure z-score, signed so higher is better for exposed sectors."),
        IndicatorConfig("credit_housing_pulse", "Credit and housing pulse", "Filings and macro", "monthly", "open_data", 0.66, 1, {"Financials": 0.62, "Real Estate": 0.76, "Utilities": -0.18}, "FRED / housing permits / mortgage data", "Composite of credit growth, mortgage conditions, permits, and housing activity."),
        IndicatorConfig("ai_job_postings", "AI job posting intensity", "Workforce and hiring", "weekly", "tos_review_required", 0.6, 2, {"Technology": 0.65, "Industrials": 0.22}, "Company career pages and licensed job feeds", "Growth in AI, data, semiconductor, and automation openings."),
        IndicatorConfig("biotech_patent_velocity", "Biotech patent velocity", "Innovation", "quarterly", "open_data", 0.58, 2, {"Healthcare": 0.78}, "USPTO PatentsView", "Patent counts by assignee and CPC classes mapped to biotech and medical devices."),
        IndicatorConfig("freight_rate_momentum", "Freight rate momentum", "Macro and commodities", "weekly", "prototype", 0.55, 1, {"Industrials": 0.58, "Materials": 0.45}, "Port authority data / SCFI feeds", "Container and freight-rate momentum proxy for goods demand."),
        IndicatorConfig("defensive_sentiment", "Defensive sentiment bid", "Sentiment and news", "daily", "prototype", 0.52, 1, {"Consumer Staples": 0.5, "Utilities": 0.52, "Healthcare": 0.18}, "Licensed news and Reddit API", "News/social defensiveness score from low-beta, staples, utilities, and healthcare language."),
        IndicatorConfig("office_reit_traffic", "Office and retail traffic", "Geospatial and foot traffic", "weekly", "licensed_required", 0.5, 1, {"Real Estate": 0.72, "Consumer Discretionary": 0.22}, "Aggregated foot-traffic providers / municipal data", "Footfall and mobility growth around office, mall, hotel, and retail zones."),
    ],
    "IN": [
        IndicatorConfig("upi_spend_growth", "UPI spend growth", "Transactions and payments", "monthly", "open_data", 0.74, 1, {"Nifty Bank": 0.45, "Nifty Financial Services": 0.6, "Nifty FMCG": 0.25}, "NPCI UPI statistics", "UPI value and transaction growth mapped to financials and consumption."),
        IndicatorConfig("gst_auto_pulse", "Auto GST and registration pulse", "Transactions and payments", "monthly", "open_data", 0.68, 1, {"Nifty Auto": 0.82, "Nifty Bank": 0.18}, "GST collections / VAHAN registration data", "Vehicle registration, tax collection, and finance-linked auto demand proxy."),
        IndicatorConfig("india_live_leadership_score", "Live sector leadership score", "Live market structure", "daily", "internal_live_market_data", 0.82, 0, {sector: 1.0 for sector in INDIA_SECTORS}, "F&O/ATM watchlist snapshots", "Live sector leadership score from change, option OI, volume, IV, RSI, and momentum proxies."),
        IndicatorConfig("india_live_price_momentum", "Live sector price momentum", "Live market structure", "daily", "internal_live_market_data", 0.78, 0, {sector: 1.0 for sector in INDIA_SECTORS}, "F&O/ATM watchlist snapshots", "Latest average underlying change across mapped sector constituents."),
        IndicatorConfig("india_live_option_oi_momentum", "Live option OI momentum", "Live options flow", "daily", "internal_live_market_data", 0.76, 0, {sector: 1.0 for sector in INDIA_SECTORS}, "ATM option watchlist snapshots", "Latest average ATM option open-interest change across mapped sector constituents."),
        IndicatorConfig("india_live_iv_pressure", "Live IV pressure", "Live options flow", "daily", "internal_live_market_data", 0.68, 0, {sector: -0.35 for sector in INDIA_SECTORS}, "ATM option watchlist snapshots", "Latest ATM IV pressure, signed as a drag when elevated."),
        IndicatorConfig("it_hiring_momentum", "IT hiring momentum", "Workforce and hiring", "weekly", "tos_review_required", 0.6, 2, {"Nifty IT": 0.84, "Nifty Media": 0.15}, "Company career pages / licensed job feeds", "Open role growth across large IT services and digital engineering companies."),
        IndicatorConfig("metal_inventory_pressure", "Metal inventory pressure", "Macro and commodities", "weekly", "prototype", 0.57, 1, {"Nifty Metal": -0.72, "Nifty Auto": -0.18}, "Exchange inventory and commodity data", "Inventory and price-pressure proxy, signed so higher indicates better sector setup."),
        IndicatorConfig("pharma_export_velocity", "Pharma export velocity", "Macro and filings", "monthly", "open_data", 0.63, 1, {"Nifty Pharma": 0.78}, "Commerce ministry exports / company disclosures", "Export growth and regulatory event proxy for pharma demand."),
        IndicatorConfig("fmcg_search_basket", "FMCG search basket", "Search and attention", "weekly", "prototype", 0.56, 1, {"Nifty FMCG": 0.72, "Nifty Media": 0.14}, "Google Trends / approved marketplace feeds", "Normalised search and category demand for staples, personal care, and packaged foods."),
        IndicatorConfig("realty_registration_pulse", "Realty registration pulse", "Geospatial and foot traffic", "monthly", "open_data", 0.62, 1, {"Nifty Realty": 0.82, "Nifty Bank": 0.24}, "State registration data / metro footfall", "Property registrations, stamp-duty collections, and urban activity proxy."),
        IndicatorConfig("media_ad_spend_proxy", "Media ad-spend proxy", "Sentiment and news", "monthly", "prototype", 0.5, 1, {"Nifty Media": 0.74, "Nifty FMCG": 0.18}, "News/RSS and advertising proxies", "Advertising demand and media campaign-intensity proxy."),
        IndicatorConfig("crude_import_pressure", "Crude import pressure", "Macro and commodities", "weekly", "open_data", 0.61, 1, {"Nifty Auto": -0.36, "Nifty FMCG": -0.22, "Nifty Financial Services": -0.14}, "Petroleum ministry / commodity feeds", "Oil import and price pressure proxy, signed so higher is less margin pressure."),
    ],
}


class SectorInteractionService:
    def overview(self) -> dict[str, Any]:
        return {
            "module": "sector_interaction",
            "description": "VAR/Granger sector lead-lag networks with correlation matrices and alternative-data source plans.",
            "countries": [
                {
                    "code": config.code,
                    "label": config.label,
                    "sector_count": len(config.sectors),
                    "sectors": config.sectors,
                    "source_note": config.source_note,
                }
                for config in COUNTRIES.values()
            ],
            "methodology": {
                "model": "VAR(p) on aligned sector returns",
                "lag_selection": "AIC over candidate lags",
                "edge_weight": "-log10(pairwise Granger p-value)",
                "network_interpretation": "Outgoing edges are leading influence; incoming edges are follower sensitivity.",
            },
        }

    def acquisition_plan(self) -> dict[str, Any]:
        return {
            "architecture": [
                "Scheduler runs source jobs daily, weekly, or monthly according to source latency.",
                "Source collectors use official APIs where available and only permitted scraping where terms allow it.",
                "Normalisation converts raw records into sector/ticker time series with common dates and source-quality metadata.",
                "Mapping tables link keywords, brands, tickers, CPC codes, and macro categories to GICS or NSE sector definitions.",
                "A PostgreSQL or lakehouse store keeps raw, cleaned, feature, and model-output layers separately.",
                "Every run records source, row counts, failures, schema drift, and versioned transformation code.",
            ],
            "source_categories": [
                {
                    "name": "Search and attention",
                    "examples": ["Google Trends", "YouTube Data API", "approved marketplace category feeds"],
                    "metrics": ["normalised search index", "view-count momentum", "category rank change"],
                },
                {
                    "name": "Transactions and payments",
                    "examples": ["aggregated card spend", "NPCI UPI", "RBI card spending", "GST collections"],
                    "metrics": ["month-on-month growth", "year-on-year growth", "sector spend breadth"],
                },
                {
                    "name": "Workforce and hiring",
                    "examples": ["company careers pages where permitted", "USAJobs", "public hiring feeds"],
                    "metrics": ["open roles", "new postings", "role mix by location"],
                },
                {
                    "name": "Innovation",
                    "examples": ["USPTO PatentsView", "patent bulk files", "CPC mappings"],
                    "metrics": ["patent filings", "assignee growth", "technology-area velocity"],
                },
                {
                    "name": "Filings and ownership",
                    "examples": ["SEC EDGAR", "Form 4", "13F", "BSE disclosures", "MCA filings"],
                    "metrics": ["insider purchase notional", "institutional position change", "disclosure frequency"],
                },
                {
                    "name": "Policy and commodities",
                    "examples": ["STOCK Act disclosures", "CFTC COT", "EIA", "USDA WASDE", "port authority data"],
                    "metrics": ["net positioning", "inventory surprise", "freight-rate momentum", "policy trade balance"],
                },
                {
                    "name": "Sentiment and news",
                    "examples": ["licensed news APIs", "RSS feeds", "Reddit API", "approved social APIs"],
                    "metrics": ["sector sentiment", "sentiment momentum", "event classifier score"],
                },
                {
                    "name": "Geospatial and foot traffic",
                    "examples": ["NASA VIIRS", "MTA turnstiles", "metro or rail open data"],
                    "metrics": ["activity proxy", "footfall growth", "regional demand proxy"],
                },
            ],
            "processing": [
                "Align all metrics to daily, weekly, or monthly timestamps.",
                "Compute z-scores, percentiles, changes, lags, and rolling reliability scores.",
                "Aggregate stock-level signals to sectors by market-cap or equal weights.",
                "Test whether alternative indicators Granger-cause sector returns before using them in rankings.",
                "Store model outputs, rejected edges, p-values, and report snapshots for auditability.",
            ],
            "legal_controls": [
                "Use official APIs or licensed feeds where required.",
                "Avoid personal data and store only aggregated or anonymised signals.",
                "Respect source terms, rate limits, robots guidance, and regional privacy rules.",
                "Treat alternative data as research input, not a substitute for compliance review.",
            ],
            "roadmap": [
                {"phase": "0-1 months", "work": "Select sources, verify access, and build sector/ticker/entity mappings."},
                {"phase": "1-3 months", "work": "Prototype collectors, raw storage, and source-quality monitoring."},
                {"phase": "2-4 months", "work": "Deploy schema, incremental loads, normalisation, and mapping engine."},
                {"phase": "3-6 months", "work": "Generate sector signals, run VAR/Granger validation, and backtest rankings."},
                {"phase": "5-7 months", "work": "Publish dashboards, network graphs, alerts, and written sector reports."},
                {"phase": "6+ months", "work": "Production monitoring, source maintenance, model recalibration, and source retirement rules."},
            ],
        }

    def source_map(self, country: str = "US") -> dict[str, Any]:
        config = self._country(country)
        indicators = self._indicator_configs(config)
        return {
            "country": config.code,
            "label": config.label,
            "sector_mapping_standard": "GICS sectors" if config.code == "US" else "NSE sector indices",
            "indicators": [
                {
                    "code": indicator.code,
                    "label": indicator.label,
                    "category": indicator.category,
                    "cadence": indicator.cadence,
                    "source_status": indicator.source_status,
                    "quality_score": indicator.quality_score,
                    "lead_months": indicator.lead_months,
                    "production_source": indicator.production_source,
                    "metric_definition": indicator.metric_definition,
                    "sector_weights": indicator.sector_weights,
                }
                for indicator in indicators
            ],
            "data_contract": {
                "required_columns": ["date", "country", "source", "indicator_code", "sector", "value", "quality_score"],
                "date_alignment": "monthly model frequency; daily/weekly source metrics are resampled using last observation and change features",
                "normalization": "rolling z-score and percentile rank by indicator before sector aggregation",
                "audit_fields": ["source_status", "terms_reviewed_at", "collector_version", "row_count", "schema_hash"],
            },
        }

    @lru_cache(maxsize=16)
    def signals(self, country: str = "US", periods: int = 160) -> dict[str, Any]:
        config = self._country(country)
        periods = max(48, min(int(periods), 500))
        runtime_source = self._runtime_indicator_source(config, min_dates=RUNTIME_SIGNAL_MIN_DATES)
        if runtime_source["active"]:
            indicators = runtime_source["indicators"]
            signal_frame = self._sector_signal_frame(config, indicators)
            source_mode = "runtime_alternative_data"
            method = "Runtime indicator observations are exposure-weighted into sector composite scores from the durable ingestion store."
        else:
            returns = self._simulate_returns(config, periods)
            indicators = self._simulate_indicators(config, returns)
            signal_frame = self._sector_signal_frame(config, indicators)
            source_mode = "synthetic_alternative_data"
            method = "Indicator z-scores are exposure-weighted into sector composite scores; production should replace synthetic indicators with approved source feeds."
        latest_scores = signal_frame.iloc[-1].sort_values(ascending=False)
        prior_scores = signal_frame.iloc[-2] if len(signal_frame) > 1 else signal_frame.iloc[-1]
        indicator_latest = self._indicator_latest_payload(config, indicators)
        rankings = [
            {
                "sector": sector,
                "score": round(float(score), 4),
                "rank": index + 1,
                "change": round(float(score - prior_scores.get(sector, 0.0)), 4),
                "stance": "overweight" if score >= 0.45 else "underweight" if score <= -0.45 else "neutral",
                "top_drivers": self._top_sector_drivers(config, indicators, sector),
            }
            for index, (sector, score) in enumerate(latest_scores.items())
        ]
        alerts = [
            {
                "sector": row["sector"],
                "severity": "high" if abs(row["score"]) >= 1.0 else "medium",
                "message": f"{row['sector']} {row['stance']} signal at {row['score']:+.2f}",
            }
            for row in rankings
            if abs(float(row["score"])) >= 0.75
        ]
        return {
            "country": config.code,
            "label": config.label,
            "as_of": signal_frame.index[-1].date().isoformat(),
            "source_mode": source_mode,
            "runtime_handoff": runtime_source["handoff"],
            "rankings": rankings,
            "indicator_latest": indicator_latest,
            "alerts": alerts[:8],
            "method": method,
        }

    @lru_cache(maxsize=16)
    def extended_network(self, country: str = "US", periods: int = 160, max_lag: int = 2, alpha: float = 0.05) -> dict[str, Any]:
        config = self._country(country)
        periods = max(48, min(int(periods), 500))
        max_lag = max(1, min(int(max_lag), 6))
        runtime_source = self._runtime_indicator_source(config, min_dates=RUNTIME_NETWORK_MIN_DATES)
        if runtime_source["active"]:
            indicators = runtime_source["indicators"]
            returns = self._simulate_returns(config, len(indicators))
            returns.index = indicators.index
            source_mode = "runtime_indicators_synthetic_sector_returns"
        else:
            returns = self._simulate_returns(config, periods)
            indicators = self._simulate_indicators(config, returns)
            source_mode = "synthetic"
        sector_model = self.model(config.code, periods, max_lag, alpha)
        lag = int(sector_model["selected_lag"])
        indicator_edges = self._indicator_edges(config, returns, indicators, lag=lag, alpha=alpha)
        indicator_nodes = [
            {
                "id": indicator.code,
                "label": indicator.label,
                "node_type": "indicator",
                "category": indicator.category,
                "quality_score": indicator.quality_score,
                "source_status": indicator.source_status,
            }
            for indicator in self._indicator_configs(config)
        ]
        sector_nodes = [
            {
                "id": sector,
                "label": sector,
                "node_type": "sector",
                "category": "sector_return",
            }
            for sector in config.sectors
        ]
        return {
            "country": config.code,
            "label": config.label,
            "source_mode": source_mode,
            "runtime_handoff": runtime_source["handoff"],
            "selected_lag": lag,
            "alpha": alpha,
            "nodes": sector_nodes + indicator_nodes,
            "edges": sector_model["network"]["edges"][:18] + indicator_edges,
            "indicator_edges": indicator_edges,
            "sector_edges": sector_model["network"]["edges"],
            "summary": {
                "sector_edge_count": len(sector_model["network"]["edges"]),
                "indicator_edge_count": len(indicator_edges),
                "indicator_count": len(indicator_nodes),
                "sector_count": len(sector_nodes),
            },
        }

    @lru_cache(maxsize=16)
    def validation_backtest(self, country: str = "US", periods: int = 160) -> dict[str, Any]:
        config = self._country(country)
        periods = max(60, min(int(periods), 500))
        runtime_source = self._runtime_indicator_source(config, min_dates=RUNTIME_BACKTEST_MIN_DATES)
        if runtime_source["active"]:
            indicators = runtime_source["indicators"]
            returns = self._simulate_returns(config, len(indicators))
            returns.index = indicators.index
            source_mode = "runtime_indicators_synthetic_sector_returns"
        else:
            returns = self._simulate_returns(config, periods)
            indicators = self._simulate_indicators(config, returns)
            source_mode = "synthetic_validation"
        signal_frame = self._sector_signal_frame(config, indicators).shift(1)
        next_returns = returns
        rows: list[dict[str, Any]] = []
        strategy_returns: list[float] = []
        for timestamp in returns.index[24:]:
            scores = signal_frame.loc[timestamp].dropna()
            if scores.empty:
                continue
            top_count = max(1, min(3, len(scores) // 3))
            leaders = list(scores.sort_values(ascending=False).head(top_count).index)
            laggards = list(scores.sort_values(ascending=True).head(top_count).index)
            leader_return = float(next_returns.loc[timestamp, leaders].mean())
            laggard_return = float(next_returns.loc[timestamp, laggards].mean())
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
        returns_array = np.asarray(strategy_returns, dtype=float)
        cumulative = float(np.prod(1.0 + returns_array) - 1.0) if len(returns_array) else 0.0
        avg = float(np.mean(returns_array)) if len(returns_array) else 0.0
        stdev = float(np.std(returns_array, ddof=1)) if len(returns_array) > 1 else 0.0
        information_ratio = (avg / stdev) * np.sqrt(12.0) if stdev > 0 else 0.0
        hit_rate = float(np.mean(returns_array > 0.0)) if len(returns_array) else 0.0
        return {
            "country": config.code,
            "label": config.label,
            "source_mode": source_mode,
            "runtime_handoff": runtime_source["handoff"],
            "summary": {
                "observations": len(rows),
                "cumulative_return_pct": round(cumulative * 100.0, 2),
                "average_monthly_return_pct": round(avg * 100.0, 2),
                "hit_rate_pct": round(hit_rate * 100.0, 2),
                "information_ratio": round(float(information_ratio), 2),
                "max_drawdown_pct": round(self._max_drawdown([row["cumulative_return"] for row in rows]) * 100.0, 2),
            },
            "equity_curve": [{"date": row["date"], "value": round(1.0 + row["cumulative_return"], 5)} for row in rows],
            "recent_windows": rows[-12:],
            "method": "Monthly long-short validation: long highest composite sector scores and short lowest composite sector scores next month.",
        }

    def pipeline_status(self, country: str = "US") -> dict[str, Any]:
        config = self._country(country)
        indicators = self._indicator_configs(config)
        connectors = [
            self._connector_status(indicator, index)
            for index, indicator in enumerate(indicators)
        ]
        readiness_values = [float(connector["readiness_score"]) for connector in connectors]
        status_counts: dict[str, int] = {}
        for connector in connectors:
            status = str(connector["source_status"])
            status_counts[status] = status_counts.get(status, 0) + 1
        blockers = [
            {
                "indicator_code": connector["indicator_code"],
                "label": connector["label"],
                "blocker": blocker,
                "next_action": connector["next_action"],
            }
            for connector in connectors
            for blocker in connector["blockers"]
        ]
        return {
            "country": config.code,
            "label": config.label,
            "as_of": pd.Timestamp.now("UTC").date().isoformat(),
            "summary": {
                "connector_count": len(connectors),
                "open_data_count": status_counts.get("open_data", 0),
                "prototype_count": status_counts.get("prototype", 0),
                "licensed_required_count": status_counts.get("licensed_required", 0),
                "tos_review_required_count": status_counts.get("tos_review_required", 0),
                "readiness_score": round(float(np.mean(readiness_values)) if readiness_values else 0.0, 3),
                "critical_blockers": len(blockers),
            },
            "connectors": connectors,
            "data_layers": [
                {
                    "layer": "raw",
                    "purpose": "Immutable source extracts with run metadata and source-specific payloads.",
                    "primary_keys": ["run_id", "source", "source_record_id"],
                    "retention": "keep full history; partition by source and ingestion date",
                },
                {
                    "layer": "normalised",
                    "purpose": "Source values aligned to country, indicator, sector, and timestamp.",
                    "primary_keys": ["date", "country", "indicator_code", "sector"],
                    "retention": "version transformations; preserve pre-restatement values",
                },
                {
                    "layer": "features",
                    "purpose": "Rolling z-scores, percentiles, changes, lagged values, quality scores, and source freshness.",
                    "primary_keys": ["date", "country", "indicator_code", "feature_name"],
                    "retention": "recomputeable but stored for audit and backtest reproducibility",
                },
                {
                    "layer": "model_outputs",
                    "purpose": "Sector rankings, Granger edges, validation snapshots, alerts, and generated reports.",
                    "primary_keys": ["run_id", "country", "model_name", "as_of"],
                    "retention": "append-only journal for audit, RAG retrieval, and post-trade review",
                },
            ],
            "execution_controls": [
                "Run each connector only after source access, terms, and privacy review are recorded.",
                "Fail closed when schema hashes change, row counts drop materially, or freshness SLAs are missed.",
                "Store collector version, transformation version, source timestamp, row counts, and error details for every run.",
                "Do not promote an indicator into live ranking unless backtest validation and source reliability exceed thresholds.",
            ],
            "blockers": blockers[:12],
        }

    def ingestion_status(self, country: str = "US") -> dict[str, Any]:
        config = self._country(country)
        pipeline = self.pipeline_status(config.code)
        runtime = sector_ingestion_store.summary(config.code)
        filtered_runtime_observations = [
            row
            for row in sector_ingestion_store.load_observations(config.code, limit=20_000)
            if not self._is_deprecated_runtime_stub(config, row)
        ]
        if len(filtered_runtime_observations) != int(runtime.get("observation_count") or 0):
            recent_runs_for_summary = sector_ingestion_store.load_runs(config.code, limit=20)
            runtime = {
                "observation_count": len(filtered_runtime_observations),
                "indicator_count": len({str(row.get("indicator_code")) for row in filtered_runtime_observations}),
                "sector_count": len({str(row.get("sector")) for row in filtered_runtime_observations}),
                "latest_observation_date": max((str(row.get("date")) for row in filtered_runtime_observations), default=None),
                "latest_created_at": max((str(row.get("created_at")) for row in filtered_runtime_observations), default=None),
                "run_count": len(recent_runs_for_summary),
                "last_run": recent_runs_for_summary[0] if recent_runs_for_summary else None,
            }
        connector_rows = []
        recent_runs = sector_ingestion_store.load_runs(config.code, limit=8)
        recent_observations = [
            row
            for row in sector_ingestion_store.load_observations(config.code, limit=200)
            if not self._is_deprecated_runtime_stub(config, row)
        ][:12]
        latest_by_indicator: dict[str, dict[str, Any]] = {}
        for row in sector_ingestion_store.load_observations(config.code, limit=20_000):
            if self._is_deprecated_runtime_stub(config, row):
                continue
            code = str(row.get("indicator_code"))
            if code not in latest_by_indicator:
                latest_by_indicator[code] = row
        for connector in pipeline["connectors"]:
            latest = latest_by_indicator.get(str(connector["indicator_code"]))
            connector_rows.append(
                {
                    **connector,
                    "latest_observation_date": latest.get("date") if latest else None,
                    "latest_value": round(float(latest.get("value", 0.0)), 4) if latest else None,
                    "has_runtime_data": latest is not None,
                }
            )
        return {
            "country": config.code,
            "label": config.label,
            "runtime_root": str(sector_ingestion_store.root),
            "storage_status": sector_ingestion_store.storage_status(),
            "runtime_summary": runtime,
            "connectors": connector_rows,
            "recent_runs": recent_runs,
            "recent_observations": recent_observations,
            "promotion_rules": [
                "Connector must have documented source access and compliance state approved.",
                "At least 24 monthly-equivalent observations are required before model validation.",
                "Schema drift, freshness misses, or row-count cliffs block promotion into live rankings.",
                "Live observations are append-only and model outputs must reference collector run IDs.",
            ],
        }

    def run_ingestion(self, country: str = "US", *, dry_run: bool = True, include_prototype: bool = False) -> dict[str, Any]:
        config = self._country(country)
        started_at = sector_ingestion_store.now_iso()
        run_id = sector_ingestion_store.build_run_id()
        observations: list[SectorObservation] = []
        blocked_connectors: list[dict[str, Any]] = []
        errors: list[str] = []
        if config.code == "IN":
            from sector_interaction.india_public_collectors import india_public_data_collector

            indicators = self._indicator_configs(config)
            result = india_public_data_collector.collect(config=config, indicators=indicators, run_id=run_id)
            observations.extend(result.observations)
            blocked_connectors.extend(result.blocked_connectors)
            errors.extend(result.errors)
            for indicator in indicators:
                can_collect = indicator.source_status == "open_data"
                if can_collect:
                    continue
                blocked_connectors.append(
                    {
                        "indicator_code": indicator.code,
                        "label": indicator.label,
                        "source_status": indicator.source_status,
                        "reason": "blocked until source access, license, terms review, or dedicated live-market ingestion is used",
                    }
                )
            stored = 0 if dry_run else sector_ingestion_store.append_observations(observations)
            finished_at = sector_ingestion_store.now_iso()
            run = CollectorRun(
                run_id=run_id,
                country=config.code,
                mode="india_public_dry_run" if dry_run else "india_public_append",
                started_at=started_at,
                finished_at=finished_at,
                status="success" if not errors else "partial_failure",
                attempted_connectors=len(indicators),
                stored_observations=stored,
                blocked_connectors=blocked_connectors,
                errors=errors,
                collector_version="sector-ingestion-india-public-v1",
            )
            if not dry_run:
                sector_ingestion_store.append_run(run)
                self.signals.cache_clear()
                self.extended_network.cache_clear()
                self.validation_backtest.cache_clear()
            return {
                "country": config.code,
                "label": config.label,
                "run": run.__dict__,
                "dry_run": dry_run,
                "preview_observations": [observation.__dict__ for observation in observations[:12]],
                "generated_observations": len(observations),
                "stored_observations": stored,
                "blocked_connectors": blocked_connectors,
                "message": (
                    "Dry run only; no India public-source observations were stored."
                    if dry_run
                    else f"Stored {stored} India public-source observations."
                ),
            }
        for indicator in self._indicator_configs(config):
            can_collect = indicator.source_status == "open_data" or (include_prototype and indicator.source_status == "prototype")
            if not can_collect:
                blocked_connectors.append(
                    {
                        "indicator_code": indicator.code,
                        "label": indicator.label,
                        "source_status": indicator.source_status,
                        "reason": "blocked until source access, license, or terms review is complete",
                    }
                )
                continue
            observations.extend(self._build_runtime_observations(config, indicator, run_id))
        stored = 0 if dry_run else sector_ingestion_store.append_observations(observations)
        finished_at = sector_ingestion_store.now_iso()
        run = CollectorRun(
            run_id=run_id,
            country=config.code,
            mode="dry_run" if dry_run else "append",
            started_at=started_at,
            finished_at=finished_at,
            status="success" if not errors else "partial_failure",
            attempted_connectors=len(self._indicator_configs(config)),
            stored_observations=stored,
            blocked_connectors=blocked_connectors,
            errors=errors,
        )
        if not dry_run:
            sector_ingestion_store.append_run(run)
            self.signals.cache_clear()
            self.extended_network.cache_clear()
            self.validation_backtest.cache_clear()
        return {
            "country": config.code,
            "label": config.label,
            "run": run.__dict__,
            "dry_run": dry_run,
            "preview_observations": [observation.__dict__ for observation in observations[:12]],
            "generated_observations": len(observations),
            "stored_observations": stored,
            "blocked_connectors": blocked_connectors,
            "message": "Dry run only; no observations were stored." if dry_run else f"Stored {stored} observations.",
        }

    async def run_india_live_market_ingestion(self, *, dry_run: bool = True) -> dict[str, Any]:
        config = self._country("IN")
        started_at = sector_ingestion_store.now_iso()
        run_id = sector_ingestion_store.build_run_id()
        observations = await self._build_india_live_market_observations(config, run_id)
        stored = 0 if dry_run else sector_ingestion_store.append_observations(observations)
        finished_at = sector_ingestion_store.now_iso()
        run = CollectorRun(
            run_id=run_id,
            country=config.code,
            mode="india_live_market_dry_run" if dry_run else "india_live_market_append",
            started_at=started_at,
            finished_at=finished_at,
            status="success",
            attempted_connectors=4,
            stored_observations=stored,
            blocked_connectors=[],
            errors=[],
            collector_version="sector-ingestion-india-live-market-v1",
        )
        if not dry_run:
            sector_ingestion_store.append_run(run)
            self.signals.cache_clear()
            self.extended_network.cache_clear()
            self.validation_backtest.cache_clear()
        return {
            "country": config.code,
            "label": config.label,
            "run": run.__dict__,
            "dry_run": dry_run,
            "preview_observations": [observation.__dict__ for observation in observations[:12]],
            "generated_observations": len(observations),
            "stored_observations": stored,
            "blocked_connectors": [],
            "message": "Dry run only; no India live-market observations were stored." if dry_run else f"Stored {stored} India live-market observations.",
        }

    def sector_report(self, country: str = "US", periods: int = 160) -> dict[str, Any]:
        config = self._country(country)
        signals = self.signals(config.code, periods)
        network = self.extended_network(config.code, periods, 2, 0.05)
        backtest = self.validation_backtest(config.code, periods)
        pipeline = self.pipeline_status(config.code)
        rankings = signals["rankings"]
        top_overweights = [row for row in rankings if row["stance"] == "overweight"][:3]
        top_underweights = [row for row in rankings if row["stance"] == "underweight"][-3:]
        top_edges = network["indicator_edges"][:5]
        quality_warnings = [
            f"{blocker['label']}: {blocker['blocker']}"
            for blocker in pipeline["blockers"][:4]
        ]
        if not quality_warnings:
            quality_warnings = ["No critical connector blockers in the current source plan."]
        return {
            "country": config.code,
            "label": config.label,
            "as_of": signals["as_of"],
            "source_mode": signals["source_mode"],
            "headline": self._report_headline(config, top_overweights, top_underweights, backtest),
            "summary_bullets": [
                f"Top composite sector: {rankings[0]['sector']} at {rankings[0]['score']:+.2f} ({rankings[0]['stance']}).",
                f"Weakest composite sector: {rankings[-1]['sector']} at {rankings[-1]['score']:+.2f} ({rankings[-1]['stance']}).",
                f"Indicator network found {network['summary']['indicator_edge_count']} alternative-data lead-lag edges at alpha {network['alpha']}.",
                f"Validation hit rate is {backtest['summary']['hit_rate_pct']:.1f}% across {backtest['summary']['observations']} synthetic monthly windows.",
                f"Production connector readiness score is {pipeline['summary']['readiness_score']:.2f}; {pipeline['summary']['critical_blockers']} blockers remain.",
            ],
            "top_overweights": top_overweights,
            "top_underweights": top_underweights,
            "strongest_indicator_edges": top_edges,
            "risk_flags": quality_warnings,
            "next_actions": self._report_next_actions(pipeline),
            "disclaimer": "Prototype research output. Current signals use deterministic synthetic data until approved production feeds are connected and validated.",
        }

    @lru_cache(maxsize=16)
    def model(self, country: str = "US", periods: int = 160, max_lag: int = 2, alpha: float = 0.05) -> dict[str, Any]:
        config = self._country(country)
        periods = max(48, min(int(periods), 500))
        max_lag = max(1, min(int(max_lag), 6))
        returns = self._simulate_returns(config, periods)
        selected_lag = self._select_lag_aic(returns, max_lag=max_lag)
        edges = self._granger_edges(returns, lag=selected_lag, alpha=alpha)
        corr = returns.corr()
        centrality = self._centrality(config.sectors, edges)
        return {
            "country": config.code,
            "label": config.label,
            "source_mode": "synthetic",
            "source_note": config.source_note,
            "periods": periods,
            "selected_lag": selected_lag,
            "alpha": alpha,
            "sectors": config.sectors,
            "correlation_matrix": self._matrix_payload(corr),
            "network": {
                "nodes": [
                    {
                        "id": sector,
                        "label": sector,
                        **centrality[sector],
                    }
                    for sector in config.sectors
                ],
                "edges": edges,
            },
            "rankings": {
                "leaders": sorted(centrality.values(), key=lambda row: row["net_influence"], reverse=True),
                "followers": sorted(centrality.values(), key=lambda row: row["incoming_weight"], reverse=True),
            },
            "dashboard_panels": [
                "sector correlation heatmap",
                "directed Granger network",
                "leader/follower ranking table",
                "edge p-value table",
                "alternative-data source coverage",
            ],
        }

    def rag_documents(self) -> list[dict[str, Any]]:
        plan = self.acquisition_plan()
        return [
            {
                "id": "playbook-sector-interaction-var-granger",
                "collection": "playbooks",
                "title": "Sector interaction VAR and Granger model",
                "source": "sector_interaction_service",
                "metadata": {"strategy_key": "sector_interaction", "scope": "model", "tags": ["VAR", "Granger", "sectors"]},
                "text": (
                    "Use aligned sector returns to fit VAR models, select lag by AIC, convert significant pairwise "
                    "Granger p-values into directed edge weights, and rank sectors by outgoing minus incoming influence."
                ),
            },
            {
                "id": "playbook-sector-alternative-data-pipeline",
                "collection": "playbooks",
                "title": "Alternative-data acquisition plan",
                "source": "sector_interaction_service",
                "metadata": {"strategy_key": "sector_interaction", "scope": "alternative-data", "tags": ["sources", "pipeline", "compliance"]},
                "text": " ".join(plan["architecture"] + plan["processing"] + plan["legal_controls"]),
            },
        ]

    def _connector_status(self, indicator: IndicatorConfig, index: int) -> dict[str, Any]:
        status_profile = {
            "open_data": {
                "base_readiness": 0.82,
                "compliance_state": "approved_open_data",
                "blockers": [],
                "next_action": "Implement incremental collector and schema drift checks.",
            },
            "internal_live_market_data": {
                "base_readiness": 0.9,
                "compliance_state": "approved_internal_market_data",
                "blockers": [],
                "next_action": "Schedule append runs and monitor live snapshot freshness.",
            },
            "prototype": {
                "base_readiness": 0.58,
                "compliance_state": "research_only",
                "blockers": ["production source and permitted access method not finalised"],
                "next_action": "Replace placeholder generator with approved API, vendor feed, or documented open dataset.",
            },
            "licensed_required": {
                "base_readiness": 0.42,
                "compliance_state": "license_required",
                "blockers": ["commercial data license required before production use"],
                "next_action": "Select vendor, confirm coverage, negotiate usage rights, and store license metadata.",
            },
            "tos_review_required": {
                "base_readiness": 0.36,
                "compliance_state": "terms_review_required",
                "blockers": ["terms-of-service review required before automated collection"],
                "next_action": "Confirm permitted API/scraping method and document rate limits, robots guidance, and privacy controls.",
            },
        }
        profile = status_profile.get(indicator.source_status, status_profile["prototype"])
        cadence_hours = {
            "daily": 24,
            "weekly": 7 * 24,
            "monthly": 31 * 24,
            "quarterly": 92 * 24,
        }.get(indicator.cadence, 7 * 24)
        readiness = min(0.98, float(profile["base_readiness"]) * (0.65 + indicator.quality_score * 0.35))
        rows_loaded = int((index + 3) * 137 * (1.0 + indicator.quality_score))
        freshness_lag_hours = int(cadence_hours * (0.25 + (index % 4) * 0.18))
        run_status = "ready" if not profile["blockers"] else "blocked" if indicator.source_status in {"licensed_required", "tos_review_required"} else "prototype"
        return {
            "indicator_code": indicator.code,
            "label": indicator.label,
            "category": indicator.category,
            "cadence": indicator.cadence,
            "source_status": indicator.source_status,
            "production_source": indicator.production_source,
            "metric_definition": indicator.metric_definition,
            "compliance_state": profile["compliance_state"],
            "run_status": run_status,
            "readiness_score": round(readiness, 3),
            "quality_score": indicator.quality_score,
            "schedule": self._schedule_for_cadence(indicator.cadence),
            "freshness_sla_hours": cadence_hours,
            "freshness_lag_hours": freshness_lag_hours,
            "rows_loaded_30d": rows_loaded,
            "mapped_sector_count": len(indicator.sector_weights),
            "blockers": list(profile["blockers"]),
            "next_action": profile["next_action"],
        }

    def _build_runtime_observations(
        self,
        config: CountryConfig,
        indicator: IndicatorConfig,
        run_id: str,
    ) -> list[SectorObservation]:
        today = pd.Timestamp.now("UTC").date().isoformat()
        created_at = sector_ingestion_store.now_iso()
        observations: list[SectorObservation] = []
        for sector, exposure in indicator.sector_weights.items():
            if sector not in config.sectors:
                continue
            value = self._deterministic_observation_value(config.code, indicator.code, sector)
            observations.append(
                SectorObservation(
                    date=today,
                    country=config.code,
                    indicator_code=indicator.code,
                    sector=sector,
                    value=round(value * float(exposure), 6),
                    quality_score=indicator.quality_score,
                    source=indicator.production_source,
                    source_status=indicator.source_status,
                    collector_version="sector-ingestion-v1",
                    run_id=run_id,
                    created_at=created_at,
                    metadata={
                        "category": indicator.category,
                        "cadence": indicator.cadence,
                        "metric_definition": indicator.metric_definition,
                        "configured_exposure": exposure,
                        "mode": "open_data_runtime_stub" if indicator.source_status == "open_data" else "prototype_runtime_stub",
                    },
                )
            )
        return observations

    async def _build_india_live_market_observations(
        self,
        config: CountryConfig,
        run_id: str,
    ) -> list[SectorObservation]:
        from sector_interaction.india_live import india_live_sector_service

        overview = await india_live_sector_service.overview()
        indicators = {
            indicator.code: indicator
            for indicator in self._indicator_configs(config)
            if indicator.source_status == "internal_live_market_data"
        }
        metric_by_indicator = {
            "india_live_leadership_score": "leadership_score",
            "india_live_price_momentum": "avg_change_pct",
            "india_live_option_oi_momentum": "avg_oi_change_pct",
            "india_live_iv_pressure": "avg_iv",
        }
        today = pd.Timestamp.now("UTC").date().isoformat()
        created_at = sector_ingestion_store.now_iso()
        observations: list[SectorObservation] = []
        for sector_row in overview.get("sectors") or []:
            sector = str(sector_row.get("sector") or "")
            if sector not in config.sectors:
                continue
            for indicator_code, metric_key in metric_by_indicator.items():
                indicator = indicators.get(indicator_code)
                if indicator is None:
                    continue
                exposure = float(indicator.sector_weights.get(sector, 0.0))
                if abs(exposure) <= 1e-9:
                    continue
                value = float(sector_row.get(metric_key) or 0.0)
                observations.append(
                    SectorObservation(
                        date=today,
                        country=config.code,
                        indicator_code=indicator.code,
                        sector=sector,
                        value=round(value * exposure, 6),
                        quality_score=indicator.quality_score,
                        source=indicator.production_source,
                        source_status=indicator.source_status,
                        collector_version="sector-ingestion-india-live-market-v1",
                        run_id=run_id,
                        created_at=created_at,
                        metadata={
                            "category": indicator.category,
                            "cadence": indicator.cadence,
                            "metric_definition": indicator.metric_definition,
                            "configured_exposure": exposure,
                            "mode": "india_live_market_observation",
                            "source_mode": overview.get("source_mode"),
                            "metric_key": metric_key,
                        },
                    )
                )
        return observations

    def _deterministic_observation_value(self, country: str, indicator_code: str, sector: str) -> float:
        raw = f"{country}:{indicator_code}:{sector}:{pd.Timestamp.now('UTC').date().isoformat()}"
        checksum = sum((index + 1) * ord(char) for index, char in enumerate(raw))
        return ((checksum % 401) - 200) / 100.0

    def _schedule_for_cadence(self, cadence: str) -> str:
        if cadence == "daily":
            return "02:30 local market time, every trading day"
        if cadence == "weekly":
            return "Saturday 06:00 local time"
        if cadence == "monthly":
            return "First weekend after source release"
        if cadence == "quarterly":
            return "First weekend after quarterly source release"
        return "Manual until cadence is confirmed"

    def _report_headline(
        self,
        config: CountryConfig,
        top_overweights: list[dict[str, Any]],
        top_underweights: list[dict[str, Any]],
        backtest: dict[str, Any],
    ) -> str:
        leader = top_overweights[0]["sector"] if top_overweights else "no clear overweight"
        laggard = top_underweights[0]["sector"] if top_underweights else "no clear underweight"
        hit_rate = float(backtest["summary"]["hit_rate_pct"])
        if top_underweights:
            return f"{config.label}: {leader} leads the composite tape while {laggard} lags; validation hit rate {hit_rate:.1f}%."
        return f"{config.label}: {leader} leads the composite tape with no clear underweight; validation hit rate {hit_rate:.1f}%."

    def _report_next_actions(self, pipeline: dict[str, Any]) -> list[str]:
        blockers = pipeline["blockers"]
        if not blockers:
            return [
                "Promote open-data connectors into scheduled collection.",
                "Start live-vs-backtest drift monitoring before using signals in trading decisions.",
                "Seed generated report snapshots into RAG memory after each validated run.",
            ]
        actions = []
        seen: set[str] = set()
        for blocker in blockers:
            action = str(blocker["next_action"])
            if action in seen:
                continue
            actions.append(action)
            seen.add(action)
            if len(actions) >= 4:
                break
        return actions

    def _country(self, country: str) -> CountryConfig:
        key = str(country or "US").upper()
        if key in {"INDIA", "INR", "NSE"}:
            key = "IN"
        if key in {"UNITED_STATES", "USA", "US"}:
            key = "US"
        if key not in COUNTRIES:
            raise ValueError(f"Unsupported country '{country}'. Use US or IN.")
        return COUNTRIES[key]

    def _indicator_configs(self, config: CountryConfig) -> list[IndicatorConfig]:
        return INDICATORS_BY_COUNTRY[config.code]

    def _runtime_indicator_source(self, config: CountryConfig, *, min_dates: int) -> dict[str, Any]:
        frame = self._runtime_indicator_frame(config)
        observed_dates = 0 if frame is None else len(frame.index)
        indicator_count = 0 if frame is None else len(frame.columns)
        handoff = {
            "active": frame is not None and observed_dates >= min_dates,
            "observed_dates": observed_dates,
            "required_dates": min_dates,
            "indicator_count": indicator_count,
            "source": "durable_ingestion_store",
            "reason": "runtime history ready" if frame is not None and observed_dates >= min_dates else "insufficient runtime history",
        }
        if frame is None or observed_dates < min_dates:
            return {"active": False, "indicators": None, "handoff": handoff}
        return {"active": True, "indicators": frame.tail(500), "handoff": handoff}

    def _runtime_indicator_frame(self, config: CountryConfig) -> pd.DataFrame | None:
        rows = sector_ingestion_store.load_observations(config.code, limit=20_000)
        if not rows:
            return None
        records: list[dict[str, Any]] = []
        valid_codes = {indicator.code for indicator in self._indicator_configs(config)}
        for row in rows:
            if self._is_deprecated_runtime_stub(config, row):
                continue
            indicator_code = str(row.get("indicator_code") or "")
            if indicator_code not in valid_codes:
                continue
            try:
                date = pd.Timestamp(str(row.get("date"))).normalize()
                value = float(row.get("value") or 0.0)
                metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
                exposure = float(metadata.get("configured_exposure") or 0.0)
            except (TypeError, ValueError):
                continue
            if abs(exposure) <= 1e-9:
                continue
            records.append(
                {
                    "date": date,
                    "indicator_code": indicator_code,
                    "base_value": value / exposure,
                }
            )
        if not records:
            return None
        frame = pd.DataFrame(records)
        pivot = frame.pivot_table(index="date", columns="indicator_code", values="base_value", aggfunc="mean").sort_index()
        if pivot.empty:
            return None
        for indicator in self._indicator_configs(config):
            if indicator.code not in pivot:
                pivot[indicator.code] = 0.0
        pivot = pivot[[indicator.code for indicator in self._indicator_configs(config)]].ffill().fillna(0.0)
        if len(pivot.index) > 1:
            pivot = pivot.apply(lambda column: pd.Series(self._zscore_array(column.to_numpy(dtype=float)), index=pivot.index))
        return pivot

    def _is_deprecated_runtime_stub(self, config: CountryConfig, row: dict[str, Any]) -> bool:
        if config.code != "IN":
            return False
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        mode = str(metadata.get("mode") or "")
        return mode in {"open_data_runtime_stub", "prototype_runtime_stub"}

    def _simulate_returns(self, config: CountryConfig, periods: int) -> pd.DataFrame:
        rng = np.random.default_rng(config.seed + periods)
        n = len(config.sectors)
        coefficients = np.zeros((n, n), dtype=float)
        np.fill_diagonal(coefficients, rng.uniform(0.08, 0.22, size=n))
        for source, target, weight in self._seed_edges(config.code, config.sectors):
            coefficients[config.sectors.index(target), config.sectors.index(source)] = weight
        coefficients += rng.normal(0.0, 0.018, size=(n, n))
        spectral_radius = max(abs(np.linalg.eigvals(coefficients)))
        if spectral_radius >= 0.82:
            coefficients *= 0.82 / spectral_radius

        shocks = rng.normal(0.0, 0.028, size=(periods + 40, n))
        common = rng.normal(0.0, 0.012, size=periods + 40)
        values = np.zeros((periods + 40, n), dtype=float)
        for t in range(1, periods + 40):
            values[t] = coefficients @ values[t - 1] + shocks[t] + common[t]
        dates = pd.date_range("2012-04-30", periods=periods, freq="ME")
        return pd.DataFrame(values[-periods:], index=dates, columns=config.sectors)

    def _simulate_indicators(self, config: CountryConfig, returns: pd.DataFrame) -> pd.DataFrame:
        rng = np.random.default_rng(config.seed + len(returns) + 10_000)
        indicators: dict[str, np.ndarray] = {}
        for indicator in self._indicator_configs(config):
            values = np.zeros(len(returns), dtype=float)
            for sector, weight in indicator.sector_weights.items():
                if sector not in returns:
                    continue
                lead = max(1, int(indicator.lead_months))
                sector_values = returns[sector].shift(-lead).fillna(returns[sector].mean()).to_numpy(dtype=float)
                values += float(weight) * sector_values * 18.0
            values += rng.normal(0.0, max(0.18, 0.7 - indicator.quality_score * 0.35), size=len(returns))
            smoothed = np.zeros_like(values)
            for index, value in enumerate(values):
                smoothed[index] = value if index == 0 else (0.68 * smoothed[index - 1]) + (0.32 * value)
            indicators[indicator.code] = self._zscore_array(smoothed)
        return pd.DataFrame(indicators, index=returns.index)

    def _sector_signal_frame(self, config: CountryConfig, indicators: pd.DataFrame) -> pd.DataFrame:
        indicator_by_code = {indicator.code: indicator for indicator in self._indicator_configs(config)}
        rolling_z = indicators.rolling(24, min_periods=8).apply(lambda values: self._last_z(values), raw=True).fillna(0.0)
        sector_scores: dict[str, pd.Series] = {}
        for sector in config.sectors:
            weighted = []
            weights = []
            for code, indicator in indicator_by_code.items():
                exposure = float(indicator.sector_weights.get(sector, 0.0))
                if abs(exposure) <= 1e-9:
                    continue
                weight = abs(exposure) * indicator.quality_score
                weighted.append(rolling_z[code] * exposure * indicator.quality_score)
                weights.append(weight)
            if not weighted:
                sector_scores[sector] = pd.Series(0.0, index=indicators.index)
                continue
            sector_scores[sector] = sum(weighted) / max(sum(weights), 1e-9)
        return pd.DataFrame(sector_scores, index=indicators.index).clip(-3.0, 3.0)

    def _indicator_latest_payload(self, config: CountryConfig, indicators: pd.DataFrame) -> list[dict[str, Any]]:
        rolling_z = indicators.rolling(24, min_periods=8).apply(lambda values: self._last_z(values), raw=True).fillna(0.0)
        rows = []
        for indicator in self._indicator_configs(config):
            latest = float(rolling_z[indicator.code].iloc[-1])
            rows.append(
                {
                    "code": indicator.code,
                    "label": indicator.label,
                    "category": indicator.category,
                    "latest_z": round(latest, 4),
                    "quality_score": indicator.quality_score,
                    "source_status": indicator.source_status,
                    "cadence": indicator.cadence,
                    "mapped_sectors": indicator.sector_weights,
                    "signal_state": "positive" if latest >= 0.35 else "negative" if latest <= -0.35 else "neutral",
                }
            )
        rows.sort(key=lambda row: abs(float(row["latest_z"])), reverse=True)
        return rows

    def _top_sector_drivers(self, config: CountryConfig, indicators: pd.DataFrame, sector: str) -> list[dict[str, Any]]:
        latest = self._indicator_latest_payload(config, indicators)
        drivers = []
        by_code = {indicator.code: indicator for indicator in self._indicator_configs(config)}
        for row in latest:
            indicator = by_code[row["code"]]
            exposure = float(indicator.sector_weights.get(sector, 0.0))
            if abs(exposure) <= 1e-9:
                continue
            contribution = exposure * float(row["latest_z"]) * indicator.quality_score
            drivers.append(
                {
                    "indicator": row["label"],
                    "category": row["category"],
                    "contribution": round(contribution, 4),
                    "latest_z": row["latest_z"],
                }
            )
        drivers.sort(key=lambda item: abs(float(item["contribution"])), reverse=True)
        return drivers[:3]

    def _indicator_edges(
        self,
        config: CountryConfig,
        returns: pd.DataFrame,
        indicators: pd.DataFrame,
        *,
        lag: int,
        alpha: float,
    ) -> list[dict[str, Any]]:
        indicator_by_code = {indicator.code: indicator for indicator in self._indicator_configs(config)}
        edges: list[dict[str, Any]] = []
        for code in indicators.columns:
            source = indicators[code].to_numpy(dtype=float)
            indicator = indicator_by_code[code]
            for sector in config.sectors:
                target = returns[sector].to_numpy(dtype=float)
                p_value = self._pairwise_granger_pvalue(source, target, lag)
                if p_value <= alpha:
                    weight = float(-np.log10(max(p_value, 1e-12)))
                    edges.append(
                        {
                            "source": code,
                            "target": sector,
                            "source_type": "indicator",
                            "target_type": "sector",
                            "category": indicator.category,
                            "p_value": round(float(p_value), 6),
                            "weight": round(weight, 4),
                            "lag": lag,
                            "relationship": f"{indicator.label} leads {sector}",
                            "configured_exposure": round(float(indicator.sector_weights.get(sector, 0.0)), 4),
                        }
                    )
        edges.sort(key=lambda row: (row["configured_exposure"] == 0.0, -row["weight"]))
        return edges[:28]

    def _zscore_array(self, values: np.ndarray) -> np.ndarray:
        mean = float(np.mean(values))
        stdev = float(np.std(values))
        if stdev <= 1e-9:
            return np.zeros_like(values)
        return (values - mean) / stdev

    def _last_z(self, values: np.ndarray) -> float:
        values = np.asarray(values, dtype=float)
        stdev = float(np.std(values))
        if stdev <= 1e-9:
            return 0.0
        return float((values[-1] - float(np.mean(values))) / stdev)

    def _max_drawdown(self, cumulative_returns: list[float]) -> float:
        if not cumulative_returns:
            return 0.0
        equity = np.asarray([1.0 + value for value in cumulative_returns], dtype=float)
        running_max = np.maximum.accumulate(equity)
        drawdowns = (running_max - equity) / np.maximum(running_max, 1e-9)
        return float(np.max(drawdowns)) if len(drawdowns) else 0.0

    def _seed_edges(self, code: str, sectors: list[str]) -> list[tuple[str, str, float]]:
        del sectors
        if code == "US":
            return [
                ("Technology", "Communication Services", 0.16),
                ("Technology", "Consumer Discretionary", 0.11),
                ("Energy", "Materials", 0.13),
                ("Financials", "Real Estate", 0.12),
                ("Industrials", "Materials", 0.1),
                ("Consumer Staples", "Utilities", 0.08),
            ]
        return [
            ("Nifty Bank", "Nifty Financial Services", 0.18),
            ("Nifty Metal", "Nifty Auto", 0.1),
            ("Nifty IT", "Nifty Media", 0.09),
            ("Nifty Financial Services", "Nifty Realty", 0.12),
            ("Nifty FMCG", "Nifty Pharma", 0.08),
        ]

    def _select_lag_aic(self, returns: pd.DataFrame, max_lag: int) -> int:
        scores = [(lag, self._var_aic(returns, lag)) for lag in range(1, max_lag + 1)]
        return min(scores, key=lambda item: item[1])[0]

    def _var_aic(self, returns: pd.DataFrame, lag: int) -> float:
        y, x = self._lagged_design(returns, lag)
        beta = np.linalg.lstsq(x, y, rcond=None)[0]
        with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
            residual = y - x @ beta
            residual = np.nan_to_num(residual, nan=0.0, posinf=0.0, neginf=0.0)
            sigma = (residual.T @ residual) / max(len(y), 1)
            sigma = np.nan_to_num(sigma, nan=0.0, posinf=1e6, neginf=0.0)
        sign, logdet = np.linalg.slogdet(sigma + np.eye(sigma.shape[0]) * 1e-9)
        if sign <= 0:
            logdet = float(np.log(np.maximum(np.linalg.det(sigma + np.eye(sigma.shape[0]) * 1e-6), 1e-12)))
        parameters = x.shape[1] * y.shape[1]
        return float(logdet + (2.0 * parameters / max(len(y), 1)))

    def _lagged_design(self, returns: pd.DataFrame, lag: int) -> tuple[np.ndarray, np.ndarray]:
        values = returns.to_numpy(dtype=float)
        y = values[lag:]
        rows = []
        for t in range(lag, len(values)):
            lagged = [values[t - step] for step in range(1, lag + 1)]
            rows.append(np.concatenate([np.ones(1), *lagged]))
        return y, np.vstack(rows)

    def _granger_edges(self, returns: pd.DataFrame, lag: int, alpha: float) -> list[dict[str, Any]]:
        sectors = list(returns.columns)
        values = returns.to_numpy(dtype=float)
        edges: list[dict[str, Any]] = []
        for source_index, source in enumerate(sectors):
            for target_index, target in enumerate(sectors):
                if source_index == target_index:
                    continue
                p_value = self._pairwise_granger_pvalue(values[:, source_index], values[:, target_index], lag)
                if p_value <= alpha:
                    edges.append(
                        {
                            "source": source,
                            "target": target,
                            "p_value": round(float(p_value), 6),
                            "weight": round(float(-np.log10(max(p_value, 1e-12))), 4),
                            "lag": lag,
                            "relationship": f"{source} leads {target}",
                        }
                    )
        edges.sort(key=lambda row: row["weight"], reverse=True)
        return edges

    def _pairwise_granger_pvalue(self, source: np.ndarray, target: np.ndarray, lag: int) -> float:
        y = target[lag:]
        unrestricted_rows = []
        restricted_rows = []
        for t in range(lag, len(target)):
            target_lags = [target[t - step] for step in range(1, lag + 1)]
            source_lags = [source[t - step] for step in range(1, lag + 1)]
            restricted_rows.append([1.0, *target_lags])
            unrestricted_rows.append([1.0, *target_lags, *source_lags])
        xr = np.asarray(restricted_rows, dtype=float)
        xu = np.asarray(unrestricted_rows, dtype=float)
        rss_r = self._rss(y, xr)
        rss_u = self._rss(y, xu)
        df_num = lag
        df_den = max(len(y) - xu.shape[1], 1)
        if rss_u <= 1e-12 or rss_r <= rss_u:
            return 1.0
        f_stat = ((rss_r - rss_u) / df_num) / (rss_u / df_den)
        return float(1.0 - f_dist.cdf(max(f_stat, 0.0), df_num, df_den))

    def _rss(self, y: np.ndarray, x: np.ndarray) -> float:
        beta = np.linalg.lstsq(x, y, rcond=None)[0]
        residual = y - x @ beta
        return float(np.sum(residual * residual))

    def _centrality(self, sectors: list[str], edges: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        rows = {
            sector: {
                "sector": sector,
                "outgoing_weight": 0.0,
                "incoming_weight": 0.0,
                "outgoing_edges": 0,
                "incoming_edges": 0,
                "net_influence": 0.0,
            }
            for sector in sectors
        }
        for edge in edges:
            source = rows[edge["source"]]
            target = rows[edge["target"]]
            source["outgoing_weight"] += edge["weight"]
            source["outgoing_edges"] += 1
            target["incoming_weight"] += edge["weight"]
            target["incoming_edges"] += 1
        for row in rows.values():
            row["outgoing_weight"] = round(row["outgoing_weight"], 4)
            row["incoming_weight"] = round(row["incoming_weight"], 4)
            row["net_influence"] = round(row["outgoing_weight"] - row["incoming_weight"], 4)
        return rows

    def _matrix_payload(self, matrix: pd.DataFrame) -> dict[str, Any]:
        return {
            "labels": list(matrix.columns),
            "values": [
                [round(float(value), 4) for value in row]
                for row in matrix.to_numpy()
            ],
        }


sector_interaction_service = SectorInteractionService()
