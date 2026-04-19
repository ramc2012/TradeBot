"""Service orchestrator for the directional long-options module."""
from __future__ import annotations

from dataclasses import asdict
from functools import lru_cache
from typing import Any

import pandas as pd

from directional_options.backtest import DirectionalOptionsBacktester
from directional_options.config import clone_default_config
from directional_options.data import DirectionalOptionsDataStore
from directional_options.features import FeatureEngine
from directional_options.regime import RegimeClassifier
from directional_options.risk import DirectionalOptionsRiskEngine
from directional_options.selector import OptionSelectionEngine
from directional_options.signals import DirectionalSignalEngine


class DirectionalOptionsService:
    """Expose module summary, latest snapshot, and bounded backtests."""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or clone_default_config()
        self.store = DirectionalOptionsDataStore(self.config["data_root"])
        self.feature_engine = FeatureEngine(self.config["feature_engine"])
        self.regime = RegimeClassifier()
        self.signals = DirectionalSignalEngine(self.config["signal_engine"])
        self.selector = OptionSelectionEngine(self.store, self.config["selector"])
        self.risk = DirectionalOptionsRiskEngine(self.config["risk"])
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
        from directional_options.dashboard import get_dashboard_mount_state

        available = [item for item in self.config["universe"] if item in self.store.available_underlyings()]
        return {
            "key": "directional_long_options",
            "label": self.config["label"],
            "description": self.config["description"],
            "underlyings": available,
            "timeframes": list(self.config["timeframes"]),
            "dashboard": get_dashboard_mount_state(),
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
