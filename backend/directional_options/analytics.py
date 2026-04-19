"""Backtest analytics for the directional long-options engine."""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Iterable

from directional_options.schemas import TradeRecord


def _safe_div(numerator: float, denominator: float) -> float:
    if abs(denominator) <= 1e-9:
        return 0.0
    return numerator / denominator


def _profit_factor(pnls: Iterable[float]) -> float:
    positives = sum(value for value in pnls if value > 0)
    negatives = sum(value for value in pnls if value < 0)
    return _safe_div(positives, abs(negatives))


def _max_losing_streak(pnls: list[float]) -> int:
    streak = 0
    worst = 0
    for value in pnls:
        if value < 0:
            streak += 1
            worst = max(worst, streak)
        else:
            streak = 0
    return worst


def _drawdown_stats(equity_curve: list[tuple[str, float]]) -> tuple[float, float, int]:
    if not equity_curve:
        return 0.0, 0.0, 0
    high_water = equity_curve[0][1]
    max_drawdown_abs = 0.0
    max_drawdown_pct = 0.0
    current_duration = 0
    max_duration = 0
    for _, equity in equity_curve:
        if equity >= high_water:
            high_water = equity
            current_duration = 0
            continue
        current_duration += 1
        max_duration = max(max_duration, current_duration)
        drawdown_abs = high_water - equity
        drawdown_pct = _safe_div(drawdown_abs, high_water)
        max_drawdown_abs = max(max_drawdown_abs, drawdown_abs)
        max_drawdown_pct = max(max_drawdown_pct, drawdown_pct)
    return max_drawdown_abs, max_drawdown_pct, max_duration


def _tail_ratio(values: list[float]) -> float:
    if len(values) < 8:
        return 0.0
    sorted_values = sorted(values)
    lower = sorted_values[max(0, int(len(sorted_values) * 0.05) - 1)]
    upper = sorted_values[min(len(sorted_values) - 1, int(len(sorted_values) * 0.95))]
    return _safe_div(abs(upper), abs(lower))


