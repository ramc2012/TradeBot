from __future__ import annotations

import json
from datetime import date, datetime, timezone
from threading import Lock
from typing import Any, Optional

import pandas as pd

from analysis.macd_engine import (
    analyze_trade,
    compute_macd,
    find_zero_crossovers,
    simulate_exit_strategies,
)
from analysis.validation_live import (
    ATM_PAIRS_SQL,
    CHAIN_METRICS_SQL,
    CHAIN_SUMMARY_SQL,
    COVERAGE_SQL,
    OPTION_CANDLES_SQL,
    ValidationArtifacts,
    ValidationReportResult,
    _aggregate_bucket,
    _aggregate_strategy_ranking,
    _build_candle_records,
    _coerce_numeric,
    _ensure_validation_views,
    _fetch_source_updated_at,
    _label_iv_regime,
    _load_frame,
    _make_engine,
    _render_table,
    _to_native,
)
from analytics.greeks_sync import (
    GreeksSyncConfig,
    compute_greeks_sync_frame,
    infer_bar_minutes,
)


_CACHE_LOCK = Lock()
_LIVE_REPORT_CACHE: dict[str, Any] = {
    "cache_key": None,
    "result": None,
}


def _recent_macd_confirmation(
    entry_idx: int,
    macd_indices: list[int],
    window: int,
) -> bool:
    lower_bound = max(0, entry_idx - window)
    return any(lower_bound <= idx <= entry_idx for idx in macd_indices)


def _summarize_track(track: str, df: pd.DataFrame) -> dict[str, Any]:
    if df.empty:
        return {
            "track": track,
            "trades": 0,
            "avg_oracle_best_exit_return_pct": 0.0,
            "avg_max_return_pct": 0.0,
            "avg_hold_to_expiry_return_pct": 0.0,
            "positive_pct": 0.0,
        }

    best_returns = df["best_exit_return_pct"].dropna()
    max_returns = df["max_return_pct"].dropna()
    hold_returns = df["held_return_pct"].dropna()
    return {
        "track": track,
        "trades": int(len(df)),
        "avg_oracle_best_exit_return_pct": round(float(best_returns.mean()), 4)
        if len(best_returns)
        else 0.0,
        "avg_max_return_pct": round(float(max_returns.mean()), 4)
        if len(max_returns)
        else 0.0,
        "avg_hold_to_expiry_return_pct": round(float(hold_returns.mean()), 4)
        if len(hold_returns)
        else 0.0,
        "positive_pct": round(float((best_returns > 0).mean() * 100.0), 2)
        if len(best_returns)
        else 0.0,
    }


def _build_comparison_rows(
    sync_df: pd.DataFrame,
    macd_df: pd.DataFrame,
) -> list[dict[str, Any]]:
    rows = [
        _summarize_track("greeks_sync", sync_df),
        _summarize_track("macd_zero_cross_baseline", macd_df),
    ]
    if not sync_df.empty and "macd_zero_cross_recent" in sync_df.columns:
        rows.append(
            _summarize_track(
                "greeks_sync_macd_confirmed",
                sync_df[sync_df["macd_zero_cross_recent"]].copy(),
            )
        )
        rows.append(
            _summarize_track(
                "greeks_sync_only",
                sync_df[~sync_df["macd_zero_cross_recent"]].copy(),
            )
        )
    return [row for row in rows if row["trades"] > 0]


