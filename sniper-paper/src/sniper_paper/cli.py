"""sniper-paper CLI.

Commands:
  introspect-db       Verify DB connectivity + show NIFTY table structure.
  extract-nifty       Pull candles + materialise to parquet.
  train               Build training rows from candles, fit model, save artifact.
  promote ID          Point ACTIVE pointer at artifact ID.
  list-models         Show available artifacts.
  run                 Start the live runner (long-running).
  api                 Start the FastAPI dashboard only.
  status              Quick CLI status report.
  init-db             Apply sql/schema.sql to the configured DB.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from sniper_paper.common.logging import setup_logging
from sniper_paper.common.settings import Settings

app = typer.Typer(no_args_is_help=True)
console = Console()


def _settings(config: str) -> Settings:
    setup_logging()
    return Settings.load(config)


@app.command()
def init_db(config: str = "configs/paper.yaml") -> None:
    """Run sql/schema.sql against the configured database."""
    import asyncpg

    settings = _settings(config)
    schema = Path("sql/schema.sql").read_text()

    async def _go() -> None:
        conn = await asyncpg.connect(settings.db_dsn())
        try:
            await conn.execute(schema)
            console.print("[green]Schema applied.[/green]")
        finally:
            await conn.close()

    asyncio.run(_go())


@app.command("introspect-db")
def introspect_db(config: str = "configs/paper.yaml") -> None:
    """Show NIFTY table layout from the nomad-curie DB."""
    from sniper_paper.training.extract_from_db import introspect

    settings = _settings(config)
    info = asyncio.run(introspect(settings.db_dsn()))
    console.print_json(json.dumps(info, default=str))


@app.command("extract-nifty")
def extract_nifty(
    config: str = "configs/paper.yaml",
    start: str = typer.Option(..., help="YYYY-MM-DD"),
    end: str = typer.Option(..., help="YYYY-MM-DD"),
    timeframe: str = "30minute",        # matches DB's interval column values
    underlying: str = "NIFTY",
    out: str = "data/nifty_candles.parquet",
) -> None:
    """Pull OHLCV from the existing TimescaleDB into a parquet file."""
    from sniper_paper.training.extract_from_db import fetch_underlying_candles

    settings = _settings(config)
    df = asyncio.run(fetch_underlying_candles(
        settings.db_dsn(),
        underlying,
        timeframe,
        datetime.fromisoformat(start),
        datetime.fromisoformat(end),
    ))
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)
    console.print(f"[green]Wrote {out_path} ({len(df):,} rows)[/green]")


@app.command()
def train(
    config: str = "configs/paper.yaml",
    candles: str = "data/nifty_candles.parquet",
    notes: str = "",
) -> None:
    """Train classifier + regressor and save a new artifact (un-promoted)."""
    import pandas as pd

    from sniper_paper.training.build_dataset import build_training_rows
    from sniper_paper.training.train import train_and_save

    settings = _settings(config)
    nifty = settings.instrument_by_name("NIFTY")
    candles_df = pd.read_parquet(candles)
    console.print(f"Loaded {len(candles_df):,} candles")

    rows = build_training_rows(candles_df, nifty, settings)
    console.print(f"Built [bold]{len(rows):,}[/bold] training rows from candles")
    if not rows:
        console.print("[red]No training rows produced. Check detector thresholds.[/red]")
        raise typer.Exit(1)

    artifact_dir = train_and_save(rows, settings, notes=notes)
    console.print(f"Artifact: [bold]{artifact_dir}[/bold]")
    console.print("Run `sniper-paper promote <artifact_id>` to make it ACTIVE.")


@app.command("train-nn")
def train_nn(
    config: str = "configs/paper.yaml",
    candles: str = "data/nifty_candles.parquet",
    epochs: int = 300,
    lr: float = 1e-3,
    turnover_penalty: float = 0.05,
    notes: str = "",
) -> None:
    """Train the multi-head SniperNet (NN) and save a new artifact (un-promoted)."""
    import pandas as pd

    from sniper_paper.training.build_dataset import build_training_rows
    from sniper_paper.training.train_nn import train_and_save_nn

    settings = _settings(config)
    nifty = settings.instrument_by_name("NIFTY")
    candles_df = pd.read_parquet(candles)
    console.print(f"Loaded {len(candles_df):,} candles")

    rows = build_training_rows(candles_df, nifty, settings)
    console.print(f"Built [bold]{len(rows):,}[/bold] training rows from candles")
    if not rows:
        console.print("[red]No training rows produced. Check detector thresholds.[/red]")
        raise typer.Exit(1)

    artifact_dir = train_and_save_nn(
        rows, settings, notes=notes, epochs=epochs, lr=lr, turnover_penalty=turnover_penalty,
    )
    console.print(f"NN artifact: [bold]{artifact_dir}[/bold]")
    console.print("Run `sniper-paper promote <artifact_id>` to make it ACTIVE.")


@app.command()
def promote(artifact_id: str, config: str = "configs/paper.yaml") -> None:
    """Point ACTIVE pointer at the given artifact id."""
    from sniper_paper.training.train import promote as _promote

    settings = _settings(config)
    artifact_dir = Path(settings.model.artifact_dir) / artifact_id
    if not artifact_dir.exists():
        console.print(f"[red]Artifact not found: {artifact_dir}[/red]")
        raise typer.Exit(1)
    _promote(artifact_dir, settings)
    console.print(f"[green]Promoted {artifact_id}[/green]")


@app.command("list-models")
def list_models(config: str = "configs/paper.yaml") -> None:
    settings = _settings(config)
    art_dir = Path(settings.model.artifact_dir)
    if not art_dir.exists():
        console.print("[yellow]No artifacts directory yet.[/yellow]")
        return
    active = ""
    pointer = Path(settings.model.active_model_pointer)
    if pointer.exists():
        active = Path(pointer.read_text().strip()).name
    table = Table(title="Available model artifacts")
    table.add_column("Artifact ID")
    table.add_column("Active")
    table.add_column("Trained at")
    for entry in sorted(art_dir.iterdir()):
        if not entry.is_dir():
            continue
        meta = entry / "metadata.json"
        ts = json.loads(meta.read_text())["trained_at"] if meta.exists() else "?"
        table.add_row(entry.name, "✓" if entry.name == active else "", ts)
    console.print(table)


@app.command()
def run(config: str = "configs/paper.yaml") -> None:
    """Start the live paper trader (long-running)."""
    from sniper_paper.runner import main_async

    asyncio.run(main_async(config))


@app.command()
def api(
    config: str = "configs/paper.yaml",
    host: str = "0.0.0.0",
    port: int = 8001,
) -> None:
    """Start only the FastAPI dashboard (no runner)."""
    import uvicorn

    _settings(config)
    uvicorn.run("sniper_paper.api.main:app", host=host, port=port, reload=False)


@app.command()
def status(config: str = "configs/paper.yaml") -> None:
    """Quick CLI status report — today's signal count + P&L + open positions."""
    import asyncpg

    settings = _settings(config)

    async def _go() -> None:
        conn = await asyncpg.connect(settings.db_dsn())
        try:
            from datetime import date
            row = await conn.fetchrow(
                "SELECT * FROM paper_daily_pnl WHERE date = $1", date.today()
            )
            n_open = await conn.fetchval(
                "SELECT count(*) FROM paper_positions WHERE status = 'open'"
            )
            console.print(f"Today: [bold]{dict(row) if row else 'no signals yet'}[/bold]")
            console.print(f"Open positions: [bold]{n_open}[/bold]")
        finally:
            await conn.close()

    asyncio.run(_go())


if __name__ == "__main__":
    app()
