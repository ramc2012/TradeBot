"""Trading routes — orders, positions, mode management."""
from __future__ import annotations
import asyncio
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.routers.auth import get_active_adapter
from brokers.base import OrderRequest
from core.config import settings
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
_trading_state_lock = asyncio.Lock()


async def _get_or_create_paper_session() -> tuple[str, PaperOrderBook, PaperPortfolio]:
    global _current_session_id
    async with _trading_state_lock:
        if _current_session_id and _current_session_id in _paper_sessions:
            order_book, portfolio = _paper_sessions[_current_session_id]
            return _current_session_id, order_book, portfolio
        session_id = str(uuid.uuid4())
        _current_session_id = session_id
        portfolio = PaperPortfolio(initial_capital=1_000_000.0, session_id=session_id)
        order_book = PaperOrderBook(on_fill=portfolio.on_fill)
        _paper_sessions[session_id] = (order_book, portfolio)
        return session_id, order_book, portfolio


async def _get_trading_state_snapshot() -> tuple[str, str, Optional[LiveOrderManager]]:
    async with _trading_state_lock:
        return _mode, _active_broker, _live_manager


def _mode_payload(mode: str, broker: str, live_manager: Optional[LiveOrderManager]) -> dict[str, object]:
    return {
        "mode": mode,
        "broker": broker,
        "paper_only": settings.PAPER_TRADING_ONLY,
        "live_manager_active": live_manager is not None,
    }


async def ensure_paper_trading_mode(*, preferred_broker: Optional[str] = None) -> dict[str, object]:
    global _mode, _active_broker, _live_manager

    async with _trading_state_lock:
        previous_live_manager = _live_manager
        _mode = "paper"
        if preferred_broker:
            _active_broker = preferred_broker
        _live_manager = None
        payload = _mode_payload(_mode, _active_broker, None)

    if previous_live_manager:
        await previous_live_manager.stop_reconciliation()

    return payload


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


class KillSwitchRequest(BaseModel):
    active: bool


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/mode")
async def set_mode(req: SetModeRequest):
    global _mode, _active_broker, _live_manager
    if req.mode not in ("paper", "live"):
        raise HTTPException(400, "mode must be paper or live")
    if req.mode == "live" and settings.PAPER_TRADING_ONLY:
        raise HTTPException(403, "Live trading is disabled in this environment. Paper mode only.")
    next_broker = req.broker or _active_broker
    new_live_manager: Optional[LiveOrderManager] = None
    if req.mode == "live":
        adapter = get_active_adapter(next_broker)
        if not adapter:
            raise HTTPException(400, "No active broker for live trading")
        new_live_manager = LiveOrderManager(adapter, _risk_manager)

    async with _trading_state_lock:
        previous_live_manager = _live_manager
        _mode = req.mode
        _active_broker = next_broker
        _live_manager = new_live_manager

    if previous_live_manager:
        await previous_live_manager.stop_reconciliation()
    if new_live_manager:
        await new_live_manager.start_reconciliation()

    return {"mode": req.mode, "broker": next_broker}


@router.get("/mode")
async def get_mode():
    mode, active_broker, live_manager = await _get_trading_state_snapshot()
    return _mode_payload(mode, active_broker, live_manager)


@router.post("/orders")
async def place_order(req: PlaceOrderRequest):
    if paper_strategy_agent.get_status().get("kill_switch_active"):
        raise HTTPException(400, "NSE kill switch is active. Release it before placing new orders.")

    mode, _, live_manager = await _get_trading_state_snapshot()
    if mode == "live" and settings.PAPER_TRADING_ONLY:
        raise HTTPException(403, "Live trading is disabled in this environment. Paper mode only.")

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

    if mode == "paper":
        session_id, ob, portfolio = await _get_or_create_paper_session()

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
            session_id=session_id,
            ltp=req.ltp,
        )
        return {
            "order_id": order.order_id,
            "status": order.status,
            "fill_price": order.fill_price,
            "mode": "paper",
        }

    elif mode == "live":
        if not live_manager:
            raise HTTPException(400, "Live trading not initialized")
        live_order = await live_manager.place_order(order_req)
        return {
            "order_id": live_order.local_id,
            "broker_order_id": live_order.broker_id,
            "status": live_order.status,
            "mode": "live",
        }

    raise HTTPException(400, "Invalid mode")


