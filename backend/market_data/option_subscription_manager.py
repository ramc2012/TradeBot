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

from api.routers.auth import get_active_adapter
from core.config import settings
from market_data import data_router
from market_data.atm_watchlist import atm_watchlist_service
from market_data.symbols import to_fyers_symbol
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


# Liquidity floor for morning subscription. Pre-open we don't yet have
# today's volume so we lean on OI (carries from prior session). 1000
# lots is conservative-but-realistic for NSE index weeklies — anything
# below this is illiquid enough that the strategy would reject it at
# entry anyway. Yesterday's volume (when available from the watchlist
# snapshot) gates contracts that have OI but no actual trading flow.
MIN_OI_LOTS = 1_000
MIN_PRIOR_VOLUME_LOTS = 100


async def _resolve_fyers_option_symbol(
    underlying: str,
    expiry: str,
    strike: float,
    option_type: str,
    chain_cache: dict[tuple[str, str], dict[tuple[float, str], str]],
) -> str | None:
    """Translate (underlying, expiry, strike, type) → Fyers WS symbol.

    The watchlist exposes Upstox-style instrument keys (NSE_FO|67184)
    that Fyers WS can't subscribe to. Fyers's own option-chain endpoint
    returns the WS-compatible key in OptionChainEntry.instrument_key —
    use that as the canonical source of truth.

    chain_cache is a per-pick scratch dict so we make at most one
    Fyers REST call per (underlying, expiry) combination.
    """
    if not underlying or not expiry or not strike or option_type not in ("CE", "PE"):
        return None
    cache_key = (underlying, expiry)
    if cache_key not in chain_cache:
        adapter = get_active_adapter("fyers")
        if adapter is None or getattr(adapter, "broker_name", "") != "fyers":
            chain_cache[cache_key] = {}
            return None
        try:
            fyers_underlying = to_fyers_symbol(underlying)
            chain = await adapter.get_option_chain(fyers_underlying, expiry)
        except Exception as exc:
            logger.warning(
                f"[OptionWS] Fyers option-chain fetch failed for "
                f"{underlying} {expiry}: {exc}"
            )
            chain_cache[cache_key] = {}
            return None
        index: dict[tuple[float, str], str] = {}
        for entry in chain.entries or []:
            sym = getattr(entry, "instrument_key", None)
            if not sym or ":" not in str(sym):
                continue
            try:
                k = (float(entry.strike), str(entry.option_type or "").upper())
            except (TypeError, ValueError):
                continue
            index[k] = str(sym)
        chain_cache[cache_key] = index
    lookup = chain_cache[cache_key]
    return lookup.get((float(strike), option_type.upper()))


def _passes_liquidity_gate(side: dict[str, Any]) -> tuple[bool, str]:
    """Return (passes, reason) for whether this CE/PE side is liquid
    enough to be worth a websocket slot.

    Allowed when:
      - the watchlist already marked is_liquid = True, OR
      - OI ≥ MIN_OI_LOTS (carryover from prior session), OR
      - prior-session volume ≥ MIN_PRIOR_VOLUME_LOTS

    The watchlist's own liquid-strike picker should already prefer a
    neighbour when the literal ATM is thin, so most contracts should
    pass cleanly. The gate exists to skip the rare case where even the
    picked strike is too thin to support live tick flow.
    """
    if bool(side.get("is_liquid")):
        return True, "is_liquid"
    oi = side.get("oi") or 0
    try:
        oi_lots = float(oi)
    except (TypeError, ValueError):
        oi_lots = 0.0
    if oi_lots >= MIN_OI_LOTS:
        return True, f"oi={int(oi_lots)}"
    volume = side.get("volume") or 0
    try:
        vol_lots = float(volume)
    except (TypeError, ValueError):
        vol_lots = 0.0
    if vol_lots >= MIN_PRIOR_VOLUME_LOTS:
        return True, f"vol={int(vol_lots)}"
    return False, f"thin (oi={int(oi_lots)} vol={int(vol_lots)})"


