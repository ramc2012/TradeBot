"""Evaluate higher-timeframe spot-regime filters on MACD option trades.

This analysis uses the persisted full-dataset MACD-zero trade sweep and filters
trades so only:

- CE entries in bullish spot regimes
- PE entries in bearish spot regimes

are kept. Spot regimes are computed from higher-timeframe spot closes.
"""
from __future__ import annotations

import csv
import json
import math
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from analysis.index_option_walkforward import DATA_ROOT


TRADE_SOURCE = DATA_ROOT / "indicator_sweep_ohlc" / "trade_results.csv"
OUTPUT_ROOT = DATA_ROOT / "spot_regime_filter"

BASELINE_EXIT_BY_GROUP = {
    "weekly_series": "trail_after_20pct_dd10pct",
    "monthly_series": "trail_after_20pct_dd10pct",
    "weekly_expiry_day": "target_10pct",
    "monthly_expiry_day": "target_10pct",
}

SPOT_TIMEFRAME_RULES = {
    "15m": "15min",
    "30m": "30min",
    "60m": "60min",
    "120m": "120min",
    "1d": "1D",
}

REGIME_MODELS = (
    "ema_alignment",
    "macd_bias",
    "ema_macd_agree",
)


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(float(statistics.fmean(values)), 4)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(float(statistics.median(values)), 4)


def _score(row: dict[str, Any]) -> float:
    opportunities = float(row.get("opportunities", 0) or 0)
    if opportunities <= 0:
        return float("-inf")
    avg_return = float(row.get("avg_return_pct", 0.0) or 0.0)
    win_rate = float(row.get("win_rate", 0.0) or 0.0)
    median_return = float(row.get("median_return_pct", 0.0) or 0.0)
    robust_underlyings = float(row.get("positive_underlyings", 0) or 0)
    base = (avg_return * 0.5) + (median_return * 0.35) + (robust_underlyings * 2.0)
    return round(base * max(win_rate, 0.01) * math.log1p(opportunities), 6)


def _summarize(df: pd.DataFrame) -> dict[str, Any]:
    returns = df["return_pct"].astype(float).tolist()
    wins = [value for value in returns if value > 0.0]
    losses = [value for value in returns if value <= 0.0]
    holding_minutes = df["holding_minutes"].astype(float).tolist() if "holding_minutes" in df else []
    return {
        "opportunities": int(len(df)),
        "win_rate": round((len(wins) / len(df)), 4) if len(df) else 0.0,
        "avg_return_pct": _mean(returns),
        "median_return_pct": _median(returns),
        "avg_win_return_pct": _mean(wins),
        "avg_loss_return_pct": _mean(losses),
        "max_return_pct": round(max(returns), 4) if returns else 0.0,
        "min_return_pct": round(min(returns), 4) if returns else 0.0,
        "avg_holding_minutes": _mean(holding_minutes),
        "median_holding_minutes": _median(holding_minutes),
    }


def _load_baseline_trades() -> pd.DataFrame:
    trades = pd.read_csv(TRADE_SOURCE)
    trades = trades[trades["indicator"] == "macd_zero"].copy()
    trades = trades[
        trades.apply(
            lambda row: row["exit_name"] == BASELINE_EXIT_BY_GROUP.get(row["group_name"]),
            axis=1,
        )
    ].copy()
    trades["entry_time"] = pd.to_datetime(trades["entry_time"])
    trades["exit_time"] = pd.to_datetime(trades["exit_time"])
    trades["return_pct"] = trades["return_pct"].astype(float)
    trades["holding_minutes"] = trades["holding_minutes"].astype(float)
    return trades.sort_values("entry_time").reset_index(drop=True)


def _compute_macd(close: pd.Series) -> tuple[pd.Series, pd.Series]:
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    return macd_line, signal_line


