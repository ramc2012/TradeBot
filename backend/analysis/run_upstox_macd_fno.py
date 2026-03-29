from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

from loguru import logger

from analysis.backtest import MACDBacktester


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = ROOT_DIR / "reports"
DEFAULT_CREDS_PATH = ROOT_DIR / "credentials.json"


def _load_upstox_token() -> str:
    if not DEFAULT_CREDS_PATH.exists():
        return ""
    payload = json.loads(DEFAULT_CREDS_PATH.read_text())
    return str(payload.get("upstox", {}).get("access_token", "")).strip()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Upstox expired-options MACD backtest across the NSE F&O universe."
    )
    parser.add_argument(
        "--from-date",
        default=(date.today() - timedelta(days=365)).isoformat(),
        help="Inclusive start date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--to-date",
        default=date.today().isoformat(),
        help="Inclusive end date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--underlyings",
        default="",
        help="Comma-separated subset of underlyings. Leave empty for the full current F&O universe.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional cap on how many underlyings to process after discovery.",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Optional output directory. Defaults to backend/reports/macd-fno-<dates>/",
    )
    parser.add_argument(
        "--timeframe",
        default="30m",
        choices=["30m", "1h"],
        help="Backtest timeframe. Uses native 30m candles or pairwise-resampled 1h candles.",
    )
    return parser.parse_args()


def _configure_logging() -> None:
    logger.remove()
    logger.add(sys.stderr, level="WARNING")


def _resolve_underlyings(args: argparse.Namespace, universe: dict[str, list[str]]) -> list[str]:
    if args.underlyings.strip():
        items = [part.strip().upper() for part in args.underlyings.split(",") if part.strip()]
    else:
        items = sorted(universe["indices"] + universe["stocks"])
    if args.limit and args.limit > 0:
        items = items[: args.limit]
    return items


def _prepare_output_dir(args: argparse.Namespace) -> Path:
    if args.output_dir.strip():
        output_dir = Path(args.output_dir).expanduser().resolve()
    else:
        output_dir = DEFAULT_OUTPUT_ROOT / (
            f"macd-fno-{args.from_date}-to-{args.to_date}-{args.timeframe}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _write_summary(output_dir: Path, results: dict) -> None:
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(results, indent=2))


def _collect_strategy_columns(trades: Iterable[dict]) -> list[str]:
    names: set[str] = set()
    for trade in trades:
        names.update((trade.get("strategy_returns") or {}).keys())
    return sorted(names)


def _write_trades_csv(output_dir: Path, trades: list[dict]) -> None:
    csv_path = output_dir / "trades.csv"
    strategy_columns = _collect_strategy_columns(trades)
    base_columns = [
        "underlying",
        "expiry",
        "option_type",
        "selection_date",
        "spot_reference_date",
        "spot_at_selection",
        "atm_strike",
        "strike",
        "entry_time",
        "entry_price",
        "max_price",
        "max_return_pct",
        "final_price",
        "held_return_pct",
        "best_exit_strategy",
        "best_exit_return_pct",
        "bars_to_max",
        "bars_held",
        "contract_trading_symbol",
        "contract_instrument_key",
    ]
    fieldnames = base_columns + strategy_columns

    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for trade in trades:
            row = {key: trade.get(key) for key in base_columns}
            for column in strategy_columns:
                row[column] = (trade.get("strategy_returns") or {}).get(column)
            writer.writerow(row)


async def _run() -> int:
    _configure_logging()
    args = _parse_args()
    token = _load_upstox_token()
    if not token:
        print("No saved Upstox token found in backend/credentials.json")
        return 1

    from_date = date.fromisoformat(args.from_date)
    to_date = date.fromisoformat(args.to_date)
    output_dir = _prepare_output_dir(args)

    backtester = MACDBacktester(access_token=token)
    if args.underlyings.strip():
        universe = {"indices": [], "stocks": []}
    else:
        universe = await backtester.fetch_fo_universe()
    underlyings = _resolve_underlyings(args, universe)

    print(
        f"Running MACD backtest for {len(underlyings)} underlyings "
        f"from {from_date} to {to_date} on {args.timeframe}"
    )

    progress_state = {"last_pct": -1.0}

    def _progress_cb(payload: dict) -> None:
        pct = float(payload.get("pct", 0.0))
        if pct - progress_state["last_pct"] >= 5.0 or pct == 100.0:
            progress_state["last_pct"] = pct
            print(
                f"[{pct:5.1f}%] {payload.get('message', '')} "
                f"| trades={payload.get('trades_found', 0)}"
            )

    results = await backtester.run(
        underlyings=underlyings,
        from_date=from_date,
        to_date=to_date,
        timeframe=args.timeframe,
        progress_cb=_progress_cb,
    )

    _write_summary(output_dir, results)
    _write_trades_csv(output_dir, results.get("all_trades", []))

    print(f"Results written to {output_dir}")
    print(f"Total opportunities: {results.get('total_opportunities', 0)}")
    print(f"Best exit strategy: {results.get('exit_analysis', {}).get('best_strategy')}")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
