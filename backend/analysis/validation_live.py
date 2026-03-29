from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from threading import Lock
from typing import Any, Iterable, Optional

import pandas as pd
from sqlalchemy import create_engine, text

from analysis.macd_engine import (
    analyze_trade,
    compute_macd,
    find_zero_crossovers,
    simulate_exit_strategies,
)
from core.config import settings


VALIDATION_VIEWS_SQL = """
CREATE OR REPLACE VIEW validation_cache_coverage_vw AS
WITH expiry_stats AS (
    SELECT
        e.underlying,
        COUNT(*) AS expiry_rows,
        COUNT(*) FILTER (WHERE e.selection_spot_price IS NOT NULL) AS expiries_with_selection_spot,
        MIN(e.selection_date) AS first_selection_date,
        MAX(e.selection_date) AS last_selection_date
    FROM fo_expiry_catalog e
    GROUP BY e.underlying
),
contract_stats AS (
    SELECT
        c.underlying,
        COUNT(*) FILTER (WHERE c.sync_status = 'complete') AS complete_contracts,
        COUNT(DISTINCT c.expiry) FILTER (WHERE c.sync_status = 'complete') AS expiries_with_complete_contracts,
        COALESCE(SUM(c.candle_count) FILTER (WHERE c.sync_status = 'complete'), 0) AS cached_option_candles,
        MIN(c.first_candle_time) FILTER (WHERE c.sync_status = 'complete') AS first_option_candle_time,
        MAX(c.last_candle_time) FILTER (WHERE c.sync_status = 'complete') AS last_option_candle_time
    FROM fo_contract_catalog c
    GROUP BY c.underlying
)
SELECT
    u.symbol AS underlying,
    u.kind,
    COALESCE(es.expiry_rows, 0) AS expiry_rows,
    COALESCE(es.expiries_with_selection_spot, 0) AS expiries_with_selection_spot,
    es.first_selection_date,
    es.last_selection_date,
    COALESCE(cs.expiries_with_complete_contracts, 0) AS expiries_with_complete_contracts,
    COALESCE(cs.complete_contracts, 0) AS complete_contracts,
    COALESCE(cs.cached_option_candles, 0) AS cached_option_candles,
    cs.first_option_candle_time,
    cs.last_option_candle_time,
    u.expiries_synced_at,
    u.spot_synced_at
FROM fo_underlying_catalog u
LEFT JOIN expiry_stats es
    ON es.underlying = u.symbol
LEFT JOIN contract_stats cs
    ON cs.underlying = u.symbol;


CREATE OR REPLACE VIEW validation_atm_monthly_pairs_vw AS
WITH paired_contracts AS (
    SELECT
        e.underlying,
        e.expiry,
        e.selection_date,
        e.selection_spot_time,
        e.selection_spot_price,
        ce.strike,
        ABS(ce.strike - e.selection_spot_price) AS strike_gap,
        ce.instrument_key AS ce_instrument_key,
        ce.trading_symbol AS ce_trading_symbol,
        ce.candle_count AS ce_candle_count,
        ce.first_candle_time AS ce_first_candle_time,
        ce.last_candle_time AS ce_last_candle_time,
        pe.instrument_key AS pe_instrument_key,
        pe.trading_symbol AS pe_trading_symbol,
        pe.candle_count AS pe_candle_count,
        pe.first_candle_time AS pe_first_candle_time,
        pe.last_candle_time AS pe_last_candle_time
    FROM fo_expiry_catalog e
    JOIN fo_contract_catalog ce
        ON ce.underlying = e.underlying
       AND ce.expiry = e.expiry
       AND ce.option_type = 'CE'
       AND ce.sync_status = 'complete'
    JOIN fo_contract_catalog pe
        ON pe.underlying = e.underlying
       AND pe.expiry = e.expiry
       AND pe.option_type = 'PE'
       AND pe.sync_status = 'complete'
       AND pe.strike = ce.strike
    WHERE e.selection_date IS NOT NULL
      AND e.selection_spot_price IS NOT NULL
),
ranked_pairs AS (
    SELECT
        p.*,
        ROW_NUMBER() OVER (
            PARTITION BY p.underlying, p.expiry
            ORDER BY
                p.strike_gap ASC,
                GREATEST(p.ce_candle_count, p.pe_candle_count) DESC,
                p.strike ASC
        ) AS atm_rank
    FROM paired_contracts p
)
SELECT *
FROM ranked_pairs;


CREATE OR REPLACE VIEW validation_chain_metrics_summary_vw AS
SELECT
    m.underlying,
    m.expiry,
    m.interval,
    COUNT(*) AS bar_count,
    MIN(m.time) AS first_bar_time,
    MAX(m.time) AS last_bar_time,
    AVG(m.oi_pcr) AS avg_oi_pcr,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY m.oi_pcr) AS median_oi_pcr,
    AVG(m.volume_pcr) AS avg_volume_pcr,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY m.volume_pcr) AS median_volume_pcr,
    AVG(m.ce_oi) AS avg_ce_oi,
    AVG(m.pe_oi) AS avg_pe_oi,
    AVG(m.ce_volume) AS avg_ce_volume,
    AVG(m.pe_volume) AS avg_pe_volume,
    AVG(m.underlying_price) AS avg_underlying_price
FROM fo_option_chain_metrics m
GROUP BY
    m.underlying,
    m.expiry,
    m.interval;
"""

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

