#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import timezone
from pathlib import Path
from typing import Any

import pandas as pd

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from agentic_rag.schemas import TradeCaseRecord
from agentic_rag.service import rag_service
from directional_options.backtest import DirectionalOptionsBacktester
from directional_options.config import clone_default_config
from directional_options.data import DirectionalOptionsDataStore
from directional_options.features import FeatureEngine
from directional_options.regime import RegimeClassifier
from directional_options.risk import DirectionalOptionsRiskEngine
from directional_options.selector import OptionSelectionEngine
from directional_options.signals import DirectionalSignalEngine


def _result_from_pnl(pnl: float) -> str:
    if pnl > 0:
        return "win"
    if pnl < 0:
        return "loss"
    return "neutral"


def _to_trade_case(
    *,
    underlying: str,
    timeframe: str,
    trade: dict[str, Any],
) -> TradeCaseRecord:
    entry_time = str(trade.get("entry_time") or "")
    exit_time = str(trade.get("exit_time") or "")
    pnl = float(trade.get("pnl") or 0.0)
    result = _result_from_pnl(pnl)
    confidence = trade.get("confidence")
    regime = str(trade.get("regime") or "")
    direction = str(trade.get("option_type") or trade.get("direction") or "").upper()
    trading_symbol = str(trade.get("trading_symbol") or trade.get("symbol") or underlying)

    features = {
        "timeframe": timeframe,
        "confidence": confidence,
        "strike": trade.get("strike"),
        "expiry": trade.get("expiry"),
        "expiry_kind": trade.get("expiry_kind"),
        "delta_bucket": trade.get("delta_bucket"),
        "expected_pnl": trade.get("expected_pnl"),
        "return_pct": trade.get("return_pct"),
        "exit_reason": trade.get("exit_reason"),
    }
    features = {k: v for k, v in features.items() if v is not None}
    tags = [timeframe, regime, direction, result]
    tags = [t for t in tags if t]

    # Stable id: strategy + underlying + timestamps + symbol
    id_value = f"directional_long_options:bt:{underlying}:{timeframe}:{entry_time}:{trading_symbol}"
    return TradeCaseRecord(
        id=id_value,
        strategy_key="directional_long_options",
        underlying=underlying,
        symbol=trading_symbol,
        setup_name=str(trade.get("setup_name") or trade.get("signal_reason") or ""),
        regime=regime,
        direction=direction,
        entry_time=entry_time,
        exit_time=exit_time,
        pnl=pnl,
        result=result,
        tags=tags,
        features=features,
        lesson=str(trade.get("selection_reason") or trade.get("signal_reason") or ""),
        source="backtest_bootstrap",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap RAG trade_cases from directional_options backtests.")
    parser.add_argument("--max-per-series", type=int, default=120, help="Max trades per (underlying,timeframe).")
    parser.add_argument("--lookback-sessions", type=int, default=60, help="Spot lookback sessions.")
    parser.add_argument("--underlying", action="append", default=[], help="Limit to specific underlying (repeatable).")
    parser.add_argument("--timeframe", action="append", default=[], help="Limit to specific timeframe (repeatable).")
    args = parser.parse_args()

    config = clone_default_config()
    store = DirectionalOptionsDataStore(config["data_root"])
    feature_engine = FeatureEngine(config=config["feature_engine"])
    regime = RegimeClassifier()
    signals = DirectionalSignalEngine(config=config["signal_engine"])
    selector = OptionSelectionEngine(store=store, config=config["selector"])
    risk = DirectionalOptionsRiskEngine(config=config["risk"])
    backtester = DirectionalOptionsBacktester(
        store=store,
        feature_engine=feature_engine,
        regime=regime,
        signals=signals,
        selector=selector,
        risk=risk,
        config=config,
    )

    underlyings = args.underlying or list(config.get("universe") or store.available_underlyings())
    timeframes = args.timeframe or list(config.get("timeframes") or ["5minute", "15minute"])

    before = rag_service.health().get("trade_cases", 0)
    appended = 0
    for underlying in underlyings:
        for timeframe in timeframes:
            result = backtester.run(
                underlying=underlying,
                timeframe=timeframe,
                lookback_sessions=int(args.lookback_sessions),
            )
            trades = list(result.get("recent_trades") or [])
            if not trades:
                continue
            # Prefer most recent; cap volume.
            trades = trades[-int(args.max_per_series) :]
            for trade in trades:
                # Normalize any pandas timestamps (should already be strings)
                for key in ("entry_time", "exit_time"):
                    val = trade.get(key)
                    if isinstance(val, pd.Timestamp):
                        trade[key] = val.tz_localize(timezone.utc).isoformat()
                rag_service.add_trade_case(_to_trade_case(underlying=underlying, timeframe=timeframe, trade=trade))
                appended += 1

    after = rag_service.health().get("trade_cases", 0)
    print(f"Bootstrapped directional_long_options trade cases: appended={appended} before={before} after={after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
