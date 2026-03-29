"""Backtester API — Options MACD Strategy."""
from __future__ import annotations

import io
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel

from backtester.options_macd_backtester import (
    OptionsMACDBacktester,
    BacktestConfig,
)

router = APIRouter(prefix="/api/backtester", tags=["backtester"])


# ── Pydantic Models ───────────────────────────────────────────────────────────

class BacktestConfigRequest(BaseModel):
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    timeframe: str = "5min"
    strike_selection: str = "ATM"
    option_types: List[str] = ["CE", "PE"]
    sl_pct: float = 0.35
    target_1_pct: float = 0.50
    target_2_pct: float = 1.00
    target_3_pct: float = 1.80
    time_exit_bars: int = 78
    capital_per_trade: float = 150_000
    max_concurrent: int = 3
    use_signal_cross: bool = True
    use_histogram_accel: bool = False


class BreezeFetchRequest(BaseModel):
    stock_code: str
    expiry_date: str
    right: str         # "call" or "put"
    strike_price: str
    from_date: str
    to_date: str
    interval: str = "5minute"
    config: Optional[BacktestConfigRequest] = None


class WalkForwardRequest(BaseModel):
    config: Optional[BacktestConfigRequest] = None
    train_pct: float = 0.70
    n_windows: int = 5
    param_grid: Optional[Dict[str, List[int]]] = None
    # Inline data (JSON array of OHLCV rows)
    data: Optional[List[Dict]] = None
    underlying: str = ""
    market: str = "NSE"