FRESHNESS_SQL = """
SELECT
    GREATEST(
        COALESCE(
            (
                SELECT MAX(
                    GREATEST(
                        COALESCE(last_synced_at, TIMESTAMPTZ 'epoch'),
                        COALESCE(updated_at, TIMESTAMPTZ 'epoch')
                    )
                )
                FROM fo_contract_catalog
            ),
            TIMESTAMPTZ 'epoch'
        ),
        COALESCE(
            (
                SELECT MAX(synced_at)
                FROM option_premium_candles
                WHERE interval = :interval
            ),
            TIMESTAMPTZ 'epoch'
        ),
        COALESCE(
            (
                SELECT MAX(synced_at)
                FROM fo_option_chain_metrics
                WHERE interval = :interval
            ),
            TIMESTAMPTZ 'epoch'
        ),
        COALESCE(
            (
                SELECT MAX(
                    GREATEST(
                        COALESCE(selection_spot_time, TIMESTAMPTZ 'epoch'),
                        COALESCE(updated_at, TIMESTAMPTZ 'epoch')
                    )
                )
                FROM fo_expiry_catalog
            ),
            TIMESTAMPTZ 'epoch'
        )
    ) AS source_updated_at
"""


@dataclass
class ValidationArtifacts:
    summary_json: str
    report_markdown: str
    trades_csv: str
    coverage_csv: str
    chain_summary_csv: str


@dataclass
class ValidationReportResult:
    payload: dict[str, Any]
    artifacts: ValidationArtifacts


_CACHE_LOCK = Lock()
_LIVE_REPORT_CACHE: dict[str, Any] = {
    "cache_key": None,
    "result": None,
}


def _sync_database_url() -> str:
    return str(settings.DATABASE_URL).replace("+asyncpg", "+psycopg2")


def _make_engine():
    return create_engine(_sync_database_url(), future=True)


