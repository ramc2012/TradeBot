"""Analyze bullish MACD signal crosses by location relative to the zero line."""
from __future__ import annotations

import csv
import json
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from analysis.index_option_walkforward import (
    DATA_ROOT,
    EXPIRY_DAY_EXITS,
    FULL_SERIES_EXITS,
    TIMEFRAME_MAP,
    IndexOptionWalkForwardRunner,
    _analyze_trade_window,
    _prepare_option_frame,
    _simulate_exit,
)
from analysis.macd_engine import compute_macd


OUTPUT_ROOT = DATA_ROOT / "macd_cross_location_sweep"


@dataclass(frozen=True)
class MacdCrossVariant:
    group_name: str
    timeframe: str
    exit_name: str
    expiry_day_only: bool

    @property
    def key(self) -> str:
        mode = "expiry_day" if self.expiry_day_only else "series"
        return f"{self.group_name}|{mode}|{self.timeframe}|{self.exit_name}"


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(float(statistics.fmean(values)), 4)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(float(statistics.median(values)), 4)


def _score(summary: dict[str, Any]) -> float:
    opportunities = float(summary.get("opportunities", 0) or 0)
    if opportunities <= 0:
        return float("-inf")
    win_rate = float(summary.get("win_rate", 0.0) or 0.0)
    avg_return = float(summary.get("avg_return_pct", 0.0) or 0.0)
    median_return = float(summary.get("median_return_pct", 0.0) or 0.0)
    return round(((avg_return * 0.6) + (median_return * 0.4)) * max(win_rate, 0.01) * math.log1p(opportunities), 6)


def _minutes_for_timeframe(timeframe: str) -> int:
    return int(TIMEFRAME_MAP[timeframe].replace("min", ""))


def _location_bucket(current_macd: float, current_signal: float) -> str:
    if current_macd < 0.0 and current_signal < 0.0:
        return "below_zero"
    if current_macd > 0.0 and current_signal > 0.0:
        return "above_zero"
    return "zero_straddle"


def _summarize_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [float(trade["return_pct"]) for trade in trades]
    wins = [value for value in returns if value > 0.0]
    losses = [value for value in returns if value <= 0.0]
    holds = [float(trade["holding_minutes"]) for trade in trades]
    possible = [float(trade["max_possible_return_pct"]) for trade in trades]
    entry_macd_pct = [float(trade["entry_macd_pct"]) for trade in trades]
    return {
        "opportunities": len(trades),
        "win_rate": round(len(wins) / len(trades), 4) if trades else 0.0,
        "avg_return_pct": _mean(returns),
        "median_return_pct": _median(returns),
        "avg_win_return_pct": _mean(wins),
        "avg_loss_return_pct": _mean(losses),
        "max_realized_return_pct": round(max(returns), 4) if returns else 0.0,
        "avg_holding_minutes": _mean(holds),
        "median_holding_minutes": _median(holds),
        "avg_max_possible_return_pct": _mean(possible),
        "max_possible_return_pct": round(max(possible), 4) if possible else 0.0,
        "avg_entry_macd_pct": _mean(entry_macd_pct),
        "median_entry_macd_pct": _median(entry_macd_pct),
    }


def _group_definitions(
    runner: IndexOptionWalkForwardRunner,
) -> list[tuple[str, list[Any], bool, list[str], list[str]]]:
    weekly = [descriptor for descriptor in runner.descriptors if descriptor.expiry_kind == "weekly"]
    monthly = [descriptor for descriptor in runner.descriptors if descriptor.expiry_kind == "monthly"]
    return [
        (
            "weekly_series",
            weekly,
            False,
            ["3m", "5m", "10m", "15m", "30m"],
            ["trail_after_20pct_dd10pct", "trail_after_30pct_dd15pct"],
        ),
        (
            "monthly_series",
            monthly,
            False,
            ["3m", "5m", "10m", "15m", "30m"],
            ["trail_after_20pct_dd10pct", "trail_after_30pct_dd15pct"],
        ),
        (
            "weekly_expiry_day",
            weekly,
            True,
            ["1m", "3m", "5m", "10m", "15m"],
            ["target_10pct", "target_20pct", "trail_after_10pct_dd5pct"],
        ),
        (
            "monthly_expiry_day",
            monthly,
            True,
            ["1m", "3m", "5m", "10m", "15m"],
            ["target_10pct", "target_20pct", "trail_after_10pct_dd5pct"],
        ),
    ]


