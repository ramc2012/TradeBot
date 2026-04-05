"""Premium-OHLC indicator sweep on the persisted index analytics dataset."""
from __future__ import annotations

import csv
import json
import math
import statistics
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

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
from analytics.technicals import (
    compute_adx,
    compute_cci,
    compute_ema_cross,
    compute_roc,
    compute_rsi,
)


OUTPUT_ROOT = DATA_ROOT / "indicator_sweep_ohlc"


@dataclass(frozen=True)
class IndicatorVariant:
    group_name: str
    timeframe: str
    indicator_name: str
    exit_name: str
    expiry_day_only: bool

    @property
    def key(self) -> str:
        mode = "expiry_day" if self.expiry_day_only else "series"
        return f"{self.group_name}|{mode}|{self.timeframe}|{self.indicator_name}|{self.exit_name}"


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
    base = (median_return * 0.6) + (avg_return * 0.4)
    return round(base * max(win_rate, 0.01) * math.log1p(opportunities), 6)


def _summarize_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [float(trade["return_pct"]) for trade in trades]
    wins = [value for value in returns if value > 0.0]
    losses = [value for value in returns if value <= 0.0]
    holds = [float(trade["holding_minutes"]) for trade in trades]
    possible = [float(trade["max_possible_return_pct"]) for trade in trades]
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
    }


def _minutes_for_timeframe(timeframe: str) -> int:
    return int(TIMEFRAME_MAP[timeframe].replace("min", ""))


def _macd_zero_signals(frame: pd.DataFrame) -> list[bool]:
    closes = [float(value) for value in frame["close"].tolist()]
    macd_line, _, _ = compute_macd(closes)
    signals = [False] * len(frame)
    for index in range(1, len(frame)):
        previous = macd_line[index - 1]
        current = macd_line[index]
        signals[index] = previous is not None and current is not None and previous <= 0.0 < current
    return signals


def _rsi_50_signals(frame: pd.DataFrame) -> list[bool]:
    closes = [float(value) for value in frame["close"].tolist()]
    rsi_values = compute_rsi(closes, period=14)
    signals = [False] * len(frame)
    for index in range(1, len(frame)):
        previous = rsi_values[index - 1]
        current = rsi_values[index]
        signals[index] = previous is not None and current is not None and previous <= 50.0 < current
    return signals


def _roc_zero_signals(frame: pd.DataFrame) -> list[bool]:
    closes = [float(value) for value in frame["close"].tolist()]
    roc_values = compute_roc(closes, period=9)
    signals = [False] * len(frame)
    for index in range(1, len(frame)):
        previous = roc_values[index - 1]
        current = roc_values[index]
        signals[index] = previous is not None and current is not None and previous <= 0.0 < current
    return signals


def _ema_cross_signals(frame: pd.DataFrame) -> list[bool]:
    closes = [float(value) for value in frame["close"].tolist()]
    ema_fast, ema_slow = compute_ema_cross(closes, fast_period=9, slow_period=21)
    signals = [False] * len(frame)
    for index in range(1, len(frame)):
        prev_fast = ema_fast[index - 1]
        prev_slow = ema_slow[index - 1]
        cur_fast = ema_fast[index]
        cur_slow = ema_slow[index]
        signals[index] = (
            prev_fast is not None
            and prev_slow is not None
            and cur_fast is not None
            and cur_slow is not None
            and prev_fast <= prev_slow
            and cur_fast > cur_slow
        )
    return signals


def _cci_zero_signals(frame: pd.DataFrame) -> list[bool]:
    highs = [float(value) for value in frame["high"].tolist()]
    lows = [float(value) for value in frame["low"].tolist()]
    closes = [float(value) for value in frame["close"].tolist()]
    cci_values = compute_cci(highs, lows, closes, period=20)
    signals = [False] * len(frame)
    for index in range(1, len(frame)):
        previous = cci_values[index - 1]
        current = cci_values[index]
        signals[index] = previous is not None and current is not None and previous <= 0.0 < current
    return signals


