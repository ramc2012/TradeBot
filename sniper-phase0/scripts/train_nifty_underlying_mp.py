"""Train an underlying-only NIFTY MP model from the persisted futures minute cache.

This is intentionally independent of options data: features are built from NIFTY futures OHLCV,
and labels measure the forward outcome on NIFTY itself.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import joblib
import numpy as np
import pandas as pd


IST = "Asia/Kolkata"


@dataclass
class TrainSummary:
    rows_features: int
    rows_labels: int
    rows_oos: int
    first_decision_time: str
    last_decision_time: str
    label_distribution: dict[str, int]
    accuracy: float
    up_precision: float
    down_precision: float
    none_recall: float
    acted_ev_atr: float
    n_acted: int
    selected_threshold: float
    selected_threshold_ev_atr: float
    selected_threshold_n_acted: int
    artifact_dir: str


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--futures-csv", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/nifty_underlying_mp"))
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--grid-minutes", type=int, default=60)
    parser.add_argument("--horizon-minutes", type=int, default=60)
    parser.add_argument("--barrier-atr", type=float, default=1.0)
    parser.add_argument("--tick-size", type=float, default=5.0)
    parser.add_argument("--train-months", type=int, default=6)
    parser.add_argument("--test-months", type=int, default=1)
    parser.add_argument("--num-boost-round", type=int, default=250)
    parser.add_argument("--min-threshold-acted", type=int, default=50)
    parser.add_argument("--no-class-balance", action="store_true")
    args = parser.parse_args()

    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)

    bars = load_bars(args.futures_csv, args.start, args.end)
    features, labels = build_dataset(
        bars,
        grid_minutes=args.grid_minutes,
        horizon_minutes=args.horizon_minutes,
        barrier_atr=args.barrier_atr,
        tick_size=args.tick_size,
    )
    if features.empty or labels.empty:
        raise SystemExit("No features/labels were built.")

    common = features.index.intersection(labels.index)
    features = features.loc[common].sort_values("decision_time")
    labels = labels.loc[common]
    model, oos = walk_forward_train(
        features,
        labels,
        train_months=args.train_months,
        test_months=args.test_months,
        num_boost_round=args.num_boost_round,
        class_balance=not args.no_class_balance,
    )
    sweep = threshold_sweep(labels, oos)
    sweep.to_csv(out / "threshold_sweep.csv", index=False)
    selected = select_threshold(sweep, min_acted=args.min_threshold_acted)

    features.to_parquet(out / "features.parquet")
    labels.to_parquet(out / "labels.parquet")
    oos.to_parquet(out / "oos_predictions.parquet")
    model["selected_threshold"] = selected
    joblib.dump(model, out / "final_model.joblib")

    summary = summarize(features, labels, oos, out, selected_threshold=selected)
    (out / "summary.json").write_text(json.dumps(asdict(summary), indent=2))
    print(json.dumps(asdict(summary), indent=2))


def load_bars(path: Path, start: str, end: str | None) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.lower() for c in df.columns]
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert(IST)
    df = df.sort_values("time").set_index("time")
    cols = ["open", "high", "low", "close", "volume"]
    df[cols] = df[cols].apply(pd.to_numeric, errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df.between_time("09:15", "15:29")
    df = df[df.index.date >= pd.Timestamp(start).date()]
    if end:
        df = df[df.index.date <= pd.Timestamp(end).date()]
    return df[cols]


def build_dataset(
    bars: pd.DataFrame,
    *,
    grid_minutes: int,
    horizon_minutes: int,
    barrier_atr: float,
    tick_size: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    sessions = {d: g.copy() for d, g in bars.groupby(bars.index.date) if len(g) >= 60}
    dates = sorted(sessions)
    daily = pd.DataFrame(
        {
            "high": {d: float(g["high"].max()) for d, g in sessions.items()},
            "low": {d: float(g["low"].min()) for d, g in sessions.items()},
            "close": {d: float(g["close"].iloc[-1]) for d, g in sessions.items()},
            "volume": {d: float(g["volume"].sum()) for d, g in sessions.items()},
        }
    ).sort_index()
    daily["range"] = daily["high"] - daily["low"]
    daily["atr_ref"] = daily["range"].shift(1).rolling(14, min_periods=5).median()
    daily["volume_ref"] = daily["volume"].shift(1).rolling(20, min_periods=5).median()

    profiles = {d: market_profile(g, tick_size) for d, g in sessions.items()}
    rows: list[dict] = []
    y_rows: list[dict] = []

    for i, d in enumerate(dates):
        if i == 0 or pd.isna(daily.loc[d, "atr_ref"]):
            continue
        prev_d = dates[i - 1]
        prev = profiles[prev_d]
        cur = sessions[d]
        atr = float(daily.loc[d, "atr_ref"])
        volume_ref = float(daily.loc[d, "volume_ref"]) if pd.notna(daily.loc[d, "volume_ref"]) else np.nan
        grid = pd.date_range(
            pd.Timestamp(d).tz_localize(IST) + pd.Timedelta(hours=9, minutes=30),
            pd.Timestamp(d).tz_localize(IST) + pd.Timedelta(hours=15),
            freq=f"{grid_minutes}min",
        )
        for dt in grid:
            hist = cur[cur.index <= dt]
            if len(hist) < 10:
                continue
            dev = market_profile(hist, tick_size)
            price = float(hist["close"].iloc[-1])
            row_id = f"nifty|{dt.isoformat()}"
            feature_row = build_feature_row(row_id, dt, hist, prev, dev, atr, volume_ref)
            label_row = label_point(row_id, dt, cur, atr, horizon_minutes, barrier_atr)
            if label_row is None:
                continue
            rows.append(feature_row)
            y_rows.append(label_row)

    features = pd.DataFrame(rows).set_index("row_id").replace([np.inf, -np.inf], np.nan)
    labels = pd.DataFrame(y_rows).set_index("row_id").replace([np.inf, -np.inf], np.nan)
    return features, labels


def market_profile(df: pd.DataFrame, tick_size: float) -> dict[str, float]:
    typical = (df["high"].astype(float) + df["low"].astype(float) + df["close"].astype(float)) / 3.0
    bins = (typical / tick_size).round() * tick_size
    vol = df["volume"].astype(float).groupby(bins).sum().sort_index()
    if vol.empty or vol.sum() <= 0:
        px = float(df["close"].iloc[-1])
        return {"poc": px, "vah": px, "val": px, "high": px, "low": px, "width": tick_size}
    poc = float(vol.idxmax())
    ranked = vol.sort_values(ascending=False)
    selected = []
    running = 0.0
    target = float(vol.sum()) * 0.70
    for price_bin, value in ranked.items():
        selected.append(float(price_bin))
        running += float(value)
        if running >= target:
            break
    val = min(selected)
    vah = max(selected)
    return {
        "poc": poc,
        "vah": float(vah),
        "val": float(val),
        "high": float(df["high"].max()),
        "low": float(df["low"].min()),
        "width": max(float(vah - val), tick_size),
    }


def build_feature_row(
    row_id: str,
    dt: pd.Timestamp,
    hist: pd.DataFrame,
    prev: dict[str, float],
    dev: dict[str, float],
    atr: float,
    volume_ref: float,
) -> dict:
    price = float(hist["close"].iloc[-1])
    open_px = float(hist["open"].iloc[0])
    ib = hist.iloc[: min(len(hist), 60)]
    prev_width = max(prev["width"], 1e-9)
    location = "above" if price > prev["vah"] else "below" if price < prev["val"] else "inside"
    open_location = "above_value" if open_px > prev["vah"] else "below_value" if open_px < prev["val"] else "in_value"
    ret = hist["close"].astype(float).diff()
    return {
        "row_id": row_id,
        "decision_time": dt,
        "u_dist_prev_poc_atr": (price - prev["poc"]) / atr,
        "u_dist_prev_vah_atr": (price - prev["vah"]) / atr,
        "u_dist_prev_val_atr": (price - prev["val"]) / atr,
        "u_dist_prev_poc_pw": (price - prev["poc"]) / prev_width,
        "u_prev_value_width_atr": prev["width"] / atr,
        "u_prev_range_atr": (prev["high"] - prev["low"]) / atr,
        "u_location_vs_prev_value": location,
        "u_open_location": open_location,
        "u_gap_atr": (open_px - prev["poc"]) / atr,
        "u_dist_dev_poc_atr": (price - dev["poc"]) / atr,
        "u_dist_dev_vah_atr": (price - dev["vah"]) / atr,
        "u_dist_dev_val_atr": (price - dev["val"]) / atr,
        "u_dist_dev_poc_pw": (price - dev["poc"]) / max(dev["width"], 1e-9),
        "u_value_migration_atr": (dev["poc"] - prev["poc"]) / atr,
        "u_dist_ib_high_atr": (price - float(ib["high"].max())) / atr,
        "u_dist_ib_low_atr": (price - float(ib["low"].min())) / atr,
        "u_price_above_ib": int(price > float(ib["high"].max())),
        "u_price_below_ib": int(price < float(ib["low"].min())),
        "u_intraday_range_atr": (float(hist["high"].max()) - float(hist["low"].min())) / atr,
        "u_cum_volume_ratio": float(hist["volume"].sum()) / volume_ref if volume_ref and volume_ref > 0 else np.nan,
        "u_ret_15m_atr": _lookback_return_atr(hist, 15, atr),
        "u_ret_30m_atr": _lookback_return_atr(hist, 30, atr),
        "u_ret_60m_atr": _lookback_return_atr(hist, 60, atr),
        "u_realized_vol_30m_atr": float(ret.tail(30).std() or 0.0) / atr,
        "c_minutes_from_open": int((dt.hour * 60 + dt.minute) - (9 * 60 + 15)),
        "c_time_bucket": f"{dt.hour:02d}",
    }


def _lookback_return_atr(hist: pd.DataFrame, minutes: int, atr: float) -> float:
    if len(hist) <= minutes:
        return 0.0
    return float(hist["close"].iloc[-1] - hist["close"].iloc[-minutes - 1]) / atr


def label_point(
    row_id: str,
    dt: pd.Timestamp,
    session: pd.DataFrame,
    atr: float,
    horizon_minutes: int,
    barrier_atr: float,
) -> dict | None:
    forward = session[(session.index > dt) & (session.index <= dt + pd.Timedelta(minutes=horizon_minutes))]
    if forward.empty:
        return None
    entry = float(forward["open"].iloc[0])
    horizon_close = float(forward["close"].iloc[-1])
    up = entry + barrier_atr * atr
    down = entry - barrier_atr * atr
    direction = "none"
    exit_time = forward.index[-1]
    exit_price = float(forward["close"].iloc[-1])
    time_to_target = float(horizon_minutes)
    mae = 0.0
    mfe = 0.0
    for ts, bar in forward.iterrows():
        high = float(bar["high"])
        low = float(bar["low"])
        mfe = max(mfe, high - entry, entry - low)
        hit_up = high >= up
        hit_down = low <= down
        if hit_up and hit_down:
            direction = "none"
            exit_time = ts
            exit_price = float(bar["close"])
            time_to_target = (ts - dt).total_seconds() / 60.0
            break
        if hit_up:
            direction = "up"
            exit_time = ts
            exit_price = up
            time_to_target = (ts - dt).total_seconds() / 60.0
            mae = max(0.0, entry - low)
            break
        if hit_down:
            direction = "down"
            exit_time = ts
            exit_price = down
            time_to_target = (ts - dt).total_seconds() / 60.0
            mae = max(0.0, high - entry)
            break
        mae = max(mae, min(max(0.0, entry - low), max(0.0, high - entry)))
    return {
        "row_id": row_id,
        "decision_time": dt,
        "label_end_time": dt + pd.Timedelta(minutes=horizon_minutes),
        "entry_price": entry,
        "exit_time": exit_time,
        "exit_price": exit_price,
        "horizon_close": horizon_close,
        "direction": direction,
        "direction_class": {"none": 0, "up": 1, "down": 2}[direction],
        "is_move": int(direction != "none"),
        "forward_return_atr": float((horizon_close - entry) / atr),
        "max_up_atr": float((forward["high"].max() - entry) / atr),
        "max_down_atr": float((entry - forward["low"].min()) / atr),
        "magnitude_atr": float(mfe / atr),
        "time_to_target": float(time_to_target),
        "mae_atr": float(mae / atr),
        "sample_weight": 1.0,
    }


def walk_forward_train(
    features: pd.DataFrame,
    labels: pd.DataFrame,
    *,
    train_months: int,
    test_months: int,
    num_boost_round: int,
    class_balance: bool,
):
    import lightgbm as lgb

    X = encode_features(features)
    y = labels.loc[X.index, "direction_class"].astype(int)
    months = pd.PeriodIndex(pd.to_datetime(features.loc[X.index, "decision_time"]).dt.tz_localize(None), freq="M")
    unique_months = sorted(months.unique())
    oos = []
    for i in range(train_months, len(unique_months), test_months):
        train_set = set(unique_months[i - train_months : i])
        test_set = set(unique_months[i : i + test_months])
        train_mask = months.isin(train_set)
        test_mask = months.isin(test_set)
        if train_mask.sum() < 200 or test_mask.sum() == 0:
            continue
        booster = train_lgb(
            X.loc[train_mask],
            y.loc[train_mask],
            num_boost_round,
            class_balance=class_balance,
        )
        proba = booster.predict(X.loc[test_mask])
        pred_class = np.asarray(proba).argmax(axis=1)
        pred = pd.DataFrame(index=X.loc[test_mask].index)
        pred["pred_direction_class"] = pred_class
        pred["pred_direction"] = [{"0": "none", "1": "up", "2": "down"}[str(c)] for c in pred_class]
        pred["p_none"] = proba[:, 0]
        pred["p_up"] = proba[:, 1]
        pred["p_down"] = proba[:, 2]
        pred["split_train_end"] = str(unique_months[i - 1])
        pred["split_test_end"] = str(unique_months[min(i + test_months - 1, len(unique_months) - 1)])
        oos.append(pred)
    if not oos:
        raise SystemExit("No walk-forward predictions produced.")
    final_model = train_lgb(X, y, num_boost_round, class_balance=class_balance)
    return {"model": final_model, "feature_columns": X.columns.tolist()}, pd.concat(oos).sort_index()


def encode_features(features: pd.DataFrame) -> pd.DataFrame:
    excluded = {"decision_time"}
    X = features.drop(columns=[c for c in excluded if c in features.columns]).copy()
    X = pd.get_dummies(X, columns=[c for c in X.columns if X[c].dtype == "object"], dummy_na=True)
    return X.apply(pd.to_numeric, errors="coerce").fillna(0.0)


def train_lgb(
    X: pd.DataFrame,
    y: pd.Series,
    num_boost_round: int,
    *,
    class_balance: bool,
):
    import lightgbm as lgb

    params = {
        "objective": "multiclass",
        "num_class": 3,
        "metric": "multi_logloss",
        "learning_rate": 0.03,
        "num_leaves": 31,
        "min_data_in_leaf": 20,
        "feature_fraction": 0.9,
        "bagging_fraction": 0.9,
        "bagging_freq": 3,
        "seed": 42,
        "verbose": -1,
    }
    weight = None
    if class_balance:
        counts = y.value_counts().to_dict()
        n = len(y)
        k = max(1, len(counts))
        weight = y.map({cls: n / (k * count) for cls, count in counts.items()}).astype(float)
    return lgb.train(
        params,
        lgb.Dataset(X, label=y, weight=weight),
        num_boost_round=num_boost_round,
        callbacks=[lgb.log_evaluation(period=0)],
    )


def threshold_sweep(labels: pd.DataFrame, oos: pd.DataFrame) -> pd.DataFrame:
    common = labels.index.intersection(oos.index)
    y = labels.loc[common]
    p = oos.loc[common]
    rows = []
    for threshold in np.arange(0.34, 0.86, 0.02):
        pred = threshold_predictions(p, float(threshold))
        rows.append(score_predictions(y, pred, threshold=float(threshold)))
    return pd.DataFrame(rows)


def select_threshold(sweep: pd.DataFrame, *, min_acted: int) -> dict:
    candidates = sweep[sweep["n_acted"] >= min_acted].copy()
    if candidates.empty:
        candidates = sweep.copy()
    best = candidates.sort_values(["ev_atr", "n_acted"], ascending=[False, False]).iloc[0]
    return {
        "threshold": float(best["threshold"]),
        "ev_atr": float(best["ev_atr"]),
        "n_acted": int(best["n_acted"]),
        "coverage": float(best["coverage"]),
        "up_precision": float(best["up_precision"]),
        "down_precision": float(best["down_precision"]),
        "none_recall": float(best["none_recall"]),
    }


def threshold_predictions(oos: pd.DataFrame, threshold: float) -> pd.Series:
    pred = []
    for _, row in oos.iterrows():
        if row["p_up"] >= threshold and row["p_up"] >= row["p_down"]:
            pred.append("up")
        elif row["p_down"] >= threshold and row["p_down"] > row["p_up"]:
            pred.append("down")
        else:
            pred.append("none")
    return pd.Series(pred, index=oos.index)


def score_predictions(labels: pd.DataFrame, pred: pd.Series, *, threshold: float | None = None) -> dict:
    common = labels.index.intersection(pred.index)
    y = labels.loc[common, "direction"].astype(str)
    pred = pred.loc[common].astype(str)
    acted = pred != "none"
    pnl = []
    for idx in common[acted]:
        if pred.loc[idx] == y.loc[idx] and y.loc[idx] != "none":
            pnl.append(float(labels.loc[idx, "magnitude_atr"]) - 0.05)
        else:
            pnl.append(-float(labels.loc[idx, "mae_atr"]) - 0.05)
    out = {
        "n_acted": int(acted.sum()),
        "coverage": float(acted.mean()),
        "ev_atr": float(np.mean(pnl)) if pnl else 0.0,
        "up_precision": float(((y == "up") & (pred == "up")).sum() / max(1, (pred == "up").sum())),
        "down_precision": float(((y == "down") & (pred == "down")).sum() / max(1, (pred == "down").sum())),
        "none_recall": float(((y == "none") & (pred == "none")).sum() / max(1, (y == "none").sum())),
        "accuracy": float((y == pred).mean()) if len(y) else 0.0,
    }
    if threshold is not None:
        out["threshold"] = round(float(threshold), 2)
    return out


def summarize(
    features: pd.DataFrame,
    labels: pd.DataFrame,
    oos: pd.DataFrame,
    out: Path,
    *,
    selected_threshold: dict,
) -> TrainSummary:
    common = labels.index.intersection(oos.index)
    base_score = score_predictions(labels.loc[common], oos.loc[common, "pred_direction"])
    return TrainSummary(
        rows_features=int(len(features)),
        rows_labels=int(len(labels)),
        rows_oos=int(len(oos)),
        first_decision_time=str(features["decision_time"].min()),
        last_decision_time=str(features["decision_time"].max()),
        label_distribution={str(k): int(v) for k, v in labels["direction"].value_counts().to_dict().items()},
        accuracy=float(base_score["accuracy"]),
        up_precision=float(base_score["up_precision"]),
        down_precision=float(base_score["down_precision"]),
        none_recall=float(base_score["none_recall"]),
        acted_ev_atr=float(base_score["ev_atr"]),
        n_acted=int(base_score["n_acted"]),
        selected_threshold=float(selected_threshold["threshold"]),
        selected_threshold_ev_atr=float(selected_threshold["ev_atr"]),
        selected_threshold_n_acted=int(selected_threshold["n_acted"]),
        artifact_dir=str(out),
    )


if __name__ == "__main__":
    main()