class MacdCrossLocationRunner:
    def __init__(self, data_root: Path = DATA_ROOT, output_root: Path = OUTPUT_ROOT):
        self.data_root = data_root
        self.output_root = output_root
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.runner = IndexOptionWalkForwardRunner(data_root=data_root, output_root=output_root / "_shared")

    def _simulate_variant(self, variant: MacdCrossVariant, descriptors: list[Any]) -> list[dict[str, Any]]:
        trades: list[dict[str, Any]] = []
        exit_catalog = EXPIRY_DAY_EXITS if variant.expiry_day_only else FULL_SERIES_EXITS
        exit_spec = exit_catalog[variant.exit_name]
        bar_minutes = _minutes_for_timeframe(variant.timeframe)

        for descriptor in descriptors:
            for option_type, option_path, symbol in (
                ("CE", descriptor.ce_path, descriptor.ce_symbol),
                ("PE", descriptor.pe_path, descriptor.pe_symbol),
            ):
                frame = _prepare_option_frame(descriptor, option_path, variant.timeframe, variant.expiry_day_only)
                if len(frame) < 40:
                    continue

                candles = frame.to_dict("records")
                closes = [float(candle["close"]) for candle in candles]
                macd_line, signal_line, histogram = compute_macd(closes)

                index = 1
                while index < len(candles):
                    prev_macd = macd_line[index - 1]
                    prev_signal = signal_line[index - 1]
                    current_macd = macd_line[index]
                    current_signal = signal_line[index]
                    current_hist = histogram[index]

                    bullish_cross = (
                        prev_macd is not None
                        and prev_signal is not None
                        and current_macd is not None
                        and current_signal is not None
                        and prev_macd <= prev_signal
                        and current_macd > current_signal
                    )
                    if not bullish_cross:
                        index += 1
                        continue

                    entry_price = float(candles[index]["close"])
                    if entry_price <= 0.0:
                        index += 1
                        continue

                    location = _location_bucket(float(current_macd), float(current_signal))
                    window = _analyze_trade_window(candles, index)
                    exit_result = _simulate_exit(candles, index, macd_line, exit_spec)
                    exit_idx = int(exit_result["exit_idx"])
                    exit_price = float(exit_result["exit_price"])
                    return_pct = ((exit_price - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0
                    holding_bars = max(exit_idx - index, 0)

                    trades.append(
                        {
                            "group_name": variant.group_name,
                            "timeframe": variant.timeframe,
                            "exit_name": variant.exit_name,
                            "expiry_day_only": variant.expiry_day_only,
                            "cross_location": location,
                            "series_id": descriptor.series_id,
                            "underlying": descriptor.underlying,
                            "expiry_kind": descriptor.expiry_kind,
                            "expiry": descriptor.expiry,
                            "option_type": option_type,
                            "symbol": symbol,
                            "strike": descriptor.selected_strike,
                            "entry_time": pd.Timestamp(candles[index]["time"]).isoformat(),
                            "entry_price": round(entry_price, 4),
                            "entry_macd": round(float(current_macd), 6),
                            "entry_signal": round(float(current_signal), 6),
                            "entry_histogram": round(float(current_hist), 6) if current_hist is not None else None,
                            "entry_macd_pct": round((float(current_macd) / entry_price) * 100.0, 6),
                            "exit_time": str(exit_result["exit_time"]),
                            "exit_price": round(exit_price, 4),
                            "exit_reason": str(exit_result["exit_reason"]),
                            "return_pct": round(return_pct, 4),
                            "max_possible_return_pct": window["max_return_pct"],
                            "max_possible_price": window["max_price"],
                            "holding_bars": holding_bars,
                            "holding_minutes": holding_bars * bar_minutes,
                            "bars_to_max": window["bars_to_max"],
                        }
                    )
                    index = exit_idx + 1

        return trades

    def run(self) -> dict[str, Any]:
        all_trades: list[dict[str, Any]] = []
        all_variant_rows: list[dict[str, Any]] = []
        group_results: dict[str, Any] = {}

        for group_name, descriptors, expiry_day_only, timeframes, exits in _group_definitions(self.runner):
            group_trades: list[dict[str, Any]] = []
            location_summary: dict[str, dict[str, Any]] = {}
            variant_rows: list[dict[str, Any]] = []
            by_location_variant: dict[str, list[dict[str, Any]]] = defaultdict(list)

            for timeframe in timeframes:
                for exit_name in exits:
                    variant = MacdCrossVariant(
                        group_name=group_name,
                        timeframe=timeframe,
                        exit_name=exit_name,
                        expiry_day_only=expiry_day_only,
                    )
                    trades = self._simulate_variant(variant, descriptors)
                    group_trades.extend(trades)

                    bucketed: dict[str, list[dict[str, Any]]] = defaultdict(list)
                    for trade in trades:
                        bucketed[trade["cross_location"]].append(trade)

                    for location, location_trades in bucketed.items():
                        summary = _summarize_trades(location_trades)
                        row = {
                            "variant_key": f"{variant.key}|{location}",
                            "group_name": group_name,
                            "timeframe": timeframe,
                            "exit_name": exit_name,
                            "expiry_day_only": expiry_day_only,
                            "cross_location": location,
                            "score": _score(summary),
                            **summary,
                        }
                        variant_rows.append(row)
                        by_location_variant[location].append(row)

            for location in ("below_zero", "zero_straddle", "above_zero"):
                location_trades = [trade for trade in group_trades if trade["cross_location"] == location]
                location_summary[location] = {
                    "overall": _summarize_trades(location_trades),
                    "by_underlying": {
                        underlying: _summarize_trades(
                            [trade for trade in location_trades if trade["underlying"] == underlying]
                        )
                        for underlying in sorted({trade["underlying"] for trade in location_trades})
                    },
                }

            best_by_location = {}
            for location, rows in by_location_variant.items():
                ordered = sorted(
                    rows,
                    key=lambda row: (
                        float(row["avg_return_pct"]),
                        float(row["win_rate"]),
                        float(row["median_return_pct"]),
                    ),
                    reverse=True,
                )
                if ordered:
                    best_by_location[location] = ordered[0]

            group_results[group_name] = {
                "series_count": len(descriptors),
                "trade_count": len(group_trades),
                "location_summary": location_summary,
                "best_by_location": best_by_location,
            }
            all_trades.extend(group_trades)
            all_variant_rows.extend(variant_rows)

        results = {
            "generated_at": datetime.now().astimezone().isoformat(),
            "data_root": str(self.data_root),
            "output_root": str(self.output_root),
            "groups": group_results,
        }
        self._write_outputs(results, all_variant_rows, all_trades)
        return results

    def _write_outputs(
        self,
        results: dict[str, Any],
        variant_rows: list[dict[str, Any]],
        trades: list[dict[str, Any]],
    ) -> None:
        self.output_root.mkdir(parents=True, exist_ok=True)
        summary_path = self.output_root / "summary.json"
        summary_path.write_text(json.dumps(results, indent=2))

        variants_path = self.output_root / "variant_results.csv"
        variant_fields = sorted({key for row in variant_rows for key in row.keys()})
        with variants_path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=variant_fields)
            writer.writeheader()
            for row in variant_rows:
                writer.writerow(row)

        trades_path = self.output_root / "trade_results.csv"
        fieldnames = sorted({key for trade in trades for key in trade.keys()})
        with trades_path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for trade in trades:
                writer.writerow(trade)

        report_path = self.output_root / "report.md"
        lines = [
            "# MACD Signal Cross Location Sweep",
            "",
            f"Generated: {results['generated_at']}",
            f"Dataset root: `{results['data_root']}`",
            "",
        ]
        for group_name, group in results["groups"].items():
            lines.extend([f"## {group_name}", ""])
            for location, detail in group["location_summary"].items():
                overall = detail["overall"]
                lines.extend(
                    [
                        f"### {location}",
                        "",
                        f"- Opportunities: {overall['opportunities']}",
                        f"- Win rate: {overall['win_rate'] * 100:.2f}%",
                        f"- Avg return: {overall['avg_return_pct']:.2f}%",
                        f"- Median return: {overall['median_return_pct']:.2f}%",
                        f"- Avg holding: {overall['avg_holding_minutes']:.1f} minutes",
                        "",
                    ]
                )
                best = group["best_by_location"].get(location)
                if best:
                    lines.extend(
                        [
                            (
                                f"- Best variant: `{best['timeframe']} | {best['exit_name']}`"
                                f" with avg return {best['avg_return_pct']:.2f}%"
                            ),
                            "",
                        ]
                    )
        report_path.write_text("\n".join(lines))


def main() -> None:
    runner = MacdCrossLocationRunner()
    results = runner.run()
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
