"""Walk-forward analysis on the persisted index analytics dataset.

This module evaluates a premium-based long-only MACD zero-cross entry on the
ATM CE/PE contract selected from the spot price at the start of each expiry
series.  It runs on the stored minute dataset under
``backend/runtime/index_analytics_data`` and compares timeframe / RSI-filter /
exit-style variants on weekly, monthly, and expiry-day slices.

The output is written back into the runtime analytics folder so future analysis
can reuse it without refetching market data.
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd

from analysis.macd_engine import compute_macd
from analytics.technicals import compute_rsi


DATA_ROOT = Path(__file__).resolve().parent.parent / "runtime" / "index_analytics_data"
OUTPUT_ROOT = DATA_ROOT / "walkforward_macd_rsi"

TIMEFRAME_MAP = {
    "1m": "1min",
    "3m": "3min",
    "5m": "5min",
    "10m": "10min",
    "15m": "15min",
    "30m": "30min",
}

RSI_FILTERS: dict[str, Optional[float]] = {
    "macd_only": None,
    "rsi_55": 55.0,
    "rsi_60": 60.0,
}

FULL_SERIES_EXITS: dict[str, dict[str, Any]] = {
    "macd_reversal": {"kind": "macd_reversal"},
    "target_20pct": {"kind": "target", "target_pct": 20.0},
    "target_30pct": {"kind": "target", "target_pct": 30.0},
    "trail_after_20pct_dd10pct": {
        "kind": "trailing",
        "activation_pct": 20.0,
        "trail_drawdown_pct": 10.0,
    },
    "trail_after_30pct_dd15pct": {
        "kind": "trailing",
        "activation_pct": 30.0,
        "trail_drawdown_pct": 15.0,
    },
    "hold_to_expiry": {"kind": "hold_to_end"},
}

EXPIRY_DAY_EXITS: dict[str, dict[str, Any]] = {
    "macd_reversal": {"kind": "macd_reversal"},
    "target_10pct": {"kind": "target", "target_pct": 10.0},
    "target_20pct": {"kind": "target", "target_pct": 20.0},
    "trail_after_10pct_dd5pct": {
        "kind": "trailing",
        "activation_pct": 10.0,
        "trail_drawdown_pct": 5.0,
    },
    "trail_after_20pct_dd10pct": {
        "kind": "trailing",
        "activation_pct": 20.0,
        "trail_drawdown_pct": 10.0,
    },
    "day_close": {"kind": "hold_to_end"},
}


@dataclass(frozen=True)
class ContractMeta:
    underlying: str
    expiry_kind: str
    expiry: str
    strike: float
    option_type: str
    trading_symbol: str
    file_path: str
    earliest_candle: pd.Timestamp
    latest_candle: pd.Timestamp
    candle_count: int


@dataclass(frozen=True)
class SeriesDescriptor:
    series_id: str
    underlying: str
    expiry_kind: str
    expiry: str
    selected_strike: float
    spot_at_start: float
    spot_start_time: str
    pair_start_time: str
    ce_path: str
    pe_path: str
    ce_symbol: str
    pe_symbol: str


@dataclass(frozen=True)
class StrategyVariant:
    timeframe: str
    rsi_filter_name: str
    exit_name: str
    expiry_day_only: bool

    @property
    def key(self) -> str:
        mode = "expiry_day" if self.expiry_day_only else "series"
        return f"{mode}|{self.timeframe}|{self.rsi_filter_name}|{self.exit_name}"


def _safe_float(value: Any) -> Optional[float]:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(numeric) or math.isinf(numeric):
        return None
    return numeric


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    if not items:
        return 0.0
    return round(float(statistics.fmean(items)), 4)


def _median(values: Iterable[float]) -> float:
    items = list(values)
    if not items:
        return 0.0
    return round(float(statistics.median(items)), 4)


def _walkforward_score(summary: dict[str, Any]) -> float:
    opportunities = float(summary.get("opportunities", 0) or 0)
    win_rate = float(summary.get("win_rate", 0.0) or 0.0)
    median_return = float(summary.get("median_return_pct", 0.0) or 0.0)
    avg_return = float(summary.get("avg_return_pct", 0.0) or 0.0)
    if opportunities <= 0:
        return float("-inf")
    base = (median_return * 0.7) + (avg_return * 0.3)
    return round(base * max(win_rate, 0.01) * math.log1p(opportunities), 6)


def _load_contract_index(data_root: Path) -> list[ContractMeta]:
    raw = json.loads((data_root / "contract_index.json").read_text())
    rows: list[ContractMeta] = []
    for item in raw.values():
        file_path = item.get("file_path")
        candle_count = int(item.get("candle_count") or 0)
        earliest = item.get("earliest_candle")
        latest = item.get("latest_candle")
        strike = item.get("strike")
        option_type = item.get("option_type")
        if (
            not file_path
            or candle_count <= 0
            or not earliest
            or not latest
            or strike is None
            or not option_type
        ):
            continue
        rows.append(
            ContractMeta(
                underlying=str(item["underlying"]),
                expiry_kind=str(item["expiry_kind"]),
                expiry=str(item["expiry"]),
                strike=float(strike),
                option_type=str(option_type),
                trading_symbol=str(item["trading_symbol"]),
                file_path=str(file_path),
                earliest_candle=pd.Timestamp(earliest),
                latest_candle=pd.Timestamp(latest),
                candle_count=candle_count,
            )
        )
    return rows


@lru_cache(maxsize=32)
def _load_csv_frame(path_str: str) -> pd.DataFrame:
    path = DATA_ROOT / path_str
    if not path.exists():
        raise FileNotFoundError(path)
    with gzip.open(path, "rt") as handle:
        df = pd.read_csv(handle, parse_dates=["time"])
    df = df.sort_values("time").reset_index(drop=True)
    for column in ("open", "high", "low", "close", "volume", "oi"):
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    return df


def _resample_frame(frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    if timeframe == "1m":
        return frame.copy()
    freq = TIMEFRAME_MAP[timeframe]
    indexed = frame.set_index("time").sort_index()
    resampled = (
        indexed.resample(freq, label="right", closed="right")
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
                "oi": "last",
            }
        )
        .dropna(subset=["open", "high", "low", "close"])
        .reset_index()
    )
    return resampled


@lru_cache(maxsize=128)
def _get_resampled_frame(path_str: str, timeframe: str) -> pd.DataFrame:
    return _resample_frame(_load_csv_frame(path_str), timeframe)


@lru_cache(maxsize=8)
def _get_spot_frame(underlying: str) -> pd.DataFrame:
    return _load_csv_frame(f"spot/underlying={underlying}/1minute.csv.gz")


def _spot_price_at_time(underlying: str, ts: pd.Timestamp) -> Optional[float]:
    spot = _get_spot_frame(underlying).set_index("time").sort_index()
    exact = spot.loc[:ts]
    if not exact.empty:
        return _safe_float(exact.iloc[-1]["close"])
    future = spot.loc[ts:]
    if not future.empty:
        return _safe_float(future.iloc[0]["close"])
    return None


def _build_series_descriptors(data_root: Path) -> list[SeriesDescriptor]:
    metas = _load_contract_index(data_root)
    by_group: dict[tuple[str, str, str], list[ContractMeta]] = {}
    for meta in metas:
        by_group.setdefault((meta.underlying, meta.expiry_kind, meta.expiry), []).append(meta)

    descriptors: list[SeriesDescriptor] = []
    for (underlying, expiry_kind, expiry), group in sorted(by_group.items()):
        ce_map = {meta.strike: meta for meta in group if meta.option_type == "CE"}
        pe_map = {meta.strike: meta for meta in group if meta.option_type == "PE"}
        common_strikes = sorted(set(ce_map) & set(pe_map))
        if not common_strikes:
            continue

        candidates: list[tuple[float, pd.Timestamp, ContractMeta, ContractMeta]] = []
        for strike in common_strikes:
            ce_meta = ce_map[strike]
            pe_meta = pe_map[strike]
            pair_start = max(ce_meta.earliest_candle, pe_meta.earliest_candle)
            pair_end = min(ce_meta.latest_candle, pe_meta.latest_candle)
            if pair_end <= pair_start:
                continue
            candidates.append((strike, pair_start, ce_meta, pe_meta))
        if not candidates:
            continue

        group_start = min(pair_start for _, pair_start, _, _ in candidates)
        group_start_day = group_start.date()
        spot_price = _spot_price_at_time(underlying, group_start)
        if spot_price is None:
            continue

        eligible = [
            candidate for candidate in candidates if candidate[1].date() == group_start_day
        ] or candidates
        strike, pair_start, ce_meta, pe_meta = min(
            eligible,
            key=lambda item: (abs(item[0] - spot_price), item[1], item[0]),
        )

        descriptors.append(
            SeriesDescriptor(
                series_id=f"{underlying}|{expiry_kind}|{expiry}",
                underlying=underlying,
                expiry_kind=expiry_kind,
                expiry=expiry,
                selected_strike=float(strike),
                spot_at_start=float(round(spot_price, 4)),
                spot_start_time=group_start.isoformat(),
                pair_start_time=pair_start.isoformat(),
                ce_path=ce_meta.file_path,
                pe_path=pe_meta.file_path,
                ce_symbol=ce_meta.trading_symbol,
                pe_symbol=pe_meta.trading_symbol,
            )
        )
    return descriptors


def _prepare_option_frame(
    descriptor: SeriesDescriptor,
    option_path: str,
    timeframe: str,
    expiry_day_only: bool,
) -> pd.DataFrame:
    frame = _get_resampled_frame(option_path, timeframe)
    pair_start = pd.Timestamp(descriptor.pair_start_time)
    frame = frame[frame["time"] >= pair_start].copy()
    if expiry_day_only:
        expiry_date = pd.Timestamp(descriptor.expiry).date()
        frame = frame[frame["time"].dt.date == expiry_date].copy()
    return frame.reset_index(drop=True)


def _exit_hold_to_end(candles: list[dict[str, Any]], entry_idx: int) -> dict[str, Any]:
    exit_idx = len(candles) - 1
    exit_price = float(candles[exit_idx]["close"])
    return {
        "exit_idx": exit_idx,
        "exit_price": exit_price,
        "exit_time": str(candles[exit_idx]["time"]),
        "exit_reason": "hold_to_end",
    }


def _exit_target(
    candles: list[dict[str, Any]],
    entry_idx: int,
    target_pct: float,
) -> dict[str, Any]:
    entry_price = float(candles[entry_idx]["close"])
    target_price = entry_price * (1.0 + target_pct / 100.0)
    for exit_idx in range(entry_idx + 1, len(candles)):
        high = float(candles[exit_idx]["high"])
        if high >= target_price:
            return {
                "exit_idx": exit_idx,
                "exit_price": target_price,
                "exit_time": str(candles[exit_idx]["time"]),
                "exit_reason": f"target_{int(target_pct)}pct_hit",
            }
    result = _exit_hold_to_end(candles, entry_idx)
    result["exit_reason"] = f"hold_to_end_target_{int(target_pct)}pct_not_hit"
    return result


def _exit_trailing(
    candles: list[dict[str, Any]],
    entry_idx: int,
    activation_pct: float,
    trail_drawdown_pct: float,
) -> dict[str, Any]:
    entry_price = float(candles[entry_idx]["close"])
    activation_price = entry_price * (1.0 + activation_pct / 100.0)
    peak_price = entry_price
    activated = False
    for exit_idx in range(entry_idx + 1, len(candles)):
        candle = candles[exit_idx]
        high = float(candle["high"])
        close = float(candle["close"])
        if high > peak_price:
            peak_price = high
        if not activated and high >= activation_price:
            activated = True
        if activated and close <= peak_price * (1.0 - trail_drawdown_pct / 100.0):
            return {
                "exit_idx": exit_idx,
                "exit_price": close,
                "exit_time": str(candle["time"]),
                "exit_reason": (
                    f"trail_after_{int(activation_pct)}pct_dd{int(trail_drawdown_pct)}pct"
                ),
            }
    result = _exit_hold_to_end(candles, entry_idx)
    result["exit_reason"] = (
        f"hold_to_end_trail_after_{int(activation_pct)}pct_dd{int(trail_drawdown_pct)}pct"
    )
    return result


def _exit_macd_reversal(
    candles: list[dict[str, Any]],
    entry_idx: int,
    macd_line: list[Optional[float]],
) -> dict[str, Any]:
    for exit_idx in range(entry_idx + 1, len(candles)):
        previous = macd_line[exit_idx - 1]
        current = macd_line[exit_idx]
        if previous is None or current is None:
            continue
        if previous >= 0.0 and current < 0.0:
            return {
                "exit_idx": exit_idx,
                "exit_price": float(candles[exit_idx]["close"]),
                "exit_time": str(candles[exit_idx]["time"]),
                "exit_reason": "macd_reversal",
            }
    result = _exit_hold_to_end(candles, entry_idx)
    result["exit_reason"] = "hold_to_end_macd_no_reversal"
    return result


def _simulate_exit(
    candles: list[dict[str, Any]],
    entry_idx: int,
    macd_line: list[Optional[float]],
    exit_spec: dict[str, Any],
) -> dict[str, Any]:
    kind = exit_spec["kind"]
    if kind == "hold_to_end":
        return _exit_hold_to_end(candles, entry_idx)
    if kind == "target":
        return _exit_target(candles, entry_idx, float(exit_spec["target_pct"]))
    if kind == "trailing":
        return _exit_trailing(
            candles,
            entry_idx,
            float(exit_spec["activation_pct"]),
            float(exit_spec["trail_drawdown_pct"]),
        )
    if kind == "macd_reversal":
        return _exit_macd_reversal(candles, entry_idx, macd_line)
    raise ValueError(f"Unsupported exit kind: {kind}")


def _analyze_trade_window(candles: list[dict[str, Any]], entry_idx: int) -> dict[str, Any]:
    entry_price = float(candles[entry_idx]["close"])
    max_price = entry_price
    max_idx = entry_idx
    for idx in range(entry_idx, len(candles)):
        high = float(candles[idx]["high"])
        if high > max_price:
            max_price = high
            max_idx = idx
    max_return_pct = ((max_price - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0
    return {
        "entry_price": round(entry_price, 4),
        "max_price": round(max_price, 4),
        "max_return_pct": round(max_return_pct, 4),
        "bars_to_max": max(max_idx - entry_idx, 0),
    }


def _simulate_trades_for_frame(
    descriptor: SeriesDescriptor,
    frame: pd.DataFrame,
    option_type: str,
    variant: StrategyVariant,
) -> list[dict[str, Any]]:
    if len(frame) < 40:
        return []

    candles = frame.to_dict("records")
    closes = [float(candle["close"]) for candle in candles]
    macd_line, _, _ = compute_macd(closes)
    rsi_values = compute_rsi(closes)
    min_rsi = RSI_FILTERS[variant.rsi_filter_name]
    exit_spec = (
        EXPIRY_DAY_EXITS[variant.exit_name]
        if variant.expiry_day_only
        else FULL_SERIES_EXITS[variant.exit_name]
    )

    trades: list[dict[str, Any]] = []
    index = 1
    while index < len(candles):
        previous = macd_line[index - 1]
        current = macd_line[index]
        if previous is None or current is None or not (previous <= 0.0 and current > 0.0):
            index += 1
            continue

        entry_rsi = rsi_values[index]
        if min_rsi is not None and (entry_rsi is None or float(entry_rsi) < min_rsi):
            index += 1
            continue

        window = _analyze_trade_window(candles, index)
        exit_result = _simulate_exit(candles, index, macd_line, exit_spec)
        exit_idx = int(exit_result["exit_idx"])
        exit_price = float(exit_result["exit_price"])
        entry_price = float(candles[index]["close"])
        return_pct = ((exit_price - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0
        holding_bars = max(exit_idx - index, 0)
        bar_minutes = int(TIMEFRAME_MAP[variant.timeframe].replace("min", "")) if variant.timeframe != "1m" else 1

        trades.append(
            {
                "series_id": descriptor.series_id,
                "underlying": descriptor.underlying,
                "expiry_kind": descriptor.expiry_kind,
                "expiry": descriptor.expiry,
                "expiry_day_only": variant.expiry_day_only,
                "timeframe": variant.timeframe,
                "rsi_filter": variant.rsi_filter_name,
                "exit_strategy": variant.exit_name,
                "option_type": option_type,
                "strike": descriptor.selected_strike,
                "symbol": descriptor.ce_symbol if option_type == "CE" else descriptor.pe_symbol,
                "spot_at_series_start": descriptor.spot_at_start,
                "entry_time": pd.Timestamp(candles[index]["time"]).isoformat(),
                "entry_price": round(entry_price, 4),
                "entry_macd": round(float(current), 6),
                "entry_rsi": round(float(entry_rsi), 4) if entry_rsi is not None else None,
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


class IndexOptionWalkForwardRunner:
    def __init__(self, data_root: Path = DATA_ROOT, output_root: Path = OUTPUT_ROOT):
        self.data_root = data_root
        self.output_root = output_root
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.descriptors = _build_series_descriptors(data_root)
        self._trade_cache: dict[str, list[dict[str, Any]]] = {}

    def _trade_cache_key(self, descriptor: SeriesDescriptor, variant: StrategyVariant) -> str:
        return f"{descriptor.series_id}|{variant.key}"

    def _get_variant_trades(
        self,
        descriptor: SeriesDescriptor,
        variant: StrategyVariant,
    ) -> list[dict[str, Any]]:
        cache_key = self._trade_cache_key(descriptor, variant)
        if cache_key in self._trade_cache:
            return self._trade_cache[cache_key]

        option_frames = {
            "CE": _prepare_option_frame(descriptor, descriptor.ce_path, variant.timeframe, variant.expiry_day_only),
            "PE": _prepare_option_frame(descriptor, descriptor.pe_path, variant.timeframe, variant.expiry_day_only),
        }

        trades: list[dict[str, Any]] = []
        for option_type, frame in option_frames.items():
            trades.extend(_simulate_trades_for_frame(descriptor, frame, option_type, variant))

        self._trade_cache[cache_key] = trades
        return trades

    @staticmethod
    def _summarize_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
        returns = [float(trade["return_pct"]) for trade in trades]
        possible = [float(trade["max_possible_return_pct"]) for trade in trades]
        holds = [float(trade["holding_minutes"]) for trade in trades]
        rsi_values = [float(trade["entry_rsi"]) for trade in trades if trade.get("entry_rsi") is not None]
        capture = [
            (float(trade["return_pct"]) / float(trade["max_possible_return_pct"]))
            for trade in trades
            if float(trade["max_possible_return_pct"]) > 0.0
        ]
        wins = [value for value in returns if value > 0.0]
        losses = [value for value in returns if value <= 0.0]
        return {
            "opportunities": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades), 4) if trades else 0.0,
            "avg_return_pct": _mean(returns),
            "median_return_pct": _median(returns),
            "max_realized_return_pct": round(max(returns), 4) if returns else 0.0,
            "avg_max_possible_return_pct": _mean(possible),
            "median_max_possible_return_pct": _median(possible),
            "max_possible_return_pct": round(max(possible), 4) if possible else 0.0,
            "avg_holding_minutes": _mean(holds),
            "median_holding_minutes": _median(holds),
            "avg_entry_rsi": _mean(rsi_values),
            "median_entry_rsi": _median(rsi_values),
            "avg_capture_ratio": _mean(capture),
        }

    def _aggregate_variant_over_series(
        self,
        descriptors: list[SeriesDescriptor],
        variant: StrategyVariant,
    ) -> dict[str, Any]:
        trades: list[dict[str, Any]] = []
        series_ids: list[str] = []
        for descriptor in descriptors:
            trades.extend(self._get_variant_trades(descriptor, variant))
            series_ids.append(descriptor.series_id)
        summary = self._summarize_trades(trades)
        summary["series_count"] = len(descriptors)
        summary["series_ids"] = series_ids
        return summary

    @staticmethod
    def _variants_for_mode(expiry_day_only: bool) -> list[StrategyVariant]:
        exits = EXPIRY_DAY_EXITS if expiry_day_only else FULL_SERIES_EXITS
        return [
            StrategyVariant(
                timeframe=timeframe,
                rsi_filter_name=rsi_name,
                exit_name=exit_name,
                expiry_day_only=expiry_day_only,
            )
            for timeframe in TIMEFRAME_MAP
            for rsi_name in RSI_FILTERS
            for exit_name in exits
        ]

    def _run_group(
        self,
        descriptors: list[SeriesDescriptor],
        expiry_day_only: bool,
        group_name: str,
    ) -> dict[str, Any]:
        variants = self._variants_for_mode(expiry_day_only)
        ordered = sorted(
            descriptors,
            key=lambda item: (pd.Timestamp(item.expiry), pd.Timestamp(item.pair_start_time), item.underlying),
        )
        if not ordered:
            return {
                "group": group_name,
                "series_count": 0,
                "windows": [],
                "selected_variant_counts": {},
                "overall": self._summarize_trades([]),
                "recommended_variant": None,
            }

        min_train = 3 if len(ordered) >= 6 else 2
        windows: list[dict[str, Any]] = []
        selected_variant_counts: dict[str, int] = {}
        oos_trades: list[dict[str, Any]] = []

        for test_index in range(min_train, len(ordered)):
            train_set = ordered[:test_index]
            test_series = ordered[test_index]
            best_variant: Optional[StrategyVariant] = None
            best_train_summary: Optional[dict[str, Any]] = None
            best_score = float("-inf")

            for variant in variants:
                train_summary = self._aggregate_variant_over_series(train_set, variant)
                score = _walkforward_score(train_summary)
                if score > best_score:
                    best_score = score
                    best_variant = variant
                    best_train_summary = train_summary

            if best_variant is None or best_train_summary is None:
                continue

            test_trades = self._get_variant_trades(test_series, best_variant)
            test_summary = self._summarize_trades(test_trades)
            selected_variant_counts[best_variant.key] = selected_variant_counts.get(best_variant.key, 0) + 1

            for trade in test_trades:
                trade["walkforward_group"] = group_name
                trade["walkforward_window"] = test_index - min_train + 1
                trade["chosen_variant_key"] = best_variant.key
                trade["train_consistency_score"] = best_score

            oos_trades.extend(test_trades)
            windows.append(
                {
                    "window_index": test_index - min_train + 1,
                    "train_series_count": len(train_set),
                    "test_series_id": test_series.series_id,
                    "test_underlying": test_series.underlying,
                    "test_expiry": test_series.expiry,
                    "selected_variant": {
                        "key": best_variant.key,
                        "timeframe": best_variant.timeframe,
                        "rsi_filter": best_variant.rsi_filter_name,
                        "exit_strategy": best_variant.exit_name,
                    },
                    "train_summary": {
                        "score": best_score,
                        **best_train_summary,
                    },
                    "test_summary": test_summary,
                }
            )

        overall = self._summarize_trades(oos_trades)
        variant_leaderboard = []
        for variant_key, count in sorted(selected_variant_counts.items(), key=lambda item: (-item[1], item[0])):
            variant_trades = [trade for trade in oos_trades if trade.get("chosen_variant_key") == variant_key]
            variant_leaderboard.append(
                {
                    "variant_key": variant_key,
                    "windows_selected": count,
                    **self._summarize_trades(variant_trades),
                }
            )

        recommended_variant = variant_leaderboard[0]["variant_key"] if variant_leaderboard else None
        by_underlying: dict[str, dict[str, Any]] = {}
        for underlying in sorted({descriptor.underlying for descriptor in ordered}):
            underlying_trades = [trade for trade in oos_trades if trade["underlying"] == underlying]
            by_underlying[underlying] = self._summarize_trades(underlying_trades)

        return {
            "group": group_name,
            "series_count": len(ordered),
            "walkforward_windows": len(windows),
            "selected_variant_counts": selected_variant_counts,
            "recommended_variant": recommended_variant,
            "overall": overall,
            "by_underlying": by_underlying,
            "variant_leaderboard": variant_leaderboard,
            "windows": windows,
            "oos_trades": oos_trades,
        }

    def run(self) -> dict[str, Any]:
        grouped: dict[str, list[SeriesDescriptor]] = {
            "weekly_series": [descriptor for descriptor in self.descriptors if descriptor.expiry_kind == "weekly"],
            "monthly_series": [descriptor for descriptor in self.descriptors if descriptor.expiry_kind == "monthly"],
        }

        results = {
            "generated_at": datetime.now().astimezone().isoformat(),
            "data_root": str(self.data_root),
            "output_root": str(self.output_root),
            "series_descriptors": [descriptor.__dict__ for descriptor in self.descriptors],
            "groups": {},
        }

        all_oos_trades: list[dict[str, Any]] = []
        for key, descriptors in grouped.items():
            series_result = self._run_group(descriptors, expiry_day_only=False, group_name=key)
            expiry_day_result = self._run_group(
                descriptors,
                expiry_day_only=True,
                group_name=f"{key}_expiry_day",
            )
            results["groups"][key] = {k: v for k, v in series_result.items() if k != "oos_trades"}
            results["groups"][f"{key}_expiry_day"] = {
                k: v for k, v in expiry_day_result.items() if k != "oos_trades"
            }
            all_oos_trades.extend(series_result["oos_trades"])
            all_oos_trades.extend(expiry_day_result["oos_trades"])

        results["overall"] = self._summarize_trades(all_oos_trades)
        self._write_outputs(results, all_oos_trades)
        return results

    def _write_outputs(self, results: dict[str, Any], trades: list[dict[str, Any]]) -> None:
        self.output_root.mkdir(parents=True, exist_ok=True)
        summary_path = self.output_root / "summary.json"
        summary_path.write_text(json.dumps(results, indent=2))

        trades_path = self.output_root / "oos_trades.csv"
        fieldnames = sorted({key for trade in trades for key in trade.keys()})
        with trades_path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for trade in trades:
                writer.writerow(trade)

        report_path = self.output_root / "report.md"
        lines = [
            "# Index Option MACD/RSI Walk-Forward",
            "",
            f"Generated: {results['generated_at']}",
            f"Dataset root: `{results['data_root']}`",
            "",
            "## Overall OOS",
            "",
            (
                f"- Opportunities: {results['overall']['opportunities']}\n"
                f"- Win rate: {results['overall']['win_rate'] * 100:.2f}%\n"
                f"- Avg return: {results['overall']['avg_return_pct']:.2f}%\n"
                f"- Median return: {results['overall']['median_return_pct']:.2f}%\n"
                f"- Max possible return: {results['overall']['max_possible_return_pct']:.2f}%\n"
                f"- Avg holding: {results['overall']['avg_holding_minutes']:.1f} minutes"
            ),
            "",
        ]
        for group_name, group in results["groups"].items():
            overall = group["overall"]
            lines.extend(
                [
                    f"## {group_name}",
                    "",
                    f"- Recommended variant: `{group['recommended_variant']}`",
                    f"- Walk-forward windows: {group['walkforward_windows']}",
                    f"- Opportunities: {overall['opportunities']}",
                    f"- Win rate: {overall['win_rate'] * 100:.2f}%",
                    f"- Avg return: {overall['avg_return_pct']:.2f}%",
                    f"- Median return: {overall['median_return_pct']:.2f}%",
                    f"- Avg max possible: {overall['avg_max_possible_return_pct']:.2f}%",
                    f"- Max possible return: {overall['max_possible_return_pct']:.2f}%",
                    f"- Avg holding: {overall['avg_holding_minutes']:.1f} minutes",
                    "",
                ]
            )
        report_path.write_text("\n".join(lines))


def main() -> None:
    runner = IndexOptionWalkForwardRunner()
    results = runner.run()
    print(json.dumps(results["overall"], indent=2))
    for group_name, group in results["groups"].items():
        print(group_name, group["recommended_variant"], group["overall"]["win_rate"], group["overall"]["opportunities"])


if __name__ == "__main__":
    main()
