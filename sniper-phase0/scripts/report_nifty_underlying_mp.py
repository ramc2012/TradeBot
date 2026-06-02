"""Generate an OOS report for the promoted NIFTY-underlying MP model artifact."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd


@dataclass
class ReportSummary:
    artifact_dir: str
    threshold: float
    rows_oos: int
    n_acted: int
    coverage: float
    hit_rate: float
    total_pnl_atr: float
    average_pnl_atr: float
    max_drawdown_atr: float
    profit_factor: float
    best_month_atr: float
    worst_month_atr: float
    positive_months: int
    acted_months: int
    up_trades: int
    down_trades: int
    up_hit_rate: float
    down_hit_rate: float


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=Path("artifacts/nifty_underlying_mp_current"))
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--cost-atr", type=float, default=0.05)
    args = parser.parse_args()

    root = args.artifact_dir
    labels = pd.read_parquet(root / "labels.parquet")
    oos = pd.read_parquet(root / "oos_predictions.parquet")
    summary_path = root / "summary.json"
    stored_summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    threshold = float(args.threshold or stored_summary.get("selected_threshold") or 0.5)

    trades = build_trades(labels, oos, threshold=threshold, cost_atr=args.cost_atr)
    daily = daily_pnl(trades)
    monthly = monthly_pnl(trades)
    threshold_sweep = build_threshold_sweep(labels, oos, cost_atr=args.cost_atr)
    report = summarize(root, trades, monthly, threshold, rows_oos=len(oos))

    trades.to_csv(root / "oos_acted_trades.csv", index=False)
    daily.to_csv(root / "daily_pnl.csv", index=False)
    monthly.to_csv(root / "monthly_pnl.csv", index=False)
    threshold_sweep.to_csv(root / "report_threshold_sweep.csv", index=False)
    (root / "backtest_report.json").write_text(json.dumps(asdict(report), indent=2))
    (root / "backtest_report.md").write_text(render_markdown(report, monthly, threshold_sweep))
    print(json.dumps(asdict(report), indent=2))


def build_trades(
    labels: pd.DataFrame,
    oos: pd.DataFrame,
    *,
    threshold: float,
    cost_atr: float,
) -> pd.DataFrame:
    common = labels.index.intersection(oos.index)
    labels = labels.loc[common].copy()
    oos = oos.loc[common].copy()
    rows = []
    for row_id, pred in oos.iterrows():
        direction = "none"
        if float(pred["p_up"]) >= threshold and float(pred["p_up"]) >= float(pred["p_down"]):
            direction = "up"
        elif float(pred["p_down"]) >= threshold and float(pred["p_down"]) > float(pred["p_up"]):
            direction = "down"
        if direction == "none":
            continue
        truth = str(labels.loc[row_id, "direction"])
        hit = direction == truth and truth != "none"
        gross = float(labels.loc[row_id, "magnitude_atr"]) if hit else -float(labels.loc[row_id, "mae_atr"])
        pnl = gross - cost_atr
        rows.append(
            {
                "row_id": row_id,
                "decision_time": labels.loc[row_id, "decision_time"],
                "month": pd.Timestamp(labels.loc[row_id, "decision_time"]).strftime("%Y-%m"),
                "pred_direction": direction,
                "truth_direction": truth,
                "p_up": float(pred["p_up"]),
                "p_down": float(pred["p_down"]),
                "p_none": float(pred["p_none"]),
                "hit": bool(hit),
                "gross_pnl_atr": gross,
                "cost_atr": cost_atr,
                "pnl_atr": pnl,
                "magnitude_atr": float(labels.loc[row_id, "magnitude_atr"]),
                "mae_atr": float(labels.loc[row_id, "mae_atr"]),
            }
        )
    return pd.DataFrame(rows)


def daily_pnl(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(columns=["date", "trades", "pnl_atr", "cum_pnl_atr", "drawdown_atr"])
    df = trades.copy()
    df["date"] = pd.to_datetime(df["decision_time"]).dt.date
    out = df.groupby("date").agg(trades=("pnl_atr", "size"), pnl_atr=("pnl_atr", "sum")).reset_index()
    out["cum_pnl_atr"] = out["pnl_atr"].cumsum()
    out["drawdown_atr"] = out["cum_pnl_atr"] - out["cum_pnl_atr"].cummax()
    return out


def monthly_pnl(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(columns=["month", "trades", "hit_rate", "pnl_atr"])
    out = (
        trades.groupby("month")
        .agg(trades=("pnl_atr", "size"), hit_rate=("hit", "mean"), pnl_atr=("pnl_atr", "sum"))
        .reset_index()
    )
    return out.sort_values("month")


def build_threshold_sweep(labels: pd.DataFrame, oos: pd.DataFrame, *, cost_atr: float) -> pd.DataFrame:
    rows = []
    for threshold in np.arange(0.50, 0.86, 0.02):
        trades = build_trades(labels, oos, threshold=float(threshold), cost_atr=cost_atr)
        if trades.empty:
            rows.append({"threshold": round(float(threshold), 2), "trades": 0, "hit_rate": 0.0, "ev_atr": 0.0, "total_atr": 0.0})
            continue
        rows.append(
            {
                "threshold": round(float(threshold), 2),
                "trades": int(len(trades)),
                "hit_rate": float(trades["hit"].mean()),
                "ev_atr": float(trades["pnl_atr"].mean()),
                "total_atr": float(trades["pnl_atr"].sum()),
            }
        )
    return pd.DataFrame(rows)


def summarize(
    root: Path,
    trades: pd.DataFrame,
    monthly: pd.DataFrame,
    threshold: float,
    *,
    rows_oos: int,
) -> ReportSummary:
    pnl = trades["pnl_atr"] if not trades.empty else pd.Series(dtype=float)
    wins = float(pnl[pnl > 0].sum())
    losses = abs(float(pnl[pnl < 0].sum()))
    daily = daily_pnl(trades)
    up = trades[trades["pred_direction"] == "up"] if not trades.empty else trades
    down = trades[trades["pred_direction"] == "down"] if not trades.empty else trades
    return ReportSummary(
        artifact_dir=str(root),
        threshold=float(threshold),
        rows_oos=int(rows_oos),
        n_acted=int(len(trades)),
        coverage=float(len(trades) / rows_oos) if rows_oos else 0.0,
        hit_rate=float(trades["hit"].mean()) if not trades.empty else 0.0,
        total_pnl_atr=float(pnl.sum()) if not pnl.empty else 0.0,
        average_pnl_atr=float(pnl.mean()) if not pnl.empty else 0.0,
        max_drawdown_atr=float(daily["drawdown_atr"].min()) if not daily.empty else 0.0,
        profit_factor=float(wins / losses) if losses > 0 else float("inf") if wins > 0 else 0.0,
        best_month_atr=float(monthly["pnl_atr"].max()) if not monthly.empty else 0.0,
        worst_month_atr=float(monthly["pnl_atr"].min()) if not monthly.empty else 0.0,
        positive_months=int((monthly["pnl_atr"] > 0).sum()) if not monthly.empty else 0,
        acted_months=int(len(monthly)),
        up_trades=int(len(up)),
        down_trades=int(len(down)),
        up_hit_rate=float(up["hit"].mean()) if not up.empty else 0.0,
        down_hit_rate=float(down["hit"].mean()) if not down.empty else 0.0,
    )


def render_markdown(report: ReportSummary, monthly: pd.DataFrame, threshold_sweep: pd.DataFrame) -> str:
    lines = [
        "# NIFTY Underlying MP OOS Report",
        "",
        f"- Artifact: `{report.artifact_dir}`",
        f"- Threshold: `{report.threshold:.2f}`",
        f"- Acted trades: `{report.n_acted}`",
        f"- Hit rate: `{report.hit_rate:.2%}`",
        f"- Total PnL: `{report.total_pnl_atr:.3f} ATR`",
        f"- Average PnL: `{report.average_pnl_atr:.3f} ATR/trade`",
        f"- Max drawdown: `{report.max_drawdown_atr:.3f} ATR`",
        f"- Profit factor: `{report.profit_factor:.2f}`",
        f"- Positive months: `{report.positive_months}/{report.acted_months}`",
        "",
        "## Direction Breakdown",
        "",
        f"- UP trades: `{report.up_trades}`, hit rate `{report.up_hit_rate:.2%}`",
        f"- DOWN trades: `{report.down_trades}`, hit rate `{report.down_hit_rate:.2%}`",
        "",
        "## Monthly PnL",
        "",
        markdown_table(monthly) if not monthly.empty else "_No acted trades._",
        "",
        "## Threshold Sweep",
        "",
        markdown_table(threshold_sweep),
        "",
    ]
    return "\n".join(lines)


def markdown_table(df: pd.DataFrame) -> str:
    if df.empty:
        return ""
    headers = list(df.columns)
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for _, row in df.iterrows():
        vals = []
        for col in headers:
            value = row[col]
            if isinstance(value, float):
                vals.append(f"{value:.4f}")
            else:
                vals.append(str(value))
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