def _to_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        if value.tzinfo is None:
            value = value.tz_localize(timezone.utc)
        return value.isoformat()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _to_native(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _to_native(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_to_native(inner) for inner in value]
    if isinstance(value, pd.Timestamp):
        return _to_iso(value)
    if isinstance(value, datetime):
        return _to_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return value
    return value


def _ensure_validation_views(engine) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(VALIDATION_VIEWS_SQL)


def _load_frame(engine, query: str, params: Optional[dict[str, Any]] = None) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql_query(text(query), conn, params=params)


def _coerce_numeric(df: pd.DataFrame, columns: Iterable[str]) -> pd.DataFrame:
    for column in columns:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    return df


def _build_candle_records(df: pd.DataFrame) -> list[dict[str, Any]]:
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


def _label_iv_regime(series: pd.Series) -> pd.Series:
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


def _label_signed_change(series: pd.Series, threshold: float, prefix: str) -> pd.Series:
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


def _label_pcr_regime(series: pd.Series, prefix: str) -> pd.Series:
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


def _aggregate_bucket(df: pd.DataFrame, column: str) -> list[dict[str, Any]]:
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


def _aggregate_strategy_ranking(df: pd.DataFrame) -> list[dict[str, Any]]:
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


def _render_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return "_No data_\n"
    header = "| " + " | ".join(columns) + " |"
    divider = "| " + " | ".join(["---"] * len(columns)) + " |"
    body = []
    for row in rows:
        body.append("| " + " | ".join(str(row.get(column, "")) for column in columns) + " |")
    return "\n".join([header, divider, *body]) + "\n"


def _build_markdown_report(
    summary: dict[str, Any],
    coverage_rows: list[dict[str, Any]],
    chain_rows: list[dict[str, Any]],
) -> str:
    lines = [
        "# NSE Cache Validation Report",
        "",
        f"Generated: {summary['generated_at']}",
        "",
        "## Scope",
        "",
        f"- Cached underlyings with option data: {summary['coverage']['underlyings_with_option_data']}",
        f"- ATM monthly expiry pairs analyzed: {summary['coverage']['atm_monthly_pairs']}",
        f"- Opportunities found: {summary['opportunities']['total_trades']}",
        (
            f"- Best average exit strategy: {summary['exit_analysis']['best_strategy']} "
            f"({summary['exit_analysis']['best_strategy_avg_return_pct']}%)"
        ),
        "",
        "## Coverage",
        "",
        _render_table(
            coverage_rows,
            [
                "underlying",
                "kind",
                "expiries_with_complete_contracts",
                "complete_contracts",
                "cached_option_candles",
                "last_option_candle_time",
            ],
        ).rstrip(),
        "",
        "## Exit Strategy Ranking",
        "",
        _render_table(
            summary["exit_analysis"]["strategy_ranking"],
            ["strategy", "trades", "avg_return_pct", "median_return_pct", "positive_pct"],
        ).rstrip(),
        "",
        "## Opportunity Breakdown",
        "",
        "### By Underlying",
        "",
        _render_table(
            summary["breakdowns"]["by_underlying"],
            [
                "underlying",
                "trades",
                "avg_oracle_best_exit_return_pct",
                "avg_max_return_pct",
                "oracle_positive_pct",
                "top_exit_strategy",
            ],
        ).rstrip(),
        "",
        "### By Option Type",
        "",
        _render_table(
            summary["breakdowns"]["by_option_type"],
            [
                "option_type",
                "trades",
                "avg_oracle_best_exit_return_pct",
                "avg_max_return_pct",
                "oracle_positive_pct",
                "top_exit_strategy",
            ],
        ).rstrip(),
        "",
        "### By IV Regime",
        "",
        _render_table(
            summary["breakdowns"]["by_iv_regime"],
            [
                "iv_regime",
                "trades",
                "avg_oracle_best_exit_return_pct",
                "avg_max_return_pct",
                "oracle_positive_pct",
                "top_exit_strategy",
            ],
        ).rstrip(),
        "",
        "### By OI / PCR Context",
        "",
        _render_table(
            summary["breakdowns"]["by_oi_change_regime"],
            [
                "oi_change_regime",
                "trades",
                "avg_oracle_best_exit_return_pct",
                "avg_max_return_pct",
                "oracle_positive_pct",
                "top_exit_strategy",
            ],
        ).rstrip(),
        "",
        _render_table(
            summary["breakdowns"]["by_oi_pcr_regime"],
            [
                "oi_pcr_regime",
                "trades",
                "avg_oracle_best_exit_return_pct",
                "avg_max_return_pct",
                "oracle_positive_pct",
                "top_exit_strategy",
            ],
        ).rstrip(),
        "",
        _render_table(
            summary["breakdowns"]["by_volume_pcr_regime"],
            [
                "volume_pcr_regime",
                "trades",
                "avg_oracle_best_exit_return_pct",
                "avg_max_return_pct",
                "oracle_positive_pct",
                "top_exit_strategy",
            ],
        ).rstrip(),
        "",
        "## Chain Summary",
        "",
        _render_table(
            chain_rows,
            [
                "underlying",
                "expiry",
                "bar_count",
                "avg_oi_pcr",
                "avg_volume_pcr",
                "avg_underlying_price",
            ],
        ).rstrip(),
        "",
    ]
    return "\n".join(lines)


def _fetch_source_updated_at(engine, interval: str) -> str:
    freshness_df = _load_frame(engine, FRESHNESS_SQL, {"interval": interval})
    if freshness_df.empty:
        return datetime.now(timezone.utc).isoformat()
    source_updated_at = freshness_df.iloc[0]["source_updated_at"]
    return _to_iso(source_updated_at) or datetime.now(timezone.utc).isoformat()


def build_live_validation_report(
    *,
    interval: str = "30minute",
    underlyings: Optional[list[str]] = None,
    from_expiry: Optional[date] = None,
    to_expiry: Optional[date] = None,
) -> ValidationReportResult:
    engine = _make_engine()
    try:
        _ensure_validation_views(engine)
        source_updated_at = _fetch_source_updated_at(engine, interval)
        cache_key = json.dumps(
            {
                "interval": interval,
                "underlyings": underlyings or [],
                "from_expiry": from_expiry.isoformat() if from_expiry else None,
                "to_expiry": to_expiry.isoformat() if to_expiry else None,
                "source_updated_at": source_updated_at,
            },
            sort_keys=True,
        )

        with _CACHE_LOCK:
            if _LIVE_REPORT_CACHE["cache_key"] == cache_key and _LIVE_REPORT_CACHE["result"] is not None:
                return _LIVE_REPORT_CACHE["result"]

        coverage_df = _load_frame(engine, COVERAGE_SQL)
        chain_summary_df = _load_frame(engine, CHAIN_SUMMARY_SQL)
        atm_pairs_df = _load_frame(
            engine,
            ATM_PAIRS_SQL,
            {
                "from_expiry": from_expiry,
                "to_expiry": to_expiry,
                "underlyings": underlyings,
            },
        )

        trade_rows: list[dict[str, Any]] = []
        for pair in atm_pairs_df.to_dict("records"):
            chain_df = _load_frame(
                engine,
                CHAIN_METRICS_SQL,
                {
                    "underlying": pair["underlying"],
                    "expiry": pair["expiry"],
                    "interval": interval,
                },
            )
            if not chain_df.empty:
                chain_df["time"] = pd.to_datetime(chain_df["time"], utc=True)
                chain_df = _coerce_numeric(
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
                option_df = _load_frame(
                    engine,
                    OPTION_CANDLES_SQL,
                    {
                        "instrument_key": instrument_key,
                        "interval": interval,
                    },
                )
                if option_df.empty:
                    continue

                option_df["time"] = pd.to_datetime(option_df["time"], utc=True)
                option_df = _coerce_numeric(
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
                    for column in [
                        "oi_pcr",
                        "volume_pcr",
                        "ce_oi",
                        "pe_oi",
                        "ce_volume",
                        "pe_volume",
                    ]:
                        merged[column] = pd.NA

                merged["prev_oi"] = merged["oi"].shift(1)
                merged["prev_volume"] = merged["volume"].shift(1)
                merged["oi_change_pct"] = (
                    (merged["oi"] - merged["prev_oi"]) / merged["prev_oi"] * 100.0
                ).where(merged["prev_oi"] > 0)
                merged["volume_change_pct"] = (
                    (merged["volume"] - merged["prev_volume"]) / merged["prev_volume"] * 100.0
                ).where(merged["prev_volume"] > 0)

                candles = _build_candle_records(merged)
                closes = [float(close) for close in merged["close"].tolist()]
                macd_line, signal_line, histogram = compute_macd(closes)
                selection_date = pd.Timestamp(pair["selection_date"]).date()
                crossover_indices = [
                    idx for idx in find_zero_crossovers(macd_line)
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
                            "entry_iv": float(entry_row["iv"]) if not pd.isna(entry_row["iv"]) else None,
                            "entry_delta": float(entry_row["delta"]) if not pd.isna(entry_row["delta"]) else None,
                            "entry_gamma": float(entry_row["gamma"]) if not pd.isna(entry_row["gamma"]) else None,
                            "entry_theta": float(entry_row["theta"]) if not pd.isna(entry_row["theta"]) else None,
                            "entry_vega": float(entry_row["vega"]) if not pd.isna(entry_row["vega"]) else None,
                            "entry_oi": int(entry_row["oi"]) if not pd.isna(entry_row["oi"]) else None,
                            "entry_volume": int(entry_row["volume"]) if not pd.isna(entry_row["volume"]) else None,
                            "oi_change_pct": round(float(entry_row["oi_change_pct"]), 4) if not pd.isna(entry_row["oi_change_pct"]) else None,
                            "volume_change_pct": round(float(entry_row["volume_change_pct"]), 4) if not pd.isna(entry_row["volume_change_pct"]) else None,
                            "oi_pcr": round(float(entry_row["oi_pcr"]), 6) if "oi_pcr" in entry_row and not pd.isna(entry_row["oi_pcr"]) else None,
                            "volume_pcr": round(float(entry_row["volume_pcr"]), 6) if "volume_pcr" in entry_row and not pd.isna(entry_row["volume_pcr"]) else None,
                            "entry_underlying_price": round(float(entry_row["underlying_price"]), 4) if not pd.isna(entry_row["underlying_price"]) else None,
                            "time_to_expiry_years": round(float(entry_row["time_to_expiry_years"]), 8) if not pd.isna(entry_row["time_to_expiry_years"]) else None,
                            "macd_value": round(float(macd_line[entry_idx]), 6) if macd_line[entry_idx] is not None else None,
                            "signal_value": round(float(signal_line[entry_idx]), 6) if signal_line[entry_idx] is not None else None,
                            "histogram": round(float(histogram[entry_idx]), 6) if histogram[entry_idx] is not None else None,
                            "best_exit_strategy": best_strategy_name,
                            "best_exit_return_pct": round(float(best_strategy_result["return_pct"]), 4),
                            "hold_to_expiry_return_pct": round(float(strategy_results["hold_to_expiry"]["return_pct"]), 4),
                            "strategy_returns_json": json.dumps(strategy_returns, sort_keys=True),
                            **trade_analysis,
                        }
                    )
    finally:
        engine.dispose()

    trades_df = pd.DataFrame(trade_rows)
    if trades_df.empty:
        strategy_ranking: list[dict[str, Any]] = []
        best_strategy = "none"
        best_strategy_avg_return = 0.0
    else:
        trades_df["iv_regime"] = _label_iv_regime(trades_df["entry_iv"])
        trades_df["oi_change_regime"] = _label_signed_change(trades_df["oi_change_pct"], 10.0, "oi")
        trades_df["volume_change_regime"] = _label_signed_change(trades_df["volume_change_pct"], 25.0, "volume")
        trades_df["oi_pcr_regime"] = _label_pcr_regime(trades_df["oi_pcr"], "oi_pcr")
        trades_df["volume_pcr_regime"] = _label_pcr_regime(trades_df["volume_pcr"], "volume_pcr")
        strategy_ranking = _aggregate_strategy_ranking(trades_df)
        best_strategy = strategy_ranking[0]["strategy"] if strategy_ranking else "none"
        best_strategy_avg_return = strategy_ranking[0]["avg_return_pct"] if strategy_ranking else 0.0

    generated_at = datetime.now(timezone.utc).isoformat()
    summary = {
        "generated_at": generated_at,
        "coverage": {
            "underlyings_with_option_data": int(coverage_df["underlying"].nunique()) if not coverage_df.empty else 0,
            "atm_monthly_pairs": int(len(atm_pairs_df)),
            "complete_cached_contracts": int(coverage_df["complete_contracts"].sum()) if not coverage_df.empty else 0,
            "cached_option_candles": int(coverage_df["cached_option_candles"].sum()) if not coverage_df.empty else 0,
        },
        "opportunities": {
            "total_trades": int(len(trades_df)),
            "months": sorted(trades_df["expiry_month"].dropna().unique().tolist()) if not trades_df.empty else [],
            "underlyings": sorted(trades_df["underlying"].dropna().unique().tolist()) if not trades_df.empty else [],
        },
        "exit_analysis": {
            "best_strategy": best_strategy,
            "best_strategy_avg_return_pct": best_strategy_avg_return,
            "hold_to_expiry_avg_return_pct": round(float(trades_df["hold_to_expiry_return_pct"].mean()), 4) if not trades_df.empty else 0.0,
            "avg_max_return_pct": round(float(trades_df["max_return_pct"].mean()), 4) if not trades_df.empty else 0.0,
            "positive_pct": round(float((trades_df["best_exit_return_pct"] > 0).mean() * 100.0), 2) if not trades_df.empty else 0.0,
            "strategy_ranking": strategy_ranking,
        },
        "breakdowns": {
            "by_underlying": _aggregate_bucket(trades_df, "underlying") if not trades_df.empty else [],
            "by_option_type": _aggregate_bucket(trades_df, "option_type") if not trades_df.empty else [],
            "by_month": _aggregate_bucket(trades_df, "expiry_month") if not trades_df.empty else [],
            "by_iv_regime": _aggregate_bucket(trades_df, "iv_regime") if not trades_df.empty else [],
            "by_oi_change_regime": _aggregate_bucket(trades_df, "oi_change_regime") if not trades_df.empty else [],
            "by_volume_change_regime": _aggregate_bucket(trades_df, "volume_change_regime") if not trades_df.empty else [],
            "by_oi_pcr_regime": _aggregate_bucket(trades_df, "oi_pcr_regime") if not trades_df.empty else [],
            "by_volume_pcr_regime": _aggregate_bucket(trades_df, "volume_pcr_regime") if not trades_df.empty else [],
        },
    }

    coverage_rows = [_to_native(row) for row in coverage_df.to_dict("records")]
    chain_rows = [_to_native(row) for row in chain_summary_df.to_dict("records")]
    markdown = _build_markdown_report(summary, coverage_rows, chain_rows)
    payload = {
        "available": summary["coverage"]["complete_cached_contracts"] > 0,
        "live": True,
        "report_key": "nse-cache-live",
        "generated_at": generated_at,
        "source_updated_at": source_updated_at,
        "summary": _to_native(summary),
        "markdown_preview": markdown,
        "files": {
            "report_markdown_url": "/api/analysis/validation-report/latest/file/report.md",
            "summary_json_url": "/api/analysis/validation-report/latest/file/summary.json",
            "trades_csv_url": "/api/analysis/validation-report/latest/file/trades.csv",
            "coverage_csv_url": "/api/analysis/validation-report/latest/file/coverage.csv",
            "chain_summary_csv_url": "/api/analysis/validation-report/latest/file/chain_summary.csv",
        },
    }
    if not payload["available"]:
        payload["detail"] = "Live validation is waiting for complete cached CE/PE pairs."

    result = ValidationReportResult(
        payload=payload,
        artifacts=ValidationArtifacts(
            summary_json=json.dumps(payload["summary"], indent=2),
            report_markdown=markdown,
            trades_csv=trades_df.to_csv(index=False),
            coverage_csv=coverage_df.to_csv(index=False),
            chain_summary_csv=chain_summary_df.to_csv(index=False),
        ),
    )

    with _CACHE_LOCK:
        _LIVE_REPORT_CACHE["cache_key"] = cache_key
        _LIVE_REPORT_CACHE["result"] = result

    return result


def get_live_validation_report_payload(**kwargs: Any) -> dict[str, Any]:
    return build_live_validation_report(**kwargs).payload


def get_live_validation_report_artifact(file_name: str, **kwargs: Any) -> tuple[str, str]:
    result = build_live_validation_report(**kwargs)
    artifacts = result.artifacts
    if file_name == "summary.json":
        return artifacts.summary_json, "application/json"
    if file_name == "report.md":
        return artifacts.report_markdown, "text/markdown; charset=utf-8"
    if file_name == "trades.csv":
        return artifacts.trades_csv, "text/csv; charset=utf-8"
    if file_name == "coverage.csv":
        return artifacts.coverage_csv, "text/csv; charset=utf-8"
    if file_name == "chain_summary.csv":
        return artifacts.chain_summary_csv, "text/csv; charset=utf-8"
    raise KeyError(file_name)
