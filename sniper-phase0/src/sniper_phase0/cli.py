"""Phase 0 CLI.

Subcommands:
  phase0 features   — load trades, compute features, write parquet
  phase0 label      — compute triple-barrier labels with net_R
  phase0 train      — walk-forward train LightGBM baseline
  phase0 eval       — write the go/no-go report
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

from sniper_phase0.data.trade_log import load_trade_log
from sniper_phase0.evaluation.reports import build_report, write_report
from sniper_phase0.evaluation.walk_forward import run_walk_forward
from sniper_phase0.features.build import build_features
from sniper_phase0.setups.generate import candidates_as_pseudo_trades, generate_for_day
from sniper_phase0.utils.provenance import make_provenance, write_with_provenance
from sniper_phase0.utils.settings import Settings

app = typer.Typer(add_completion=False, no_args_is_help=True)
console = Console()


@app.command()
def features(config: str = "configs/base.yaml") -> None:
    """Build features.parquet from the trade log + tick/book data."""
    settings = Settings.load(config)
    trades = load_trade_log(settings.paths.trade_log)
    console.print(f"Loaded [bold]{len(trades)}[/bold] trades")

    features_df, _avail = build_features(trades, settings)
    prov = make_provenance(
        settings.model_dump(),
        input_paths=[settings.paths.trade_log],
        extra={"n_trades": len(trades), "n_features": features_df.shape[1]},
    )
    write_with_provenance(features_df, settings.paths.features_out, prov)
    console.print(f"Wrote [green]{settings.paths.features_out}[/green]")


@app.command()
def label(config: str = "configs/base.yaml") -> None:
    """Compute triple-barrier labels with net_R after costs.

    For Phase 0 v0, we use the actual exit price from the Zerodha trade log as
    the de-facto exit (the trades already happened). When forward-tick data is
    available, swap to true triple-barrier labelling by calling labels.triple_barrier.
    """
    from sniper_phase0.labels.cost_model import net_pnl

    settings = Settings.load(config)
    trades = load_trade_log(settings.paths.trade_log)

    rows = []
    for _, t in trades.iterrows():
        gross, net, _tc = net_pnl(
            entry_price=float(t["entry_price"]),
            exit_price=float(t["exit_price"]),
            qty=int(t["qty"]),
            side=t["side"],
            costs=settings.costs,
        )
        stop_distance = max(1e-6, abs(t["entry_price"]) * settings.labeling.default_stop_pct / 100.0)
        rows.append(
            {
                "trade_id": int(t["trade_id"]),
                "outcome": "actual",
                "exit_ts": t["exit_ts"],
                "exit_price": float(t["exit_price"]),
                "gross_R": gross / (stop_distance * t["qty"]),
                "net_R": net / (stop_distance * t["qty"]),
                "mae": float("nan"),
                "mfe": float("nan"),
            }
        )
    labels_df = pd.DataFrame(rows)
    prov = make_provenance(settings.model_dump(), input_paths=[settings.paths.trade_log])
    write_with_provenance(labels_df, settings.paths.labels_out, prov)
    console.print(f"Wrote [green]{settings.paths.labels_out}[/green] ({len(labels_df)} labels)")


@app.command()
def train(config: str = "configs/base.yaml") -> None:
    """Run walk-forward training and emit per-fold predictions."""
    settings = Settings.load(config)
    features_df = pd.read_parquet(settings.paths.features_out)
    labels_df = pd.read_parquet(settings.paths.labels_out)

    folds = run_walk_forward(features_df, labels_df, settings)
    if not folds:
        console.print("[red]No folds produced. Check walk_forward.start/end vs your data.[/red]")
        raise typer.Exit(code=1)

    table = Table(title="Walk-forward folds")
    table.add_column("test_start")
    table.add_column("test_end")
    table.add_column("n_trades")
    table.add_column("mean_net_R")
    for f in folds:
        table.add_row(
            str(f.test_start.date()), str(f.test_end.date()),
            str(len(f.predictions)), f"{f.predictions['net_R'].mean():.3f}",
        )
    console.print(table)

    out_dir = Path(settings.paths.reports_out)
    out_dir.mkdir(parents=True, exist_ok=True)
    all_preds = pd.concat([f.predictions.assign(test_start=f.test_start) for f in folds], ignore_index=True)
    all_preds.to_parquet(out_dir / "predictions.parquet", index=False)


@app.command()
def candidates(
    config: str = "configs/base.yaml",
    start: str = typer.Option(None, help="YYYY-MM-DD (defaults to walk_forward.start)"),
    end: str = typer.Option(None, help="YYYY-MM-DD (defaults to walk_forward.end)"),
    out: str = typer.Option(
        "data/processed/setup_candidates.parquet",
        help="Output parquet path (trade-log compatible schema).",
    ),
) -> None:
    """Generate setup-family candidates as a parallel decision_ts source.

    Output mirrors the trade-log schema, so the existing `phase0 features` /
    `phase0 label` / `phase0 train` pipeline runs unchanged — just point
    `paths.trade_log` at the generated file.
    """
    settings = Settings.load(config)
    s = pd.Timestamp(start or settings.walk_forward.start)
    e = pd.Timestamp(end or settings.walk_forward.end)

    all_candidates = []
    days = pd.date_range(s, e, freq="B")
    for instrument in settings.instruments:
        for d in days:
            cands = generate_for_day(instrument, d, settings)
            all_candidates.extend(cands)
        console.print(f"{instrument}: {len(all_candidates)} candidates so far")

    df = candidates_as_pseudo_trades(all_candidates)
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)
    console.print(f"Wrote [green]{out_path}[/green] ({len(df)} candidates)")


@app.command()
def eval(config: str = "configs/base.yaml") -> None:
    """Build the go/no-go report."""
    settings = Settings.load(config)
    features_df = pd.read_parquet(settings.paths.features_out)
    labels_df = pd.read_parquet(settings.paths.labels_out)
    folds = run_walk_forward(features_df, labels_df, settings)
    report = build_report(folds, settings, features=features_df)
    path = write_report(report, settings.paths.reports_out)
    console.print(f"Wrote [green]{path}[/green]")
    console.print(
        f"Skip-accuracy by EV (bottom decile): "
        f"[bold]{report['overall_skip_accuracy_by_ev']:.3f}[/bold] "
        f"(gate >= {settings.decision_gate.skip_accuracy_bottom_decile_min})"
    )
    console.print(
        f"  (p_win-ranked, diagnostic only: "
        f"{report['overall_skip_accuracy_by_pwin_diagnostic']:.3f})"
    )
    console.print(
        f"Training rows purged: {report['purging']['n_train_purged_total']} / "
        f"{report['purging']['n_train_total']}"
    )
    console.print(f"Phase 0 pass: [bold]{report['gate']['phase0_pass']}[/bold]")


if __name__ == "__main__":
    app()
