"""Performance analytics — equity curve, drawdown, Sharpe, calendar P&L."""
from __future__ import annotations
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import numpy as np


@dataclass
class EquityPoint:
    timestamp: str
    equity: float
    cumulative_pnl: float


@dataclass
class PerformanceSummary:
    total_trades: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    max_drawdown: float
    sharpe_ratio: float
    total_pnl: float
    day_pnl: float


class PerformanceAnalytics:
    """Computes performance metrics from a list of trade records."""

    def __init__(self, initial_capital: float = 1_000_000.0):
        self.initial_capital = initial_capital

    def equity_curve(self, trades: List[dict]) -> List[EquityPoint]:
        """trades: list of { pnl, exit_time } sorted by exit_time."""
        curve = []
        cumulative = 0.0
        equity = self.initial_capital
        for t in sorted(trades, key=lambda x: x.get("exit_time", "")):
            cumulative += t.get("pnl", 0)
            equity = self.initial_capital + cumulative
            curve.append(EquityPoint(
                timestamp=str(t.get("exit_time", "")),
                equity=round(equity, 2),
                cumulative_pnl=round(cumulative, 2),
            ))
        return curve

    def calendar_heatmap(self, trades: List[dict]) -> Dict[str, float]:
        """Returns { 'YYYY-MM-DD': daily_pnl }."""
        daily: Dict[str, float] = defaultdict(float)
        for t in trades:
            exit_time = t.get("exit_time")
            if exit_time:
                d = str(exit_time)[:10]
                daily[d] += t.get("pnl", 0)
        return dict(daily)

    def summary(self, trades: List[dict], period: str = "all") -> PerformanceSummary:
        filtered = self._filter_period(trades, period)
        if not filtered:
            return PerformanceSummary(0, 0, 0, 0, 0, 0, 0, 0, 0)

        pnls = [t.get("pnl", 0) for t in filtered]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        return PerformanceSummary(
            total_trades=len(filtered),
            win_rate=round(len(wins) / len(filtered), 4),
            avg_win=round(sum(wins) / len(wins), 2) if wins else 0,
            avg_loss=round(sum(losses) / len(losses), 2) if losses else 0,
            profit_factor=round(min(profit_factor, 999), 2),
            max_drawdown=round(self._max_drawdown(pnls), 4),
            sharpe_ratio=round(self._sharpe(pnls), 4),
            total_pnl=round(sum(pnls), 2),
            day_pnl=round(sum(
                t.get("pnl", 0) for t in filtered
                if str(t.get("exit_time", ""))[:10] == date.today().isoformat()
            ), 2),
        )

    def strategy_breakdown(self, trades: List[dict]) -> dict:
        """Group P&L by instrument_type, symbol, time of day."""
        by_type: Dict[str, float] = defaultdict(float)
        by_symbol: Dict[str, float] = defaultdict(float)
        by_hour: Dict[int, float] = defaultdict(float)

        for t in trades:
            by_type[t.get("instrument_type", "UNKNOWN")] += t.get("pnl", 0)
            by_symbol[t.get("symbol", "UNKNOWN")] += t.get("pnl", 0)
            exit_time = t.get("exit_time")
            if exit_time:
                try:
                    hour = datetime.fromisoformat(str(exit_time)).hour
                    by_hour[hour] += t.get("pnl", 0)
                except Exception:
                    pass

        return {
            "by_instrument_type": dict(by_type),
            "by_symbol": dict(by_symbol),
            "by_hour": {str(h): round(v, 2) for h, v in sorted(by_hour.items())},
        }

    def rolling_sharpe(self, trades: List[dict], window: int = 20) -> List[dict]:
        """Rolling N-trade Sharpe ratio."""
        result = []
        for i in range(window, len(trades) + 1):
            window_pnls = [t.get("pnl", 0) for t in trades[i - window:i]]
            sharpe = self._sharpe(window_pnls)
            result.append({
                "timestamp": str(trades[i - 1].get("exit_time", "")),
                "sharpe": round(sharpe, 4),
            })
        return result

    @staticmethod
    def _max_drawdown(pnls: List[float]) -> float:
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnls:
            equity += p
            if equity > peak:
                peak = equity
            dd = (peak - equity) / (abs(peak) + 1e-8)
            if dd > max_dd:
                max_dd = dd
        return max_dd

    @staticmethod
    def _sharpe(pnls: List[float]) -> float:
        if len(pnls) < 2:
            return 0.0
        arr = np.array(pnls)
        std = np.std(arr)
        if std == 0:
            return 0.0
        return float(np.mean(arr) / std * math.sqrt(252))

    @staticmethod
    def _filter_period(trades: List[dict], period: str) -> List[dict]:
        if period == "all":
            return trades
        today = date.today()
        if period == "today":
            cutoff = today
        elif period == "week":
            cutoff = today - timedelta(days=7)
        elif period == "month":
            cutoff = today - timedelta(days=30)
        else:
            return trades

        result = []
        for t in trades:
            exit_time = t.get("exit_time")
            if exit_time:
                try:
                    d = datetime.fromisoformat(str(exit_time)).date()
                    if d >= cutoff:
                        result.append(t)
                except Exception:
                    pass
        return result
