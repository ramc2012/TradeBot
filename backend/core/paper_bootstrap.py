from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from core.config import settings

async def bootstrap_paper_trading_runtime() -> dict[str, Any]:
    if not settings.PAPER_TRADING_ONLY:
        return {
            "enabled": False,
            "paper_only": False,
            "reason": "Paper-only bootstrap disabled.",
        }

    from api.routers.auth import get_broker_connection_snapshot
    from api.routers.trading import ensure_paper_trading_mode
    from market_data.commodity_atm_watchlist import commodity_atm_watchlist_service
    from paper_engine.commodity_strategy_agent import commodity_strategy_agent
    from paper_engine.strategy_agent import paper_strategy_agent

    broker_snapshot = await get_broker_connection_snapshot(force_validate=False)
    preferred_broker = "fyers" if broker_snapshot.get("fyers_ready") else None
    if preferred_broker is None and broker_snapshot.get("upstox_ready"):
        preferred_broker = "upstox"

    trading_mode = await ensure_paper_trading_mode(preferred_broker=preferred_broker)

    await paper_strategy_agent.ensure_recovered_state()
    nse_control = paper_strategy_agent.get_control_state()
    if nse_control.get("kill_switch_active"):
        paper_strategy_agent.set_kill_switch(False)
    nse_control = paper_strategy_agent.get_control_state()
    if not nse_control.get("auto_run_enabled") or not nse_control.get("loop_active"):
        await paper_strategy_agent.set_auto_run(True)
    nse_status = paper_strategy_agent.get_status()

    commodity_control = commodity_strategy_agent.get_control_state()
    if commodity_control.get("kill_switch_active"):
        commodity_control = await commodity_strategy_agent.set_kill_switch(False)
    if commodity_control.get("start_required") or not commodity_control.get("loop_active"):
        await commodity_strategy_agent.start(force=True)
    commodity_status = commodity_strategy_agent.get_status()

    prewarm_tasks: list[asyncio.Task[None]] = []
    if settings.PAPER_RUNTIME_PREWARM_ENABLED and broker_snapshot.get("broker_ready"):
        prewarm_tasks.append(asyncio.create_task(_prewarm_market_intelligence_runtime()))
        if commodity_strategy_agent.get_symbols():
            prewarm_tasks.append(
                asyncio.create_task(
                    _prewarm_commodity_watchlists(
                        commodity_strategy_agent=commodity_strategy_agent,
                        commodity_atm_watchlist_service=commodity_atm_watchlist_service,
                    )
                )
            )

    payload = {
        "enabled": True,
        "paper_only": True,
        "broker_snapshot": broker_snapshot,
        "trading_mode": trading_mode,
        "nse": {
            "loop_active": nse_status.get("loop_active"),
            "kill_switch_active": nse_status.get("kill_switch_active"),
            "auto_run_enabled": nse_status.get("auto_run_enabled"),
        },
        "commodity": {
            "loop_active": commodity_status.get("loop_active"),
            "kill_switch_active": commodity_status.get("kill_switch_active"),
            "start_required": commodity_status.get("start_required"),
            "tracked_symbols": (commodity_status.get("summary") or {}).get("tracked_symbols", 0),
        },
        "prewarm_tasks_started": len(prewarm_tasks),
    }
    logger.info(f"[Paper Bootstrap] {payload}")
    return payload


async def _prewarm_market_intelligence_runtime() -> None:
    try:
        from market_data.market_intelligence_runtime import market_intelligence_runtime

        payload = await asyncio.wait_for(market_intelligence_runtime.refresh_nse_runtime(), timeout=90)
        logger.info(
            "[Paper Bootstrap] Market intelligence prewarm finished "
            f"(spot_rows={payload.get('spot_gap_fill', {}).get('stored_total', 0)})"
        )
    except Exception as exc:
        logger.warning(f"[Paper Bootstrap] Market intelligence prewarm failed: {exc}")


async def _prewarm_commodity_watchlists(*, commodity_strategy_agent, commodity_atm_watchlist_service) -> None:
    try:
        symbols = commodity_strategy_agent.get_symbols()
        await asyncio.wait_for(commodity_strategy_agent.ensure_selected_option_setup_locks(), timeout=30)
        await asyncio.wait_for(
            commodity_atm_watchlist_service.get_contract_catalog(
                symbols,
                commodity_strategy_agent.get_selected_option_expiries(),
                commodity_strategy_agent.get_selected_option_lookup_symbols(),
            ),
            timeout=45,
        )
        await asyncio.wait_for(
            commodity_atm_watchlist_service.get_watchlist(
                symbols,
                commodity_strategy_agent.get_selected_option_expiries(),
                commodity_strategy_agent.get_selected_option_lookup_symbols(),
                None,
            ),
            timeout=45,
        )
        logger.info(f"[Paper Bootstrap] Commodity watchlist prewarm finished for symbols={symbols}")
    except Exception as exc:
        logger.warning(f"[Paper Bootstrap] Commodity watchlist prewarm failed: {exc}")
