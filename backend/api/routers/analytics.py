"""Analytics routes — performance, equity curve, portfolio Greeks, sector."""
from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Query

from analytics.performance import PerformanceAnalytics
from analytics.sector import sector_tracker
from analytics.greeks import aggregate_portfolio_greeks
from api.routers.trading import _paper_position_rows, _paper_trade_rows

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

_perf = PerformanceAnalytics()


async def _get_trades_as_dicts() -> list:
    return await _paper_trade_rows()


@router.get("/performance")
async def get_performance(
    period: str = Query("today", regex="^(today|week|month|all)$"),
    session_id: Optional[str] = Query(None),
):
    trades = await _get_trades_as_dicts()
    summary = _perf.summary(trades, period)
    return {
        "period": period,
        "total_trades": summary.total_trades,
        "win_rate": summary.win_rate,
        "avg_win": summary.avg_win,
        "avg_loss": summary.avg_loss,
        "profit_factor": summary.profit_factor,
        "max_drawdown": summary.max_drawdown,
        "sharpe_ratio": summary.sharpe_ratio,
        "total_pnl": summary.total_pnl,
        "day_pnl": summary.day_pnl,
    }


@router.get("/equity-curve")
async def get_equity_curve():
    trades = await _get_trades_as_dicts()
    curve = _perf.equity_curve(trades)
    return [{"timestamp": p.timestamp, "equity": p.equity, "pnl": p.cumulative_pnl} for p in curve]


@router.get("/calendar-heatmap")
async def get_calendar_heatmap():
    trades = await _get_trades_as_dicts()
    return _perf.calendar_heatmap(trades)


@router.get("/strategy-breakdown")
async def get_strategy_breakdown():
    trades = await _get_trades_as_dicts()
    return _perf.strategy_breakdown(trades)


@router.get("/portfolio-greeks")
async def get_portfolio_greeks():
    positions = await _paper_position_rows()
    # Build position dicts with Greek inputs
    greek_inputs = []
    for pos in positions:
        if pos.get("instrument_type") in ("CE", "PE"):
            greek_inputs.append({
                "symbol": pos["symbol"],
                "option_type": pos.get("option_type", pos.get("instrument_type", "CE")),
                "qty": pos["qty"],
                "action": pos["action"],
                "spot": pos.get("ltp", pos.get("avg_price", 100)),
                "strike": pos.get("strike", 100),
                "expiry_days": 7,   # approximate
                "iv": 0.20,
                "r": 0.065,
            })
    if not greek_inputs:
        return {"delta": 0, "gamma": 0, "theta": 0, "vega": 0}
    pg = aggregate_portfolio_greeks(greek_inputs)
    return {"delta": pg.delta, "gamma": pg.gamma, "theta": pg.theta, "vega": pg.vega}


@router.get("/sector-rotation")
async def get_sector_rotation(
    timeframe: str = Query(
        "daily",
        pattern="^(hourly|daily|weekly|monthly|quarterly|yearly|hour|day|week|month|quarter|year)$",
    ),
):
    return await sector_tracker.get_sector_rotation(timeframe)


@router.get("/sector-rotation/{sector_code}/components")
async def get_sector_rotation_components(
    sector_code: str,
    timeframe: str = Query(
        "daily",
        pattern="^(hourly|daily|weekly|monthly|quarterly|yearly|hour|day|week|month|quarter|year)$",
    ),
):
    return await sector_tracker.get_sector_components(sector_code, timeframe)


@router.get("/macro-dashboard")
async def get_macro_dashboard():
    return await sector_tracker.get_macro_dashboard()


@router.get("/rolling-sharpe")
async def get_rolling_sharpe(window: int = Query(20)):
    trades = await _get_trades_as_dicts()
    return _perf.rolling_sharpe(trades, window)
