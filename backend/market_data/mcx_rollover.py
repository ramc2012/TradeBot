"""MCX contract auto-rollover.

The commodity (MP+OF) agent tracks specific-expiry futures (e.g.
MCX:GOLD26JUNFUT). When a contract expires the stored symbol goes stale and the
watchlist can empty. This daemon periodically re-resolves each configured
symbol's ROOT to its current front-month future and rewrites the agent's symbol
list, so the desk always tracks live contracts and never empties on expiry.

Pure resolution + config update via the agent's own update_symbols — the scan
and trading logic are untouched.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from loguru import logger

from market_data.upstox_commodity import _parse_mcx_future_symbol, resolve_upstox_mcx_future

IST = timezone(timedelta(hours=5, minutes=30))
_MONTH_ABBR = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def _canonical(root: str, expiry_ms: int) -> str:
    dt = datetime.fromtimestamp(int(expiry_ms) / 1000, tz=IST)
    return f"MCX:{root}{dt.year % 100:02d}{_MONTH_ABBR[dt.month - 1]}FUT"


async def roll_forward() -> list[str]:
    """Re-resolve configured commodity symbols to current front-month futures.

    Returns the (possibly rolled) symbol list. Only writes config when something
    actually changed, to avoid commentary spam.
    """
    from paper_engine.commodity_strategy_agent import commodity_strategy_agent

    symbols = commodity_strategy_agent.get_symbols()
    if not symbols:
        return []
    rolled: list[str] = []
    changed = False
    for sym in symbols:
        parsed = _parse_mcx_future_symbol(sym)
        root = parsed[0] if parsed else None
        cur = sym
        if root:
            try:
                res = await resolve_upstox_mcx_future(sym)
                if res and res.get("expiry"):
                    cur = _canonical(root, res["expiry"])
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[MCX rollover] resolve failed for {sym}: {exc}")
        if cur != sym:
            changed = True
        rolled.append(cur)
    seen: set[str] = set()
    rolled = [s for s in rolled if not (s in seen or seen.add(s))]
    if changed:
        commodity_strategy_agent.update_symbols(rolled)
        logger.info(f"[MCX rollover] rolled futures → {rolled}")
    return rolled


async def run_daemon(poll_hours: int = 6) -> None:
    logger.info(f"[MCX rollover] daemon starting (poll={poll_hours}h)")
    while True:
        try:
            await roll_forward()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[MCX rollover] failed: {exc}")
        await asyncio.sleep(max(3600, poll_hours * 3600))
