"""Equity curves for the saved original MACD walk-forward run by timeframe."""
from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path
from typing import Any

import pandas as pd

from analysis.index_option_walkforward import OUTPUT_ROOT as WALKFORWARD_OUTPUT_ROOT


TRADES_PATH = WALKFORWARD_OUTPUT_ROOT / "oos_trades.csv"
OUTPUT_ROOT = WALKFORWARD_OUTPUT_ROOT / "timeframe_equity"
TIMEFRAMES = ("1m", "3m", "5m", "10m", "15m", "30m")
STARTING_EQUITY = 100.0


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(float(statistics.fmean(values)), 4)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(float(statistics.median(values)), 4)


def _max_drawdown(equity_values: list[float]) -> float:
    if not equity_values:
        return 0.0
    peak = equity_values[0]
    worst = 0.0
    for value in equity_values:
        if value > peak:
            peak = value
        if peak > 0:
            worst = min(worst, (value - peak) / peak * 100.0)
    return round(worst, 4)


def _load_rows() -> list[dict[str, Any]]:
    with TRADES_PATH.open() as handle:
        reader = csv.DictReader(handle)
        rows = [dict(row) for row in reader]
    for row in rows:
        row["return_pct"] = float(row["return_pct"])
        row["holding_minutes"] = float(row["holding_minutes"])
        row["exit_time"] = pd.Timestamp(row["exit_time"])
        row["entry_time"] = pd.Timestamp(row["entry_time"])
    return rows


def _build_curve(rows: list[dict[str, Any]], timeframe: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: (row["exit_time"], row["entry_time"], row["series_id"], row["option_type"]))
    equity = STARTING_EQUITY
    points: list[dict[str, Any]] = [
        {
            "timeframe": timeframe,
            "trade_index": 0,
            "timestamp": ordered[0]["entry_time"].isoformat() if ordered else "",
            "equity": round(equity, 4),
            "return_pct": 0.0,
            "series_id": "",
            "option_type": "",
        }
    ]
    equity_values = [equity]
    returns = [float(row["return_pct"]) for row in ordered]
    wins = [value for value in returns if value > 0.0]
    losses = [value for value in returns if value <= 0.0]
    for index, row in enumerate(ordered, start=1):
        equity *= 1.0 + (float(row["return_pct"]) / 100.0)
        equity = max(equity, 0.0)
        equity_values.append(equity)
        points.append(
            {
                "timeframe": timeframe,
                "trade_index": index,
                "timestamp": row["exit_time"].isoformat(),
                "equity": round(equity, 4),
                "return_pct": round(float(row["return_pct"]), 4),
                "series_id": row["series_id"],
                "option_type": row["option_type"],
            }
        )

    summary = {
        "opportunities": len(ordered),
        "win_rate": round(len(wins) / len(ordered), 4) if ordered else 0.0,
        "avg_return_pct": _mean(returns),
        "median_return_pct": _median(returns),
        "avg_win_return_pct": _mean(wins),
        "avg_loss_return_pct": _mean(losses),
        "avg_holding_minutes": _mean([float(row["holding_minutes"]) for row in ordered]),
        "ending_equity": round(equity, 4),
        "net_return_pct": round(((equity / STARTING_EQUITY) - 1.0) * 100.0, 4) if ordered else 0.0,
        "max_drawdown_pct": _max_drawdown(equity_values),
    }
    return points, summary


def run() -> dict[str, Any]:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    rows = _load_rows()

    all_points: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "generated_at": pd.Timestamp.now(tz="Asia/Kolkata").isoformat(),
        "source_trades": str(TRADES_PATH),
        "starting_equity": STARTING_EQUITY,
        "timeframes": {},
    }

    for timeframe in TIMEFRAMES:
        subset = [row for row in rows if row["timeframe"] == timeframe]
        points, timeframe_summary = _build_curve(subset, timeframe)
        summary["timeframes"][timeframe] = timeframe_summary
        all_points.extend(points)

    summary_path = OUTPUT_ROOT / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))

    curves_path = OUTPUT_ROOT / "equity_curves.csv"
    fieldnames = ["timeframe", "trade_index", "timestamp", "equity", "return_pct", "series_id", "option_type"]
    with curves_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in all_points:
            writer.writerow(row)

    lines = [
        "# Original MACD Walk-Forward Equity By Timeframe",
        "",
        f"Generated: {summary['generated_at']}",
        f"Source trades: `{summary['source_trades']}`",
        f"Starting equity: {STARTING_EQUITY:.2f}",
        "",
    ]
    for timeframe in TIMEFRAMES:
        stats = summary["timeframes"][timeframe]
        lines.extend(
            [
                f"## {timeframe}",
                "",
                f"- Trades: {stats['opportunities']}",
                f"- Ending equity: {stats['ending_equity']:.2f}",
                f"- Net return: {stats['net_return_pct']:.2f}%",
                f"- Win rate: {stats['win_rate'] * 100:.2f}%",
                f"- Avg return per trade: {stats['avg_return_pct']:.2f}%",
                f"- Max drawdown: {stats['max_drawdown_pct']:.2f}%",
                "",
            ]
        )
    (OUTPUT_ROOT / "report.md").write_text("\n".join(lines))
    return summary


def main() -> None:
    print(json.dumps(run(), indent=2))


if __name__ == "__main__":
    main()
