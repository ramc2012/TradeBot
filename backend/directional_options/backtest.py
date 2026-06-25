"""Bounded event-driven backtest for directional long-option entries."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from typing import Any, Optional

import pandas as pd

from directional_options.analytics import build_trade_analytics
from directional_options.ai_model import HybridDirectionalOptionsModel
from directional_options.data import DirectionalOptionsDataStore
from directional_options.exits import evaluate_exit
from directional_options.features import FeatureEngine, timeframe_minutes
from directional_options.regime import RegimeClassifier
from directional_options.risk import DirectionalOptionsRiskEngine
from directional_options.schemas import PositionState, TradeRecord
from directional_options.selector import OptionSelectionEngine
from directional_options.signals import DirectionalSignalEngine


class DirectionalOptionsBacktester:
    """Run a conservative single-position long-premium simulation."""

    def __init__(
        self,
        *,
        store: DirectionalOptionsDataStore,
        feature_engine: FeatureEngine,
        regime: RegimeClassifier,
        signals: DirectionalSignalEngine,
        selector: OptionSelectionEngine,
        risk: DirectionalOptionsRiskEngine,
        config: dict[str, Any],
        ai_model: HybridDirectionalOptionsModel | None = None,
    ) -> None:
        self.store = store
        self.feature_engine = feature_engine
        self.regime = regime
        self.signals = signals
        self.selector = selector
        self.risk = risk
        self.config = config
        self.ai_model = ai_model

    def run(
        self,
        *,
        underlying: str,
        timeframe: str,
        lookback_sessions: int,
        feature_frame: pd.DataFrame | None = None,
    ) -> dict[str, object]:
        spot = self.store.load_spot_frame(underlying)
        frame = feature_frame if feature_frame is not None else self.feature_engine.build_frame(
            spot,
            timeframe,
            lookback_sessions=lookback_sessions,
        )
        if frame.empty:
            return {
                "summary": {
                    "trade_count": 0,
                    "engine_score": 0.0,
                },
                "stability": {},
                "monthly": [],
                "regime_breakdown": [],
                "exit_breakdown": [],
                "delta_breakdown": [],
                "walkforward_windows": [],
                "equity_curve": [],
                "recent_trades": [],
            }

        risk_cfg = self.config["risk"]
        execution_cfg = self.config["execution"]
        starting_equity = float(risk_cfg["starting_equity"])
        fee_per_unit = float(execution_cfg["fee_per_unit"])
        cash = starting_equity
        trades: list[TradeRecord] = []
        equity_curve: list[tuple[str, float]] = []
        open_position: PositionState | None = None
        daily_realized: dict[str, float] = defaultdict(float)
        weekly_realized: dict[str, float] = defaultdict(float)
        trades_per_day: dict[str, int] = defaultdict(int)

        for _, row in frame.iterrows():
            timestamp = pd.Timestamp(row["time"])
            spot_price = float(row["close"])
            day_key = timestamp.date().isoformat()
            iso = timestamp.isocalendar()
            week_key = f"{iso.year}-W{iso.week:02d}"
            regime = self.regime.classify(row, timeframe=timeframe)

            if open_position is not None:
                current_mark = self._mark_price(open_position.contract.file_path, timestamp, open_position.entry_mark_price)
                open_position.held_bars += 1
                open_position.peak_mark_price = max(open_position.peak_mark_price, current_mark)
                exit_reason = self._exit_reason(open_position, spot_price, current_mark, timestamp)
                equity_curve.append((timestamp.isoformat(), round(cash + (current_mark * open_position.quantity_units), 2)))
                if exit_reason:
                    exit_fill = self._sell_fill(open_position.contract.option_price, current_mark, open_position.contract)
                    pnl = ((exit_fill - open_position.entry_fill_price) * open_position.quantity_units) - (2.0 * fee_per_unit * open_position.quantity_units)
                    cash += (exit_fill * open_position.quantity_units) - (fee_per_unit * open_position.quantity_units)
                    theta_cost = abs(open_position.contract.theta) * self._held_years(open_position.held_bars, timeframe) * open_position.quantity_units
                    spread_cost = (
                        ((open_position.contract.option_price * open_position.contract.spread_pct) / 2.0)
                        + ((current_mark * open_position.contract.spread_pct) / 2.0)
                    ) * open_position.quantity_units
                    slippage_cost = (
                        (open_position.contract.option_price * open_position.contract.slippage_pct)
                        + (current_mark * open_position.contract.slippage_pct)
                    ) * open_position.quantity_units
                    trades.append(
                        TradeRecord(
                            underlying=underlying,
                            trading_symbol=open_position.contract.trading_symbol,
                            option_type=open_position.contract.option_type,
                            expiry=open_position.contract.expiry,
                            expiry_kind=open_position.contract.expiry_kind,
                            strike=open_position.contract.strike,
                            qty_lots=open_position.quantity_lots,
                            qty_units=open_position.quantity_units,
                            entry_time=open_position.entry_time,
                            exit_time=timestamp.isoformat(),
                            entry_spot=round(open_position.entry_spot, 2),
                            exit_spot=round(spot_price, 2),
                            entry_price=round(open_position.entry_fill_price, 2),
                            exit_price=round(exit_fill, 2),
                            pnl=round(pnl, 2),
                            return_pct=round(((exit_fill - open_position.entry_fill_price) / max(open_position.entry_fill_price, 1.0)) * 100.0, 2),
                            premium_paid=round(open_position.entry_fill_price * open_position.quantity_units, 2),
                            expected_pnl=round(open_position.expected_pnl * open_position.quantity_units, 2),
                            expected_move=round(open_position.expected_move, 2),
                            realized_move=round(spot_price - open_position.entry_spot, 2),
                            confidence=round(open_position.confidence, 4),
                            regime=open_position.regime,
                            delta_bucket=open_position.contract.delta_bucket,
                            exit_reason=exit_reason,
                            spread_cost=round(spread_cost, 2),
                            slippage_cost=round(slippage_cost, 2),
                            theta_cost=round(theta_cost, 2),
                        )
                    )
                    daily_realized[day_key] += pnl
                    weekly_realized[week_key] += pnl
                    open_position = None
                continue

            equity_curve.append((timestamp.isoformat(), round(cash, 2)))
            if trades_per_day[day_key] >= int(self.config["backtest"]["max_trades_per_day"]):
                continue

            signal = self.signals.predict(row, regime, timeframe)
            if signal is None:
                continue

            selection = self.selector.select(
                underlying=underlying,
                timestamp=timestamp,
                spot_price=spot_price,
                row=row,
                signal=signal,
                regime=regime,
                timeframe=timeframe,
            )
            candidate = selection["best"]
            if candidate is None:
                continue
            if self.ai_model is not None:
                rule_eval = self.ai_model.evaluate(
                    row=row,
                    signal=asdict(signal),
                    regime=asdict(regime),
                    candidate=asdict(candidate),
                )
                if not rule_eval.allowed:
                    continue

            risk = self.risk.approve(
                candidate=candidate,
                signal=signal,
                equity=cash,
                daily_realized=daily_realized[day_key],
                weekly_realized=weekly_realized[week_key],
            )
            if not risk.approved or risk.quantity_units <= 0:
                continue

            entry_fill = self._buy_fill(candidate.option_price, candidate)
            entry_total = (entry_fill * risk.quantity_units) + (fee_per_unit * risk.quantity_units)
            if entry_total > cash:
                continue

            cash -= entry_total
            trades_per_day[day_key] += 1
            open_position = PositionState(
                underlying=underlying,
                contract=candidate,
                entry_time=timestamp.isoformat(),
                entry_spot=spot_price,
                entry_mark_price=candidate.option_price,
                entry_fill_price=entry_fill,
                stop_price=candidate.option_price * (1.0 - float(risk_cfg["planned_stop_pct"])),
                target_price=candidate.option_price * (1.0 + float(risk_cfg["profit_target_pct"])),
                stop_underlying=spot_price - (signal.expected_move * 0.55) if signal.direction == "CE" else spot_price + (signal.expected_move * 0.55),
                quantity_lots=risk.quantity_lots,
                quantity_units=risk.quantity_units,
                max_horizon_bars=signal.expected_horizon_bars,
                expected_move=signal.expected_move,
                expected_pnl=candidate.expected_pnl,
                confidence=signal.confidence,
                regime=regime.label,
                peak_mark_price=candidate.option_price,
            )

        if open_position is not None:
            last_time = pd.Timestamp(frame["time"].iloc[-1])
            last_spot = float(frame["close"].iloc[-1])
            current_mark = self._mark_price(open_position.contract.file_path, last_time, open_position.entry_mark_price)
            exit_fill = self._sell_fill(open_position.contract.option_price, current_mark, open_position.contract)
            pnl = ((exit_fill - open_position.entry_fill_price) * open_position.quantity_units) - (2.0 * fee_per_unit * open_position.quantity_units)
            cash += (exit_fill * open_position.quantity_units) - (fee_per_unit * open_position.quantity_units)
            theta_cost = abs(open_position.contract.theta) * self._held_years(max(open_position.held_bars, 1), timeframe) * open_position.quantity_units
            spread_cost = (
                ((open_position.contract.option_price * open_position.contract.spread_pct) / 2.0)
                + ((current_mark * open_position.contract.spread_pct) / 2.0)
            ) * open_position.quantity_units
            slippage_cost = (
                (open_position.contract.option_price * open_position.contract.slippage_pct)
                + (current_mark * open_position.contract.slippage_pct)
            ) * open_position.quantity_units
            trades.append(
                TradeRecord(
                    underlying=underlying,
                    trading_symbol=open_position.contract.trading_symbol,
                    option_type=open_position.contract.option_type,
                    expiry=open_position.contract.expiry,
                    expiry_kind=open_position.contract.expiry_kind,
                    strike=open_position.contract.strike,
                    qty_lots=open_position.quantity_lots,
                    qty_units=open_position.quantity_units,
                    entry_time=open_position.entry_time,
                    exit_time=last_time.isoformat(),
                    entry_spot=round(open_position.entry_spot, 2),
                    exit_spot=round(last_spot, 2),
                    entry_price=round(open_position.entry_fill_price, 2),
                    exit_price=round(exit_fill, 2),
                    pnl=round(pnl, 2),
                    return_pct=round(((exit_fill - open_position.entry_fill_price) / max(open_position.entry_fill_price, 1.0)) * 100.0, 2),
                    premium_paid=round(open_position.entry_fill_price * open_position.quantity_units, 2),
                    expected_pnl=round(open_position.expected_pnl * open_position.quantity_units, 2),
                    expected_move=round(open_position.expected_move, 2),
                    realized_move=round(last_spot - open_position.entry_spot, 2),
                    confidence=round(open_position.confidence, 4),
                    regime=open_position.regime,
                    delta_bucket=open_position.contract.delta_bucket,
                    exit_reason="session_end",
                    spread_cost=round(spread_cost, 2),
                    slippage_cost=round(slippage_cost, 2),
                    theta_cost=round(theta_cost, 2),
                )
            )
            equity_curve.append((last_time.isoformat(), round(cash, 2)))

        return build_trade_analytics(
            trades=trades,
            equity_curve=equity_curve,
            starting_equity=starting_equity,
        )

    def _held_years(self, held_bars: int, timeframe: str) -> float:
        return max((held_bars * timeframe_minutes(timeframe)) / (252.0 * 375.0), 1.0 / (252.0 * 375.0))

    def _mark_price(self, file_path: str, timestamp: pd.Timestamp, fallback: float) -> float:
        frame = self.store.load_option_frame(file_path)
        rows = frame.loc[frame["time"] <= timestamp]
        if rows.empty:
            return fallback
        return float(rows.iloc[-1]["close"])

    @staticmethod
    def _buy_fill(mark_price: float, contract) -> float:
        return round(mark_price * (1.0 + (contract.spread_pct / 2.0) + contract.slippage_pct), 4)

    @staticmethod
    def _sell_fill(entry_mark_price: float, current_mark: float, contract) -> float:
        del entry_mark_price
        return round(current_mark * (1.0 - (contract.spread_pct / 2.0) - contract.slippage_pct), 4)

    def _exit_reason(
        self,
        position: PositionState,
        spot_price: float,
        current_mark: float,
        timestamp: pd.Timestamp,
    ) -> Optional[str]:
        # Delegate to the shared exit ladder (directional_options.exits) so the
        # backtest and the live paper book enforce one identical exit regime.
        risk_cfg = self.config["risk"]
        expiry_days_left = max((pd.Timestamp(position.contract.expiry).date() - timestamp.date()).days, 0)
        return evaluate_exit(
            option_type=position.contract.option_type,
            current_premium=current_mark,
            entry_basis_premium=position.entry_mark_price,
            return_basis_premium=position.entry_fill_price,
            peak_premium=position.peak_mark_price,
            current_spot=spot_price,
            stop_underlying=position.stop_underlying,
            expiry_days_left=expiry_days_left,
            held_bars=position.held_bars,
            max_horizon_bars=position.max_horizon_bars,
            planned_stop_pct=float(risk_cfg["planned_stop_pct"]),
            profit_target_pct=float(risk_cfg["profit_target_pct"]),
            trail_giveback_pct=float(risk_cfg["trail_giveback_pct"]),
            expiry_guard_days=float(risk_cfg["expiry_guard_days"]),
        )