def _adx_di_signals(frame: pd.DataFrame) -> list[bool]:
    highs = [float(value) for value in frame["high"].tolist()]
    lows = [float(value) for value in frame["low"].tolist()]
    closes = [float(value) for value in frame["close"].tolist()]
    adx_values, plus_di, minus_di = compute_adx(highs, lows, closes, period=14)
    signals = [False] * len(frame)
    for index in range(1, len(frame)):
        previous_plus = plus_di[index - 1]
        previous_minus = minus_di[index - 1]
        current_plus = plus_di[index]
        current_minus = minus_di[index]
        previous_adx = adx_values[index - 1]
        current_adx = adx_values[index]
        signals[index] = (
            previous_plus is not None
            and previous_minus is not None
            and current_plus is not None
            and current_minus is not None
            and previous_adx is not None
            and current_adx is not None
            and previous_plus <= previous_minus
            and current_plus > current_minus
            and current_adx >= 20.0
            and current_adx >= previous_adx
        )
    return signals


INDICATOR_BUILDERS: dict[str, Callable[[pd.DataFrame], list[bool]]] = {
    "macd_zero": _macd_zero_signals,
    "rsi_50_cross": _rsi_50_signals,
    "roc_zero": _roc_zero_signals,
    "ema_9_21_cross": _ema_cross_signals,
    "cci_zero": _cci_zero_signals,
    "adx_di_cross": _adx_di_signals,
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
            ["target_30pct", "trail_after_20pct_dd10pct", "trail_after_30pct_dd15pct"],
        ),
        (
            "monthly_series",
            monthly,
            False,
            ["3m", "5m", "10m", "15m", "30m"],
            ["target_30pct", "trail_after_20pct_dd10pct", "trail_after_30pct_dd15pct"],
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


class OptionIndicatorSweepRunner:
    def __init__(self, data_root: Path = DATA_ROOT, output_root: Path = OUTPUT_ROOT):
        self.data_root = data_root
        self.output_root = output_root
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.runner = IndexOptionWalkForwardRunner(data_root=data_root, output_root=output_root / "_shared")

    def _simulate_variant(self, variant: IndicatorVariant, descriptors: list[Any]) -> list[dict[str, Any]]:
        trades: list[dict[str, Any]] = []
        exit_catalog = EXPIRY_DAY_EXITS if variant.expiry_day_only else FULL_SERIES_EXITS
        exit_spec = exit_catalog[variant.exit_name]
        bar_minutes = _minutes_for_timeframe(variant.timeframe)
        indicator_builder = INDICATOR_BUILDERS[variant.indicator_name]

        for descriptor in descriptors:
            for option_type, option_path, symbol in (
                ("CE", descriptor.ce_path, descriptor.ce_symbol),
                ("PE", descriptor.pe_path, descriptor.pe_symbol),
            ):
                frame = _prepare_option_frame(descriptor, option_path, variant.timeframe, variant.expiry_day_only)
                if len(frame) < 40:
                    continue
                signals = indicator_builder(frame)
                candles = frame.to_dict("records")
                closes = [float(candle["close"]) for candle in candles]
                macd_line, _, _ = compute_macd(closes)

                index = 1
                while index < len(candles):
                    if not signals[index]:
                        index += 1
                        continue
                    exit_result = _simulate_exit(candles, index, macd_line, exit_spec)
                    exit_idx = int(exit_result["exit_idx"])
                    entry_price = float(candles[index]["close"])
                    exit_price = float(exit_result["exit_price"])
                    return_pct = ((exit_price - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0
                    max_stats = _analyze_trade_window(candles, index)
                    holding_bars = max(exit_idx - index, 0)
                    trades.append(
                        {
                            "group_name": variant.group_name,
                            "indicator": variant.indicator_name,
                            "timeframe": variant.timeframe,
                            "exit_name": variant.exit_name,
                            "expiry_day_only": variant.expiry_day_only,
                            "series_id": descriptor.series_id,
                            "underlying": descriptor.underlying,
                            "expiry_kind": descriptor.expiry_kind,
                            "expiry": descriptor.expiry,
                            "option_type": option_type,
                            "symbol": symbol,
                            "strike": descriptor.selected_strike,
                            "entry_time": pd.Timestamp(candles[index]["time"]).isoformat(),
                            "entry_price": round(entry_price, 4),
                            "exit_time": str(exit_result["exit_time"]),
                            "exit_price": round(exit_price, 4),
                            "exit_reason": str(exit_result["exit_reason"]),
                            "return_pct": round(return_pct, 4),
                            "holding_minutes": holding_bars * bar_minutes,
                            "max_possible_return_pct": max_stats["max_return_pct"],
                        }
                    )
                    index = exit_idx + 1
        return trades

    def run(self) -> dict[str, Any]:
        all_variant_rows: list[dict[str, Any]] = []
        all_trade_rows: list[dict[str, Any]] = []
        summary: dict[str, Any] = {
            "generated_at": datetime.now().astimezone().isoformat(),
            "data_root": str(self.data_root),
            "output_root": str(self.output_root),
            "groups": {},
        }

        for group_name, descriptors, expiry_day_only, timeframes, exit_names in _group_definitions(self.runner):
            rows: list[dict[str, Any]] = []
            for indicator_name in INDICATOR_BUILDERS:
                for timeframe in timeframes:
                    for exit_name in exit_names:
                        variant = IndicatorVariant(
                            group_name=group_name,
                            timeframe=timeframe,
                            indicator_name=indicator_name,
                            exit_name=exit_name,
                            expiry_day_only=expiry_day_only,
                        )
                        trades = self._simulate_variant(variant, descriptors)
                        variant_summary = _summarize_trades(trades)
                        row = {
                            "variant_key": variant.key,
                            "group_name": group_name,
                            "indicator": indicator_name,
                            "timeframe": timeframe,
                            "exit_name": exit_name,
                            "expiry_day_only": expiry_day_only,
                            "score": _score(variant_summary),
                            **variant_summary,
                        }
                        rows.append(row)
                        all_variant_rows.append(row)
                        all_trade_rows.extend(trades)

            rows.sort(key=lambda item: (item["score"], item["avg_return_pct"], item["opportunities"]), reverse=True)
            positive = [
                row
                for row in rows
                if row["avg_return_pct"] > 0.0 and row["opportunities"] >= (20 if "expiry_day" not in group_name else 8)
            ]

            best_per_indicator = []
            for indicator_name in INDICATOR_BUILDERS:
                indicator_rows = [row for row in rows if row["indicator"] == indicator_name]
                if indicator_rows:
                    best_per_indicator.append(indicator_rows[0])

            summary["groups"][group_name] = {
                "series_count": len(descriptors),
                "best_overall": rows[0] if rows else None,
                "top_ranked": rows[:10],
                "positive_expectancy": positive[:10],
                "best_per_indicator": best_per_indicator,
            }

        self._write_outputs(summary, all_variant_rows, all_trade_rows)
        return summary

    def _write_outputs(
        self,
        summary: dict[str, Any],
        variant_rows: list[dict[str, Any]],
        trade_rows: list[dict[str, Any]],
    ) -> None:
        self.output_root.mkdir(parents=True, exist_ok=True)
        (self.output_root / "summary.json").write_text(json.dumps(summary, indent=2))

        variant_path = self.output_root / "variant_results.csv"
        if variant_rows:
            variant_fields = list(variant_rows[0].keys())
            with variant_path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=variant_fields)
                writer.writeheader()
                writer.writerows(variant_rows)

        trade_path = self.output_root / "trade_results.csv"
        if trade_rows:
            trade_fields = list(trade_rows[0].keys())
            with trade_path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=trade_fields)
                writer.writeheader()
                writer.writerows(trade_rows)

        report_lines = [
            "# Option Premium Indicator Sweep",
            "",
            f"Generated: {summary['generated_at']}",
            f"Dataset root: `{summary['data_root']}`",
            "",
        ]
        for group_name, group_summary in summary["groups"].items():
            report_lines.append(f"## {group_name}")
            report_lines.append("")
            best = group_summary["best_overall"]
            if best:
                report_lines.extend(
                    [
                        f"- Best overall: `{best['indicator']} | {best['timeframe']} | {best['exit_name']}`",
                        f"- Opportunities: {best['opportunities']}",
                        f"- Win rate: {best['win_rate'] * 100:.2f}%",
                        f"- Avg return: {best['avg_return_pct']:.2f}%",
                        f"- Median return: {best['median_return_pct']:.2f}%",
                        f"- Avg win / loss: {best['avg_win_return_pct']:.2f}% / {best['avg_loss_return_pct']:.2f}%",
                        "",
                    ]
                )
        (self.output_root / "report.md").write_text("\n".join(report_lines))


def main() -> None:
    summary = OptionIndicatorSweepRunner().run()
    for group_name, group in summary["groups"].items():
        print(group_name, group["best_overall"])


if __name__ == "__main__":
    main()
