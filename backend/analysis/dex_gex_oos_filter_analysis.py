"""Evaluate DEX/GEX usefulness on the saved out-of-sample MACD trade set."""
from __future__ import annotations

import csv
import json
import math
import statistics
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from analysis.index_option_walkforward import (
    DATA_ROOT,
    _load_contract_index,
    _load_csv_frame,
    _spot_price_at_time,
)
from analytics.greeks import bs_greeks, implied_volatility


OUTPUT_ROOT = DATA_ROOT / "dex_gex_oos_filter"
TRADES_PATH = DATA_ROOT / "walkforward_macd_rsi" / "oos_trades.csv"
RISK_FREE_RATE = 0.065
STALE_LOOKBACK_MINUTES = 10
MAX_CONTRACTS_PER_SNAPSHOT = 60


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(float(statistics.fmean(values)), 4)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(float(statistics.median(values)), 4)


def _expiry_timestamp(expiry: str) -> pd.Timestamp:
    return pd.Timestamp(f"{expiry}T15:30:00+05:30")


@lru_cache(maxsize=256)
def _indexed_frame(path_str: str) -> pd.DataFrame:
    return _load_csv_frame(path_str).set_index("time").sort_index()


def _row_at_or_before(path_str: str, ts: pd.Timestamp) -> Optional[dict[str, Any]]:
    indexed = _indexed_frame(path_str)
    if indexed.empty:
        return None
    last_ts = indexed.index.asof(ts)
    if pd.isna(last_ts):
        return None
    last_ts = pd.Timestamp(last_ts)
    if last_ts.date() != ts.date():
        return None
    age_minutes = (ts - last_ts).total_seconds() / 60.0
    if age_minutes > STALE_LOOKBACK_MINUTES:
        return None
    row = indexed.loc[last_ts]
    if hasattr(row, "to_dict"):
        data = row.to_dict()
    else:
        data = dict(row)
    data["time"] = last_ts
    return data


@lru_cache(maxsize=500000)
def _estimate_greeks(
    option_price: float,
    spot_price: float,
    strike: float,
    time_to_expiry_years: float,
    option_type: str,
) -> tuple[float, float, float]:
    if option_price <= 0.0 or spot_price <= 0.0 or strike <= 0.0 or time_to_expiry_years <= 0.0:
        return (0.0, 0.0, 0.0)
    iv = implied_volatility(
        market_price=option_price,
        S=spot_price,
        K=strike,
        T=time_to_expiry_years,
        r=RISK_FREE_RATE,
        option_type=option_type,
    )
    if iv <= 0.0:
        return (0.0, 0.0, 0.0)
    greeks = bs_greeks(
        S=spot_price,
        K=strike,
        T=time_to_expiry_years,
        r=RISK_FREE_RATE,
        sigma=iv,
        option_type=option_type,
    )
    return (float(greeks.delta), float(greeks.gamma), float(iv))


