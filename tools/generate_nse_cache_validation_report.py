#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd
from sqlalchemy import create_engine, text


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from analysis.macd_engine import (  # type: ignore  # noqa: E402
    analyze_trade,
    compute_macd,
    find_zero_crossovers,
    simulate_exit_strategies,
)


ATM_PAIRS_SQL = """
SELECT
    underlying,
    expiry,
    selection_date,
    selection_spot_time,
    selection_spot_price,
    strike,
    strike_gap,
    ce_instrument_key,
    ce_trading_symbol,
    ce_candle_count,
    ce_first_candle_time,
    ce_last_candle_time,
    pe_instrument_key,
    pe_trading_symbol,
    pe_candle_count,
    pe_first_candle_time,
    pe_last_candle_time,
    atm_rank
FROM validation_atm_monthly_pairs_vw
WHERE atm_rank = 1
  AND (:from_expiry IS NULL OR expiry >= :from_expiry)
  AND (:to_expiry IS NULL OR expiry <= :to_expiry)
  AND (
        :underlyings IS NULL
        OR underlying = ANY(:underlyings)
      )
ORDER BY underlying, expiry;
"""


OPTION_CANDLES_SQL = """
SELECT
    time,
    open,
    high,
    low,
    close,
    volume,
    oi,
    iv,
    delta,
    gamma,
    theta,
    vega,
    underlying_price,
    time_to_expiry_years
FROM option_premium_candles
WHERE instrument_key = :instrument_key
  AND interval = :interval
ORDER BY time ASC;
"""


CHAIN_METRICS_SQL = """
SELECT
    time,
    oi_pcr,
    volume_pcr,
    ce_oi,
    pe_oi,
    ce_volume,
    pe_volume,
    underlying_price
FROM fo_option_chain_metrics
WHERE underlying = :underlying
  AND expiry = :expiry
  AND interval = :interval
ORDER BY time ASC;
"""


COVERAGE_SQL = """
SELECT *
FROM validation_cache_coverage_vw
WHERE complete_contracts > 0
ORDER BY cached_option_candles DESC, underlying;
"""


