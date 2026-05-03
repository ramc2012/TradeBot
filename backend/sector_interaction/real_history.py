"""Real sector-index history adapter for sector interaction models."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from analytics.sector import SECTOR_CONFIGS, sector_tracker
from sector_interaction.service import sector_interaction_service


REAL_MODEL_MIN_PERIODS = 48


class RealSectorHistoryService:
    async def india_model(
        self,
        *,
        periods: int = 160,
        max_lag: int = 2,
        alpha: float = 0.05,
        timeframe: str = "daily",
    ) -> dict[str, Any]:
        returns, source, detail, close_counts = await self._load_india_returns(
            periods=max(int(periods), REAL_MODEL_MIN_PERIODS),
            timeframe=timeframe,
        )
        observed_periods = 0 if returns is None else len(returns.index)
        sectors_available = [] if returns is None else list(returns.columns)
        if returns is None or observed_periods < REAL_MODEL_MIN_PERIODS or len(sectors_available) < 4:
            return self._insufficient_payload(
                periods=periods,
                max_lag=max_lag,
                alpha=alpha,
                timeframe=timeframe,
                source=source,
                detail=detail,
                observed_periods=observed_periods,
                sectors_available=sectors_available,
                close_counts=close_counts,
            )

        max_lag = max(1, min(int(max_lag), 6))
        selected_lag = sector_interaction_service._select_lag_aic(returns, max_lag=max_lag)
        edges = sector_interaction_service._granger_edges(returns, lag=selected_lag, alpha=alpha)
        corr = returns.corr()
        centrality = sector_interaction_service._centrality(list(returns.columns), edges)
        return {
            "country": "IN",
            "label": "India",
            "source_mode": "real_sector_index_history",
            "source": source,
            "source_note": detail or "Real broker/NSE sector-index history loaded through the sector rotation history adapter.",
            "timeframe": timeframe,
            "periods": observed_periods,
            "requested_periods": periods,
            "selected_lag": selected_lag,
            "alpha": alpha,
            "sectors": list(returns.columns),
            "close_counts": close_counts,
            "correlation_matrix": sector_interaction_service._matrix_payload(corr),
            "network": {
                "nodes": [
                    {
                        "id": sector,
                        "label": sector,
                        **centrality[sector],
                    }
                    for sector in returns.columns
                ],
                "edges": edges,
            },
            "rankings": {
                "leaders": sorted(centrality.values(), key=lambda row: row["net_influence"], reverse=True),
                "followers": sorted(centrality.values(), key=lambda row: row["incoming_weight"], reverse=True),
            },
            "dashboard_panels": [
                "real sector-index correlation heatmap",
                "real directed Granger network",
                "leader/follower ranking table",
                "edge p-value table",
            ],
            "real_data_contract": {
                "synthetic_used": False,
                "minimum_periods": REAL_MODEL_MIN_PERIODS,
                "observed_periods": observed_periods,
                "sector_count": len(returns.columns),
            },
        }

    async def _load_india_returns(
        self,
        *,
        periods: int,
        timeframe: str,
    ) -> tuple[pd.DataFrame | None, str, str | None, dict[str, int]]:
        series_map, source, detail = await sector_tracker._load_index_series(timeframe)
        records: dict[str, pd.Series] = {}
        close_counts: dict[str, int] = {}
        for config in SECTOR_CONFIGS:
            if not config.upstox_symbol and not config.app_symbol.startswith("NSE:"):
                continue
            series = series_map.get(config.code)
            if not series:
                continue
            close_counts[config.label] = len(series)
            if len(series) < REAL_MODEL_MIN_PERIODS:
                continue
            rows = [
                (pd.Timestamp(ts).normalize(), float(close))
                for ts, close in series
                if close is not None and np.isfinite(float(close))
            ]
            if len(rows) < REAL_MODEL_MIN_PERIODS:
                continue
            frame = pd.DataFrame(rows, columns=["date", "close"]).drop_duplicates("date", keep="last")
            frame = frame.sort_values("date").tail(periods)
            records[config.label] = frame.set_index("date")["close"]

        if len(records) < 4:
            return None, source, detail, close_counts
        close_frame = pd.DataFrame(records).sort_index().ffill().dropna(how="any")
        returns = close_frame.pct_change().replace([np.inf, -np.inf], np.nan).dropna(how="any")
        if returns.empty:
            return None, source, detail, close_counts
        return returns.tail(periods), source, detail, close_counts

    def _insufficient_payload(
        self,
        *,
        periods: int,
        max_lag: int,
        alpha: float,
        timeframe: str,
        source: str,
        detail: str | None,
        observed_periods: int,
        sectors_available: list[str],
        close_counts: dict[str, int],
    ) -> dict[str, Any]:
        return {
            "country": "IN",
            "label": "India",
            "source_mode": "insufficient_real_sector_history",
            "source": source,
            "source_note": detail or "Real sector-index history is not sufficient for VAR/Granger estimation. Synthetic fallback is disabled for India.",
            "timeframe": timeframe,
            "periods": observed_periods,
            "requested_periods": periods,
            "selected_lag": None,
            "alpha": alpha,
            "sectors": sectors_available,
            "close_counts": close_counts,
            "correlation_matrix": {"labels": sectors_available, "values": []},
            "network": {"nodes": [], "edges": []},
            "rankings": {"leaders": [], "followers": []},
            "dashboard_panels": [],
            "real_data_contract": {
                "synthetic_used": False,
                "minimum_periods": REAL_MODEL_MIN_PERIODS,
                "observed_periods": observed_periods,
                "sector_count": len(sectors_available),
                "required_action": "Backfill daily NSE sector-index history for at least 48 aligned observations across four or more sectors.",
            },
        }


real_sector_history_service = RealSectorHistoryService()
