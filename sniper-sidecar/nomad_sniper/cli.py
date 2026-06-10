"""Click-based CLI — grid pipeline stages (contract §3/§4).

    sniper build-grid-features      # A+B+C+D+E features at every grid point × session × underlying
    sniper build-labels             # directional grid labels (triple-barrier + option gate)
    sniper train-directional        # walk-forward multi-head LightGBM, write OOS predictions
    sniper evaluate                 # directional Phase 0 verdict
    sniper validate-overlay         # realized-trade agreement overlay (contract §7)
    sniper validate-trades          # load + summarize the Zerodha trade log (overlay input)
"""

from __future__ import annotations

from pathlib import Path

import click
import pandas as pd
import yaml
from rich.console import Console
from rich.table import Table

from nomad_sniper.data.bars import discover_underlyings, load_minute_bars, load_spot_bars
from nomad_sniper.data.round_trips import pair_round_trips
from nomad_sniper.data.trades import load_zerodha_trades
from nomad_sniper.evaluation.phase0 import run_phase0_verdict
from nomad_sniper.evaluation.splits import sample_uniqueness_weights, walk_forward
from nomad_sniper.features.pipeline import build_features_for_grid
from nomad_sniper.labels.directional import build_labels_for_grid
from nomad_sniper.models.directional import train_directional_model
from nomad_sniper.utils.cache import is_cached, write_manifest
from nomad_sniper.utils.logging import get_logger
from nomad_sniper.utils.settings import settings

console = Console()
log = get_logger()

JOIN_KEYS = ["underlying_key", "decision_time"]
CATEGORICAL_FEATURES = ["u_location_vs_prev_value", "u_open_location", "u_htf_week_location", "u_htf_month_location", "u_htf_quarter_location", "u_htf_year_location", "c_time_of_day_bucket"]


def _load_yaml(path: str | Path) -> dict:
    p = Path(path)
    return yaml.safe_load(p.read_text()) if p.exists() else {}


def _load_all_bars() -> dict[str, pd.DataFrame]:
    bars_by_underlying: dict[str, pd.DataFrame] = {}
    for u in discover_underlyings():
        try:
            bars_by_underlying[u] = load_minute_bars(u)
        except FileNotFoundError as e:
            log.warning(f"Skipping {u}: {e}")
    if not bars_by_underlying:
        raise click.ClickException(
            "No underlying bar files in data/raw/. Drop upstox_<underlying>_fut_<YYYYMMDD>.parquet first."
        )
    return bars_by_underlying


def _session_dates(bars_by_underlying: dict[str, pd.DataFrame]) -> list:
    dates: set = set()
    for bars in bars_by_underlying.values():
        dates.update(bars.index.date)
    return sorted(dates)


@click.group()
def cli():
    """Nomad Curie Sniper — directional grid CLI."""


@cli.command("build-grid-features")
@click.option("--features-config", default="configs/features.yaml")
@click.option("--label-config", default="configs/label.yaml")
@click.option("--output", default=None)
@click.option("--ablation-underlying", is_flag=True, default=False,
              help="Keep `underlying` as a feature column (contract §3 ablation only).")