def _window_metrics(trades: list[TradeRecord]) -> list[dict[str, float]]:
    if not trades:
        return []
    window_size = max(1, len(trades) // 4)
    windows: list[dict[str, float]] = []
    for start in range(0, len(trades), window_size):
        chunk = trades[start : start + window_size]
        if not chunk:
            continue
        pnls = [trade.pnl for trade in chunk]
        windows.append(
            {
                "expectancy": statistics.fmean(pnls),
                "profit_factor": _profit_factor(pnls),
                "trades": float(len(chunk)),
            }
        )
    return windows


def build_trade_analytics(
    *,
    trades: list[TradeRecord],
    equity_curve: list[tuple[str, float]],
    starting_equity: float,
) -> dict[str, object]:
    pnls = [trade.pnl for trade in trades]
    returns = [trade.return_pct for trade in trades]
    premium_paid = sum(trade.premium_paid for trade in trades)
    expected_edge = sum(abs(trade.expected_pnl) for trade in trades)
    theta_cost = sum(trade.theta_cost for trade in trades)
    friction = sum(trade.spread_cost + trade.slippage_cost for trade in trades)
    total_pnl = sum(pnls)
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    avg_win = statistics.fmean(wins) if wins else 0.0
    avg_loss = statistics.fmean(losses) if losses else 0.0
    payoff_ratio = _safe_div(avg_win, abs(avg_loss))
    profit_factor = _profit_factor(pnls)
    max_drawdown_abs, max_drawdown_pct, drawdown_duration = _drawdown_stats(equity_curve)
    ending_equity = equity_curve[-1][1] if equity_curve else starting_equity
    total_return = _safe_div(ending_equity - starting_equity, starting_equity)
    equity_points = len(equity_curve)
    annualized_return = 0.0
    if equity_points > 1 and starting_equity > 0:
        annualized_return = ((1.0 + total_return) ** (252.0 / max(equity_points, 1))) - 1.0

    month_pnl: dict[str, float] = defaultdict(float)
    regime_groups: dict[str, list[TradeRecord]] = defaultdict(list)
    exit_groups: dict[str, list[TradeRecord]] = defaultdict(list)
    delta_groups: dict[str, list[TradeRecord]] = defaultdict(list)
    for trade in trades:
        month_pnl[trade.exit_time[:7]] += trade.pnl
        regime_groups[trade.regime].append(trade)
        exit_groups[trade.exit_reason].append(trade)
        delta_groups[trade.delta_bucket].append(trade)

    rolling_expectancies: list[float] = []
    if trades:
        for index in range(len(trades)):
            window = trades[max(0, index - 19) : index + 1]
            rolling_expectancies.append(statistics.fmean(trade.pnl for trade in window))

    rolling_pf_values: list[float] = []
    if trades:
        for index in range(len(trades)):
            window = trades[max(0, index - 49) : index + 1]
            rolling_pf_values.append(_profit_factor(trade.pnl for trade in window))

    monthly_rows = [
        {"month": month, "pnl": round(pnl, 2)}
        for month, pnl in sorted(month_pnl.items())
    ]
    profitable_months = sum(1 for row in monthly_rows if row["pnl"] > 0)
    profitable_months_pct = _safe_div(profitable_months, len(monthly_rows))

    walkforward = _window_metrics(trades)
    walkforward_expectancy = statistics.fmean(window["expectancy"] for window in walkforward) if walkforward else 0.0
    walkforward_pf = statistics.fmean(window["profit_factor"] for window in walkforward) if walkforward else 0.0
    positive_windows_pct = _safe_div(sum(1 for window in walkforward if window["expectancy"] > 0), len(walkforward))
    rolling_expectancy_p25 = 0.0
    if rolling_expectancies:
        sorted_expectancies = sorted(rolling_expectancies)
        rolling_expectancy_p25 = sorted_expectancies[max(0, int(len(sorted_expectancies) * 0.25) - 1)]

    parameter_instability = statistics.pstdev(returns) / 100.0 if len(returns) > 1 else 0.0
    engine_score = 100.0 * (
        (0.25 * max(-1.0, min(walkforward_expectancy / 5_000.0, 1.25)))
        + (0.20 * max(0.0, min(walkforward_pf / 2.0, 1.25)))
        + (0.15 * max(0.0, min(_safe_div(annualized_return, max(max_drawdown_pct, 0.01)), 2.0)))
        + (0.15 * profitable_months_pct)
        + (0.10 * max(-1.0, min(rolling_expectancy_p25 / 5_000.0, 1.0)))
        + (0.10 * max(-1.0, min(_safe_div(total_pnl, max(premium_paid, 1.0)), 1.0)))
        - (0.15 * min(max_drawdown_pct, 1.0))
        - (0.10 * min(_safe_div(friction, max(expected_edge, 1.0)), 1.0))
        - (0.10 * min(parameter_instability, 1.0))
    )

    return {
        "summary": {
            "trade_count": len(trades),
            "total_pnl": round(total_pnl, 2),
            "ending_equity": round(ending_equity, 2),
            "total_return_pct": round(total_return * 100.0, 2),
            "cagr_pct": round(annualized_return * 100.0, 2),
            "max_drawdown_pct": round(max_drawdown_pct * 100.0, 2),
            "max_drawdown_abs": round(max_drawdown_abs, 2),
            "drawdown_duration_bars": drawdown_duration,
            "calmar": round(_safe_div(annualized_return, max(max_drawdown_pct, 0.01)), 2),
            "profit_factor": round(profit_factor, 2),
            "win_rate_pct": round(_safe_div(len(wins), len(trades)) * 100.0, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "payoff_ratio": round(payoff_ratio, 2),
            "expectancy": round(statistics.fmean(pnls), 2) if pnls else 0.0,
            "premium_efficiency": round(_safe_div(total_pnl, max(premium_paid, 1.0)), 4),
            "spread_tax_ratio": round(_safe_div(friction, max(expected_edge, 1.0)), 4),
            "theta_bleed_ratio": round(_safe_div(theta_cost, max(expected_edge, 1.0)), 4),
            "percent_profitable_months": round(profitable_months_pct * 100.0, 2),
            "percent_profitable_walkforward_windows": round(positive_windows_pct * 100.0, 2),
            "max_losing_streak": _max_losing_streak(pnls),
            "tail_ratio": round(_tail_ratio(returns), 2),
            "engine_score": round(engine_score, 2),
        },
        "stability": {
            "rolling_20_trade_expectancy": round(rolling_expectancies[-1], 2) if rolling_expectancies else 0.0,
            "rolling_expectancy_p25": round(rolling_expectancy_p25, 2),
            "rolling_50_trade_profit_factor": round(rolling_pf_values[-1], 2) if rolling_pf_values else 0.0,
            "walkforward_expectancy": round(walkforward_expectancy, 2),
            "walkforward_profit_factor": round(walkforward_pf, 2),
        },
        "monthly": monthly_rows,
        "regime_breakdown": [
            {
                "regime": regime,
                "trades": len(items),
                "expectancy": round(statistics.fmean(trade.pnl for trade in items), 2),
                "win_rate_pct": round(_safe_div(sum(1 for trade in items if trade.pnl > 0), len(items)) * 100.0, 2),
            }
            for regime, items in sorted(regime_groups.items())
        ],
        "exit_breakdown": [
            {
                "exit_reason": reason,
                "trades": len(items),
                "avg_pnl": round(statistics.fmean(trade.pnl for trade in items), 2),
            }
            for reason, items in sorted(exit_groups.items())
        ],
        "delta_breakdown": [
            {
                "delta_bucket": bucket,
                "trades": len(items),
                "avg_return_pct": round(statistics.fmean(trade.return_pct for trade in items), 2),
            }
            for bucket, items in sorted(delta_groups.items())
        ],
        "walkforward_windows": [
            {
                "window": index + 1,
                "trades": int(window["trades"]),
                "expectancy": round(window["expectancy"], 2),
                "profit_factor": round(window["profit_factor"], 2),
            }
            for index, window in enumerate(walkforward)
        ],
        "equity_curve": [
            {"time": timestamp, "equity": round(equity, 2)}
            for timestamp, equity in equity_curve
        ],
        "recent_trades": [
            trade.__dict__
            for trade in trades[-10:]
        ],
    }