CHAIN_SUMMARY_SQL = """
SELECT *
FROM validation_chain_metrics_summary_vw
WHERE underlying IN (
    SELECT DISTINCT underlying
    FROM validation_atm_monthly_pairs_vw
    WHERE atm_rank = 1
)
ORDER BY underlying, expiry DESC;
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a local-cache validation report for the NSE options MACD "
            "strategy using only TimescaleDB data."
        )
    )
    parser.add_argument(
        "--db-host",
        default=os.getenv("NOMADCURIE_DB_HOST", "localhost"),
        help="Postgres host. Default: localhost",
    )
    parser.add_argument(
        "--db-port",
        default=int(os.getenv("NOMADCURIE_DB_PORT", "5433")),
        type=int,
        help="Postgres port. Default: 5433",
    )
    parser.add_argument(
        "--db-name",
        default=os.getenv("NOMADCURIE_DB_NAME", "nomadcurie"),
        help="Database name. Default: nomadcurie",
    )
    parser.add_argument(
        "--db-user",
        default=os.getenv("NOMADCURIE_DB_USER", "nomadcurie"),
        help="Database user. Default: nomadcurie",
    )
    parser.add_argument(
        "--db-password",
        default=os.getenv("NOMADCURIE_DB_PASSWORD", "nomadcurie"),
        help="Database password. Default: nomadcurie",
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
            "Directory for outputs. Default: reports/validation/"
            "nse-cache-<timestamp>"
        ),
    )
    return parser.parse_args()


def connect(args: argparse.Namespace):
    dsn = (
        f"postgresql+psycopg2://{args.db_user}:{args.db_password}"
        f"@{args.db_host}:{args.db_port}/{args.db_name}"
    )
    return create_engine(dsn, future=True)


def maybe_date(value: str) -> Optional[date]:
    if not value:
        return None
    return date.fromisoformat(value)


def maybe_underlyings(value: str) -> Optional[list[str]]:
    items = [item.strip().upper() for item in value.split(",") if item.strip()]
    return items or None


def run_sql_file(conn, path: Path) -> None:
    sql_text = path.read_text()
    with conn.begin() as db:
        db.exec_driver_sql(sql_text)


def load_frame(conn, query: str, params: Optional[dict[str, Any]] = None) -> pd.DataFrame:
    with conn.connect() as db:
        return pd.read_sql_query(text(query), db, params=params)


def make_output_dir(raw_output_dir: str) -> Path:
    if raw_output_dir:
        output_dir = Path(raw_output_dir)
    else:
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        output_dir = REPO_ROOT / "reports" / "validation" / f"nse-cache-{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def write_report_artifacts(
    target_dir: Path,
    summary: dict[str, Any],
    markdown: str,
    trades_df: pd.DataFrame,
    coverage_df: pd.DataFrame,
    chain_summary_df: pd.DataFrame,
) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    (target_dir / "report.md").write_text(markdown)
    trades_df.to_csv(target_dir / "trades.csv", index=False)
    coverage_df.to_csv(target_dir / "coverage.csv", index=False)
    chain_summary_df.to_csv(target_dir / "chain_summary.csv", index=False)


def coerce_numeric(df: pd.DataFrame, columns: Iterable[str]) -> pd.DataFrame:
    for column in columns:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    return df


def build_candle_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in df.itertuples(index=False):
        records.append(
            {
                "time": row.time.isoformat(),
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
                "volume": int(row.volume) if not pd.isna(row.volume) else 0,
                "oi": int(row.oi) if not pd.isna(row.oi) else 0,
            }
        )
    return records


def label_iv_regime(series: pd.Series) -> pd.Series:
    valid = series.dropna()
    if len(valid) < 3:
        return pd.Series(["unknown"] * len(series), index=series.index)
    low_cut = valid.quantile(0.33)
    high_cut = valid.quantile(0.67)

    def classify(value: Any) -> str:
        if pd.isna(value):
            return "unknown"
        if float(value) <= low_cut:
            return "iv_low"
        if float(value) <= high_cut:
            return "iv_mid"
        return "iv_high"

    return series.apply(classify)


def label_signed_change(series: pd.Series, threshold: float, prefix: str) -> pd.Series:
    def classify(value: Any) -> str:
        if pd.isna(value):
            return f"{prefix}_unknown"
        number = float(value)
        if number <= -threshold:
            return f"{prefix}_down"
        if number >= threshold:
            return f"{prefix}_up"
        return f"{prefix}_flat"

    return series.apply(classify)


def label_pcr_regime(series: pd.Series, prefix: str) -> pd.Series:
    def classify(value: Any) -> str:
        if pd.isna(value):
            return f"{prefix}_unknown"
        number = float(value)
        if number < 0.8:
            return f"{prefix}_low"
        if number <= 1.2:
            return f"{prefix}_balanced"
        return f"{prefix}_high"

    return series.apply(classify)


def aggregate_bucket(df: pd.DataFrame, column: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bucket, group in df.groupby(column, dropna=False):
        best_returns = group["best_exit_return_pct"].dropna()
        max_returns = group["max_return_pct"].dropna()
        hold_returns = group["held_return_pct"].dropna()
        rows.append(
            {
                column: str(bucket),
                "trades": int(len(group)),
                "avg_oracle_best_exit_return_pct": round(float(best_returns.mean()), 4) if len(best_returns) else 0.0,
                "median_oracle_best_exit_return_pct": round(float(best_returns.median()), 4) if len(best_returns) else 0.0,
                "avg_max_return_pct": round(float(max_returns.mean()), 4) if len(max_returns) else 0.0,
                "avg_hold_to_expiry_return_pct": round(float(hold_returns.mean()), 4) if len(hold_returns) else 0.0,
                "oracle_positive_pct": round(float((best_returns > 0).mean() * 100.0), 2) if len(best_returns) else 0.0,
                "top_exit_strategy": (
                    group["best_exit_strategy"].mode().iloc[0]
                    if not group["best_exit_strategy"].mode().empty
                    else "none"
                ),
            }
        )
    rows.sort(
        key=lambda item: (
            item["avg_oracle_best_exit_return_pct"],
            item["trades"],
        ),
        reverse=True,
    )
    return rows


def aggregate_strategy_ranking(df: pd.DataFrame) -> list[dict[str, Any]]:
    buckets: dict[str, list[float]] = defaultdict(list)
    for raw in df["strategy_returns_json"]:
        strategy_returns = json.loads(raw)
        for name, value in strategy_returns.items():
            buckets[name].append(float(value))

    rows: list[dict[str, Any]] = []
    for name, returns in buckets.items():
        series = pd.Series(returns, dtype="float64")
        rows.append(
            {
                "strategy": name,
                "trades": int(len(series)),
                "avg_return_pct": round(float(series.mean()), 4),
                "median_return_pct": round(float(series.median()), 4),
                "positive_pct": round(float((series > 0).mean() * 100.0), 2),
            }
        )
    rows.sort(
        key=lambda item: (
            item["avg_return_pct"],
            item["median_return_pct"],
            item["positive_pct"],
        ),
        reverse=True,
    )
    return rows


def render_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return "_No data_\n"
    header = "| " + " | ".join(columns) + " |"
    divider = "| " + " | ".join(["---"] * len(columns)) + " |"
    body = []
    for row in rows:
        body.append("| " + " | ".join(str(row.get(column, "")) for column in columns) + " |")
    return "\n".join([header, divider, *body]) + "\n"


def build_markdown_report(
    summary: dict[str, Any],
    coverage_rows: list[dict[str, Any]],
    chain_rows: list[dict[str, Any]],
) -> str:
    lines: list[str] = []
    lines.append("# NSE Cache Validation Report")
    lines.append("")
    lines.append(f"Generated: {summary['generated_at']}")
    lines.append("")
    lines.append("## Scope")
    lines.append("")
    lines.append(
        f"- Cached underlyings with option data: {summary['coverage']['underlyings_with_option_data']}"
    )
    lines.append(f"- ATM monthly expiry pairs analyzed: {summary['coverage']['atm_monthly_pairs']}")
    lines.append(f"- Opportunities found: {summary['opportunities']['total_trades']}")
    lines.append(
        f"- Best average exit strategy: {summary['exit_analysis']['best_strategy']} "
        f"({summary['exit_analysis']['best_strategy_avg_return_pct']}%)"
    )
    lines.append("")

    lines.append("## Coverage")
    lines.append("")
    lines.append(
        render_table(
            coverage_rows,
            [
                "underlying",
                "kind",
                "expiries_with_complete_contracts",
                "complete_contracts",
                "cached_option_candles",
                "last_option_candle_time",
            ],
        ).rstrip()
    )
    lines.append("")

    lines.append("## Exit Strategy Ranking")
    lines.append("")
    lines.append(
        render_table(
            summary["exit_analysis"]["strategy_ranking"],
            ["strategy", "trades", "avg_return_pct", "median_return_pct", "positive_pct"],
        ).rstrip()
    )
    lines.append("")

    lines.append("## Opportunity Breakdown")
    lines.append("")
    lines.append("### By Underlying")
    lines.append("")
    lines.append(
        render_table(
            summary["breakdowns"]["by_underlying"],
            [
                "underlying",
                "trades",
                "avg_oracle_best_exit_return_pct",
                "avg_max_return_pct",
                "oracle_positive_pct",
                "top_exit_strategy",
            ],
        ).rstrip()
    )
    lines.append("")
    lines.append("### By Option Type")
    lines.append("")
    lines.append(
        render_table(
            summary["breakdowns"]["by_option_type"],
            [
                "option_type",
                "trades",
                "avg_oracle_best_exit_return_pct",
                "avg_max_return_pct",
                "oracle_positive_pct",
                "top_exit_strategy",
            ],
        ).rstrip()
    )
    lines.append("")
    lines.append("### By IV Regime")
    lines.append("")
    lines.append(
        render_table(
            summary["breakdowns"]["by_iv_regime"],
            [
                "iv_regime",
                "trades",
                "avg_oracle_best_exit_return_pct",
                "avg_max_return_pct",
                "oracle_positive_pct",
                "top_exit_strategy",
            ],
        ).rstrip()
    )
    lines.append("")
    lines.append("### By OI / PCR Context")
    lines.append("")
    lines.append(
        render_table(
            summary["breakdowns"]["by_oi_change_regime"],
            [
                "oi_change_regime",
                "trades",
                "avg_oracle_best_exit_return_pct",
                "avg_max_return_pct",
                "oracle_positive_pct",
                "top_exit_strategy",
            ],
        ).rstrip()
    )
    lines.append("")
    lines.append(
        render_table(
            summary["breakdowns"]["by_oi_pcr_regime"],
            [
                "oi_pcr_regime",
                "trades",
                "avg_oracle_best_exit_return_pct",
                "avg_max_return_pct",
                "oracle_positive_pct",
                "top_exit_strategy",
            ],
        ).rstrip()
    )
    lines.append("")
    lines.append(
        render_table(
            summary["breakdowns"]["by_volume_pcr_regime"],
            [
                "volume_pcr_regime",
                "trades",
                "avg_oracle_best_exit_return_pct",
                "avg_max_return_pct",
                "oracle_positive_pct",
                "top_exit_strategy",
            ],
        ).rstrip()
    )
    lines.append("")

    lines.append("## Chain Summary")
    lines.append("")
    lines.append(
        render_table(
            chain_rows,
            [
                "underlying",
                "expiry",
                "bar_count",
                "avg_oi_pcr",
                "avg_volume_pcr",
                "avg_underlying_price",
            ],
        ).rstrip()
    )
    lines.append("")
    return "\n".join(lines)


def generate_report(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    output_dir = make_output_dir(args.output_dir)
    underlyings = maybe_underlyings(args.underlyings)
    from_expiry = maybe_date(args.from_expiry)
    to_expiry = maybe_date(args.to_expiry)

    engine = connect(args)
    try:
        run_sql_file(engine, REPO_ROOT / "sql" / "nse_strategy_validation_views.sql")

        coverage_df = load_frame(engine, COVERAGE_SQL)
        chain_summary_df = load_frame(engine, CHAIN_SUMMARY_SQL)
        atm_pairs_df = load_frame(
            engine,
            ATM_PAIRS_SQL,
            {
                "from_expiry": from_expiry,
                "to_expiry": to_expiry,
                "underlyings": underlyings,
            },
        )

        trade_rows: list[dict[str, Any]] = []
        pair_rows = atm_pairs_df.to_dict("records")
        for pair in pair_rows:
            chain_df = load_frame(
                engine,
                CHAIN_METRICS_SQL,
                {
                    "underlying": pair["underlying"],
                    "expiry": pair["expiry"],
                    "interval": args.interval,
                },
            )
            if not chain_df.empty:
                chain_df["time"] = pd.to_datetime(chain_df["time"], utc=True)
                chain_df = coerce_numeric(
                    chain_df,
                    [
                        "oi_pcr",
                        "volume_pcr",
                        "ce_oi",
                        "pe_oi",
                        "ce_volume",
                        "pe_volume",
                        "underlying_price",
                    ],
                ).sort_values("time")

            for option_type in ("CE", "PE"):
                instrument_key = pair[f"{option_type.lower()}_instrument_key"]
                trading_symbol = pair[f"{option_type.lower()}_trading_symbol"]
                option_df = load_frame(
                    engine,
                    OPTION_CANDLES_SQL,
                    {
                        "instrument_key": instrument_key,
                        "interval": args.interval,
                    },
                )
                if option_df.empty:
                    continue

                option_df["time"] = pd.to_datetime(option_df["time"], utc=True)
                option_df = coerce_numeric(
                    option_df,
                    [
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume",
                        "oi",
                        "iv",
                        "delta",
                        "gamma",
                        "theta",
                        "vega",
                        "underlying_price",
                        "time_to_expiry_years",
                    ],
                ).sort_values("time")
                if not chain_df.empty:
                    merged = pd.merge_asof(
                        option_df,
                        chain_df,
                        on="time",
                        direction="backward",
                        suffixes=("", "_chain"),
                    )
                else:
                    merged = option_df.copy()
                    merged["oi_pcr"] = pd.NA
                    merged["volume_pcr"] = pd.NA
                    merged["ce_oi"] = pd.NA
                    merged["pe_oi"] = pd.NA
                    merged["ce_volume"] = pd.NA
                    merged["pe_volume"] = pd.NA

                merged["prev_oi"] = merged["oi"].shift(1)
                merged["prev_volume"] = merged["volume"].shift(1)
                merged["oi_change_pct"] = (
                    (merged["oi"] - merged["prev_oi"]) / merged["prev_oi"] * 100.0
                ).where(merged["prev_oi"] > 0)
                merged["volume_change_pct"] = (
                    (merged["volume"] - merged["prev_volume"]) / merged["prev_volume"] * 100.0
                ).where(merged["prev_volume"] > 0)

                candles = build_candle_records(merged)
                closes = [float(close) for close in merged["close"].tolist()]
                macd_line, signal_line, histogram = compute_macd(closes)
                selection_date = pd.Timestamp(pair["selection_date"]).date()
                crossover_indices = [
                    idx
                    for idx in find_zero_crossovers(macd_line)
                    if merged["time"].iloc[idx].date() >= selection_date
                ]

                for entry_idx in crossover_indices:
                    trade_analysis = analyze_trade(candles, entry_idx)
                    strategy_results = simulate_exit_strategies(candles, entry_idx)
                    strategy_returns = {
                        name: result["return_pct"]
                        for name, result in strategy_results.items()
                    }
                    best_strategy_name, best_strategy_result = max(
                        strategy_results.items(),
                        key=lambda item: item[1]["return_pct"],
                    )
                    entry_row = merged.iloc[entry_idx]
                    trade_rows.append(
                        {
                            "underlying": pair["underlying"],
                            "expiry": str(pair["expiry"]),
                            "expiry_month": str(pair["expiry"])[:7],
                            "selection_date": str(pair["selection_date"]),
                            "selection_spot_price": float(pair["selection_spot_price"]),
                            "strike": float(pair["strike"]),
                            "strike_gap": float(pair["strike_gap"]),
                            "option_type": option_type,
                            "instrument_key": instrument_key,
                            "trading_symbol": trading_symbol,
                            "entry_time": entry_row["time"].isoformat(),
                            "entry_price": float(entry_row["close"]),
                            "entry_iv": (
                                float(entry_row["iv"])
                                if not pd.isna(entry_row["iv"])
                                else None
                            ),
                            "entry_delta": (
                                float(entry_row["delta"])
                                if not pd.isna(entry_row["delta"])
                                else None
                            ),
                            "entry_gamma": (
                                float(entry_row["gamma"])
                                if not pd.isna(entry_row["gamma"])
                                else None
                            ),
                            "entry_theta": (
                                float(entry_row["theta"])
                                if not pd.isna(entry_row["theta"])
                                else None
                            ),
                            "entry_vega": (
                                float(entry_row["vega"])
                                if not pd.isna(entry_row["vega"])
                                else None
                            ),
                            "entry_oi": int(entry_row["oi"]) if not pd.isna(entry_row["oi"]) else None,
                            "entry_volume": (
                                int(entry_row["volume"])
                                if not pd.isna(entry_row["volume"])
                                else None
                            ),
                            "oi_change_pct": (
                                round(float(entry_row["oi_change_pct"]), 4)
                                if not pd.isna(entry_row["oi_change_pct"])
                                else None
                            ),
                            "volume_change_pct": (
                                round(float(entry_row["volume_change_pct"]), 4)
                                if not pd.isna(entry_row["volume_change_pct"])
                                else None
                            ),
                            "oi_pcr": (
                                round(float(entry_row["oi_pcr"]), 6)
                                if "oi_pcr" in entry_row and not pd.isna(entry_row["oi_pcr"])
                                else None
                            ),
                            "volume_pcr": (
                                round(float(entry_row["volume_pcr"]), 6)
                                if "volume_pcr" in entry_row and not pd.isna(entry_row["volume_pcr"])
                                else None
                            ),
                            "entry_underlying_price": (
                                round(float(entry_row["underlying_price"]), 4)
                                if not pd.isna(entry_row["underlying_price"])
                                else None
                            ),
                            "time_to_expiry_years": (
                                round(float(entry_row["time_to_expiry_years"]), 8)
                                if not pd.isna(entry_row["time_to_expiry_years"])
                                else None
                            ),
                            "macd_value": (
                                round(float(macd_line[entry_idx]), 6)
                                if macd_line[entry_idx] is not None
                                else None
                            ),
                            "signal_value": (
                                round(float(signal_line[entry_idx]), 6)
                                if signal_line[entry_idx] is not None
                                else None
                            ),
                            "histogram": (
                                round(float(histogram[entry_idx]), 6)
                                if histogram[entry_idx] is not None
                                else None
                            ),
                            "best_exit_strategy": best_strategy_name,
                            "best_exit_return_pct": round(float(best_strategy_result["return_pct"]), 4),
                            "hold_to_expiry_return_pct": round(
                                float(strategy_results["hold_to_expiry"]["return_pct"]),
                                4,
                            ),
                            "strategy_returns_json": json.dumps(strategy_returns, sort_keys=True),
                            **trade_analysis,
                        }
                    )
    finally:
        engine.dispose()

    trades_df = pd.DataFrame(trade_rows)
    if trades_df.empty:
        raise SystemExit("No cached ATM monthly MACD opportunities were found for the selected scope.")

    trades_df["iv_regime"] = label_iv_regime(trades_df["entry_iv"])
    trades_df["oi_change_regime"] = label_signed_change(trades_df["oi_change_pct"], 10.0, "oi")
    trades_df["volume_change_regime"] = label_signed_change(
        trades_df["volume_change_pct"], 25.0, "volume"
    )
    trades_df["oi_pcr_regime"] = label_pcr_regime(trades_df["oi_pcr"], "oi_pcr")
    trades_df["volume_pcr_regime"] = label_pcr_regime(
        trades_df["volume_pcr"], "volume_pcr"
    )

    strategy_ranking = aggregate_strategy_ranking(trades_df)
    best_strategy = strategy_ranking[0]["strategy"] if strategy_ranking else "none"
    best_strategy_avg_return = strategy_ranking[0]["avg_return_pct"] if strategy_ranking else 0.0

    summary = {
        "generated_at": datetime.now().isoformat(),
        "coverage": {
            "underlyings_with_option_data": int(
                coverage_df["underlying"].nunique() if not coverage_df.empty else 0
            ),
            "atm_monthly_pairs": int(len(atm_pairs_df)),
            "complete_cached_contracts": int(
                coverage_df["complete_contracts"].sum() if not coverage_df.empty else 0
            ),
            "cached_option_candles": int(
                coverage_df["cached_option_candles"].sum() if not coverage_df.empty else 0
            ),
        },
        "opportunities": {
            "total_trades": int(len(trades_df)),
            "months": sorted(trades_df["expiry_month"].dropna().unique().tolist()),
            "underlyings": sorted(trades_df["underlying"].dropna().unique().tolist()),
        },
        "exit_analysis": {
            "best_strategy": best_strategy,
            "best_strategy_avg_return_pct": best_strategy_avg_return,
            "hold_to_expiry_avg_return_pct": round(
                float(trades_df["hold_to_expiry_return_pct"].mean()), 4
            ),
            "avg_max_return_pct": round(float(trades_df["max_return_pct"].mean()), 4),
            "positive_pct": round(
                float((trades_df["best_exit_return_pct"] > 0).mean() * 100.0),
                2,
            ),
            "strategy_ranking": strategy_ranking,
        },
        "breakdowns": {
            "by_underlying": aggregate_bucket(trades_df, "underlying"),
            "by_option_type": aggregate_bucket(trades_df, "option_type"),
            "by_month": aggregate_bucket(trades_df, "expiry_month"),
            "by_iv_regime": aggregate_bucket(trades_df, "iv_regime"),
            "by_oi_change_regime": aggregate_bucket(trades_df, "oi_change_regime"),
            "by_volume_change_regime": aggregate_bucket(
                trades_df, "volume_change_regime"
            ),
            "by_oi_pcr_regime": aggregate_bucket(trades_df, "oi_pcr_regime"),
            "by_volume_pcr_regime": aggregate_bucket(
                trades_df, "volume_pcr_regime"
            ),
        },
    }

    coverage_rows = coverage_df.to_dict("records")
    chain_rows = chain_summary_df.to_dict("records")

    markdown = build_markdown_report(summary, coverage_rows, chain_rows)
    trades_export = trades_df.copy()
    write_report_artifacts(
        output_dir,
        summary,
        markdown,
        trades_export,
        coverage_df,
        chain_summary_df,
    )
    write_report_artifacts(
        REPO_ROOT / "backend" / "reports" / "validation" / "nse-cache-current",
        summary,
        markdown,
        trades_export,
        coverage_df,
        chain_summary_df,
    )

    return output_dir, summary


def main() -> None:
    args = parse_args()
    output_dir, summary = generate_report(args)
    print(f"Validation report written to: {output_dir}")
    print(f"Opportunities: {summary['opportunities']['total_trades']}")
    print(
        "Best strategy: "
        f"{summary['exit_analysis']['best_strategy']} "
        f"({summary['exit_analysis']['best_strategy_avg_return_pct']}%)"
    )


if __name__ == "__main__":
    main()