@click.option("--force", is_flag=True, default=False, help="Rebuild even if a valid cache exists.")
def build_grid_features_cmd(features_config, label_config, output, ablation_underlying, force):
    """Build the pooled grid feature matrix (contract §3). Cached — skips if inputs unchanged."""
    fcfg = _load_yaml(features_config)
    lcfg = _load_yaml(label_config)
    output = Path(output) if output else settings.interim_dir / "grid_features.parquet"

    # Cache fingerprint includes futures (upstox_*) AND spot (spot_*) inputs.
    raw_inputs = sorted(settings.raw_dir.glob("upstox_*.parquet")) + sorted(settings.raw_dir.glob("spot_*.parquet"))
    cache_cfg = {
        "grid_minutes": lcfg.get("grid_minutes", 5),
        "grid_start": lcfg.get("grid_start", "09:30"),
        "grid_end": lcfg.get("grid_end", "15:00"),
        "ablation_underlying": bool(ablation_underlying or fcfg.get("ablation_include_underlying", False)),
        "normalization": fcfg.get("normalization", {}),
    }
    if not force and is_cached(output, raw_inputs, cache_cfg):
        n = len(pd.read_parquet(output))
        console.print(f"[cyan]✓ cached[/cyan] {n} grid feature rows at {output} (use --force to rebuild)")
        return

    bars = _load_all_bars()
    sessions = _session_dates(bars)
    # Index spot per instrument (option family's underlying reference — options price off spot).
    spot_by_underlying = {}
    for u in bars:
        s = load_spot_bars(u)
        if s is not None:
            spot_by_underlying[u] = s
    if spot_by_underlying:
        console.print(f"[cyan]using index spot for option family on: {sorted(spot_by_underlying)}[/cyan]")
    df = build_features_for_grid(
        sessions, bars,
        grid_minutes=cache_cfg["grid_minutes"],
        grid_start=cache_cfg["grid_start"],
        grid_end=cache_cfg["grid_end"],
        include_underlying_column=cache_cfg["ablation_underlying"],
        spot_by_underlying=spot_by_underlying,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(output)
    write_manifest(output, raw_inputs, cache_cfg)
    console.print(f"[green]Wrote {len(df)} grid feature rows → {output}[/green] (cached)")


@cli.command("bless-cache")
@click.option("--features-config", default="configs/features.yaml")
@click.option("--label-config", default="configs/label.yaml")
def bless_cache_cmd(features_config, label_config):
    """Write cache manifests for already-built grid_features/grid_labels parquets WITHOUT
    rebuilding — use after a long build done by an older binary, so future runs skip it."""
    fcfg = _load_yaml(features_config); lcfg = _load_yaml(label_config)
    raw = sorted(settings.raw_dir.glob("upstox_*.parquet"))
    feat = settings.interim_dir / "grid_features.parquet"
    lab = settings.processed_dir / "grid_labels.parquet"
    if feat.exists():
        write_manifest(feat, raw, {
            "grid_minutes": lcfg.get("grid_minutes", 5), "grid_start": lcfg.get("grid_start", "09:30"),
            "grid_end": lcfg.get("grid_end", "15:00"),
            "ablation_underlying": bool(fcfg.get("ablation_include_underlying", False)),
            "normalization": fcfg.get("normalization", {})})
        console.print(f"[green]blessed[/green] {feat}")
    if lab.exists():
        write_manifest(lab, raw, {k: lcfg.get(k, d) for k, d in {
            "gate_mode": "atr_proxy", "m_breakeven": 0.6, "barrier_m": 1.0,
            "label_horizon_minutes": 60, "grid_minutes": 5, "grid_start": "09:30",
            "grid_end": "15:00", "cost_inr_per_unit": 4.0}.items()})
        console.print(f"[green]blessed[/green] {lab}")


@cli.command("build-labels")
@click.option("--label-config", default="configs/label.yaml")
@click.option("--output", default=None)
@click.option("--force", is_flag=True, default=False, help="Rebuild even if a valid cache exists.")
def build_labels_cmd(label_config, output, force):
    """Build directional grid labels (contract §4). Cached — skips if inputs unchanged."""
    lcfg = _load_yaml(label_config)
    output = Path(output) if output else settings.processed_dir / "grid_labels.parquet"

    raw_inputs = sorted(settings.raw_dir.glob("upstox_*.parquet"))
    cache_cfg = {k: lcfg.get(k, d) for k, d in {
        "gate_mode": "atr_proxy", "m_breakeven": 0.6, "barrier_m": 1.0,
        "label_horizon_minutes": 60, "grid_minutes": 5,
        "grid_start": "09:30", "grid_end": "15:00", "cost_inr_per_unit": 4.0,
    }.items()}
    if not force and is_cached(output, raw_inputs, cache_cfg):
        df = pd.read_parquet(output)
        counts = df["direction"].value_counts().to_dict() if not df.empty else {}
        console.print(f"[cyan]✓ cached[/cyan] {len(df)} labels at {output}  {counts} (use --force to rebuild)")
        return

    bars = _load_all_bars()
    sessions = _session_dates(bars)
    df = build_labels_for_grid(
        sessions, bars,
        gate_mode=cache_cfg["gate_mode"], m_breakeven=cache_cfg["m_breakeven"],
        m=cache_cfg["barrier_m"], horizon_minutes=cache_cfg["label_horizon_minutes"],
        grid_minutes=cache_cfg["grid_minutes"], grid_start=cache_cfg["grid_start"],
        grid_end=cache_cfg["grid_end"], cost_inr_per_unit=cache_cfg["cost_inr_per_unit"],
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(output)
    write_manifest(output, raw_inputs, cache_cfg)
    counts = df["direction"].value_counts().to_dict() if not df.empty else {}
    console.print(f"[green]Wrote {len(df)} labels → {output}[/green]  direction counts: {counts} (cached)")


@cli.command("train-directional")
@click.option("--features", default=None)
@click.option("--labels", default=None)
@click.option("--baseline-config", default="configs/baseline.yaml")
@click.option("--label-config", default="configs/label.yaml")
@click.option("--output", default=None)
def train_directional_cmd(features, labels, baseline_config, label_config, output):
    """Walk-forward multi-head training; write OOS predictions for the verdict."""
    bcfg = _load_yaml(baseline_config)
    lcfg = _load_yaml(label_config)
    features_path = Path(features) if features else settings.interim_dir / "grid_features.parquet"
    labels_path = Path(labels) if labels else settings.processed_dir / "grid_labels.parquet"
    output = Path(output) if output else settings.processed_dir / "oos_predictions.parquet"

    feats = pd.read_parquet(features_path)
    labs = pd.read_parquet(labels_path)
    merged = feats.merge(labs, on=JOIN_KEYS, how="inner")
    if merged.empty:
        raise click.ClickException("Feature/label join produced 0 rows (check grid configs match).")

    decision_times = pd.to_datetime(merged["decision_time"])
    horizon = lcfg.get("label_horizon_minutes", 60)
    wf = bcfg.get("walk_forward", {})
    cats = [c for c in CATEGORICAL_FEATURES if c in merged.columns]
    if "underlying" in merged.columns:
        cats.append("underlying")

    oos_rows = []
    n_folds = 0
    for split in walk_forward(
        decision_times,
        train_months=wf.get("train_months", 6),
        test_months=wf.get("test_months", 1),
        purge_days=wf.get("purge_days", 2),
        embargo_minutes=wf.get("embargo_minutes", horizon),
        min_train_size=wf.get("min_train_size", 50),
    ):
        tr = split.train_mask(decision_times)
        te = split.test_mask(decision_times)
        if tr.sum() < wf.get("min_train_size", 50) or te.sum() < 5:
            continue
        n_folds += 1
        train_df = merged[tr]
        weights = sample_uniqueness_weights(decision_times[tr], horizon_minutes=horizon)
        model = train_directional_model(
            train_df, train_df, categorical_features=cats,
            sample_weight=weights, params=bcfg.get("params"),
            num_boost_round=bcfg.get("training", {}).get("num_boost_round", 400),
        )
        test_df = merged[te]
        preds = model.predict(test_df)
        block = pd.DataFrame({
            "underlying_key": test_df["underlying_key"].values,
            "decision_time": test_df["decision_time"].values,
            "pred_direction": preds["direction"],
            "is_move_proba": preds["is_move"],
            "true_direction": test_df["direction"].values,
            "magnitude_atr": test_df["magnitude_atr"].values,
            "mae_atr": test_df["mae_atr"].values,
        })
        if "magnitude_atr" in preds:
            block["pred_magnitude_atr"] = preds["magnitude_atr"]
        oos_rows.append(block)

    if not oos_rows:
        raise click.ClickException("No walk-forward folds produced test rows.")
    oos = pd.concat(oos_rows, ignore_index=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    oos.to_parquet(output)
    console.print(f"[green]{n_folds} folds → {len(oos)} OOS predictions → {output}[/green]")


@cli.command("evaluate")
@click.option("--predictions", default=None)
@click.option("--label-config", default="configs/label.yaml")
@click.option("--atr-inr", type=float, default=100.0, help="ATR in rupees, for INR reporting only.")
@click.option("--leakage-tests-passed/--leakage-tests-not-passed", default=False)
@click.option("--instrument-independence-passed/--instrument-independence-not-passed", default=False)
def evaluate_cmd(predictions, label_config, atr_inr, leakage_tests_passed, instrument_independence_passed):
    """Compute the directional Phase 0 verdict → artifacts/phase0_verdict.json."""
    predictions_path = Path(predictions) if predictions else settings.processed_dir / "oos_predictions.parquet"
    preds = pd.read_parquet(predictions_path)
    verdict = run_phase0_verdict(
        preds, atr_inr=atr_inr,
        leakage_tests_passed=leakage_tests_passed,
        instrument_independence_passed=instrument_independence_passed,
    )
    color = "green" if verdict.verdict == "go" else "red"
    console.print(f"[bold {color}]Phase 0 verdict: {verdict.verdict.upper()}[/bold {color}]")
    console.print(
        f"  none-recall={verdict.none_recall:.2f}  up/down-prec={verdict.updown_precision:.2f}  "
        f"acted-EV@2x={verdict.acted_ev_atr_at_2x:+.4f} ATR"
    )
    for r in verdict.reasons:
        console.print(f"  [yellow]• {r}[/yellow]")


@cli.command("validate-overlay")
@click.option("--csv", type=click.Path(dir_okay=False), default=None)
@click.option("--predictions", default=None)
def validate_overlay_cmd(csv, predictions):
    """Realized-trade agreement overlay (contract §7): does the model agree with winners and
    disagree with losers? Reported as agreement rates — never mixed into training."""
    from nomad_sniper.labels.actual_trades import label_actual_trades

    csv = csv or (settings.raw_dir / "zerodha_trades_fy25_fy26.csv")
    predictions_path = Path(predictions) if predictions else settings.processed_dir / "oos_predictions.parquet"
    if not Path(csv).exists():
        raise click.ClickException(f"Trade CSV not found: {csv}")
    if not predictions_path.exists():
        raise click.ClickException("Run train-directional first to produce OOS predictions.")

    trades = load_zerodha_trades(csv)
    rts = pair_round_trips(trades)
    labels = label_actual_trades(rts)
    preds = pd.read_parquet(predictions_path)
    preds["decision_time"] = pd.to_datetime(preds["decision_time"])

    # Nearest grid prediction at/ before each trade entry, same underlying.
    agree = 0
    n = 0
    for rt in rts:
        u = _infer_underlying(rt.symbol)
        if u is None:
            continue
        cand = preds[(preds["underlying_key"] == u) & (preds["decision_time"] <= pd.Timestamp(rt.entry_at))]
        if cand.empty:
            continue
        row = cand.sort_values("decision_time").iloc[-1]
        called = row["pred_direction"]
        won = labels.loc[labels.index == rt.entry_trade_id, "is_winner"]
        won_flag = bool(won.iloc[0]) if len(won) else (rt.gross_pnl > 0)
        trade_dir = "up" if rt.direction == "long" else "down"
        if won_flag and called == trade_dir or (not won_flag) and called in ("none", _opposite(trade_dir)):
            agree += 1
        n += 1

    rate = (agree / n) if n else 0.0
    console.print(f"Realized-trade overlay: agreement {rate:.1%} over {n} matched trades "
                  f"(winners-in-direction or losers skipped/opposite).")


def _infer_underlying(symbol: str) -> str | None:
    s = symbol.upper()
    if s.startswith("BANKNIFTY"):
        return "banknifty"
    if s.startswith("FINNIFTY"):
        return "finnifty"
    if s.startswith("NIFTY"):
        return "nifty"
    return None


def _opposite(d: str) -> str:
    return "down" if d == "up" else "up"


@cli.command("validate-trades")
@click.option("--csv", type=click.Path(dir_okay=False), default=None)
def validate_trades_cmd(csv):
    """Load the Zerodha trade log, pair round trips, print summary (overlay input)."""
    csv = csv or (settings.raw_dir / "zerodha_trades_fy25_fy26.csv")
    if not Path(csv).exists():
        raise click.ClickException(f"Trade CSV not found: {csv}")
    trades = load_zerodha_trades(csv)
    rts = pair_round_trips(trades)
    table = Table(title="Trade log summary")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Raw legs", str(len(trades)))
    table.add_row("Round trips", str(len(rts)))
    if rts:
        gross = sum(r.gross_pnl for r in rts)
        wins = sum(1 for r in rts if r.gross_pnl > 0)
        table.add_row("Gross P&L (₹)", f"{gross:,.0f}")
        table.add_row("Gross win rate", f"{wins / len(rts):.1%}")
    console.print(table)


@cli.command("check-data")
@click.option("--raw-dir", type=click.Path(file_okay=False), default=None,
              help="Defaults to data/raw/.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the full report as JSON.")
def check_data_cmd(raw_dir, as_json):
    """Scan data/raw/, validate file conventions/coverage/schema, and report the achievable gate."""
    import json as _json

    from nomad_sniper.data.readiness import check_data_readiness

    report = check_data_readiness(Path(raw_dir) if raw_dir else None)
    if as_json:
        console.print_json(_json.dumps(report.as_dict(), default=str))
        return

    table = Table(title=f"Data readiness — {report.raw_dir}")
    table.add_column("Underlying")
    table.add_column("Futures", justify="right")
    table.add_column("Coverage")
    table.add_column("Option files", justify="right")
    table.add_column("CE+PE expiries", justify="right")
    table.add_column("IV?")
    for r in report.underlyings:
        cov = f"{(r.coverage_start or '—')[:10]} → {(r.coverage_end or '—')[:10]}" if r.coverage_start else "—"
        table.add_row(
            r.underlying,
            f"{len([f for f in r.futures_files if not f.missing_required and not f.error])} ok"
            if r.futures_files else "[red]none[/red]",
            cov,
            str(r.option_files),
            str(r.option_ce_pe_complete_expiries),
            "yes" if r.option_has_iv else "no",
        )
    console.print(table)

    gate_color = {"none": "red", "atr_proxy": "yellow", "bs_proxy": "cyan", "actual_option": "green"}
    console.print(f"Recommended gate mode: [bold {gate_color.get(report.recommended_gate, 'white')}]"
                  f"{report.recommended_gate}[/bold {gate_color.get(report.recommended_gate, 'white')}]")
    for b in report.blocking:
        console.print(f"  [red]BLOCKING:[/red] {b}")
    for w in report.warnings:
        console.print(f"  [yellow]note:[/yellow] {w}")
    if not report.blocking:
        console.print("[green]Ready to run:[/green] set configs/label.yaml gate_mode and run "
                      "build-grid-features → build-labels → train-directional → evaluate.")


@cli.command("calibrate-breakeven")
@click.option("--spot", type=float, required=True, help="ATM reference spot (points).")
@click.option("--atr", "atr_points", type=float, required=True, help="ATR_ref in points (prior-close 14-session).")
@click.option("--horizon-minutes", type=int, default=60, help="Holding horizon H (label_horizon_minutes).")
@click.option("--days-to-expiry", type=float, required=True, help="Calendar days to the ATM option's expiry.")
@click.option("--iv", type=float, default=None, help="Annualized ATM IV (e.g. 0.14). Provide this OR --straddle.")
@click.option("--straddle", "straddle_price", type=float, default=None,
              help="ATM straddle price in points (inverted to IV if --iv not given).")
@click.option("--cost", "cost_inr_per_unit", type=float, default=4.0, help="Round-trip option cost per unit (points).")
@click.option("--opt-type", type=click.Choice(["call", "put"]), default="call")
def calibrate_breakeven_cmd(spot, atr_points, horizon_minutes, days_to_expiry, iv,
                            straddle_price, cost_inr_per_unit, opt_type):
    """Compute m_breakeven — the ATR move an ATM long must clear over H after theta+cost.

    This is the single number that gates every label (contract §8). Set the result as
    `m_breakeven` in configs/label.yaml.
    """
    from nomad_sniper.labels.breakeven import calibrate_m_breakeven

    res = calibrate_m_breakeven(
        spot=spot, atr_points=atr_points, horizon_minutes=horizon_minutes,
        days_to_expiry=days_to_expiry, iv=iv, straddle_price=straddle_price,
        cost_inr_per_unit=cost_inr_per_unit, opt_type=opt_type,
    )
    table = Table(title="m_breakeven calibration")
    table.add_column("Field")
    table.add_column("Value", justify="right")
    table.add_row("m_breakeven (set in label.yaml)", f"[bold green]{res.m_breakeven}[/bold green]")
    table.add_row("breakeven move (points)", f"{res.breakeven_move_points}")
    table.add_row("ATR (points)", f"{res.atr_points}")
    table.add_row("IV used", f"{res.iv_used}")
    table.add_row("entry premium", f"{res.entry_premium}")
    table.add_row("exit premium @ breakeven", f"{res.exit_premium_at_breakeven}")
    table.add_row("cost / unit", f"{res.cost_inr_per_unit}")
    table.add_row("horizon (min)", f"{res.horizon_minutes}")
    table.add_row("DTE (days)", f"{res.days_to_expiry}")
    console.print(table)
    if res.note:
        console.print(f"  [yellow]note:[/yellow] {res.note}")
    console.print(f"→ Set [bold]m_breakeven: {res.m_breakeven}[/bold] in configs/label.yaml")


if __name__ == "__main__":
    cli()