def _spot_regime_frame(underlying: str, spot_timeframe: str) -> pd.DataFrame:
    path = DATA_ROOT / "spot" / f"underlying={underlying}" / "1minute.csv.gz"
    frame = pd.read_csv(path)
    frame["time"] = pd.to_datetime(frame["time"])
    frame = frame.sort_values("time")

    if spot_timeframe == "1d":
        # Use only completed daily bars. Each bar is stamped at the session's
        # last minute so intraday entries consume the previous completed day.
        frame["session_date"] = frame["time"].dt.date
        frame = (
            frame.groupby("session_date", as_index=False)
            .agg(time=("time", "max"), close=("close", "last"))
            .dropna()
        )
    else:
        rule = SPOT_TIMEFRAME_RULES[spot_timeframe]
        frame = (
            frame.set_index("time")
            .resample(rule, label="right", closed="right")
            .agg({"close": "last"})
            .dropna()
            .reset_index()
        )

    close = frame["close"].astype(float)
    frame["ema20"] = close.ewm(span=20, adjust=False).mean()
    frame["ema50"] = close.ewm(span=50, adjust=False).mean()
    macd_line, signal_line = _compute_macd(close)
    frame["macd"] = macd_line
    frame["signal"] = signal_line

    frame["ema_alignment"] = "neutral"
    frame.loc[
        (frame["close"] > frame["ema20"]) & (frame["ema20"] > frame["ema50"]),
        "ema_alignment",
    ] = "bullish"
    frame.loc[
        (frame["close"] < frame["ema20"]) & (frame["ema20"] < frame["ema50"]),
        "ema_alignment",
    ] = "bearish"

    frame["macd_bias"] = "neutral"
    frame.loc[(frame["macd"] > 0.0) & (frame["signal"] > 0.0), "macd_bias"] = "bullish"
    frame.loc[(frame["macd"] < 0.0) & (frame["signal"] < 0.0), "macd_bias"] = "bearish"

    frame["ema_macd_agree"] = "neutral"
    frame.loc[
        (frame["ema_alignment"] == "bullish") & (frame["macd_bias"] == "bullish"),
        "ema_macd_agree",
    ] = "bullish"
    frame.loc[
        (frame["ema_alignment"] == "bearish") & (frame["macd_bias"] == "bearish"),
        "ema_macd_agree",
    ] = "bearish"

    return frame[["time", *REGIME_MODELS]].sort_values("time")