class SensitivityRequest(BaseModel):
    param: str
    values: List[Any]
    config: Optional[BacktestConfigRequest] = None
    data: Optional[List[Dict]] = None
    underlying: str = ""
    market: str = "NSE"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_config(req: Optional[BacktestConfigRequest]) -> BacktestConfig:
    if not req:
        return BacktestConfig()
    return BacktestConfig(
        macd_fast=req.macd_fast,
        macd_slow=req.macd_slow,
        macd_signal=req.macd_signal,
        timeframe=req.timeframe,
        strike_selection=req.strike_selection,
        option_types=req.option_types,
        sl_pct=req.sl_pct,
        target_1_pct=req.target_1_pct,
        target_2_pct=req.target_2_pct,
        target_3_pct=req.target_3_pct,
        time_exit_bars=req.time_exit_bars,
        capital_per_trade=req.capital_per_trade,
        max_concurrent=req.max_concurrent,
        use_signal_cross=req.use_signal_cross,
        use_histogram_accel=req.use_histogram_accel,
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/run-csv")
async def run_backtest_csv(
    file: UploadFile = File(...),
    underlying: str = Form(""),
    market: str = Form("US"),
    config_json: str = Form("{}"),
):
    """
    Upload a CSV file (OptionsDX format or custom) and run backtest.
    config_json: JSON string of BacktestConfigRequest fields.
    """
    try:
        cfg_dict = json.loads(config_json) if config_json else {}
        cfg_req = BacktestConfigRequest(**cfg_dict) if cfg_dict else BacktestConfigRequest()
        config = _build_config(cfg_req)

        contents = await file.read()
        buf = io.StringIO(contents.decode("utf-8"))

        bt = OptionsMACDBacktester(config)
        bt.load_from_csv(buf, underlying=underlying, market=market)
        results = bt.run_backtest()
        report = bt.generate_report(results)

        return {
            "status": "ok",
            "report": report,
            "result_count": len(results),
        }
    except Exception as e:
        raise HTTPException(400, f"Backtest failed: {str(e)}")


@router.post("/run-json")
async def run_backtest_json(body: Dict[str, Any]):
    """
    Run backtest with inline JSON data.

    Body:
      data: list of {timestamp, expiry, strike, option_type, open, high, low, close, volume}
      underlying: str
      market: str
      config: BacktestConfigRequest fields
    """
    try:
        import pandas as pd

        data = body.get("data", [])
        underlying = body.get("underlying", "")
        market = body.get("market", "NSE")
        cfg_dict = body.get("config", {})
        cfg_req = BacktestConfigRequest(**cfg_dict) if cfg_dict else BacktestConfigRequest()
        config = _build_config(cfg_req)

        if not data:
            raise ValueError("data field is empty")

        df = pd.DataFrame(data)
        bt = OptionsMACDBacktester(config)
        bt.load_from_dataframe(df, underlying=underlying, market=market)
        results = bt.run_backtest()
        report = bt.generate_report(results)

        return {
            "status": "ok",
            "report": report,
            "result_count": len(results),
        }
    except Exception as e:
        raise HTTPException(400, f"Backtest failed: {str(e)}")


@router.post("/run-breeze")
async def run_backtest_breeze(req: BreezeFetchRequest):
    """
    Fetch option data from ICICI Breeze and run backtest.
    Requires ICICI Breeze to be connected (via /api/auth/icici-breeze/connect).
    """
    from api.routers.auth import _active_brokers
    from brokers.icici_breeze import ICICIBreezeAdapter

    icici = _active_brokers.get("icici_breeze")
    if not icici:
        raise HTTPException(400, "ICICI Breeze not connected. Connect via /api/auth/icici-breeze/connect first.")

    adapter: ICICIBreezeAdapter = icici["adapter"]
    config = _build_config(req.config)

    try:
        data = await adapter.get_historical_options(
            stock_code=req.stock_code,
            expiry_date=req.expiry_date,
            right=req.right,
            strike_price=req.strike_price,
            from_date=req.from_date,
            to_date=req.to_date,
            interval=req.interval,
        )

        if not data:
            raise HTTPException(404, "No historical data returned from Breeze API")

        bt = OptionsMACDBacktester(config)
        opt_type = "CE" if req.right.lower() == "call" else "PE"
        bt.load_from_breeze(
            breeze_data=data,
            underlying=req.stock_code,
            expiry=req.expiry_date,
            strike=float(req.strike_price),
            option_type=opt_type,
            market="NSE",
        )
        results = bt.run_backtest()
        report = bt.generate_report(results)

        return {
            "status": "ok",
            "report": report,
            "bars_loaded": len(data),
            "result_count": len(results),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Breeze backtest failed: {str(e)}")


@router.post("/walk-forward")
async def walk_forward_optimize(req: WalkForwardRequest):
    """
    Walk-forward optimization to validate MACD parameters without overfitting.
    """
    try:
        import pandas as pd

        config = _build_config(req.config)
        bt = OptionsMACDBacktester(config)

        if req.data:
            df = pd.DataFrame(req.data)
            bt.load_from_dataframe(df, underlying=req.underlying, market=req.market)
        else:
            raise HTTPException(400, "data field required for walk-forward optimization")

        wf_result = bt.walk_forward_optimize(
            train_pct=req.train_pct,
            n_windows=req.n_windows,
            param_grid=req.param_grid,
        )
        return {"status": "ok", **wf_result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Walk-forward failed: {str(e)}")


@router.post("/sensitivity")
async def parameter_sensitivity(req: SensitivityRequest):
    """
    Test strategy sensitivity to a single MACD parameter.
    Returns metrics for each parameter value.
    """
    try:
        import pandas as pd

        config = _build_config(req.config)
        bt = OptionsMACDBacktester(config)

        if req.data:
            df = pd.DataFrame(req.data)
            bt.load_from_dataframe(df, underlying=req.underlying, market=req.market)
        else:
            raise HTTPException(400, "data field required")

        results = bt.parameter_sensitivity(req.param, req.values)
        return {"status": "ok", "param": req.param, "results": results}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Sensitivity analysis failed: {str(e)}")


@router.get("/default-config")
async def get_default_config():
    """Return default backtester configuration."""
    return BacktestConfigRequest().dict()