async def compute_session_option_symbols() -> tuple[list[str], list[str]]:
    """Resolve the option symbols to subscribe for today's session.

    Reads atm_watchlist (no extra broker call) which already knows each
    underlying's nearest active expiry and applies its own liquid-strike
    picker. We then layer one more explicit liquidity gate
    (_passes_liquidity_gate) so we never burn a websocket slot on a
    thin contract that the strategy would reject anyway at entry.

    Returns (kept_symbols, dropped_for_liquidity) so the boot log can
    show exactly which contracts were skipped and why.
    """
    desired: list[str] = []
    skipped: list[str] = []
    try:
        payload = await atm_watchlist_service.get_watchlist(live_refresh=False)
    except Exception as exc:
        logger.warning(f"[OptionWS] watchlist load failed during session pick: {exc}")
        return desired, skipped

    rows = payload.get("rows") or []
    by_underlying: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        underlying = str(row.get("underlying") or "").upper()
        if underlying not in S2_UNDERLYINGS:
            continue
        by_underlying.setdefault(underlying, row)

    # Scratch cache: at most one Fyers REST call per (underlying, expiry).
    chain_cache: dict[tuple[str, str], dict[tuple[float, str], str]] = {}

    for underlying, row in by_underlying.items():
        expiry = str(row.get("expiry") or "").strip()
        for side_key in ("ce", "pe"):
            side = row.get(side_key) or {}
            if not isinstance(side, dict):
                continue
            label = f"{underlying} {side_key.upper()} {side.get('strike')}"
            # Liquidity check first — saves a Fyers REST call for any
            # contract we'd reject anyway.
            passes, why = _passes_liquidity_gate(side)
            if not passes:
                skipped.append(f"{label}  [{why}]")
                continue
            # Source the broker-WS key in this order:
            #   1. live_symbol if the watchlist already populated it
            #      with a Fyers-format string (some chain queries do)
            #   2. else translate via Fyers option-chain lookup using
            #      (underlying, expiry, strike, option_type)
            # Upstox numeric keys (NSE_FO|XXXX) won't work on Fyers WS
            # so we never use them directly.
            broker_key: str | None = None
            candidate = str(side.get("live_symbol") or "").strip()
            if (
                candidate
                and ":" in candidate
                and " " not in candidate
                and not candidate.startswith(("NSE_FO|", "BSE_FO|", "MCX_FO|"))
            ):
                broker_key = candidate
            if not broker_key:
                strike_value = side.get("strike")
                try:
                    strike_float = float(strike_value) if strike_value is not None else None
                except (TypeError, ValueError):
                    strike_float = None
                if strike_float is not None:
                    broker_key = await _resolve_fyers_option_symbol(
                        underlying=underlying,
                        expiry=expiry,
                        strike=strike_float,
                        option_type=side_key.upper(),
                        chain_cache=chain_cache,
                    )
            if not broker_key:
                skipped.append(f"{label}  [no-fyers-symbol resolved]")
                continue
            desired.append(broker_key)

    seen: set[str] = set()
    out: list[str] = []
    for sym in desired:
        if sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out, skipped


async def perform_session_open_pick() -> dict[str, Any]:
    """One-shot at session open: pick today's option subscription set,
    apply it to the broker WS, lock it for the rest of the day.
    """
    global _locked_option_symbols, _locked_for_date

    enabled = _is_enabled()
    desired, skipped = await compute_session_option_symbols()
    today_ist = _now_ist().strftime("%Y-%m-%d")

    current_subs = set(data_router._subscribed_symbols)  # type: ignore[attr-defined]
    # Manage only the option subset — never touch the spot index subs.
    locked_to_drop = [s for s in _locked_option_symbols if s not in desired]
    new_to_add = [s for s in desired if s not in current_subs]

    summary = {
        "enabled": enabled,
        "session_date": today_ist,
        "desired_count": len(desired),
        "skipped_for_liquidity": skipped,
        "current_subs": len(current_subs),
        "to_add": new_to_add,
        "to_remove_from_yesterday": locked_to_drop,
        "applied": False,
    }

    if skipped:
        logger.info(
            f"[OptionWS] {len(skipped)} contract(s) skipped for liquidity: "
            + "; ".join(skipped[:6])
        )

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
        await data_router.remove_subscriptions(locked_to_drop)
    if new_to_add:
        await data_router.add_subscriptions(new_to_add)

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
    # the lock to today. Retry on failure with a short backoff so a
    # transient error doesn't push the next attempt to tomorrow.
    if _market_hours_now() and _locked_for_date != _now_ist().strftime("%Y-%m-%d"):
        for attempt in range(5):
            try:
                await perform_session_open_pick()
                break
            except Exception as exc:
                logger.warning(
                    f"[OptionWS] startup session-open pick failed "
                    f"(attempt {attempt + 1}/5): {exc}"
                )
                await asyncio.sleep(30.0)

    while True:
        # If we're still in market hours and today's pick is missing
        # (all retries above failed), retry quickly. Otherwise sleep
        # until the next scheduled 09:05 IST trigger.
        today_ist = _now_ist().strftime("%Y-%m-%d")
        if _market_hours_now() and _locked_for_date != today_ist:
            sleep_seconds = 60.0
            next_pick_at = _now_ist() + timedelta(seconds=sleep_seconds)
        else:
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
