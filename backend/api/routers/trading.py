"""Trading routes — orders, positions, mode management."""
from __future__ import annotations
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.routers.auth import get_active_adapter, _active_brokers
from brokers.base import OrderRequest
from live_engine import LiveOrderManager, RiskManager
from paper_engine import PaperOrderBook, PaperPortfolio
from paper_engine.strategy_agent import paper_strategy_agent

router = APIRouter(prefix="/api/trading", tags=["trading"])

# ── State ────────────────────────────────────────────────────────────────────
_mode = "paper"
_active_broker = "fyers"
_risk_manager = RiskManager()
_paper_sessions: dict[str, tuple[PaperOrderBook, PaperPortfolio]] = {}
_current_session_id: Optional[str] = None
_live_manager: Optional[LiveOrderManager] = None


def _get_or_create_paper_session() -> tuple[PaperOrderBook, PaperPortfolio]:
    global _current_session_id
    if _current_session_id and _current_session_id in _paper_sessions:
        return _paper_sessions[_current_session_id]
    session_id = str(uuid.uuid4())
    _current_session_id = session_id
    portfolio = PaperPortfolio(initial_capital=1_000_000.0, session_id=session_id)
    order_book = PaperOrderBook(on_fill=portfolio.on_fill)
    _paper_sessions[session_id] = (order_book, portfolio)
    return order_book, portfolio


# ── Pydantic Models ──────────────────────────────────────────────────────────

class PlaceOrderRequest(BaseModel):
    symbol: str
    exchange: str = "NSE"
    action: str
    order_type: str = "MARKET"
    qty: int
    instrument_type: str = "CE"
    price: Optional[float] = None
    sl: Optional[float] = None
    target: Optional[float] = None
    product: str = "INTRADAY"
    expiry: Optional[str] = None
    strike: Optional[float] = None
    option_type: Optional[str] = None
    ltp: Optional[float] = None  # for paper market orders

class ModifyOrderRequest(BaseModel):
    price: Optional[float] = None
    qty: Optional[int] = None
    sl: Optional[float] = None

class SetModeRequest(BaseModel):
    mode: str  # paper | live
    broker: Optional[str] = None


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/mode")
async def set_mode(req: SetModeRequest):
    global _mode, _active_broker, _live_manager
    if req.mode not in ("paper", "live"):
        raise HTTPException(400, "mode must be paper or live")
    _mode = req.mode
    if req.broker:
        _active_broker = req.broker
    if _mode == "live":
        adapter = get_active_adapter(_active_broker)
        if not adapter:
            raise HTTPException(400, "No active broker for live trading")
        _live_manager = LiveOrderManager(adapter, _risk_manager)
        await _live_manager.start_reconciliation()
    return {"mode": _mode, "broker": _active_broker}


@router.post("/orders")
async def place_order(req: PlaceOrderRequest):
    order_req = OrderRequest(
        symbol=req.symbol,
        exchange=req.exchange,
        action=req.action,
        order_type=req.order_type,
        qty=req.qty,
        instrument_type=req.instrument_type,
        price=req.price,
        sl=req.sl,
        target=req.target,
        product=req.product,
        expiry=req.expiry,
        strike=req.strike,
        option_type=req.option_type,
    )

    if _mode == "paper":
        ob, portfolio = _get_or_create_paper_session()

        # Risk check
        allowed, reason = _risk_manager.check_order(
            symbol=req.symbol,
            action=req.action,
            qty=req.qty,
            price=req.price or req.ltp or 0,
            sl=req.sl,
            total_capital=portfolio.total_equity,
        )
        if not allowed:
            raise HTTPException(400, f"Risk check failed: {reason}")

        order = ob.place_order(
            symbol=req.symbol,
            action=req.action,
            order_type=req.order_type,
            qty=req.qty,
            price=req.price,
            sl=req.sl,
            target=req.target,
            instrument_type=req.instrument_type,
            expiry=req.expiry,
            strike=req.strike,
            option_type=req.option_type,
            session_id=_current_session_id,
            ltp=req.ltp,
        )
        return {
            "order_id": order.order_id,
            "status": order.status,
            "fill_price": order.fill_price,
            "mode": "paper",
        }

    elif _mode == "live":
        if not _live_manager:
            raise HTTPException(400, "Live trading not initialized")
        live_order = await _live_manager.place_order(order_req)
        return {
            "order_id": live_order.local_id,
            "broker_order_id": live_order.broker_id,
            "status": live_order.status,
            "mode": "live",
        }

    raise HTTPException(400, "Invalid mode")


