"""Deduce and validate simple commodity MP+OF rules in 15-minute ATR units.

Consumes the causal trade ledger produced by ``_commodity_wf_driver.py``.
Rule selection uses only the chronological development sample.  The final 30%
is opened once, after selection, and a nested expanding-window replay reports
whether the selection process itself travels through time.
"""
from __future__ import annotations

import csv
import json
import math
import random
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from statistics import median, pstdev
from typing import Any, Callable, Iterable, Optional


ROOT = Path(__file__).resolve().parent.parent
INPUT = ROOT / "runtime" / "index_analytics_data" / "commodity_walkforward" / "edge_trades.csv"
OUTPUT = ROOT / "runtime" / "index_analytics_data" / "commodity_walkforward" / "atr_edge_summary.json"


def _number(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def load_trades(path: Path = INPUT) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    numeric = {
        "gross_r", "net_5bps_r", "gross_atr_15m", "net_5bps_atr_15m",
        "entry_atr_1m", "entry_atr_15m", "entry_atr_pct", "entry_hour_ist",
        "stop_distance_atr_15m", "target_distance_atr_15m", "mfe_atr_15m",
        "mae_atr_15m", "ib_range_atr", "price_from_poc_atr",
        "ib_range_atr_15m", "price_from_poc_atr_15m",
        "cvd_pressure_ratio", "of_volume_coverage", "confidence",
    }
    out: list[dict[str, Any]] = []
    for row in rows:
        converted = dict(row)
        for key in numeric:
            converted[key] = _number(row.get(key))
        if converted.get("entry_atr_1m") and converted.get("entry_atr_15m"):
            scale = float(converted["entry_atr_1m"]) / float(converted["entry_atr_15m"])
            if converted.get("ib_range_atr_15m") is None and converted.get("ib_range_atr") is not None:
                converted["ib_range_atr_15m"] = float(converted["ib_range_atr"]) * scale
            if converted.get("price_from_poc_atr_15m") is None and converted.get("price_from_poc_atr") is not None:
                converted["price_from_poc_atr_15m"] = float(converted["price_from_poc_atr"]) * scale
        converted["date"] = date.fromisoformat(str(row["session_date"]))
        if converted.get("entry_atr_15m") and converted.get("net_5bps_atr_15m") is not None:
            out.append(converted)
    return sorted(out, key=lambda item: (item["date"], item.get("entry_time") or ""))


@dataclass(frozen=True)
class Rule:
    name: str
    description: str
    predicate: Callable[[dict[str, Any]], bool]

    def select(self, rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return [row for row in rows if self.predicate(row)]


def _root_in(roots: set[str]) -> Callable[[dict[str, Any]], bool]:
    return lambda row: str(row.get("underlying")) in roots


def build_rules(rows: list[dict[str, Any]]) -> list[Rule]:
    present = sorted({str(row["underlying"]) for row in rows})
    groups: dict[str, set[str]] = {
        "all": set(present),
        "base_metals": {"ALUMINI", "ZINCMINI"},
        "metals_ex_gold": {"ALUMINI", "SILVERM", "ZINCMINI"},
        "energy": {"CRUDEOIL", "NATURALGAS"},
    }
    groups.update({root.lower(): {root} for root in present})
    rules: list[Rule] = []
    for group_name, roots in groups.items():
        roots = roots & set(present)
        if not roots:
            continue
        base = _root_in(roots)
        label = ", ".join(sorted(roots))
        rules.append(Rule(group_name, label, base))
        rules.append(Rule(
            f"{group_name}:ib_break", f"{label}; IB break only",
            lambda row, b=base: b(row) and row.get("entry_style") == "ib_break",
        ))
        for side in ("BUY", "SELL"):
            rules.append(Rule(
                f"{group_name}:{side.lower()}", f"{label}; {side} only",
                lambda row, b=base, s=side: b(row) and row.get("action") == s,
            ))
        hour_bands = ((9.0, 13.0, "morning"), (13.0, 17.0, "afternoon"), (17.0, 24.0, "evening"))
        for low, high, band in hour_bands:
            rules.append(Rule(
                f"{group_name}:{band}", f"{label}; entry {low:g}:00-{high:g}:00 IST",
                lambda row, b=base, lo=low, hi=high: (
                    b(row) and row.get("entry_hour_ist") is not None
                    and lo <= float(row["entry_hour_ist"]) < hi
                ),
            ))
        for maximum in (2.0, 2.5, 3.0, 3.5, 4.0):
            rules.append(Rule(
                f"{group_name}:stop_le_{maximum:g}atr",
                f"{label}; structural stop <= {maximum:g} x 15m ATR",
                lambda row, b=base, cap=maximum: (
                    b(row) and row.get("stop_distance_atr_15m") is not None
                    and float(row["stop_distance_atr_15m"]) <= cap
                ),
            ))
        for maximum in (2.0, 3.0, 4.0, 5.0):
            rules.append(Rule(
                f"{group_name}:ib_le_{maximum:g}atr",
                f"{label}; initial balance <= {maximum:g} x 15m ATR at entry",
                lambda row, b=base, cap=maximum: (
                    b(row) and row.get("ib_range_atr_15m") is not None
                    and float(row["ib_range_atr_15m"]) <= cap
                ),
            ))
        rules.append(Rule(
            f"{group_name}:htf_directional",
            f"{label}; BUY strong / SELL weak HTF value",
            lambda row, b=base: b(row) and (
                (row.get("action") == "BUY" and row.get("htf_bias") == "strong")
                or (row.get("action") == "SELL" and row.get("htf_bias") == "weak")
            ),
        ))
        rules.append(Rule(
            f"{group_name}:directional_ib_break",
            f"{label}; IB break aligned with strong/weak HTF value",
            lambda row, b=base: b(row) and row.get("entry_style") == "ib_break" and (
                (row.get("action") == "BUY" and row.get("htf_bias") == "strong")
                or (row.get("action") == "SELL" and row.get("htf_bias") == "weak")
            ),
        ))
        rules.append(Rule(
            f"{group_name}:htf_neutral", f"{label}; neutral HTF value",
            lambda row, b=base: b(row) and row.get("htf_bias") == "neutral",
        ))
    # Names are deterministic and unique even if a group has no observations.
    return list({rule.name: rule for rule in rules}.values())


def _drawdown(values: list[float]) -> float:
    equity = peak = worst = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return worst


def stats(rows: list[dict[str, Any]], *, bootstrap: bool = True) -> dict[str, Any]:
    atr = [float(row["net_5bps_atr_15m"]) for row in rows]
    rs = [float(row["net_5bps_r"]) for row in rows]
    wins = [value for value in atr if value > 0]
    losses = [value for value in atr if value < 0]
    result: dict[str, Any] = {
        "trades": len(rows),
        "total_net_atr": round(sum(atr), 4),
        "expectancy_net_atr": round(sum(atr) / len(atr), 6) if atr else None,
        "total_net_r": round(sum(rs), 4),
        "expectancy_net_r": round(sum(rs) / len(rs), 6) if rs else None,
        "win_rate": round(len(wins) / len(atr), 4) if atr else None,
        "profit_factor_atr": round(sum(wins) / abs(sum(losses)), 4) if losses else None,
        "max_drawdown_atr": round(_drawdown(atr), 4),
    }
    if atr:
        result.update({
            "median_stop_atr": round(median(float(row["stop_distance_atr_15m"]) for row in rows), 4),
            "median_target_atr": round(median(float(row["target_distance_atr_15m"]) for row in rows), 4),
            "median_mfe_atr": round(median(float(row["mfe_atr_15m"]) for row in rows), 4),
            "median_mae_atr": round(median(float(row["mae_atr_15m"]) for row in rows), 4),
        })
    if bootstrap and len(atr) >= 2:
        rng = random.Random(20260704)
        means = []
        r_means = []
        for _ in range(5000):
            indexes = [rng.randrange(len(atr)) for _ in atr]
            sample = [atr[index] for index in indexes]
            means.append(sum(sample) / len(sample))
            r_means.append(sum(rs[index] for index in indexes) / len(indexes))
        means.sort()
        r_means.sort()
        result["bootstrap_expectancy_atr_95ci"] = [
            round(means[int(0.025 * len(means))], 6),
            round(means[int(0.975 * len(means))], 6),
        ]
        result["bootstrap_expectancy_r_95ci"] = [
            round(r_means[int(0.025 * len(r_means))], 6),
            round(r_means[int(0.975 * len(r_means))], 6),
        ]
    return result


def _temporal_stability(rows: list[dict[str, Any]], rule: Rule, chunks: int = 4) -> tuple[int, int]:
    dates = sorted({row["date"] for row in rows})
    if not dates:
        return 0, 0
    positive = tested = 0
    for index in range(chunks):
        low = len(dates) * index // chunks
        high = len(dates) * (index + 1) // chunks
        allowed = set(dates[low:high])
        selected = rule.select(row for row in rows if row["date"] in allowed)
        if len(selected) < 5:
            continue
        tested += 1
        if sum(float(row["net_5bps_r"]) for row in selected) > 0:
            positive += 1
    return positive, tested


def rank_rules(
    train: list[dict[str, Any]],
    rules: list[Rule],
    *,
    min_trades: int = 30,
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for rule in rules:
        selected = rule.select(train)
        if len(selected) < min_trades:
            continue
        # Select on capital-normalized R, not raw ATR sum. ATR is the strategy
        # geometry; R is the economically comparable outcome after each
        # contract's stop distance and lot size.
        values = [float(row["net_5bps_r"]) for row in selected]
        mean = sum(values) / len(values)
        stderr = pstdev(values) / math.sqrt(len(values)) if len(values) > 1 else math.inf
        positive, tested = _temporal_stability(train, rule)
        ranked.append({
            "rule": rule,
            "train": stats(selected, bootstrap=False),
            "positive_train_chunks": positive,
            "tested_train_chunks": tested,
            # Conservative development score: lower one-standard-error bound,
            # with an explicit stability penalty. OOS never enters this score.
            "selection_score": mean - stderr - max(tested - positive, 0) * 0.10,
        })
    return sorted(ranked, key=lambda item: item["selection_score"], reverse=True)


def _quantiles(values: list[float]) -> dict[str, Optional[float]]:
    if not values:
        return {"p25": None, "median": None, "p75": None}
    ordered = sorted(values)
    return {
        "p25": round(ordered[int(0.25 * (len(ordered) - 1))], 4),
        "median": round(median(ordered), 4),
        "p75": round(ordered[int(0.75 * (len(ordered) - 1))], 4),
    }


def excursion_profile(rows: list[dict[str, Any]]) -> dict[str, Any]:
    winners = [row for row in rows if float(row["net_5bps_atr_15m"]) > 0]
    losers = [row for row in rows if float(row["net_5bps_atr_15m"]) <= 0]
    return {
        "all_stop_atr": _quantiles([float(row["stop_distance_atr_15m"]) for row in rows]),
        "all_target_atr": _quantiles([float(row["target_distance_atr_15m"]) for row in rows]),
        "winner_mfe_atr": _quantiles([float(row["mfe_atr_15m"]) for row in winners]),
        "winner_mae_atr": _quantiles([float(row["mae_atr_15m"]) for row in winners]),
        "loser_mfe_atr": _quantiles([float(row["mfe_atr_15m"]) for row in losers]),
        "loser_mae_atr": _quantiles([float(row["mae_atr_15m"]) for row in losers]),
    }


def fixed_rule_folds(
    rows: list[dict[str, Any]], rule: Rule, dates: list[date]
) -> list[dict[str, Any]]:
    folds = []
    cursor = int(len(dates) * 0.50)
    step = max(int(len(dates) * 0.10), 1)
    while cursor < len(dates):
        test_dates = set(dates[cursor:cursor + step])
        selected = rule.select(row for row in rows if row["date"] in test_dates)
        folds.append({
            "test_start": min(test_dates).isoformat(),
            "test_end": max(test_dates).isoformat(),
            "test": stats(selected, bootstrap=False),
        })
        cursor += step
    return folds


def run() -> dict[str, Any]:
    rows = load_trades()
    dates = sorted({row["date"] for row in rows})
    split = dates[int(len(dates) * 0.70)]
    development = [row for row in rows if row["date"] < split]
    held_out = [row for row in rows if row["date"] >= split]
    rules = build_rules(rows)
    ranked = rank_rules(development, rules)
    best = ranked[0] if ranked else None
    best_rule = best["rule"] if best else None

    high_conviction = Rule(
        "compressed_ib_expansion",
        "IB break within 3x ATR of POC; natural structural stop at least 3x ATR",
        lambda row: (
            row.get("entry_style") == "ib_break"
            and row.get("price_from_poc_atr_15m") is not None
            and abs(float(row["price_from_poc_atr_15m"])) <= 3.0
            and row.get("stop_distance_atr_15m") is not None
            and float(row["stop_distance_atr_15m"]) >= 3.0
        ),
    )
    high_rows = high_conviction.select(rows)
    high_dev = high_conviction.select(development)
    high_oos = high_conviction.select(held_out)
    high_by_root = {
        root: stats([row for row in high_rows if row.get("underlying") == root])
        for root in sorted({str(row.get("underlying")) for row in high_rows})
    }
    admission: dict[str, Any] = {}
    for root in sorted({str(row.get("underlying")) for row in rows}):
        ib = [
            row for row in rows
            if row.get("underlying") == root and row.get("entry_style") == "ib_break"
        ]
        near_poc = [
            row for row in ib
            if row.get("price_from_poc_atr_15m") is not None
            and abs(float(row["price_from_poc_atr_15m"])) <= 3.0
        ]
        admitted = high_conviction.select(ib)
        admission[root] = {
            "ib_breaks": len(ib),
            "within_3atr_of_poc": len(near_poc),
            "structural_stop_at_least_3atr": len(admitted),
        }

    top_rules = []
    for item in ranked[:10]:
        rule = item["rule"]
        top_rules.append({
            "name": rule.name,
            "description": rule.description,
            "selection_score": round(float(item["selection_score"]), 6),
            "positive_train_chunks": item["positive_train_chunks"],
            "tested_train_chunks": item["tested_train_chunks"],
            "development": stats(rule.select(development)),
            "held_out": stats(rule.select(held_out)),
            "walkforward_folds": fixed_rule_folds(rows, rule, dates),
        })

    # Nested expanding-window selection: choose on all prior data, apply to the
    # next 10% date block. This validates the deduction process, not merely one
    # fortunate final split.
    folds = []
    nested_rows: list[dict[str, Any]] = []
    cursor = int(len(dates) * 0.50)
    step = max(int(len(dates) * 0.10), 1)
    while cursor < len(dates):
        train_dates = set(dates[:cursor])
        test_dates = set(dates[cursor : cursor + step])
        train = [row for row in rows if row["date"] in train_dates]
        test = [row for row in rows if row["date"] in test_dates]
        fold_ranked = rank_rules(train, rules)
        if not fold_ranked or not test_dates:
            break
        selected_rule = fold_ranked[0]["rule"]
        selected_test = selected_rule.select(test)
        nested_rows.extend(selected_test)
        folds.append({
            "train_end": max(train_dates).isoformat(),
            "test_start": min(test_dates).isoformat(),
            "test_end": max(test_dates).isoformat(),
            "selected_rule": selected_rule.name,
            "test": stats(selected_test, bootstrap=False),
        })
        cursor += step

    result = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "unit": "causal ATR(14) over completed 15-minute bars at entry",
        "cost": "5 bps per side",
        "coverage": {
            "trades_with_15m_atr": len(rows),
            "first": dates[0].isoformat(),
            "last": dates[-1].isoformat(),
            "development_trades": len(development),
            "held_out_trades": len(held_out),
            "held_out_start": split.isoformat(),
        },
        "all_trades": stats(rows),
        "excursions": excursion_profile(rows),
        "high_conviction_setup": {
            "name": high_conviction.name,
            "description": high_conviction.description,
            "development": stats(high_dev),
            "held_out": stats(high_oos),
            "combined": stats(high_rows),
            "walkforward_folds": fixed_rule_folds(rows, high_conviction, dates),
            "by_root": high_by_root,
            "admission_funnel": admission,
        },
        "selected_rule": (
            {
                "name": best_rule.name,
                "description": best_rule.description,
                "development": stats(best_rule.select(development)),
                "held_out": stats(best_rule.select(held_out)),
            }
            if best_rule is not None else None
        ),
        "top_development_rules": top_rules,
        "nested_walkforward": {
            "folds": folds,
            "combined_test": stats(nested_rows),
        },
    }
    OUTPUT.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return result


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, default=str))