class DexGexSnapshotter:
    def __init__(self, data_root: Path = DATA_ROOT):
        self.data_root = data_root
        self._series_contracts: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for meta in _load_contract_index(data_root):
            if meta.candle_count <= 0:
                continue
            self._series_contracts[(meta.underlying, meta.expiry_kind, meta.expiry)].append(
                {
                    "path": meta.file_path,
                    "strike": float(meta.strike),
                    "option_type": meta.option_type,
                    "symbol": meta.trading_symbol,
                }
            )

    @lru_cache(maxsize=5000)
    def snapshot(self, underlying: str, expiry_kind: str, expiry: str, timestamp_iso: str) -> dict[str, Any]:
        ts = pd.Timestamp(timestamp_iso)
        spot_price = _spot_price_at_time(underlying, ts)
        if spot_price is None or spot_price <= 0.0:
            return self._empty_snapshot()

        contracts = self._series_contracts.get((underlying, expiry_kind, expiry), [])
        expiry_ts = _expiry_timestamp(expiry)
        time_to_expiry_years = max((expiry_ts - ts).total_seconds(), 60.0) / (365.0 * 24.0 * 60.0 * 60.0)
        candidates = sorted(
            contracts,
            key=lambda contract: (abs(float(contract["strike"]) - float(spot_price)), contract["option_type"]),
        )[:MAX_CONTRACTS_PER_SNAPSHOT]

        dex = 0.0
        gex = 0.0
        total_oi = 0.0
        active_contracts = 0
        ce_contracts = 0
        pe_contracts = 0
        weighted_iv = 0.0

        for contract in candidates:
            row = _row_at_or_before(contract["path"], ts)
            if not row:
                continue
            option_price = float(row.get("close") or 0.0)
            oi = float(row.get("oi") or 0.0)
            if option_price <= 0.0 or oi <= 0.0:
                continue

            delta, gamma, iv = _estimate_greeks(
                round(option_price, 4),
                round(float(spot_price), 4),
                round(float(contract["strike"]), 4),
                round(time_to_expiry_years, 8),
                str(contract["option_type"]),
            )
            if delta == 0.0 and gamma == 0.0:
                continue

            sign = 1.0 if contract["option_type"] == "CE" else -1.0
            dex += delta * oi * float(spot_price)
            gex += sign * gamma * oi * float(spot_price)
            total_oi += oi
            weighted_iv += iv * oi
            active_contracts += 1
            if contract["option_type"] == "CE":
                ce_contracts += 1
            else:
                pe_contracts += 1

        avg_iv = (weighted_iv / total_oi) if total_oi > 0 else 0.0
        return {
            "spot_price": round(float(spot_price), 4),
            "dex": round(dex, 4),
            "gex": round(gex, 6),
            "avg_iv": round(avg_iv, 6),
            "active_contracts": active_contracts,
            "contracts_considered": len(candidates),
            "ce_contracts": ce_contracts,
            "pe_contracts": pe_contracts,
            "total_oi": round(total_oi, 2),
        }

    @staticmethod
    def _empty_snapshot() -> dict[str, Any]:
        return {
            "spot_price": 0.0,
            "dex": 0.0,
            "gex": 0.0,
            "avg_iv": 0.0,
            "active_contracts": 0,
            "contracts_considered": 0,
            "ce_contracts": 0,
            "pe_contracts": 0,
            "total_oi": 0.0,
        }


def _trade_direction_sign(option_type: str) -> int:
    return 1 if option_type == "CE" else -1


def _alignment(direction: int, signal_value: float) -> str:
    if signal_value == 0.0:
        return "neutral"
    return "aligned" if direction == (1 if signal_value > 0.0 else -1) else "anti"


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


