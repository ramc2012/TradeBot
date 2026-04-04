"""Compare original MACD zero-cross walk-forward results across ATM/ITM selections."""
from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path
from typing import Any

import pandas as pd

from analysis.index_option_walkforward import (
    DATA_ROOT,
    OUTPUT_ROOT as ATM_OUTPUT_ROOT,
    ContractMeta,
    IndexOptionWalkForwardRunner,
    SeriesDescriptor,
    _load_contract_index,
    _spot_price_at_time,
)


OUTPUT_ROOT = DATA_ROOT / "walkforward_macd_itm_compare"


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(float(statistics.fmean(values)), 4)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(float(statistics.median(values)), 4)


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [float(row["return_pct"]) for row in rows]
    wins = [value for value in returns if value > 0.0]
    losses = [value for value in returns if value <= 0.0]
    holds = [float(row["holding_minutes"]) for row in rows]
    return {
        "opportunities": len(rows),
        "win_rate": round(len(wins) / len(rows), 4) if rows else 0.0,
        "avg_return_pct": _mean(returns),
        "median_return_pct": _median(returns),
        "avg_win_return_pct": _mean(wins),
        "avg_loss_return_pct": _mean(losses),
        "avg_holding_minutes": _mean(holds),
    }


def _load_oos_rows(path: Path) -> list[dict[str, Any]]:
    with path.open() as handle:
        reader = csv.DictReader(handle)
        rows = [dict(row) for row in reader]
    for row in rows:
        row["return_pct"] = float(row["return_pct"])
        row["holding_minutes"] = float(row["holding_minutes"])
    return rows


def _summarize_by_option(rows: list[dict[str, Any]]) -> dict[str, Any]:
    overall = _summarize(rows)
    by_option = {}
    for option_type in ("CE", "PE"):
        subset = [row for row in rows if row["option_type"] == option_type]
        by_option[option_type] = _summarize(subset)
    by_group_option = {}
    for group_name in sorted({row["walkforward_group"] for row in rows}):
        by_group_option[group_name] = {}
        for option_type in ("CE", "PE"):
            subset = [
                row
                for row in rows
                if row["walkforward_group"] == group_name and row["option_type"] == option_type
            ]
            by_group_option[group_name][option_type] = _summarize(subset)
    return {
        "overall": overall,
        "by_option_type": by_option,
        "by_group_option_type": by_group_option,
    }


def _build_itm_descriptors(data_root: Path, itm_steps: int) -> list[SeriesDescriptor]:
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

        eligible = [candidate for candidate in candidates if candidate[1].date() == group_start_day] or candidates
        atm_strike, _, _, _ = min(
            eligible,
            key=lambda item: (abs(item[0] - spot_price), item[1], item[0]),
        )
        atm_index = common_strikes.index(atm_strike)
        ce_index = max(atm_index - itm_steps, 0)
        pe_index = min(atm_index + itm_steps, len(common_strikes) - 1)
        ce_meta = ce_map[common_strikes[ce_index]]
        pe_meta = pe_map[common_strikes[pe_index]]
        pair_start = max(ce_meta.earliest_candle, pe_meta.earliest_candle)
        pair_end = min(ce_meta.latest_candle, pe_meta.latest_candle)
        if pair_end <= pair_start:
            continue

        descriptors.append(
            SeriesDescriptor(
                series_id=f"{underlying}|{expiry_kind}|{expiry}|ITM{itm_steps}",
                underlying=underlying,
                expiry_kind=expiry_kind,
                expiry=expiry,
                selected_strike=float(atm_strike),
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


def _run_itm_walkforward(itm_steps: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    output_root = OUTPUT_ROOT / f"itm_{itm_steps}"
    runner = IndexOptionWalkForwardRunner(data_root=DATA_ROOT, output_root=output_root)
    runner.descriptors = _build_itm_descriptors(DATA_ROOT, itm_steps=itm_steps)
    runner.run()
    rows = _load_oos_rows(output_root / "oos_trades.csv")
    return _summarize_by_option(rows), rows


def run() -> dict[str, Any]:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    atm_rows = _load_oos_rows(ATM_OUTPUT_ROOT / "oos_trades.csv")
    summary = {
        "generated_at": pd.Timestamp.now(tz="Asia/Kolkata").isoformat(),
        "atm": _summarize_by_option(atm_rows),
    }
    for itm_steps in (1, 2):
        itm_summary, _ = _run_itm_walkforward(itm_steps)
        summary[f"itm_{itm_steps}"] = itm_summary

    (OUTPUT_ROOT / "summary.json").write_text(json.dumps(summary, indent=2))

    lines = [
        "# MACD Zero-Cross ATM vs ITM Comparison",
        "",
        f"Generated: {summary['generated_at']}",
        "",
    ]
    for key in ("atm", "itm_1", "itm_2"):
        block = summary[key]
        lines.extend(
            [
                f"## {key.upper()}",
                "",
                f"- Overall avg return: {block['overall']['avg_return_pct']:.2f}%",
                f"- Overall win rate: {block['overall']['win_rate'] * 100:.2f}%",
                f"- CE avg return: {block['by_option_type']['CE']['avg_return_pct']:.2f}%",
                f"- PE avg return: {block['by_option_type']['PE']['avg_return_pct']:.2f}%",
                "",
            ]
        )
    (OUTPUT_ROOT / "report.md").write_text("\n".join(lines))
    return summary


def main() -> None:
    print(json.dumps(run(), indent=2))


if __name__ == "__main__":
    main()
