"""Train/test NIFTY MP regressors across explicit date periods with ATR-native labels.

The target is the 60-minute forward NIFTY futures return measured in ATR units:

    forward_return_atr = (horizon_close - entry_price) / prior_session_atr_ref

The model predicts the continuous ATR move. Directional action thresholds are reported only as a
secondary diagnostic; the primary objective is tracking/predicting move size in ATR terms.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from train_nifty_underlying_mp import build_dataset, encode_features, load_bars


@dataclass
class PeriodExperiment:
    name: str
    train_start: str
    train_end: str
    test_start: str
    test_end: str


@dataclass
class PeriodResult:
    name: str
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    train_rows: int
    test_rows: int
    action_threshold_atr: float
    n_acted: int
    coverage: float
    hit_rate: float
    total_pnl_atr: float
    average_pnl_atr: float
    max_drawdown_atr: float
    long_trades: int
    short_trades: int
    long_hit_rate: float
    short_hit_rate: float
    buy_hold_return_atr: float
    buy_hold_return_pct: float
    pred_mean_atr: float
    actual_mean_atr: float
    mae_atr: float
    rmse_atr: float
    corr: float
    r2: float
    sign_accuracy: float
    predicted_vol_atr: float
    actual_vol_atr: float


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--futures-csv", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/nifty_atr_period_sweep"))
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2026-05-29")
    parser.add_argument("--grid-minutes", type=int, default=60)
    parser.add_argument("--horizon-minutes", type=int, default=60)
    parser.add_argument("--barrier-atr", type=float, default=0.35)
    parser.add_argument("--action-threshold-atr", type=float, default=0.10)
    parser.add_argument("--cost-atr", type=float, default=0.05)
    parser.add_argument("--num-boost-round", type=int, default=250)
    parser.add_argument(
        "--experiment",
        action="append",
        default=[],
        help="NAME,TRAIN_START,TRAIN_END,TEST_START,TEST_END. Repeatable.",
    )
    args = parser.parse_args()

    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)

    bars = load_bars(args.futures_csv, args.start, args.end)
    features, labels = build_dataset(
        bars,
        grid_minutes=args.grid_minutes,
        horizon_minutes=args.horizon_minutes,
        barrier_atr=args.barrier_atr,
        tick_size=5.0,
    )
    common = features.index.intersection(labels.index)
    features = features.loc[common].sort_values("decision_time")
    labels = labels.loc[common]
    labels.to_parquet(out / "atr_labels.parquet")
    features.to_parquet(out / "features.parquet")

    experiments = parse_experiments(args.experiment) or default_experiments()
    results: list[PeriodResult] = []
    all_trades = []
    all_predictions = []
    for experiment in experiments:
        result, trades, predictions, model = run_experiment(
            experiment,
            features,
            labels,
            bars,
            action_threshold_atr=args.action_threshold_atr,
            cost_atr=args.cost_atr,
            num_boost_round=args.num_boost_round,
        )
        results.append(result)
        if not trades.empty:
            all_trades.append(trades)
        all_predictions.append(predictions)
        joblib.dump(model, out / f"model_{safe_name(experiment.name)}.joblib")

    summary = pd.DataFrame([asdict(r) for r in results])
    summary.to_csv(out / "period_summary.csv", index=False)
    if all_trades:
        pd.concat(all_trades, ignore_index=True).to_csv(out / "period_trades.csv", index=False)
    if all_predictions:
        pd.concat(all_predictions, ignore_index=True).to_csv(out / "period_predictions.csv", index=False)
    (out / "summary.json").write_text(json.dumps([asdict(r) for r in results], indent=2))
    print(summary.to_string(index=False))


def parse_experiments(raw: list[str]) -> list[PeriodExperiment]:
    experiments = []
    for item in raw:
        parts = [p.strip() for p in item.split(",")]
        if len(parts) != 5:
            raise SystemExit("--experiment must be NAME,TRAIN_START,TRAIN_END,TEST_START,TEST_END")
        experiments.append(PeriodExperiment(*parts))
    return experiments


def default_experiments() -> list[PeriodExperiment]:
    return [
        PeriodExperiment("train_2024_test_2025h1", "2024-01-08", "2024-12-31", "2025-01-01", "2025-06-30"),
        PeriodExperiment("train_2024h2_2025h1_test_2025h2", "2024-07-01", "2025-06-30", "2025-07-01", "2025-12-31"),
        PeriodExperiment("train_2025_test_2026", "2025-01-01", "2025-12-31", "2026-01-01", "2026-05-29"),
        PeriodExperiment("train_2024_2025_test_2026", "2024-01-08", "2025-12-31", "2026-01-01", "2026-05-29"),
    ]


def run_experiment(
    experiment: PeriodExperiment,
    features: pd.DataFrame,
    labels: pd.DataFrame,
    bars: pd.DataFrame,
    *,
    action_threshold_atr: float,
    cost_atr: float,
    num_boost_round: int,
) -> tuple[PeriodResult, pd.DataFrame, pd.DataFrame, dict]:
    decision_times = pd.to_datetime(features["decision_time"])
    train_mask = (decision_times >= pd.Timestamp(experiment.train_start, tz="Asia/Kolkata")) & (
        decision_times <= pd.Timestamp(experiment.train_end, tz="Asia/Kolkata") + pd.Timedelta(hours=23, minutes=59)
    )
    test_mask = (decision_times >= pd.Timestamp(experiment.test_start, tz="Asia/Kolkata")) & (
        decision_times <= pd.Timestamp(experiment.test_end, tz="Asia/Kolkata") + pd.Timedelta(hours=23, minutes=59)
    )
    X_all = encode_features(features)
    train_idx = features.index[train_mask]
    test_idx = features.index[test_mask]
    if len(train_idx) < 200 or len(test_idx) < 20:
        raise SystemExit(f"Not enough rows for {experiment.name}: train={len(train_idx)} test={len(test_idx)}")

    model = train_regressor(
        X_all.loc[train_idx],
        labels.loc[train_idx, "forward_return_atr"].astype(float),
        num_boost_round=num_boost_round,
    )
    pred = pd.Series(model.predict(X_all.loc[test_idx]), index=test_idx, name="pred_forward_return_atr")
    pred_frame = pd.DataFrame(
        {
            "row_id": test_idx,
            "decision_time": labels.loc[test_idx, "decision_time"].to_numpy(),
            "pred_forward_return_atr": pred.to_numpy(),
            "actual_forward_return_atr": labels.loc[test_idx, "forward_return_atr"].astype(float).to_numpy(),
            "actual_max_up_atr": labels.loc[test_idx, "max_up_atr"].astype(float).to_numpy(),
            "actual_max_down_atr": labels.loc[test_idx, "max_down_atr"].astype(float).to_numpy(),
        }
    )
    pred_frame["experiment"] = experiment.name
    trades = score_period(labels.loc[test_idx], pred, action_threshold_atr=action_threshold_atr, cost_atr=cost_atr)
    buy_hold = buy_hold_atr(bars, labels.loc[test_idx], experiment)
    daily = daily_drawdown(trades)
    longs = trades[trades["action"] == "long"] if not trades.empty else trades
    shorts = trades[trades["action"] == "short"] if not trades.empty else trades
    regression = regression_metrics(labels.loc[test_idx, "forward_return_atr"].astype(float), pred)
    result = PeriodResult(
        name=experiment.name,
        train_start=experiment.train_start,
        train_end=experiment.train_end,
        test_start=experiment.test_start,
        test_end=experiment.test_end,
        train_rows=int(len(train_idx)),
        test_rows=int(len(test_idx)),
        action_threshold_atr=float(action_threshold_atr),
        n_acted=int(len(trades)),
        coverage=float(len(trades) / len(test_idx)),
        hit_rate=float(trades["hit"].mean()) if not trades.empty else 0.0,
        total_pnl_atr=float(trades["pnl_atr"].sum()) if not trades.empty else 0.0,
        average_pnl_atr=float(trades["pnl_atr"].mean()) if not trades.empty else 0.0,
        max_drawdown_atr=float(daily["drawdown_atr"].min()) if not daily.empty else 0.0,
        long_trades=int(len(longs)),
        short_trades=int(len(shorts)),
        long_hit_rate=float(longs["hit"].mean()) if not longs.empty else 0.0,
        short_hit_rate=float(shorts["hit"].mean()) if not shorts.empty else 0.0,
        buy_hold_return_atr=float(buy_hold["return_atr"]),
        buy_hold_return_pct=float(buy_hold["return_pct"]),
        pred_mean_atr=float(regression["pred_mean_atr"]),
        actual_mean_atr=float(regression["actual_mean_atr"]),
        mae_atr=float(regression["mae_atr"]),
        rmse_atr=float(regression["rmse_atr"]),
        corr=float(regression["corr"]),
        r2=float(regression["r2"]),
        sign_accuracy=float(regression["sign_accuracy"]),
        predicted_vol_atr=float(regression["predicted_vol_atr"]),
        actual_vol_atr=float(regression["actual_vol_atr"]),
    )
    if not trades.empty:
        trades["experiment"] = experiment.name
    return result, trades, pred_frame, {"model": model, "feature_columns": X_all.columns.tolist(), "experiment": asdict(experiment)}


def train_regressor(X: pd.DataFrame, y: pd.Series, *, num_boost_round: int):
    import lightgbm as lgb

    params = {
        "objective": "regression",
        "metric": "l2",
        "learning_rate": 0.03,
        "num_leaves": 31,
        "min_data_in_leaf": 20,
        "feature_fraction": 0.9,
        "bagging_fraction": 0.9,
        "bagging_freq": 3,
        "seed": 42,
        "verbose": -1,
    }
    return lgb.train(params, lgb.Dataset(X, label=y), num_boost_round=num_boost_round, callbacks=[lgb.log_evaluation(period=0)])


def score_period(
    labels: pd.DataFrame,
    pred: pd.Series,
    *,
    action_threshold_atr: float,
    cost_atr: float,
) -> pd.DataFrame:
    rows = []
    for row_id, forecast in pred.items():
        action = None
        if forecast >= action_threshold_atr:
            action = "long"
            pnl = float(labels.loc[row_id, "forward_return_atr"]) - cost_atr
            hit = pnl > 0
        elif forecast <= -action_threshold_atr:
            action = "short"
            pnl = -float(labels.loc[row_id, "forward_return_atr"]) - cost_atr
            hit = pnl > 0
        if action is None:
            continue
        rows.append(
            {
                "row_id": row_id,
                "decision_time": labels.loc[row_id, "decision_time"],
                "action": action,
                "pred_forward_return_atr": float(forecast),
                "forward_return_atr": float(labels.loc[row_id, "forward_return_atr"]),
                "max_up_atr": float(labels.loc[row_id, "max_up_atr"]),
                "max_down_atr": float(labels.loc[row_id, "max_down_atr"]),
                "pnl_atr": float(pnl),
                "hit": bool(hit),
            }
        )
    return pd.DataFrame(rows)


def regression_metrics(actual: pd.Series, pred: pd.Series) -> dict:
    common = actual.index.intersection(pred.index)
    y = actual.loc[common].astype(float)
    p = pred.loc[common].astype(float)
    err = p - y
    ss_res = float((err**2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    corr = float(y.corr(p)) if len(y) > 1 and y.std() > 0 and p.std() > 0 else 0.0
    return {
        "pred_mean_atr": float(p.mean()),
        "actual_mean_atr": float(y.mean()),
        "mae_atr": float(err.abs().mean()),
        "rmse_atr": float(np.sqrt((err**2).mean())),
        "corr": corr,
        "r2": float(1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0,
        "sign_accuracy": float((np.sign(p) == np.sign(y)).mean()),
        "predicted_vol_atr": float(p.std(ddof=0)),
        "actual_vol_atr": float(y.std(ddof=0)),
    }


def daily_drawdown(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(columns=["date", "pnl_atr", "cum_pnl_atr", "drawdown_atr"])
    df = trades.copy()
    df["date"] = pd.to_datetime(df["decision_time"]).dt.date
    daily = df.groupby("date").agg(pnl_atr=("pnl_atr", "sum")).reset_index()
    daily["cum_pnl_atr"] = daily["pnl_atr"].cumsum()
    daily["drawdown_atr"] = daily["cum_pnl_atr"] - daily["cum_pnl_atr"].cummax()
    return daily


def buy_hold_atr(bars: pd.DataFrame, labels: pd.DataFrame, experiment: PeriodExperiment) -> dict:
    start = pd.Timestamp(experiment.test_start, tz="Asia/Kolkata")
    end = pd.Timestamp(experiment.test_end, tz="Asia/Kolkata") + pd.Timedelta(hours=23, minutes=59)
    window = bars[(bars.index >= start) & (bars.index <= end)]
    if window.empty:
        return {"return_atr": 0.0, "return_pct": 0.0}
    entry = float(window.iloc[0]["open"])
    exit_ = float(window.iloc[-1]["close"])
    atr = labels["forward_return_atr"].replace([np.inf, -np.inf], np.nan).dropna()
    # Convert buy-hold points to ATR using the median point value implied by labels.
    point_move = (labels["horizon_close"].astype(float) - labels["entry_price"].astype(float)).abs()
    atr_points = point_move / labels["forward_return_atr"].abs().replace(0, np.nan)
    atr_ref = float(atr_points.replace([np.inf, -np.inf], np.nan).dropna().median())
    return {"return_atr": (exit_ - entry) / atr_ref, "return_pct": 100.0 * (exit_ - entry) / entry}


def safe_name(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name)


if __name__ == "__main__":
    main()