@router.get("/orders")
async def get_orders():
    if _mode == "paper":
        ob, _ = _get_or_create_paper_session()
        orders = ob.get_open_orders()
        return [
            {
                "order_id": o.order_id,
                "symbol": o.symbol,
                "action": o.action,
                "qty": o.qty,
                "price": o.price,
                "status": o.status,
            }
            for o in orders
        ]
    elif _mode == "live" and _active_broker in _active_brokers:
        adapter = _active_brokers[_active_broker]["adapter"]
        return await adapter.get_order_book()
    return []


@router.put("/orders/{order_id}")
async def modify_order(order_id: str, req: ModifyOrderRequest):
    if _mode == "live" and _live_manager:
        resp = await _live_manager.broker.modify_order(order_id, req.dict(exclude_none=True))
        return resp
    raise HTTPException(400, "Modify only supported in live mode")


@router.delete("/orders/{order_id}")
async def cancel_order(order_id: str):
    if _mode == "paper":
        ob, _ = _get_or_create_paper_session()
        success = ob.cancel_order(order_id)
        return {"cancelled": success}
    elif _mode == "live" and _live_manager:
        success = await _live_manager.cancel_order(order_id)
        return {"cancelled": success}
    return {"cancelled": False}


@router.get("/positions")
async def get_positions():
    if _mode == "paper":
        _, portfolio = _get_or_create_paper_session()
        return portfolio.get_positions_list()
    elif _mode == "live" and _active_broker in _active_brokers:
        adapter = _active_brokers[_active_broker]["adapter"]
        return await adapter.get_positions()
    return []


@router.get("/trades")
async def get_trades():
    if _mode == "live" and _active_broker in _active_brokers:
        adapter = _active_brokers[_active_broker]["adapter"]
        return await adapter.get_trade_book()
    if _mode == "paper":
        _, portfolio = _get_or_create_paper_session()
        return [
            {
                "symbol": t.symbol,
                "action": t.action,
                "qty": t.qty,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "pnl": t.pnl,
                "entry_time": t.entry_time.isoformat(),
                "exit_time": t.exit_time.isoformat(),
            }
            for t in portfolio._trade_history
        ]
    return []


@router.post("/kill-switch")
async def kill_switch():
    if _mode == "live" and _live_manager:
        cancelled = await _live_manager.kill_switch()
        return {"cancelled_orders": cancelled, "trading_disabled": True}
    # Paper mode: cancel all open orders
    if _mode == "paper":
        ob, _ = _get_or_create_paper_session()
        count = 0
        for order in list(ob.get_open_orders()):
            if ob.cancel_order(order.order_id):
                count += 1
        return {"cancelled_orders": count, "mode": "paper"}
    return {"cancelled_orders": 0}


@router.get("/portfolio-summary")
async def portfolio_summary():
    _, portfolio = _get_or_create_paper_session()
    return portfolio.get_summary()


@router.get("/strategy-agent/status")
async def strategy_agent_status():
    return paper_strategy_agent.get_status()


@router.post("/strategy-agent/run-once")
async def run_strategy_agent_once(force: bool = True):
    return await paper_strategy_agent.run_once(force=force)


@router.get("/risk-status")
async def risk_status():
    return _risk_manager.get_status()


@router.put("/risk-config")
async def update_risk_config(config: dict):
    _risk_manager.update_config(**config)
    return _risk_manager.get_status()
