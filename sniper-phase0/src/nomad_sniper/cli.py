"""Click-based CLI. Each command corresponds to one Phase 0 pipeline stage.

Usage:
    sniper validate-trades --csv data/raw/zerodha_trades_fy25_fy26.csv
    sniper build-features
    sniper label-trades
    sniper train-baseline
    sniper evaluate-phase0
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

import click
import pandas as pd
import yaml
from rich.console import Console
from rich.table import Table

from nomad_sniper.data.bars import UNDERLYINGS, load_minute_bars
from nomad_sniper.data.round_trips import pair_round_trips
from nomad_sniper.data.trades import load_zerodha_trades
from nomad_sniper.evaluation.phase0 import run_directional_phase0_verdict, run_phase0_verdict
from nomad_sniper.evaluation.splits import sample_uniqueness_weights, walk_forward
from nomad_sniper.features.pipeline import build_features_for_grid, build_features_for_trades
from nomad_sniper.labels.directional import build_directional_labels_for_grid
from nomad_sniper.labels.actual_trades import label_actual_trades
from nomad_sniper.live.signal_engine import build_alpha_signal
from nomad_sniper.models.directional import train_directional_model
from nomad_sniper.utils.logging import get_logger
from nomad_sniper.utils.settings import settings
from nomad_sniper.utils.timeutil import decision_grid

console = Console()
log = get_logger()


@click.group()
def cli():
    """Nomad Curie Sniper — Phase 0 CLI."""
    pass


@cli.command("validate-trades")
@click.option("--csv", type=click.Path(exists=True, dir_okay=False),
              default=None, help="Zerodha trade CSV. Defaults to data/raw/zerodha_trades_fy25_fy26.csv")
def validate_trades_cmd(csv):
    """Load the Zerodha trade log, pair round trips, and print summary stats."""
    csv = csv or (settings.raw_dir / "zerodha_trades_fy25_fy26.csv")
    trades = load_zerodha_trades(csv)
    rts = pair_round_trips(trades)

    table = Table(title="Trade log summary")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Raw legs loaded", str(len(trades)))
    table.add_row("Round trips paired", str(len(rts)))
    if rts:
        longs = sum(1 for r in rts if r.direction == "long")
        shorts = len(rts) - longs
        gross = sum(r.gross_pnl for r in rts)
        wins = sum(1 for r in rts if r.gross_pnl > 0)
        table.add_row("Long round trips", str(longs))
        table.add_row("Short round trips", str(shorts))
        table.add_row("Gross P&L (₹)", f"{gross:,.0f}")
        table.add_row("Gross win rate", f"{wins / len(rts):.1%}")
        first = min(r.entry_at for r in rts)
        last = max(r.exit_at for r in rts)
        table.add_row("Date range", f"{first.date()} → {last.date()}")
    console.print(table)


@cli.command("build-features")
@click.option("--csv", type=click.Path(exists=True, dir_okay=False), default=None)
@click.option("--output", type=click.Path(), default=None,
              help="Output parquet path. Defaults to data/interim/features.parquet")
def build_features_cmd(csv, output):
    """Build features for every paired round trip."""
    csv = csv or (settings.raw_dir / "zerodha_trades_fy25_fy26.csv")
    output = Path(output) if output else settings.interim_dir / "features.parquet"

    trades = load_zerodha_trades(csv)
    rts = pair_round_trips(trades)

    # Load bars for each underlying once
    bars_by_underlying = {}
    for u in UNDERLYINGS:
        try:
            bars_by_underlying[u] = load_minute_bars(u)
        except FileNotFoundError as e:
            log.warning(f"Skipping {u}: {e}")

    if not bars_by_underlying:
        raise click.ClickException(
            "No underlying bar files found in data/raw/. Drop in "
            "upstox_<underlying>_fut_<YYYYMMDD>.parquet files first."
        )

    entries = []
    for rt in rts:
        underlying = _infer_underlying_from_symbol(rt.symbol)
        if underlying is None:
            continue
        entries.append((rt.entry_trade_id, rt.entry_at, underlying))

    log.info(f"Building features for {len(entries)} trade entries")
    features = build_features_for_trades(entries, bars_by_underlying)

    output.parent.mkdir(parents=True, exist_ok=True)
    features.to_parquet(output)
    console.print(f"[green]Wrote {len(features)} feature rows → {output}[/green]")


@cli.command("build-grid-features")
@click.option("--output", type=click.Path(), default=None)
@click.option("--start", "start_date", type=str, default=None, help="YYYY-MM-DD inclusive")
@click.option("--end", "end_date", type=str, default=None, help="YYYY-MM-DD inclusive")
@click.option("--grid-minutes", type=int, default=None)
@click.option("--underlying", "underlyings", multiple=True, help="Underlying to include, repeatable. Example: --underlying nifty")
@click.option("--futures-dir", type=click.Path(exists=True, file_okay=False), default=None,
              help="Backend futures cache root containing underlying=NIFTY/1minute.csv.gz")
@click.option("--include-underlying/--drop-underlying", default=False)
def build_grid_features_cmd(output, start_date, end_date, grid_minutes, underlyings, futures_dir, include_underlying):
    """Build normalized features for every decision-grid point."""
    label_cfg = _load_yaml(Path("configs/label.yaml"))
    grid_minutes = grid_minutes or int(label_cfg.get("grid_minutes", 5))
    output = Path(output) if output else settings.interim_dir / "grid_features.parquet"
    bars_by_underlying = _load_available_bars(underlyings=underlyings, futures_dir=Path(futures_dir) if futures_dir else None)
    points = _grid_points_from_bars(bars_by_underlying, start_date, end_date, grid_minutes)
    features = build_features_for_grid(
        points,
        bars_by_underlying,
        include_underlying=include_underlying,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    features.to_parquet(output)
    console.print(f"[green]Wrote {len(features)} grid feature rows -> {output}[/green]")


@cli.command("build-labels")
@click.option("--output", type=click.Path(), default=None)
@click.option("--start", "start_date", type=str, default=None)
@click.option("--end", "end_date", type=str, default=None)
@click.option("--grid-minutes", type=int, default=None)
@click.option("--underlying", "underlyings", multiple=True, help="Underlying to include, repeatable. Example: --underlying nifty")
@click.option("--futures-dir", type=click.Path(exists=True, file_okay=False), default=None,
              help="Backend futures cache root containing underlying=NIFTY/1minute.csv.gz")
def build_labels_cmd(output, start_date, end_date, grid_minutes, underlyings, futures_dir):
    """Build directional labels for every decision-grid point."""
    cfg = _load_yaml(Path("configs/label.yaml"))
    grid_minutes = grid_minutes or int(cfg.get("grid_minutes", 5))
    output = Path(output) if output else settings.processed_dir / "directional_labels.parquet"
    bars_by_underlying = _load_available_bars(underlyings=underlyings, futures_dir=Path(futures_dir) if futures_dir else None)
    points = _grid_points_from_bars(bars_by_underlying, start_date, end_date, grid_minutes)
    labels = build_directional_labels_for_grid(
        points,
        bars_by_underlying,
        horizon_minutes=int(cfg.get("label_horizon_minutes", 60)),
        barrier_m=float(cfg.get("barrier_m", 1.0)),
        gate_mode=str(cfg.get("gate_mode", "atr_proxy")),
        m_breakeven=float(cfg.get("m_breakeven", 0.75)),
    )
    if not labels.empty:
        labels["sample_weight"] = sample_uniqueness_weights(labels)
    output.parent.mkdir(parents=True, exist_ok=True)
    labels.to_parquet(output)
    console.print(f"[green]Wrote {len(labels)} directional label rows -> {output}[/green]")


@cli.command("label-trades")
@click.option("--csv", type=click.Path(exists=True, dir_okay=False), default=None)
@click.option("--output", type=click.Path(), default=None)
def label_trades_cmd(csv, output):
    """Label round trips with net P&L using the Zerodha F&O cost model."""
    csv = csv or (settings.raw_dir / "zerodha_trades_fy25_fy26.csv")
    output = Path(output) if output else settings.processed_dir / "labels.parquet"

    trades = load_zerodha_trades(csv)
    rts = pair_round_trips(trades)
    labels = label_actual_trades(rts)

    output.parent.mkdir(parents=True, exist_ok=True)
    labels.to_parquet(output)
    console.print(f"[green]Wrote {len(labels)} labels → {output}[/green]")


@cli.command("train-baseline")
@click.option("--features", type=click.Path(exists=True), default=None)
@click.option("--labels", type=click.Path(exists=True), default=None)
@click.option("--output", type=click.Path(), default=None)
def train_baseline_cmd(features, labels, output):
    """Train walk-forward LightGBM and save out-of-sample predictions."""
    features_path = Path(features) if features else settings.interim_dir / "features.parquet"
    labels_path = Path(labels) if labels else settings.processed_dir / "labels.parquet"
    output = Path(output) if output else settings.processed_dir / "oos_predictions.parquet"

    X = pd.read_parquet(features_path)
    y_df = pd.read_parquet(labels_path)
    common = X.index.intersection(y_df.index)
    X = X.loc[common]
    y = y_df.loc[common, "is_winner"]
    decision_times = pd.to_datetime(X["decision_time"])

    cat_cols = [c for c in ("location_vs_prev_value", "open_location",
                            "time_of_day_bucket", "underlying") if c in X.columns]

    oos_rows = []
    from nomad_sniper.models.lightgbm_skip import train_skip_classifier

    for split in walk_forward(decision_times):
        train_mask = split.train_mask(decision_times)
        test_mask = split.test_mask(decision_times)
        if not train_mask.any() or not test_mask.any():
            continue
        clf = train_skip_classifier(
            X.loc[train_mask], y.loc[train_mask],
            categorical_features=cat_cols,
        )
        proba = clf.predict_proba_take(X.loc[test_mask])
        for tid, p in zip(X.loc[test_mask].index, proba):
            oos_rows.append({"trade_id": tid, "p_take": float(p),
                             "split_train_end": split.train_end, "split_test_end": split.test_end})

    if not oos_rows:
        raise click.ClickException("No walk-forward splits produced any test rows.")

    oos = pd.DataFrame(oos_rows).set_index("trade_id")
    output.parent.mkdir(parents=True, exist_ok=True)
    oos.to_parquet(output)
    console.print(f"[green]Wrote OOS predictions ({len(oos)} rows) → {output}[/green]")


@cli.command("train-directional")
@click.option("--features", type=click.Path(exists=True), default=None)
@click.option("--labels", type=click.Path(exists=True), default=None)
@click.option("--output", type=click.Path(), default=None)
@click.option("--model-output", type=click.Path(), default=None,
              help="Optional final model path trained on all overlapping rows.")
def train_directional_cmd(features, labels, output, model_output):
    """Walk-forward train the multi-head directional model and save OOS predictions."""
    features_path = Path(features) if features else settings.interim_dir / "grid_features.parquet"
    labels_path = Path(labels) if labels else settings.processed_dir / "directional_labels.parquet"
    output = Path(output) if output else settings.processed_dir / "directional_oos_predictions.parquet"
    X = pd.read_parquet(features_path)
    y = pd.read_parquet(labels_path)
    common = X.index.intersection(y.index)
    X = X.loc[common]
    y = y.loc[common]
    if X.empty:
        raise click.ClickException("No overlapping feature/label rows.")
    cfg = _load_yaml(Path("configs/baseline.yaml"))
    wf = cfg.get("walk_forward", {})
    cat_cols = [c for c in cfg.get("categorical_features", []) if c in X.columns]
    decision_times = pd.to_datetime(X["decision_time"])
    oos = []
    for split in walk_forward(
        decision_times,
        train_months=int(wf.get("train_months", 6)),
        test_months=int(wf.get("test_months", 1)),
        purge_days=int(wf.get("purge_days", 0)),
        embargo_minutes=int(wf.get("embargo_minutes", 60)),
        min_train_size=int(wf.get("min_train_size", 50)),
    ):
        tr = split.train_mask(decision_times, label_end_times=y["label_end_time"])
        te = split.test_mask(decision_times)
        if tr.sum() < int(wf.get("min_train_size", 50)) or not te.any():
            continue
        model = train_directional_model(
            X.loc[tr],
            y.loc[tr],
            categorical_features=cat_cols,
            sample_weight=y.loc[tr, "sample_weight"],
            params=cfg.get("params", {}),
            num_boost_round=int(cfg.get("training", {}).get("num_boost_round", 300)),
        )
        pred = model.predict_frame(X.loc[te])
        pred["split_train_end"] = split.train_end
        pred["split_test_end"] = split.test_end
        oos.append(pred)
    if not oos:
        raise click.ClickException("No walk-forward splits produced predictions.")
    out = pd.concat(oos).sort_index()
    output.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(output)
    console.print(f"[green]Wrote OOS directional predictions ({len(out)} rows) -> {output}[/green]")

    if model_output:
        final_model = train_directional_model(
            X,
            y,
            categorical_features=cat_cols,
            sample_weight=y["sample_weight"] if "sample_weight" in y else None,
            params=cfg.get("params", {}),
            num_boost_round=int(cfg.get("training", {}).get("num_boost_round", 300)),
        )
        final_model.save(Path(model_output))
        console.print(f"[green]Wrote final directional model -> {model_output}[/green]")


@cli.command("evaluate-phase0")
@click.option("--labels", type=click.Path(exists=True), default=None)
@click.option("--predictions", type=click.Path(exists=True), default=None)
@click.option("--threshold", type=float, default=0.5,
              help="Take/skip threshold on p_take. Below this → skip.")
@click.option("--leakage-tests-passed/--leakage-tests-not-passed", default=False,
              help="Did `pytest tests/test_no_leakage.py` pass? Must be explicit.")
def evaluate_phase0_cmd(labels, predictions, threshold, leakage_tests_passed):
    """Compute the Phase 0 verdict and write artifacts/phase0_verdict.json."""
    labels_path = Path(labels) if labels else settings.processed_dir / "labels.parquet"
    predictions_path = (
        Path(predictions) if predictions else settings.processed_dir / "oos_predictions.parquet"
    )
    y = pd.read_parquet(labels_path)
    p = pd.read_parquet(predictions_path)
    skip = (p["p_take"] < threshold).astype(int)

    verdict = run_phase0_verdict(
        labels=y,
        skip_decisions=skip,
        leakage_tests_passed=leakage_tests_passed,
    )

    color = "green" if verdict.verdict == "go" else "red"
    console.print(f"[bold {color}]Phase 0 verdict: {verdict.verdict.upper()}[/bold {color}]")
    if verdict.reasons:
        for r in verdict.reasons:
            console.print(f"  [yellow]• {r}[/yellow]")


@cli.command("evaluate")
@click.option("--labels", type=click.Path(exists=True), default=None)
@click.option("--predictions", type=click.Path(exists=True), default=None)
@click.option("--leakage-tests-passed/--leakage-tests-not-passed", default=False)
@click.option("--instrument-tests-passed/--instrument-tests-not-passed", default=False)
def evaluate_directional_cmd(labels, predictions, leakage_tests_passed, instrument_tests_passed):
    """Compute the directional Phase-0 verdict."""
    labels_path = Path(labels) if labels else settings.processed_dir / "directional_labels.parquet"
    predictions_path = (
        Path(predictions) if predictions else settings.processed_dir / "directional_oos_predictions.parquet"
    )
    verdict = run_directional_phase0_verdict(
        pd.read_parquet(labels_path),
        pd.read_parquet(predictions_path),
        leakage_tests_passed=leakage_tests_passed,
        instrument_independence_tests_passed=instrument_tests_passed,
    )
    color = "green" if verdict.verdict == "go" else "red"
    console.print(f"[bold {color}]Directional Phase 0 verdict: {verdict.verdict.upper()}[/bold {color}]")
    for reason in verdict.reasons:
        console.print(f"  [yellow]• {reason}[/yellow]")


@cli.command("build-signal")
@click.option("--features", type=click.Path(exists=True), required=True)
@click.option("--predictions", type=click.Path(exists=True), required=True)
@click.option("--row-id", type=str, default=None, help="Feature/prediction row id. Defaults to last row.")
@click.option("--output", type=click.Path(), default=None)
def build_signal_cmd(features, predictions, row_id, output):
    """Compose the final alpha-machine signal for one prediction row."""
    X = pd.read_parquet(features)
    P = pd.read_parquet(predictions)
    common = X.index.intersection(P.index)
    if len(common) == 0:
        raise click.ClickException("No overlapping feature/prediction rows.")
    selected = row_id or common[-1]
    if selected not in common:
        raise click.ClickException(f"row-id {selected!r} not present in both files.")
    signal = build_alpha_signal(P.loc[selected].to_dict(), X.loc[selected].to_dict())
    payload = signal.to_dict()
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, default=str))
        console.print(f"[green]Wrote alpha signal -> {path}[/green]")
    else:
        console.print_json(json.dumps(payload, default=str))


@cli.command("validate-overlay")
@click.option("--features", type=click.Path(exists=True), default=None)
@click.option("--labels", type=click.Path(exists=True), default=None)
@click.option("--predictions", type=click.Path(exists=True), default=None)
def validate_overlay_cmd(features, labels, predictions):
    """Report realized-trade overlay readiness. Training remains grid-only."""
    for path in (features, labels, predictions):
        if path:
            console.print(f"[green]Found {path}[/green]")
    console.print("Overlay command is intentionally read-only: realized trades are validation only.")


def _infer_underlying_from_symbol(symbol: str) -> str | None:
    """Map an F&O symbol like 'NIFTY25FEB22000CE' → 'nifty'."""
    s = symbol.upper()
    if s.startswith("BANKNIFTY"):
        return "banknifty"
    if s.startswith("FINNIFTY"):
        return "finnifty"
    if s.startswith("NIFTY"):
        return "nifty"
    return None


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _load_available_bars(
    *,
    underlyings: tuple[str, ...] | list[str] | None = None,
    futures_dir: Path | None = None,
) -> dict[str, pd.DataFrame]:
    bars_by_underlying = {}
    selected = [u.lower() for u in (underlyings or UNDERLYINGS)]
    for underlying in selected:
        try:
            bars_by_underlying[underlying] = load_minute_bars(underlying, futures_dir=futures_dir)
        except FileNotFoundError as exc:
            log.warning(f"Skipping {underlying}: {exc}")
    if not bars_by_underlying:
        raise click.ClickException("No underlying bar files found in data/raw/.")
    return bars_by_underlying


def _grid_points_from_bars(
    bars_by_underlying: dict[str, pd.DataFrame],
    start_date: str | None,
    end_date: str | None,
    grid_minutes: int,
) -> list[tuple[str, object]]:
    starts = [bars.index.date.min() for bars in bars_by_underlying.values()]
    ends = [bars.index.date.max() for bars in bars_by_underlying.values()]
    start = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else max(starts)
    end = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else min(ends)
    sessions = pd.bdate_range(start=start, end=end).date
    return [
        (underlying, dt)
        for underlying in bars_by_underlying
        for session in sessions
        for dt in decision_grid(session, grid_minutes=grid_minutes)
    ]


if __name__ == "__main__":
    cli()
