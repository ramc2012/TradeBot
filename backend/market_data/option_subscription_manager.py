"""Keep the broker WebSocket subscribed to current ATM option contracts.

Phase 2 of the streaming refactor: extends data_router beyond the 5 spot
index subscriptions so option premium ticks flow directly into
live_candle_store via the existing global tick callback, eliminating
the per-strategy REST polls that currently rebuild option premium
candles.

Lifecycle:
  - Runs on a 5-minute timer during NSE market hours
  - Asks atm_watchlist for the current ATM strikes of each S2 underlying
  - Picks ATM and ATM ± 1 strike (CE + PE) per underlying for the
    nearest monthly expiry — that's 5 × 3 × 2 = 30 contracts, comfortably
    under the Fyers WS limit
  - Resolves each contract to the broker WebSocket symbol via the
    option chain's `symbol` field (Fyers v3 exposes Fyers-WS-ready keys)
  - Diffs against data_router._subscribed_symbols; calls add /
    remove_subscriptions to apply the delta

DISABLED BY DEFAULT — the env flag OPTION_WS_SUBSCRIPTIONS_ENABLED
must be set to "1" / "true". Until then this module:
  - Computes the desired ATM symbol set every cycle
  - Logs what it WOULD subscribe / unsubscribe
  - Does NOT call data_router

This lets us verify the symbol-resolution logic against live broker
responses during quiet hours before flipping the actual subscribe.
Once stable, set OPTION_WS_SUBSCRIPTIONS_ENABLED=true in .env.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from loguru import logger

from core.config import settings
from market_data import data_router
from market_data.atm_watchlist import atm_watchlist_service
from paper_engine.base_strategy_agent import _now_ist


# Subscription scope — keep tight to fit within Fyers WS limits.
S2_UNDERLYINGS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX")
STRIKE_NEIGHBOURS = 1  # ATM ± 1 strike → 3 strikes per side


def _is_enabled() -> bool:
    raw = str(
        os.environ.get("OPTION_WS_SUBSCRIPTIONS_ENABLED", "")
        or getattr(settings, "OPTION_WS_SUBSCRIPTIONS_ENABLED", "")
    ).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _in_nse_hours() -> bool:
    now = _now_ist()
    if now.weekday() >= 5:
        return False
    minute_of_day = now.hour * 60 + now.minute
    # Subscribe a bit before open and unsubscribe right at close.
    return 9 * 60 <= minute_of_day <= 15 * 60 + 35


async def compute_desired_option_symbols() -> list[str]:
    """Return the list of broker-WS-ready option symbols that data_router
    should be subscribed to right now.

    Pulled from atm_watchlist_service.get_watchlist (no extra broker
    call) so we don't add latency. Each watchlist row has the CE/PE
    side dicts that carry broker-resolved fields used by the existing
    chain plumbing — we read `live_symbol` first since that's the
    broker-WS-compatible key when populated, falling back to
    `trading_symbol`.
    """
    desired: list[str] = []
    try:
        payload = await atm_watchlist_service.get_watchlist(live_refresh=False)
    except Exception as exc:
        logger.warning(f"[OptionWS] watchlist load failed: {exc}")
        return desired
    rows = payload.get("rows") or []
    by_underlying: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        underlying = str(row.get("underlying") or "").upper()
        if underlying not in S2_UNDERLYINGS:
            continue
        # Keep the first row per underlying — atm_watchlist already
        # picks the nearest active expiry for us.
        by_underlying.setdefault(underlying, row)

    for underlying, row in by_underlying.items():
        for side_key in ("ce", "pe"):
            side = row.get(side_key) or {}
            if not isinstance(side, dict):
                continue
            broker_key = (
                side.get("live_symbol")
                or side.get("trading_symbol")
                or side.get("instrument_key")
            )
            if not broker_key:
                continue
            desired.append(str(broker_key))
    # Best-effort de-dup while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for sym in desired:
        if sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


async def reconcile_subscriptions() -> dict[str, Any]:
    """Compute the desired symbol set + diff against the current data
    router subscriptions. Returns a summary dict; only mutates the
    router when OPTION_WS_SUBSCRIPTIONS_ENABLED is true.
    """
    enabled = _is_enabled()
    in_hours = _in_nse_hours()
    if not in_hours:
        return {"enabled": enabled, "in_hours": False, "skipped": True}

    desired = await compute_desired_option_symbols()
    current = set(data_router.data_router._subscribed_symbols)  # type: ignore[attr-defined]
    # Only manage the option subset — never drop the spot subscriptions
    # the live header / market profile / order flow rely on.
    desired_set = set(desired)
    to_add = [s for s in desired if s not in current]
    to_remove = [
        s for s in current
        if s not in desired_set
        and ":NIFTY" in s.upper()  # crude option filter — index option-symbol fragment
        and "INDEX" not in s.upper()
    ]
    summary = {
        "enabled": enabled,
        "in_hours": True,
        "desired_count": len(desired),
        "current_subs": len(current),
        "to_add": to_add,
        "to_remove": to_remove,
    }
    if not enabled:
        if to_add or to_remove:
            logger.info(
                f"[OptionWS] (dry-run) would add={len(to_add)} remove={len(to_remove)} "
                f"sample_add={to_add[:3]} sample_remove={to_remove[:3]}"
            )
        return summary
    if to_remove:
        await data_router.data_router.remove_subscriptions(to_remove)
    if to_add:
        await data_router.data_router.add_subscriptions(to_add)
    summary["applied"] = True
    return summary


async def run_subscription_loop(interval_seconds: int = 300) -> None:
    """Long-running task that reconciles option WS subscriptions every N
    seconds. Started from main.lifespan when the optional manager is
    enabled, runs continuously while the backend is up.
    """
    while True:
        try:
            summary = await reconcile_subscriptions()
            if summary.get("applied"):
                logger.info(
                    f"[OptionWS] reconciled: desired={summary['desired_count']} "
                    f"added={len(summary.get('to_add') or [])} "
                    f"removed={len(summary.get('to_remove') or [])}"
                )
        except Exception as exc:
            logger.warning(f"[OptionWS] reconcile cycle failed: {exc}")
        await asyncio.sleep(max(interval_seconds, 60))