def run() -> dict[str, Any]:
    snapshotter = DexGexSnapshotter()
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    with TRADES_PATH.open() as handle:
        reader = csv.DictReader(handle)
        trades = [dict(row) for row in reader]

    enriched: list[dict[str, Any]] = []
    for trade in trades:
        snapshot = snapshotter.snapshot(
            trade["underlying"],
            trade["expiry_kind"],
            trade["expiry"],
            trade["entry_time"],
        )
        direction = _trade_direction_sign(trade["option_type"])
        dex_alignment = _alignment(direction, float(snapshot["dex"]))
        gex_alignment = _alignment(direction, float(snapshot["gex"]))
        combined_alignment = (
            "aligned"
            if dex_alignment == "aligned" and gex_alignment == "aligned"
            else "anti"
            if dex_alignment == "anti" and gex_alignment == "anti"
            else "mixed"
        )
        enriched.append(
            {
                **trade,
                **snapshot,
                "direction_sign": direction,
                "dex_alignment": dex_alignment,
                "gex_alignment": gex_alignment,
                "combined_alignment": combined_alignment,
            }
        )

    by_group_alignment: dict[str, Any] = {}
    for group_name in sorted({row["walkforward_group"] for row in enriched}):
        group_rows = [row for row in enriched if row["walkforward_group"] == group_name]
        group_summary = {
            "baseline": _summarize(group_rows),
            "dex_alignment": {},
            "gex_alignment": {},
            "combined_alignment": {},
        }
        for key in ("dex_alignment", "gex_alignment", "combined_alignment"):
            for bucket in sorted({row[key] for row in group_rows}):
                rows = [row for row in group_rows if row[key] == bucket]
                group_summary[key][bucket] = _summarize(rows)
        by_group_alignment[group_name] = group_summary

    overall = {
        "baseline": _summarize(enriched),
        "dex_alignment": {},
        "gex_alignment": {},
        "combined_alignment": {},
        "coverage": {
            "trades": len(enriched),
            "snapshot_contracts_avg": _mean([float(row["active_contracts"]) for row in enriched]),
            "snapshot_contracts_median": _median([float(row["active_contracts"]) for row in enriched]),
            "trades_with_nonzero_dex": sum(1 for row in enriched if float(row["dex"]) != 0.0),
            "trades_with_nonzero_gex": sum(1 for row in enriched if float(row["gex"]) != 0.0),
        },
    }
    for key in ("dex_alignment", "gex_alignment", "combined_alignment"):
        for bucket in sorted({row[key] for row in enriched}):
            rows = [row for row in enriched if row[key] == bucket]
            overall[key][bucket] = _summarize(rows)

    by_underlying_combined: dict[str, Any] = {}
    for underlying in sorted({row["underlying"] for row in enriched}):
        subset = [row for row in enriched if row["underlying"] == underlying]
        by_underlying_combined[underlying] = {
            "baseline": _summarize(subset),
            "combined_alignment": {
                bucket: _summarize([row for row in subset if row["combined_alignment"] == bucket])
                for bucket in sorted({row["combined_alignment"] for row in subset})
            },
        }

    summary = {
        "generated_at": pd.Timestamp.now(tz="Asia/Kolkata").isoformat(),
        "data_root": str(DATA_ROOT),
        "source_trades": str(TRADES_PATH),
        "dex_definition": "sum(delta * oi * spot_price) using BS greeks inferred from option close",
        "gex_definition": "sum(sign(option_type) * gamma * oi * spot_price) using repo option_chain sign convention",
        "proxy_scope": f"nearest {MAX_CONTRACTS_PER_SNAPSHOT} contracts by strike distance to spot at each trade entry",
        "overall": overall,
        "by_group": by_group_alignment,
        "by_underlying": by_underlying_combined,
        "alignment_counts": {
            key: Counter(row[key] for row in enriched)
            for key in ("dex_alignment", "gex_alignment", "combined_alignment")
        },
    }

    (OUTPUT_ROOT / "summary.json").write_text(json.dumps(summary, indent=2, default=lambda x: dict(x)))

    fieldnames = sorted({key for row in enriched for key in row.keys()})
    with (OUTPUT_ROOT / "trades_with_dex_gex.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in enriched:
            writer.writerow(row)

    lines = [
        "# DEX/GEX OOS Filter Analysis",
        "",
        f"Generated: {summary['generated_at']}",
        f"Source trades: `{summary['source_trades']}`",
        "",
        "## Overall",
        "",
        f"- Baseline opportunities: {overall['baseline']['opportunities']}",
        f"- Baseline avg return: {overall['baseline']['avg_return_pct']:.2f}%",
        f"- Baseline win rate: {overall['baseline']['win_rate'] * 100:.2f}%",
        f"- DEX aligned avg return: {overall['dex_alignment'].get('aligned', {}).get('avg_return_pct', 0.0):.2f}%",
        f"- GEX aligned avg return: {overall['gex_alignment'].get('aligned', {}).get('avg_return_pct', 0.0):.2f}%",
        f"- Both aligned avg return: {overall['combined_alignment'].get('aligned', {}).get('avg_return_pct', 0.0):.2f}%",
        "",
    ]
    (OUTPUT_ROOT / "report.md").write_text("\n".join(lines))

    return summary


def main() -> None:
    summary = run()
    print(json.dumps(summary["overall"], indent=2, default=lambda x: dict(x)))


if __name__ == "__main__":
    main()
