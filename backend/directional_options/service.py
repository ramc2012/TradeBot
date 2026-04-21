"""Service orchestrator for the directional long-options module."""
from __future__ import annotations

from dataclasses import asdict
from functools import lru_cache
from typing import Any, Optional

import pandas as pd

from directional_options.backtest import DirectionalOptionsBacktester
from directional_options.config import clone_default_config
from directional_options.data import DirectionalOptionsDataStore
from directional_options.features import FeatureEngine
from directional_options.paper import DirectionalOptionsPaperStore
from directional_options.regime import RegimeClassifier
from directional_options.risk import DirectionalOptionsRiskEngine
from directional_options.selector import OptionSelectionEngine
from directional_options.signals import DirectionalSignalEngine
from market_data.market_intelligence_runtime import market_intelligence_runtime


class DirectionalOptionsService:
    """Expose research, live snapshot, and paper-trading surfaces."""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or clone_default_config()
        self.store = DirectionalOptionsDataStore(self.config["data_root"])
        self.feature_engine = FeatureEngine(self.config["feature_engine"])
        self.regime = RegimeClassifier()
        self.signals = DirectionalSignalEngine(self.config["signal_engine"])
        self.selector = OptionSelectionEngine(self.store, self.config["selector"])
        self.risk = DirectionalOptionsRiskEngine(self.config["risk"])
        self.paper = DirectionalOptionsPaperStore(self.config["paper_trading"]["journal_root"])
        self.backtester = DirectionalOptionsBacktester(
            store=self.store,
            feature_engine=self.feature_engine,
            regime=self.regime,
            signals=self.signals,
            selector=self.selector,
            risk=self.risk,
            config=self.config,
        )

    def summary(self) -> dict[str, object]:
        from core.market_hours_paper_supervisor import market_hours_paper_supervisor
        from directional_options.dashboard import get_dashboard_mount_state

        available = [item for item in self.config["universe"] if item in self.store.available_underlyings()]
        automation = market_hours_paper_supervisor.get_runner_status("directional_options")
        return {
            "key": "directional_long_options",
            "label": self.config["label"],
            "description": self.config["description"],
            "underlyings": available,
            "timeframes": list(self.config["timeframes"]),
            "dashboard": get_dashboard_mount_state(),
            "auto_started": bool(automation.get("enabled") and automation.get("loop_active")),
            "automation": automation,
            "coverage": [self.store.coverage_summary(underlying) for underlying in available],
        }

    @lru_cache(maxsize=24)
    def workspace(
        self,
        underlying: str,
        timeframe: str,
        lookback_sessions: int,
    ) -> dict[str, object]:
        summary = self.summary()
        spot = self.store.load_spot_frame(underlying)
        latest_tradeable = self.store.latest_tradeable_timestamp(underlying)
        if latest_tradeable is not None:
            spot = spot.loc[spot["time"] <= latest_tradeable].reset_index(drop=True)
        feature_frame = self.feature_engine.build_frame(
            spot,
            timeframe,
            lookback_sessions=lookback_sessions,
        )
        snapshot = self._snapshot(underlying=underlying, timeframe=timeframe, feature_frame=feature_frame)
        backtest = self.backtester.run(
            underlying=underlying,
            timeframe=timeframe,
            lookback_sessions=lookback_sessions,
            feature_frame=feature_frame,
        )
        return {
            "module": summary,
            "selection": {
                "underlying": underlying,
                "timeframe": timeframe,
                "lookback_sessions": lookback_sessions,
            },
            "snapshot": snapshot,
            "backtest": backtest,
        }

    async def live_snapshot(
        self,
        underlying: str,
        timeframe: str,
        lookback_sessions: int,
    ) -> dict[str, object]:
        summary = self.summary()
        lookback_days = max(int(self.config["paper_trading"]["live_lookback_days"]), lookback_sessions)
        spot, history_source, history_symbol = await self.store.load_live_spot_frame(
            underlying,
            lookback_days=lookback_days,
        )
        feature_frame = self.feature_engine.build_frame(
            spot,
            timeframe,
            lookback_sessions=lookback_sessions,
        )
        strategy_health = await market_intelligence_runtime.get_strategy_health()
        snapshot = await self._live_snapshot(
            underlying=underlying,
            timeframe=timeframe,
            feature_frame=feature_frame,
            strategy_health=strategy_health,
            history_source=history_source,
            history_symbol=history_symbol,
        )
        return {
            "module": summary,
            "selection": {
                "underlying": underlying,
                "timeframe": timeframe,
                "lookback_sessions": lookback_sessions,
            },
            "snapshot": snapshot,
            "paper_positions": await self.paper.list_positions(symbol=underlying, status="all", limit=8),
            "paper_journal": await self.paper.list_journal(symbol=underlying, limit=8),
        }

    async def record_paper_snapshot(
        self,
        underlying: str,
        timeframe: str,
        lookback_sessions: int,
    ) -> dict[str, object]:
        payload = await self.live_snapshot(underlying, timeframe, lookback_sessions)
        open_positions = await self.paper.list_positions(symbol=underlying, status="open", limit=50)
        position_marks: dict[str, dict[str, object]] = {}
        for row in open_positions.get("open_positions", []):
            premium, mark_time, price_source = await self.store.latest_local_option_mark(
                underlying=str(row.get("underlying") or underlying),
                expiry=str(row.get("expiry") or ""),
                strike=float(row.get("strike") or 0.0),
                option_type=str(row.get("option_type") or ""),
                instrument_key=str(row.get("instrument_key") or "") or None,
            )
            if premium is None:
                continue
            position_marks[str(row.get("position_id") or "")] = {
                "premium": premium,
                "spot": float(payload["snapshot"].get("spot_price") or row.get("latest_spot") or 0.0),
                "mark_time": mark_time,
                "price_source": price_source,
            }
        paper_summary = await self.paper.sync_snapshot(payload, position_marks=position_marks)
        payload["paper_summary"] = paper_summary
        payload["paper_positions"] = await self.paper.list_positions(symbol=underlying, status="all", limit=8)
        payload["paper_journal"] = await self.paper.list_journal(symbol=underlying, limit=8)
        return payload

    async def paper_journal(self, symbol: str | None = None, limit: int = 50) -> dict[str, object]:
        return await self.paper.list_journal(symbol=symbol, limit=limit)

    async def paper_positions(
        self,
        symbol: str | None = None,
        status: str = "all",
        limit: int = 50,
    ) -> dict[str, object]:
        return await self.paper.list_positions(symbol=symbol, status=status, limit=limit)

    def _snapshot(
        self,
        *,
        underlying: str,
        timeframe: str,
        feature_frame: pd.DataFrame,
    ) -> dict[str, object]:
        if feature_frame.empty:
            return {
                "as_of": None,
                "reason": "No spot candles were available for the selected symbol/timeframe.",
            }

        row = feature_frame.iloc[-1]
        timestamp = pd.Timestamp(row["time"])
        spot_price = float(row["close"])
        feature_snapshot = self.feature_engine.snapshot(row)
        regime = self.regime.classify(row)
        signal = self.signals.predict(row, regime, timeframe)

        selection_reason = "Regime is not tradeable."
        candidate_payload: dict[str, object] | None = None
        candidates_payload: list[dict[str, object]] = []
        risk_payload: dict[str, object] | None = None
        if signal is not None:
            selection = self.selector.select(
                underlying=underlying,
                timestamp=timestamp,
                spot_price=spot_price,
                row=row,
                signal=signal,
                regime=regime,
                timeframe=timeframe,
            )
            selection_reason = selection["reason"]
            candidate = selection["best"]
            candidates_payload = [asdict(item) for item in selection["candidates"]]
            if candidate is not None:
                candidate_payload = asdict(candidate)
                risk_payload = asdict(
                    self.risk.approve(
                        candidate=candidate,
                        signal=signal,
                        equity=float(self.config["risk"]["starting_equity"]),
                    )
                )

        return {
            "as_of": timestamp.isoformat(),
            "underlying": underlying,
            "timeframe": timeframe,
            "spot_price": round(spot_price, 2),
            "feature_snapshot": asdict(feature_snapshot),
            "regime": asdict(regime),
            "signal": asdict(signal) if signal is not None else None,
            "selected_contract": candidate_payload,
            "contract_candidates": candidates_payload,
            "risk": risk_payload,
            "selection_reason": selection_reason,
        }

    async def _live_snapshot(
        self,
        *,
        underlying: str,
        timeframe: str,
        feature_frame: pd.DataFrame,
        strategy_health: dict[str, Any],
        history_source: str,
        history_symbol: str,
    ) -> dict[str, object]:
        data_status = self._build_live_data_status(
            underlying=underlying,
            feature_frame=feature_frame,
            strategy_health=strategy_health,
            history_source=history_source,
            history_symbol=history_symbol,
        )
        if feature_frame.empty:
            data_status["execution_ready"] = False
            data_status["degraded_reason"] = "missing_spot_history"
            return {
                "as_of": None,
                "underlying": underlying,
                "timeframe": timeframe,
                "spot_price": None,
                "feature_snapshot": None,
                "regime": None,
                "signal": None,
                "selected_contract": None,
                "contract_candidates": [],
                "risk": None,
                "selection_reason": "No local spot candles were available for the selected symbol/timeframe.",
                "data_status": data_status,
            }

        row = feature_frame.iloc[-1]
        timestamp = pd.Timestamp(row["time"])
        spot_price = float(row["close"])
        feature_snapshot = self.feature_engine.snapshot(row)
        regime = self.regime.classify(row)
        signal = self.signals.predict(row, regime, timeframe)

        selection_reason = "Regime is not tradeable."
        candidate_payload: dict[str, object] | None = None
        candidates_payload: list[dict[str, object]] = []
        risk_payload: dict[str, object] | None = None

        if not bool(data_status.get("execution_ready")):
            selection_reason = (
                str(data_status.get("degraded_reason") or "Local market-intelligence data is not fresh enough for paper execution.")
                .replace("_", " ")
            )
        elif signal is not None:
            snapshot_rows = await self.store.list_live_contract_snapshots(
                underlying=underlying,
                option_type=signal.direction,
                spot_price=spot_price,
                as_of=timestamp,
                max_days_to_expiry=float(self.config["selector"]["max_days_to_expiry"]),
            )
            selection = self.selector.select_from_live_snapshots(
                underlying=underlying,
                timestamp=timestamp,
                spot_price=spot_price,
                row=row,
                signal=signal,
                regime=regime,
                timeframe=timeframe,
                snapshot_rows=snapshot_rows,
            )
            selection_reason = selection["reason"]
            candidate = selection["best"]
            candidates_payload = [asdict(item) for item in selection["candidates"]]
            if candidate is not None:
                candidate_payload = asdict(candidate)
                risk_payload = asdict(
                    self.risk.approve(
                        candidate=candidate,
                        signal=signal,
                        equity=float(self.config["risk"]["starting_equity"]),
                    )
                )

        return {
            "as_of": timestamp.isoformat(),
            "underlying": underlying,
            "timeframe": timeframe,
            "spot_price": round(spot_price, 2),
            "feature_snapshot": asdict(feature_snapshot),
            "regime": asdict(regime),
            "signal": asdict(signal) if signal is not None else None,
            "selected_contract": candidate_payload,
            "contract_candidates": candidates_payload,
            "risk": risk_payload,
            "selection_reason": selection_reason,
            "data_status": data_status,
            "history_source": history_source,
            "history_symbol": history_symbol,
        }

    def _build_live_data_status(
        self,
        *,
        underlying: str,
        feature_frame: pd.DataFrame,
        strategy_health: dict[str, Any],
        history_source: str,
        history_symbol: str,
    ) -> dict[str, object]:
        latest_watchlist_time = strategy_health.get("latest_watchlist_time")
        watchlist_age_seconds = strategy_health.get("watchlist_age_seconds")
        latest_spot_map = dict(strategy_health.get("latest_spot_rows") or {})
        latest_spot_time = latest_spot_map.get(str(underlying).upper())
        stale_limit = float(self.config["paper_trading"]["stale_watchlist_seconds"])
        spot_age_seconds: Optional[float] = None
        if latest_spot_time:
            spot_ts = pd.Timestamp(latest_spot_time)
            if spot_ts.tzinfo is None:
                spot_ts = spot_ts.tz_localize("UTC")
            spot_age_seconds = max(0.0, (pd.Timestamp.utcnow() - spot_ts).total_seconds())
        execution_ready = bool(
            not feature_frame.empty
            and latest_spot_time
            and strategy_health.get("watchlist_rows_today")
            and (watchlist_age_seconds is None or float(watchlist_age_seconds) <= stale_limit)
            and (spot_age_seconds is None or float(spot_age_seconds) <= stale_limit)
        )
        degraded_reason = None
        if feature_frame.empty:
            degraded_reason = "missing_spot_history"
        elif not latest_spot_time:
            degraded_reason = "shared_spot_store_missing_symbol"
        elif not strategy_health.get("watchlist_rows_today"):
            degraded_reason = "local_watchlist_empty"
        elif watchlist_age_seconds is not None and float(watchlist_age_seconds) > stale_limit:
            degraded_reason = "local_watchlist_stale"
        elif spot_age_seconds is not None and float(spot_age_seconds) > stale_limit:
            degraded_reason = "shared_spot_store_stale"
        return {
            "history_source": history_source,
            "history_symbol": history_symbol,
            "latest_watchlist_time": latest_watchlist_time,
            "watchlist_age_seconds": watchlist_age_seconds,
            "watchlist_rows_today": int(strategy_health.get("watchlist_rows_today") or 0),
            "latest_spot_time": latest_spot_time,
            "spot_age_seconds": spot_age_seconds,
            "execution_ready": execution_ready,
            "degraded_reason": degraded_reason,
        }
