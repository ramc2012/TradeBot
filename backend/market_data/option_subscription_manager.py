"""Pre-market option subscription manager — pick ATM strikes once,
subscribe for the day, leave them alone.

Rationale (corrected from the prior 5-min reconciliation design):

  * Picking ATM is a SPARSE decision — once per session, anchored on
    the pre-open / overnight spot. Re-picking every 5 minutes churns
    the broker WebSocket and produces overlapping CE/PE histories
    that confuse downstream MACD computation.

  * Liquidity check happens at TRADE ENTRY, not at subscription time.
    atm_watchlist's liquid-strike picker (±1 neighbour with 1.5×
    volume lift) handles "this specific strike is too thin, pick the
    next one" inside the strategy entry path. The subscription layer
    just needs to make sure the candidate strikes are streaming.

  * Subscription scope = ATM ± 1 strike per S2 underlying. Three
    strikes × CE+PE × 5 indices = 30 contracts — comfortably under
    Fyers WS limits and gives the strategy three liquid candidates
    per side to choose from at trade time.

Lifecycle:
  - Backend startup during market hours: reconcile once immediately,
    using the current spot as the ATM anchor.
  - Scheduled daily at 09:05 IST: pick ATM strikes for the new
    session based on the pre-open spot snapshot, reconcile WS subs.
  - No mid-session reconcile — strikes locked for the day even as
    spot moves. The strategy entry path picks the right strike from
    the locked set.

Disabled by default behind OPTION_WS_SUBSCRIPTIONS_ENABLED. Until set
to "true", the manager logs what it WOULD subscribe each session-open
but does not call the broker WS.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from typing import Any

from loguru import logger

from core.config import settings
from market_data import data_router
from market_data.atm_watchlist import atm_watchlist_service
from paper_engine.base_strategy_agent import _now_ist


# Subscription scope — keep tight to fit within Fyers WS limits.
S2_UNDERLYINGS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX")

# Session-open trigger time (IST). 09:05 sits between the BSE pre-open
# auction and the regular session opening tick, giving us a spot anchor
# from the broker without racing the very first market tick.
SESSION_OPEN_PICK_HOUR = 9
SESSION_OPEN_PICK_MINUTE = 5

# Holds the option symbols we locked at the morning's session-open pick
# so we don't re-pick mid-day even when the spot moves and atm_watchlist
# starts returning a different ATM strike.
_locked_option_symbols: set[str] = set()
_locked_for_date: str | None = None


def _is_enabled() -> bool:
    raw = str(
        os.environ.get("OPTION_WS_SUBSCRIPTIONS_ENABLED", "")
        or getattr(settings, "OPTION_WS_SUBSCRIPTIONS_ENABLED", "")
    ).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _market_hours_now() -> bool:
    now = _now_ist()
    if now.weekday() >= 5:
        return False
    minute_of_day = now.hour * 60 + now.minute
    return 9 * 60 <= minute_of_day <= 15 * 60 + 35


def _next_session_open_pick() -> datetime:
    """Return the next IST datetime at which we should re-pick ATM.

    Always SESSION_OPEN_PICK_HOUR:MM today if that's still in the future,
    otherwise the same time on the next trading day.
    """
    now = _now_ist()
    target = now.replace(
        hour=SESSION_OPEN_PICK_HOUR,
        minute=SESSION_OPEN_PICK_MINUTE,
        second=0,
        microsecond=0,
    )
    if target <= now:
        target = target + timedelta(days=1)
    while target.weekday() >= 5:
        target = target + timedelta(days=1)
    return target


async def compute_session_option_symbols() -> list[str]:
    """Resolve the ATM±1 option symbols to subscribe for today's session.

    Reads atm_watchlist (no extra broker call) which already knows each
    underlying's nearest active expiry and its ATM-anchored CE/PE pair.
    Falls back gracefully when a row is missing — we just skip that
    underlying rather than block the whole pick.
    """
    desired: list[str] = []
    try:
        payload = await atm_watchlist_service.get_watchlist(live_refresh=False)
    except Exception as exc:
        logger.warning(f"[OptionWS] watchlist load failed during session pick: {exc}")
        return desired

    rows = payload.get("rows") or []
    by_underlying: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        underlying = str(row.get("underlying") or "").upper()
        if underlying not in S2_UNDERLYINGS:
            continue
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

    seen: set[str] = set()
    out: list[str] = []
    for sym in desired:
        if sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


async def perform_session_open_pick() -> dict[str, Any]:
    """One-shot at session open: pick today's option subscription set,
    apply it to the broker WS, lock it for the rest of the day.
    """
    global _locked_option_symbols, _locked_for_date

    enabled = _is_enabled()
    desired = await compute_session_option_symbols()
    today_ist = _now_ist().strftime("%Y-%m-%d")

    current_subs = set(data_router.data_router._subscribed_symbols)  # type: ignore[attr-defined]
    # Manage only the option subset — never touch the spot index subs.
    locked_to_drop = [s for s in _locked_option_symbols if s not in desired]
    new_to_add = [s for s in desired if s not in current_subs]

    summary = {
        "enabled": enabled,
        "session_date": today_ist,
        "desired_count": len(desired),
        "current_subs": len(current_subs),
        "to_add": new_to_add,
        "to_remove_from_yesterday": locked_to_drop,
        "applied": False,
    }

    if not enabled:
        logger.info(
            f"[OptionWS] session-open pick (DRY-RUN) date={today_ist} "
            f"desired={len(desired)} would_add={len(new_to_add)} "
            f"yesterday_locked={len(_locked_option_symbols)} sample={desired[:4]}"
        )
        _locked_option_symbols = set(desired)
        _locked_for_date = today_ist
        return summary

    if locked_to_drop:
        await data_router.data_router.remove_subscriptions(locked_to_drop)
    if new_to_add:
        await data_router.data_router.add_subscriptions(new_to_add)

    _locked_option_symbols = set(desired)
    _locked_for_date = today_ist
    summary["applied"] = True
    logger.info(
        f"[OptionWS] session-open pick applied date={today_ist} "
        f"locked={len(desired)} added={len(new_to_add)} removed={len(locked_to_drop)}"
    )
    return summary


async def run_subscription_loop() -> None:
    """Long-running task: pick once at session open daily, otherwise idle.

    On backend startup mid-session, picks immediately so we don't wait
    until tomorrow to start streaming options. Then sleeps until the
    next 09:05 IST trigger and repeats.
    """
    # If we boot during market hours, do the pick right now and bind
    # the lock to today.
    if _market_hours_now() and _locked_for_date != _now_ist().strftime("%Y-%m-%d"):
        try:
            await perform_session_open_pick()
        except Exception as exc:
            logger.warning(f"[OptionWS] startup session-open pick failed: {exc}")

    while True:
        next_pick_at = _next_session_open_pick()
        sleep_seconds = max(60.0, (next_pick_at - _now_ist()).total_seconds())
        logger.info(
            f"[OptionWS] next session-open pick scheduled for "
            f"{next_pick_at.isoformat()} (sleeping {int(sleep_seconds)}s)"
        )
        try:
            await asyncio.sleep(sleep_seconds)
        except asyncio.CancelledError:
            raise
        try:
            await perform_session_open_pick()
        except Exception as exc:
            logger.warning(f"[OptionWS] scheduled session-open pick failed: {exc}")
