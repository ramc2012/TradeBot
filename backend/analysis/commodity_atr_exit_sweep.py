"""Chronological ATR stop/trail sweep over causal MP+OF entries.

The entry set comes from ``_commodity_wf_driver.py``.  Each variant changes
only risk management before the original market-led exit: no minimum hold, no
time stop, and the original value-migration/roll/end exit remains available.
Stops are checked before a newly-ratcheted trail on each one-minute OHLC bar,
which is conservative when intrabar ordering is unknowable.
"""
from __future__ import annotations

import bisect
import csv
import json
import math
import random
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from statistics import pstdev
from typing import Any, Optional

from analysis._commodity_wf_driver import load_sessions


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "runtime" / "index_analytics_data" / "commodity_walkforward"
TRADES_PATH = DATA_DIR / "edge_trades.csv"
OUTPUT_PATH = DATA_DIR / "atr_exit_sweep.json"


def _float(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _epoch(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


@dataclass(frozen=True)
class Variant:
    stop_atr: float
    arm_atr: float
    trail_atr: float

    @property
    def name(self) -> str:
        return f"stop_{self.stop_atr:g}_arm_{self.arm_atr:g}_trail_{self.trail_atr:g}"


def variants() -> list[Variant]:
    return [
        Variant(stop, arm, trail)
        for stop in (1.5, 2.0, 2.5, 3.0, 3.5)
        for arm in (2.0, 3.0, 4.0, 5.0)
        for trail in (1.5, 2.0, 2.5, 3.0)
    ]


def load_entries() -> list[dict[str, Any]]:
    with TRADES_PATH.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    out = []
    for row in rows:
        atr = _float(row.get("entry_atr_15m"))
        entry = _float(row.get("entry_price"))
        exit_price = _float(row.get("exit_price"))
        if not atr or not entry or exit_price is None:
            continue
        item = dict(row)
        item.update({
            "atr": atr,
            "entry": entry,
            "original_exit": exit_price,
            "entry_epoch": _epoch(str(row["entry_time"])),
            "exit_epoch": _epoch(str(row["exit_time"])),
            "date": date.fromisoformat(str(row["session_date"])),
        })
        out.append(item)
    return sorted(out, key=lambda row: (row["date"], row["entry_epoch"]))


def load_paths(entries: list[dict[str, Any]]) -> dict[str, tuple[list[float], list[dict[str, Any]]]]:
    paths: dict[str, tuple[list[float], list[dict[str, Any]]]] = {}
    for root in sorted({str(row["underlying"]) for row in entries}):
        sessions, _meta = load_sessions(root)
        bars = [bar for session in sessions.values() for bar in session]
        bars.sort(key=lambda bar: _epoch(str(bar["time"])))
        paths[root] = ([_epoch(str(bar["time"])) for bar in bars], bars)
    return paths


def trade_path(
    entry: dict[str, Any],
    paths: dict[str, tuple[list[float], list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    times, bars = paths[str(entry["underlying"])]
    start = bisect.bisect_right(times, float(entry["entry_epoch"]))
    end = bisect.bisect_right(times, float(entry["exit_epoch"]))
    return bars[start:end]


def simulate(entry: dict[str, Any], path: list[dict[str, Any]], variant: Variant) -> dict[str, Any]:
    price = float(entry["entry"])
    atr = float(entry["atr"])
    side = 1.0 if entry.get("action") == "BUY" else -1.0
    stop = price - side * variant.stop_atr * atr
    peak = price
    armed = False
    exit_price = float(entry["original_exit"])
    exit_reason = "original_market_exit"
    exit_time = str(entry["exit_time"])

    for bar in path:
        high = float(bar.get("high") or bar.get("close") or price)
        low = float(bar.get("low") or bar.get("close") or price)
        # Existing stop is executable before today's unknown high/low ordering
        # can ratchet it. Same-bar stop/ratchet ambiguity therefore resolves to
        # the adverse side, never to an optimistic trail fill.
        stopped = low <= stop if side > 0 else high >= stop
        if stopped:
            exit_price = stop
            exit_reason = "atr_stop" if not armed else "atr_trail"
            exit_time = str(bar["time"])
            break
        peak = max(peak, high) if side > 0 else min(peak, low)
        favorable = side * (peak - price)
        if favorable >= variant.arm_atr * atr:
            armed = True
            candidate = peak - side * variant.trail_atr * atr
            stop = max(stop, candidate) if side > 0 else min(stop, candidate)

    gross_points = side * (exit_price - price)
    cost_points = (price + exit_price) * 5.0 / 10000.0
    return {
        "underlying": entry["underlying"],
        "session_date": entry["session_date"],
        "date": entry["date"],
        "entry_time": entry["entry_time"],
        "exit_time": exit_time,
        "exit_reason": exit_reason,
        "net_atr": (gross_points - cost_points) / atr,
    }


def stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(row["net_atr"]) for row in rows]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    result = {
        "trades": len(values),
        "total_net_atr": round(sum(values), 4),
        "expectancy_net_atr": round(sum(values) / len(values), 6) if values else None,
        "win_rate": round(len(wins) / len(values), 4) if values else None,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 4) if losses else None,
        "max_drawdown_atr": round(drawdown, 4),
    }
    if len(values) >= 2:
        rng = random.Random(20260704)
        means = []
        for _ in range(5000):
            sample = [values[rng.randrange(len(values))] for _ in values]
            means.append(sum(sample) / len(sample))
        means.sort()
        result["bootstrap_expectancy_atr_95ci"] = [
            round(means[int(0.025 * len(means))], 6),
            round(means[int(0.975 * len(means))], 6),
        ]
    return result


def _rank(
    entries: list[dict[str, Any]],
    paths: dict[str, tuple[list[float], list[dict[str, Any]]]],
) -> list[tuple[Variant, list[dict[str, Any]], float]]:
    ranked = []
    for variant in variants():
        results = [simulate(entry, entry["_path"], variant) for entry in entries]
        values = [float(row["net_atr"]) for row in results]
        if len(values) < 30:
            continue
        mean = sum(values) / len(values)
        score = mean - pstdev(values) / math.sqrt(len(values))
        ranked.append((variant, results, score))
    return sorted(ranked, key=lambda item: item[2], reverse=True)


def run() -> dict[str, Any]:
    all_entries = load_entries()
    # Entry study selected this theory-led cohort on development data: base
    # metals only, and only initiative direction aligned with higher-timeframe
    # value. Keep the held-out observations sealed while selecting exits.
    entries = [
        row for row in all_entries
        if row.get("underlying") in {"ALUMINI", "ZINCMINI"}
        and row.get("entry_style") == "ib_break"
        and (
            (row.get("action") == "BUY" and row.get("htf_bias") == "strong")
            or (row.get("action") == "SELL" and row.get("htf_bias") == "weak")
        )
    ]
    paths = load_paths(entries)
    for entry in entries:
        entry["_path"] = trade_path(entry, paths)
    # Preserve the same sealed date boundary used by the universe-wide entry
    # study. Recomputing a 70/30 split inside a sparse winning cohort would move
    # late-May observations back into development after seeing the cohort.
    dates = sorted({row["date"] for row in all_entries})
    split = dates[int(len(dates) * 0.70)]
    development = [row for row in entries if row["date"] < split]
    held_out = [row for row in entries if row["date"] >= split]

    ranked = _rank(development, paths)
    best = ranked[0][0]
    held_result = [simulate(entry, entry["_path"], best) for entry in held_out]
    top = []
    for variant, train_result, score in ranked[:10]:
        test_result = [simulate(entry, entry["_path"], variant) for entry in held_out]
        top.append({
            "variant": variant.name,
            "stop_atr": variant.stop_atr,
            "arm_atr": variant.arm_atr,
            "trail_atr": variant.trail_atr,
            "selection_score": round(score, 6),
            "development": stats(train_result),
            "held_out": stats(test_result),
        })

    # Nested selection of stop/arm/trail on prior observations only.
    folds = []
    nested: list[dict[str, Any]] = []
    cursor = int(len(dates) * 0.50)
    step = max(int(len(dates) * 0.10), 1)
    while cursor < len(dates):
        train_dates = set(dates[:cursor])
        test_dates = set(dates[cursor:cursor + step])
        train = [row for row in entries if row["date"] in train_dates]
        test = [row for row in entries if row["date"] in test_dates]
        fold_ranked = _rank(train, paths)
        if not fold_ranked or not test:
            break
        chosen = fold_ranked[0][0]
        results = [simulate(entry, entry["_path"], chosen) for entry in test]
        nested.extend(results)
        folds.append({
            "test_start": min(test_dates).isoformat(),
            "test_end": max(test_dates).isoformat(),
            "selected_variant": chosen.name,
            "test": stats(results),
        })
        cursor += step

    result = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "method": "same causal entries; 15m ATR stop and delayed runner trail; 5bps per side",
        "limitations": [
            "A tighter exit can make later same-symbol signals reachable; those extra entries are not added.",
            "One-minute OHLC cannot reveal intrabar ordering; existing stops are checked before trail ratchets.",
        ],
        "coverage": {
            "all_entries": len(all_entries),
            "cohort": "ALUMINI + ZINCMINI, HTF-directional IB break",
            "entries": len(entries),
            "development": len(development),
            "held_out": len(held_out),
            "held_out_start": split.isoformat(),
        },
        "selected": {
            "variant": best.name,
            "stop_atr": best.stop_atr,
            "arm_atr": best.arm_atr,
            "trail_atr": best.trail_atr,
            "development": stats(ranked[0][1]),
            "held_out": stats(held_result),
        },
        "top_development_variants": top,
        "nested_walkforward": {"folds": folds, "combined_test": stats(nested)},
    }
    OUTPUT_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