def _build_markdown_report(
    summary: dict[str, Any],
    coverage_rows: list[dict[str, Any]],
    chain_rows: list[dict[str, Any]],
) -> str:
    lines = [
        "# Greeks Sync Research Report",
        "",
        f"Generated: {summary['generated_at']}",
        "",
        "## Scope",
        "",
        f"- Cached underlyings with option data: {summary['coverage']['underlyings_with_option_data']}",
        f"- ATM monthly expiry pairs analyzed: {summary['coverage']['atm_monthly_pairs']}",
        f"- Greeks Sync signals found: {summary['signals']['total_signals']}",
        f"- Strong Greeks Sync signals: {summary['signals']['strong_signals']}",
        f"- Average Greeks Sync score: {summary['signals']['avg_score']}",
        f"- MACD confirmation rate inside sync window: {summary['signals']['macd_confirmed_pct']}%",
        "",
        "## Signal Model",
        "",
        f"- Delta lookback bars: {summary['signal_model']['delta_lookback_bars']}",
        f"- IV lookback minutes: {summary['signal_model']['iv_lookback_minutes']}",
        f"- Gamma threshold: {summary['signal_model']['gamma_threshold']}",
        f"- Score threshold: {summary['signal_model']['score_threshold']}",
        f"- Strong score threshold: {summary['signal_model']['strong_score_threshold']}",
        "",
        "## Research Track Comparison",
        "",
        _render_table(
            summary["comparison"]["track_ranking"],
            [
                "track",
                "trades",
                "avg_oracle_best_exit_return_pct",
                "avg_max_return_pct",
                "avg_hold_to_expiry_return_pct",
                "positive_pct",
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
        "### By Score Bucket",
        "",
        _render_table(
            summary["breakdowns"]["by_score_bucket"],
            [
                "greeks_sync_score_bucket",
                "trades",
                "avg_oracle_best_exit_return_pct",
                "avg_max_return_pct",
                "oracle_positive_pct",
                "top_exit_strategy",
            ],
        ).rstrip(),
        "",
        "### By MACD Confirmation",
        "",
        _render_table(
            summary["breakdowns"]["by_macd_confirmation"],
            [
                "macd_confirmation_regime",
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
        "### By Theta Overwhelm",
        "",
        _render_table(
            summary["breakdowns"]["by_theta_ratio_bucket"],
            [
                "theta_ratio_bucket",
                "trades",
                "avg_oracle_best_exit_return_pct",
                "avg_max_return_pct",
                "oracle_positive_pct",
                "top_exit_strategy",
            ],
        ).rstrip(),
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


def build_live_greeks_sync_report(
    *,
    interval: str = "30minute",
    underlyings: Optional[list[str]] = None,
    from_expiry: Optional[date] = None,
    to_expiry: Optional[date] = None,
    config: Optional[GreeksSyncConfig] = None,
) -> ValidationReportResult:
    cfg = config or GreeksSyncConfig()
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
                "score_threshold": cfg.score_threshold,
                "strong_score_threshold": cfg.strong_score_threshold,
            },
            sort_keys=True,
        )

        with _CACHE_LOCK:
            if (
                _LIVE_REPORT_CACHE["cache_key"] == cache_key
                and _LIVE_REPORT_CACHE["result"] is not None
            ):
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

        sync_rows: list[dict[str, Any]] = []
        macd_rows: list[dict[str, Any]] = []

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

                scored = compute_greeks_sync_frame(
                    merged,
                    option_type,
                    config=cfg,
                    bar_minutes=infer_bar_minutes(merged),
                )

                candles = _build_candle_records(scored)
                closes = [float(close) for close in scored["close"].tolist()]
                macd_line, signal_line, histogram = compute_macd(closes)
                selection_date = pd.Timestamp(pair["selection_date"]).date()
                macd_indices = [
                    idx
                    for idx in find_zero_crossovers(macd_line)
                    if scored["time"].iloc[idx].date() >= selection_date
                ]
                sync_indices = [
                    int(idx)
                    for idx in scored.index[
                        scored["greeks_sync_signal"]
                        & (scored["time"].dt.date >= selection_date)
                    ].tolist()
                ]

                for entry_idx in macd_indices:
                    trade_analysis = analyze_trade(candles, entry_idx)
                    strategy_results = simulate_exit_strategies(candles, entry_idx)
                    best_strategy_name, best_strategy_result = max(
                        strategy_results.items(),
                        key=lambda item: item[1]["return_pct"],
                    )
                    macd_rows.append(
                        {
                            "underlying": pair["underlying"],
                            "expiry": str(pair["expiry"]),
                            "option_type": option_type,
                            "entry_time": scored.iloc[entry_idx]["time"].isoformat(),
                            "best_exit_strategy": best_strategy_name,
                            "best_exit_return_pct": round(
                                float(best_strategy_result["return_pct"]), 4
                            ),
                            **trade_analysis,
                        }
                    )

                for entry_idx in sync_indices:
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
                    entry_row = scored.iloc[entry_idx]
                    sync_rows.append(
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
                            "entry_underlying_price": round(float(entry_row["underlying_price"]), 4)
                            if not pd.isna(entry_row["underlying_price"])
                            else None,
                            "time_to_expiry_years": round(float(entry_row["time_to_expiry_years"]), 8)
                            if not pd.isna(entry_row["time_to_expiry_years"])
                            else None,
                            "macd_value": round(float(macd_line[entry_idx]), 6)
                            if macd_line[entry_idx] is not None
                            else None,
                            "signal_value": round(float(signal_line[entry_idx]), 6)
                            if signal_line[entry_idx] is not None
                            else None,
                            "histogram": round(float(histogram[entry_idx]), 6)
                            if histogram[entry_idx] is not None
                            else None,
                            "greeks_sync_score": round(float(entry_row["greeks_sync_score"]), 4),
                            "greeks_sync_strength": entry_row["greeks_sync_strength"],
                            "greeks_sync_score_bucket": entry_row["greeks_sync_score_bucket"],
                            "delta_score": round(float(entry_row["delta_score"]), 4),
                            "gamma_score": round(float(entry_row["gamma_score"]), 4),
                            "vega_score": round(float(entry_row["vega_score"]), 4),
                            "theta_score": round(float(entry_row["theta_score"]), 4),
                            "delta_momentum": round(float(entry_row["delta_momentum"]), 6),
                            "iv_change_pct_points": round(float(entry_row["iv_change_pct_points"]), 6),
                            "theta_overwhelm_ratio": round(float(entry_row["theta_overwhelm_ratio"]), 6),
                            "theta_ratio_bucket": entry_row["theta_ratio_bucket"],
                            "directional_contribution": round(float(entry_row["directional_contribution"]), 6),
                            "convexity_contribution": round(float(entry_row["convexity_contribution"]), 6),
                            "vega_iv_contribution": round(float(entry_row["vega_iv_contribution"]), 6),
                            "theta_bar_drag": round(float(entry_row["theta_bar_drag"]), 6),
                            "oi_change_pct": round(float(entry_row["oi_change_pct"]), 4)
                            if not pd.isna(entry_row["oi_change_pct"])
                            else None,
                            "volume_change_pct": round(float(entry_row["volume_change_pct"]), 4)
                            if not pd.isna(entry_row["volume_change_pct"])
                            else None,
                            "oi_pcr": round(float(entry_row["oi_pcr"]), 6)
                            if "oi_pcr" in entry_row and not pd.isna(entry_row["oi_pcr"])
                            else None,
                            "volume_pcr": round(float(entry_row["volume_pcr"]), 6)
                            if "volume_pcr" in entry_row and not pd.isna(entry_row["volume_pcr"])
                            else None,
                            "macd_zero_cross_recent": _recent_macd_confirmation(
                                entry_idx,
                                macd_indices,
                                cfg.macd_confirmation_window,
                            ),
                            "best_exit_strategy": best_strategy_name,
                            "best_exit_return_pct": round(
                                float(best_strategy_result["return_pct"]), 4
                            ),
                            "hold_to_expiry_return_pct": round(
                                float(strategy_results["hold_to_expiry"]["return_pct"]),
                                4,
                            ),
                            "strategy_returns_json": json.dumps(
                                strategy_returns,
                                sort_keys=True,
                            ),
                            **trade_analysis,
                        }
                    )
    finally:
        engine.dispose()

    sync_df = pd.DataFrame(sync_rows)
    macd_df = pd.DataFrame(macd_rows)

    if sync_df.empty:
        strategy_ranking: list[dict[str, Any]] = []
        best_strategy = "none"
        best_strategy_avg_return = 0.0
        comparison_rows = _build_comparison_rows(sync_df, macd_df)
    else:
        sync_df["iv_regime"] = _label_iv_regime(sync_df["entry_iv"])
        sync_df["macd_confirmation_regime"] = sync_df["macd_zero_cross_recent"].map(
            {True: "macd_confirmed", False: "greeks_only"}
        )
        strategy_ranking = _aggregate_strategy_ranking(sync_df)
        best_strategy = strategy_ranking[0]["strategy"] if strategy_ranking else "none"
        best_strategy_avg_return = (
            strategy_ranking[0]["avg_return_pct"] if strategy_ranking else 0.0
        )
        comparison_rows = _build_comparison_rows(sync_df, macd_df)

    generated_at = datetime.now(timezone.utc).isoformat()
    summary = {
        "generated_at": generated_at,
        "research_track": "greeks_sync",
        "coverage": {
            "underlyings_with_option_data": int(coverage_df["underlying"].nunique())
            if not coverage_df.empty
            else 0,
            "atm_monthly_pairs": int(len(atm_pairs_df)),
            "complete_cached_contracts": int(coverage_df["complete_contracts"].sum())
            if not coverage_df.empty
            else 0,
            "cached_option_candles": int(coverage_df["cached_option_candles"].sum())
            if not coverage_df.empty
            else 0,
        },
        "signal_model": {
            "delta_lookback_bars": cfg.delta_lookback_bars,
            "iv_lookback_minutes": cfg.iv_lookback_minutes,
            "gamma_threshold": cfg.gamma_threshold,
            "score_threshold": cfg.score_threshold,
            "strong_score_threshold": cfg.strong_score_threshold,
            "macd_confirmation_window": cfg.macd_confirmation_window,
        },
        "signals": {
            "total_signals": int(len(sync_df)),
            "strong_signals": int((sync_df["greeks_sync_strength"] == "strong").sum())
            if not sync_df.empty
            else 0,
            "avg_score": round(float(sync_df["greeks_sync_score"].mean()), 4)
            if not sync_df.empty
            else 0.0,
            "median_score": round(float(sync_df["greeks_sync_score"].median()), 4)
            if not sync_df.empty
            else 0.0,
            "avg_theta_overwhelm_ratio": round(
                float(sync_df["theta_overwhelm_ratio"].mean()),
                4,
            )
            if not sync_df.empty
            else 0.0,
            "macd_confirmed_pct": round(
                float(sync_df["macd_zero_cross_recent"].mean() * 100.0),
                2,
            )
            if not sync_df.empty
            else 0.0,
        },
        "comparison": {
            "track_ranking": comparison_rows,
        },
        "exit_analysis": {
            "best_strategy": best_strategy,
            "best_strategy_avg_return_pct": best_strategy_avg_return,
            "strategy_ranking": strategy_ranking,
        },
        "breakdowns": {
            "by_underlying": _aggregate_bucket(sync_df, "underlying")
            if not sync_df.empty
            else [],
            "by_option_type": _aggregate_bucket(sync_df, "option_type")
            if not sync_df.empty
            else [],
            "by_score_bucket": _aggregate_bucket(sync_df, "greeks_sync_score_bucket")
            if not sync_df.empty
            else [],
            "by_macd_confirmation": _aggregate_bucket(
                sync_df,
                "macd_confirmation_regime",
            )
            if not sync_df.empty
            else [],
            "by_iv_regime": _aggregate_bucket(sync_df, "iv_regime")
            if not sync_df.empty
            else [],
            "by_theta_ratio_bucket": _aggregate_bucket(sync_df, "theta_ratio_bucket")
            if not sync_df.empty
            else [],
        },
    }

    coverage_rows = [_to_native(row) for row in coverage_df.to_dict("records")]
    chain_rows = [_to_native(row) for row in chain_summary_df.to_dict("records")]
    markdown = _build_markdown_report(summary, coverage_rows, chain_rows)
    payload = {
        "available": summary["coverage"]["complete_cached_contracts"] > 0,
        "live": True,
        "report_key": "greeks-sync-live",
        "generated_at": generated_at,
        "source_updated_at": source_updated_at,
        "summary": _to_native(summary),
        "markdown_preview": markdown,
        "files": {
            "report_markdown_url": "/api/analysis/greeks-sync-report/latest/file/report.md",
            "summary_json_url": "/api/analysis/greeks-sync-report/latest/file/summary.json",
            "trades_csv_url": "/api/analysis/greeks-sync-report/latest/file/trades.csv",
            "coverage_csv_url": "/api/analysis/greeks-sync-report/latest/file/coverage.csv",
            "chain_summary_csv_url": "/api/analysis/greeks-sync-report/latest/file/chain_summary.csv",
        },
    }
    if not payload["available"]:
        payload["detail"] = "Greeks Sync research is waiting for complete cached CE/PE pairs."

    result = ValidationReportResult(
        payload=payload,
        artifacts=ValidationArtifacts(
            summary_json=json.dumps(payload["summary"], indent=2),
            report_markdown=markdown,
            trades_csv=sync_df.to_csv(index=False),
            coverage_csv=coverage_df.to_csv(index=False),
            chain_summary_csv=chain_summary_df.to_csv(index=False),
        ),
    )

    with _CACHE_LOCK:
        _LIVE_REPORT_CACHE["cache_key"] = cache_key
        _LIVE_REPORT_CACHE["result"] = result

    return result


def get_live_greeks_sync_report_payload(**kwargs: Any) -> dict[str, Any]:
    return build_live_greeks_sync_report(**kwargs).payload


def get_live_greeks_sync_report_artifact(file_name: str, **kwargs: Any) -> tuple[str, str]:
    result = build_live_greeks_sync_report(**kwargs)
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
