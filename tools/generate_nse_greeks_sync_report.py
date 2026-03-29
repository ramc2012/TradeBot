#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from analysis.validation_greeks_sync import (  # type: ignore  # noqa: E402
    build_live_greeks_sync_report,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a standalone Greeks Sync research report from the "
            "existing NSE cache."
        )
    )
    parser.add_argument(
        "--interval",
        default="30minute",
        help="Candle interval to analyze. Default: 30minute",
    )
    parser.add_argument(
        "--underlyings",
        default="",
        help="Comma-separated subset of underlyings. Leave empty for all cached ATM pairs.",
    )
    parser.add_argument(
        "--from-expiry",
        default="",
        help="Optional inclusive expiry filter in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--to-expiry",
        default="",
        help="Optional inclusive expiry filter in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help=(
            "Directory for outputs. Default: reports/greeks-sync/"
            "greeks-sync-<timestamp>"
        ),
    )
    return parser.parse_args()


def maybe_date(value: str) -> Optional[date]:
    if not value:
        return None
    return date.fromisoformat(value)


def maybe_underlyings(value: str) -> Optional[list[str]]:
    items = [item.strip().upper() for item in value.split(",") if item.strip()]
    return items or None


def make_output_dir(raw_output_dir: str) -> Path:
    if raw_output_dir:
        output_dir = Path(raw_output_dir)
    else:
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        output_dir = REPO_ROOT / "reports" / "greeks-sync" / f"greeks-sync-{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def main() -> None:
    args = parse_args()
    result = build_live_greeks_sync_report(
        interval=args.interval,
        underlyings=maybe_underlyings(args.underlyings),
        from_expiry=maybe_date(args.from_expiry),
        to_expiry=maybe_date(args.to_expiry),
    )

    output_dir = make_output_dir(args.output_dir)
    artifacts = result.artifacts
    (output_dir / "summary.json").write_text(artifacts.summary_json)
    (output_dir / "report.md").write_text(artifacts.report_markdown)
    (output_dir / "trades.csv").write_text(artifacts.trades_csv)
    (output_dir / "coverage.csv").write_text(artifacts.coverage_csv)
    (output_dir / "chain_summary.csv").write_text(artifacts.chain_summary_csv)

    print(f"Wrote Greeks Sync report to {output_dir}")


if __name__ == "__main__":
    main()