@router.get("/orders")
async def get_orders():
    mode, active_broker, live_manager = await _get_trading_state_snapshot()
    if mode == "paper":
        _, ob, _ = await _get_or_create_paper_session()
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
    elif mode == "live":
        adapter = get_active_adapter(active_broker)
        if not adapter:
            return []
        return await adapter.get_order_book()
    return []


@router.put("/orders/{order_id}")
async def modify_order(order_id: str, req: ModifyOrderRequest):
    mode, _, live_manager = await _get_trading_state_snapshot()
    if mode == "live" and live_manager:
        resp = await live_manager.broker.modify_order(order_id, req.dict(exclude_none=True))
        return resp
    raise HTTPException(400, "Modify only supported in live mode")


@router.delete("/orders/{order_id}")
async def cancel_order(order_id: str):
    mode, _, live_manager = await _get_trading_state_snapshot()
    if mode == "paper":
        _, ob, _ = await _get_or_create_paper_session()
        success = ob.cancel_order(order_id)
        return {"cancelled": success}
    elif mode == "live" and live_manager:
        success = await live_manager.cancel_order(order_id)
        return {"cancelled": success}
    return {"cancelled": False}


@router.get("/positions")
async def get_positions():
    mode, active_broker, _ = await _get_trading_state_snapshot()
    if mode == "paper":
        _, _, portfolio = await _get_or_create_paper_session()
        return portfolio.get_positions_list()
    elif mode == "live":
        adapter = get_active_adapter(active_broker)
        if not adapter:
            return []
        return await adapter.get_positions()
    return []


@router.get("/trades")
async def get_trades():
    mode, active_broker, _ = await _get_trading_state_snapshot()
    if mode == "live":
        adapter = get_active_adapter(active_broker)
        if not adapter:
            return []
        return await adapter.get_trade_book()
    if mode == "paper":
        _, _, portfolio = await _get_or_create_paper_session()
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
    control = paper_strategy_agent.set_kill_switch(True)
    mode, _, live_manager = await _get_trading_state_snapshot()
    if mode == "live" and live_manager:
        cancelled = await live_manager.kill_switch()
        return {
            "cancelled_orders": cancelled,
            "trading_disabled": True,
            **control,
        }
    # Paper mode: cancel all open orders
    if mode == "paper":
        _, ob, _ = await _get_or_create_paper_session()
        count = 0
        for order in list(ob.get_open_orders()):
            if ob.cancel_order(order.order_id):
                count += 1
        return {"cancelled_orders": count, "mode": "paper", **control}
    return {"cancelled_orders": 0, **control}


@router.get("/kill-switch")
async def get_kill_switch_state():
    return paper_strategy_agent.get_control_state()


@router.put("/kill-switch")
async def update_kill_switch(body: KillSwitchRequest):
    return paper_strategy_agent.set_kill_switch(body.active)


@router.get("/portfolio-summary")
async def portfolio_summary():
    _, _, portfolio = await _get_or_create_paper_session()
    return portfolio.get_summary()


@router.get("/strategy-agent/status")
async def strategy_agent_status():
    await paper_strategy_agent.ensure_recovered_state()
    return paper_strategy_agent.get_status()


@router.get("/strategy-agent/equity-history")
async def strategy_equity_history():
    """Return equity curve for all strategy portfolios."""
    status = paper_strategy_agent.get_status()
    result = []
    for strat in status.get("strategies", []):
        key = strat.get("key", "")
        runtime = paper_strategy_agent.get_runtime(key)
        if runtime:
            result.append({
                "key": key,
                "label": strat.get("label", ""),
                "equity_curve": runtime.portfolio.get_equity_curve(),
                "initial_capital": strat["summary"].get("initial_capital", 1_000_000),
            })
    return result


@router.post("/strategy-agent/run-once")
async def run_strategy_agent_once(force: bool = True):
    return await paper_strategy_agent.run_once(force=force)


@router.put("/strategy-agent/auto-run")
async def set_strategy_agent_auto_run(enabled: bool):
    """Enable or disable the recurring background scan loop."""
    return await paper_strategy_agent.set_auto_run(enabled)


@router.get("/risk-status")
async def risk_status():
    return _risk_manager.get_status()


@router.put("/risk-config")
async def update_risk_config(config: dict):
    _risk_manager.update_config(**config)
    return _risk_manager.get_status()