class SpotRegimeFilterAnalysis:
    def __init__(self) -> None:
        self.output_root = OUTPUT_ROOT
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.trades = _load_baseline_trades()
        self._regime_cache: dict[tuple[str, str], pd.DataFrame] = {}

    def _regime_lookup(self, underlying: str, spot_timeframe: str) -> pd.DataFrame:
        key = (underlying, spot_timeframe)
        if key not in self._regime_cache:
            self._regime_cache[key] = _spot_regime_frame(underlying, spot_timeframe)
        return self._regime_cache[key]

    def _apply_filter(self, trades: pd.DataFrame, spot_timeframe: str, regime_model: str) -> pd.DataFrame:
        tagged: list[pd.DataFrame] = []
        for underlying in sorted(trades["underlying"].unique()):
            regime = self._regime_lookup(underlying, spot_timeframe)
            subset = trades[trades["underlying"] == underlying].sort_values("entry_time").copy()
            tagged.append(
                pd.merge_asof(
                    subset,
                    regime,
                    left_on="entry_time",
                    right_on="time",
                    direction="backward",
                )
            )
        tagged_frame = pd.concat(tagged, ignore_index=True) if tagged else trades.iloc[0:0].copy()
        return tagged_frame[
            ((tagged_frame["option_type"] == "CE") & (tagged_frame[regime_model] == "bullish"))
            | ((tagged_frame["option_type"] == "PE") & (tagged_frame[regime_model] == "bearish"))
        ].copy()

    def run(self) -> dict[str, Any]:
        all_rows: list[dict[str, Any]] = []
        all_underlying_rows: list[dict[str, Any]] = []

        baseline_rows: dict[tuple[str, str], dict[str, Any]] = {}
        for (group_name, timeframe), group_df in self.trades.groupby(["group_name", "timeframe"]):
            summary = _summarize(group_df)
            row = {
                "group_name": group_name,
                "timeframe": timeframe,
                "spot_timeframe": "baseline",
                "regime_model": "unfiltered",
                "kept_pct": 100.0,
                "positive_underlyings": int(
                    sum(
                        group_df[group_df["underlying"] == underlying]["return_pct"].mean() > 0.0
                        for underlying in sorted(group_df["underlying"].unique())
                    )
                ),
                **summary,
            }
            row["score"] = _score(row)
            baseline_rows[(group_name, timeframe)] = row
            all_rows.append(row)
            for underlying, underlying_df in group_df.groupby("underlying"):
                underlying_row = {
                    "group_name": group_name,
                    "timeframe": timeframe,
                    "spot_timeframe": "baseline",
                    "regime_model": "unfiltered",
                    "underlying": underlying,
                    "kept_pct": 100.0,
                    **_summarize(underlying_df),
                }
                all_underlying_rows.append(underlying_row)

        for spot_timeframe in SPOT_TIMEFRAME_RULES:
            for regime_model in REGIME_MODELS:
                filtered = self._apply_filter(self.trades, spot_timeframe, regime_model)
                for (group_name, timeframe), group_df in filtered.groupby(["group_name", "timeframe"]):
                    base_row = baseline_rows[(group_name, timeframe)]
                    summary = _summarize(group_df)
                    positive_underlyings = 0
                    for underlying in sorted(group_df["underlying"].unique()):
                        avg_return = float(group_df[group_df["underlying"] == underlying]["return_pct"].mean())
                        if avg_return > 0.0:
                            positive_underlyings += 1
                    row = {
                        "group_name": group_name,
                        "timeframe": timeframe,
                        "spot_timeframe": spot_timeframe,
                        "regime_model": regime_model,
                        "kept_pct": round(
                            (len(group_df) / max(base_row["opportunities"], 1)) * 100.0,
                            2,
                        ),
                        "positive_underlyings": positive_underlyings,
                        "avg_return_delta_pct": round(summary["avg_return_pct"] - base_row["avg_return_pct"], 4),
                        "win_rate_delta_pct": round((summary["win_rate"] - base_row["win_rate"]) * 100.0, 2),
                        "median_return_delta_pct": round(
                            summary["median_return_pct"] - base_row["median_return_pct"],
                            4,
                        ),
                        **summary,
                    }
                    row["score"] = _score(row)
                    all_rows.append(row)

                    for underlying, underlying_df in group_df.groupby("underlying"):
                        underlying_summary = _summarize(underlying_df)
                        base_underlying = self.trades[
                            (self.trades["group_name"] == group_name)
                            & (self.trades["timeframe"] == timeframe)
                            & (self.trades["underlying"] == underlying)
                        ]
                        underlying_row = {
                            "group_name": group_name,
                            "timeframe": timeframe,
                            "spot_timeframe": spot_timeframe,
                            "regime_model": regime_model,
                            "underlying": underlying,
                            "kept_pct": round(
                                (len(underlying_df) / max(len(base_underlying), 1)) * 100.0,
                                2,
                            ),
                            "avg_return_delta_pct": round(
                                underlying_summary["avg_return_pct"] - float(base_underlying["return_pct"].mean()),
                                4,
                            ),
                            **underlying_summary,
                        }
                        all_underlying_rows.append(underlying_row)

        summary = self._build_summary(all_rows, all_underlying_rows)
        self._write_outputs(summary, all_rows, all_underlying_rows)
        return summary

    def _build_summary(
        self,
        all_rows: list[dict[str, Any]],
        all_underlying_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        rows_df = pd.DataFrame(all_rows)
        filtered_df = rows_df[rows_df["regime_model"] != "unfiltered"].copy()
        underlying_df = pd.DataFrame(all_underlying_rows)

        summary: dict[str, Any] = {
            "generated_at": datetime.now().astimezone().isoformat(),
            "trade_source": str(TRADE_SOURCE),
            "output_root": str(self.output_root),
            "spot_timeframes": list(SPOT_TIMEFRAME_RULES.keys()),
            "regime_models": list(REGIME_MODELS),
            "baseline_exit_by_group": BASELINE_EXIT_BY_GROUP,
            "baseline_overall": _summarize(self.trades),
            "groups": {},
            "best_overall_filtered": [],
            "robust_candidates": [],
        }

        best_overall = filtered_df.sort_values(["score", "avg_return_pct", "opportunities"], ascending=False)
        summary["best_overall_filtered"] = best_overall.head(20).to_dict(orient="records")

        robust = filtered_df[
            (filtered_df["avg_return_pct"] > 0.0)
            & (filtered_df["opportunities"] >= 20)
            & (filtered_df["positive_underlyings"] >= 2)
        ].sort_values(["score", "avg_return_pct", "opportunities"], ascending=False)
        summary["robust_candidates"] = robust.head(20).to_dict(orient="records")

        for (group_name, timeframe), baseline_row in rows_df[rows_df["regime_model"] == "unfiltered"].set_index(
            ["group_name", "timeframe"]
        ).iterrows():
            group_filtered = filtered_df[
                (filtered_df["group_name"] == group_name) & (filtered_df["timeframe"] == timeframe)
            ].copy()
            group_filtered = group_filtered.sort_values(["score", "avg_return_pct", "opportunities"], ascending=False)
            key = f"{group_name}|{timeframe}"
            summary["groups"][key] = {
                "baseline": baseline_row.to_dict(),
                "best_filtered": group_filtered.head(10).to_dict(orient="records"),
                "positive_filtered": group_filtered[group_filtered["avg_return_pct"] > 0.0]
                .head(10)
                .to_dict(orient="records"),
                "underlying_breakdown": underlying_df[
                    (underlying_df["group_name"] == group_name) & (underlying_df["timeframe"] == timeframe)
                ].sort_values(
                    ["spot_timeframe", "regime_model", "underlying"]
                ).to_dict(orient="records"),
            }

        return summary

    def _write_outputs(
        self,
        summary: dict[str, Any],
        all_rows: list[dict[str, Any]],
        all_underlying_rows: list[dict[str, Any]],
    ) -> None:
        self.output_root.mkdir(parents=True, exist_ok=True)
        (self.output_root / "summary.json").write_text(json.dumps(summary, indent=2))

        if all_rows:
            fieldnames = sorted({key for row in all_rows for key in row.keys()})
            with (self.output_root / "filtered_results.csv").open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(all_rows)

        if all_underlying_rows:
            fieldnames = sorted({key for row in all_underlying_rows for key in row.keys()})
            with (self.output_root / "underlying_results.csv").open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(all_underlying_rows)

        lines = [
            "# Spot Regime Filter Analysis",
            "",
            f"Generated: {summary['generated_at']}",
            f"Trade source: `{summary['trade_source']}`",
            "",
            "## Baseline",
            "",
            f"- Opportunities: {summary['baseline_overall']['opportunities']}",
            f"- Win rate: {summary['baseline_overall']['win_rate'] * 100:.2f}%",
            f"- Avg return: {summary['baseline_overall']['avg_return_pct']:.2f}%",
            "",
            "## Robust Candidates",
            "",
        ]
        for row in summary["robust_candidates"][:10]:
            lines.extend(
                [
                    f"- `{row['group_name']} | {row['timeframe']} | spot {row['spot_timeframe']} | {row['regime_model']}`",
                    f"  opportunities={row['opportunities']}, kept={row['kept_pct']:.2f}%, "
                    f"win={row['win_rate'] * 100:.2f}%, avg={row['avg_return_pct']:.2f}%, "
                    f"median={row['median_return_pct']:.2f}%",
                ]
            )
        (self.output_root / "report.md").write_text("\n".join(lines))


def main() -> None:
    summary = SpotRegimeFilterAnalysis().run()
    print(json.dumps(summary["robust_candidates"][:5], indent=2))


if __name__ == "__main__":
    main()
