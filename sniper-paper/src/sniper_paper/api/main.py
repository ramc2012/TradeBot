"""FastAPI app exposing live state. Mounted on port 8001 (8000 = nomad-curie)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from sniper_paper.api.routes.model_routes import router as model_router
from sniper_paper.common.settings import Settings
from sniper_paper.persistence.db import get_pool, init_pool

settings = Settings.load("configs/paper.yaml")

app = FastAPI(title="Sniper Paper", version="0.1.0")
app.include_router(model_router)


@app.on_event("startup")
async def _startup() -> None:
    await init_pool(settings)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/api/status")
async def status():
    pool = await get_pool()
    today_pnl = await pool.fetchrow(
        "SELECT * FROM sniper_paper_daily_pnl WHERE date = $1", date.today()
    )
    open_pos = await pool.fetch(
        "SELECT instrument, side, entry_price, qty, entry_ts, stop_price, target_price FROM sniper_paper_positions WHERE status = 'open'"
    )
    return {
        "today_pnl": dict(today_pnl) if today_pnl else None,
        "open_positions": [dict(r) for r in open_pos],
        "instruments": [i.model_dump() for i in settings.instruments],
    }


@app.get("/api/signals/recent")
async def recent_signals(limit: int = 50):
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT signal_id, decision_ts, instrument, setup_name, side,
               entry_price, stop_price, target_price,
               p_win, expected_net_R, in_distribution, gate_decision, gate_reason
        FROM sniper_paper_signals
        ORDER BY decision_ts DESC
        LIMIT $1
        """,
        limit,
    )
    return [dict(r) for r in rows]


@app.get("/api/positions/recent")
async def recent_positions(limit: int = 50):
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT position_id, instrument, side, qty,
               entry_ts, entry_price, exit_ts, exit_price,
               stop_price, target_price, outcome,
               gross_pnl, costs_inr, net_pnl, net_R, mae, mfe, status
        FROM sniper_paper_positions
        ORDER BY entry_ts DESC
        LIMIT $1
        """,
        limit,
    )
    return [dict(r) for r in rows]


@app.get("/api/pnl/daily")
async def daily_pnl(days: int = 30):
    pool = await get_pool()
    since = date.today() - timedelta(days=days)
    rows = await pool.fetch(
        "SELECT * FROM sniper_paper_daily_pnl WHERE date >= $1 ORDER BY date ASC",
        since,
    )
    return [dict(r) for r in rows]


frontend_dir = Path(__file__).resolve().parents[3] / "frontend"


def _serve_html(name: str) -> HTMLResponse:
    path = frontend_dir / name
    if path.exists():
        return HTMLResponse(path.read_text())
    return HTMLResponse(
        f"<html><body><h1>Sniper Paper</h1><p>frontend/{name} missing</p></body></html>",
        status_code=404,
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return _serve_html("index.html")


@app.get("/model.html", response_class=HTMLResponse)
async def page_model() -> HTMLResponse:
    return _serve_html("model.html")


@app.get("/features.html", response_class=HTMLResponse)
async def page_features() -> HTMLResponse:
    return _serve_html("features.html")


@app.get("/signals.html", response_class=HTMLResponse)
async def page_signals() -> HTMLResponse:
    return _serve_html("signals.html")


@app.get("/signal.html", response_class=HTMLResponse)
async def page_signal() -> HTMLResponse:
    return _serve_html("signal.html")


if frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")
