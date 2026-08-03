"""Live position-mark overlay.

The strategy agents refresh a position's ``current_price`` only on their
60-second scan. The watchlist LTP, by contrast, streams sub-second off the
tick feed. This module bridges the two: it overlays the freshest live tick
onto open-position dicts in the WebSocket payload layer so the dashboard's
P&L updates per tick instead of per scan.

Design
------
* The held-position **subscription refresh** (in
  :mod:`market_data.option_subscription_manager`) resolves each open NSE
  option leg to its Fyers/app symbol and registers the mapping here via
  :func:`register_position_symbol`. That same routine subscribes the leg to
  the broker WS so it actually ticks.
* The WS payload factories call :func:`overlay_live_marks`, which looks up
  each position's registered app symbol, fetches the freshest mark from
  ``data_router.get_live_mark`` (in-process buffer → Redis last-value), and
  recomputes ``current_price`` / ``unrealized_pnl`` / ``return_pct`` /
  ``notional_value`` from the position's own side + qty + entry.

Safety
------
* If a position has no registered symbol, no live tick, or a stale tick
  (older than the freshness budget inside ``get_live_mark``), the position is
  left exactly as the agent serialized it — the scan-cadence mark. There is
  no failure mode that produces a *wrong* mark; the worst case is the old
  60-second behaviour.
* The overlay never mutates agent state — it operates on the serialized
  dicts the WS is about to send.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from loguru import logger

IST = timezone(timedelta(hours=5, minutes=30))


# position identity (its `symbol` field, e.g. "OPT:NIFTY:2026-06-30:24000:PE"
# or "MCX:NATURALGAS26JUNFUT") -> feed app symbol (e.g. "NSE:NIFTY26JUN24000PE")
_APP_SYMBOL_BY_POSITION: dict[str, str] = {}

# Long-side markers across the desks: NSE option positions are long-premium
# (CE/PE), commodity futures carry an explicit BUY/SELL action.
_LONG_VALUES = {"BUY", "CE", "LONG", "B"}

# Max allowed ratio between a live tick and the agent's scan-cadence reference
# before the tick is treated as broker-feed corruption and discarded. A real
# 30-DTE option premium barely moves between two 60s scans; the observed
# cross-wired values are index-magnitude (7x+). 4x cleanly separates them
# while leaving generous headroom for legitimate fast moves.
MAX_LIVE_DIVERGENCE_RATIO = 4.0


def register_position_symbol(position_symbol: str, app_symbol: str) -> None:
    """Map a position's identity to the feed app symbol that prices it."""
    ps = str(position_symbol or "").strip()
    app = str(app_symbol or "").strip()
    if ps and app:
        _APP_SYMBOL_BY_POSITION[ps] = app


def registered_app_symbol(position_symbol: str) -> Optional[str]:
    return _APP_SYMBOL_BY_POSITION.get(str(position_symbol or "").strip())


def prune_registry(active_position_symbols: Iterable[str]) -> None:
    """Drop registry entries for positions that have since closed."""
    active = {str(s or "").strip() for s in active_position_symbols}
    for key in list(_APP_SYMBOL_BY_POSITION.keys()):
        if key not in active:
            _APP_SYMBOL_BY_POSITION.pop(key, None)


def _is_long(side: Any) -> bool:
    return str(side or "").strip().upper() in _LONG_VALUES


async def overlay_live_marks(
    positions: list[dict[str, Any]],
    *,
    side_field: str = "action",
    symbol_field: str = "symbol",
    extra_symbol_fields: tuple[str, ...] = ("live_symbol", "trading_symbol"),
    max_age_seconds: float = 30.0,
    force_long: bool = False,
) -> list[dict[str, Any]]:
    """Return ``positions`` with live LTP + recomputed P&L where available.

    Mutates the dicts in place (they're freshly serialized per WS push) and
    also returns the list for convenience. Adds a ``mark_source`` field:
    ``"live_tick"`` when a fresh tick was applied, otherwise left unset so the
    UI can show whether a row is streaming or scan-marked.

    ``force_long=True`` treats every position as long (multiplier +1). Use it
    for long-premium option books (NSE S1/S2) where both CE and PE legs are
    *bought* — there the ``option_type`` field is CE/PE, not a trade
    direction, so the BUY/SELL heuristic doesn't apply. ``force_long=False``
    (default) reads ``side_field`` for a BUY/SELL direction (commodity futures).
    """
    if not positions:
        return positions
    # Local import to avoid a module-load cycle (data_router pulls in broker
    # adapters which can be heavy).
    from market_data.data_router import data_router

    for pos in positions:
        if not isinstance(pos, dict):
            continue
        # Resolve the feed symbol: prefer the registered mapping, then any
        # symbol field that already looks like a feed app symbol.
        candidates: list[str] = []
        primary = str(pos.get(symbol_field) or "").strip()
        if primary:
            mapped = registered_app_symbol(primary)
            if mapped:
                candidates.append(mapped)
            candidates.append(primary)
        for fld in extra_symbol_fields:
            val = str(pos.get(fld) or "").strip()
            if val:
                candidates.append(val)

        live: Optional[float] = None
        for sym in candidates:
            if not sym:
                continue
            try:
                live = await data_router.get_live_mark(sym, max_age_seconds=max_age_seconds)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[live_marks] get_live_mark failed for {sym}: {exc}")
                live = None
            if live:
                break
        if not live:
            continue

        try:
            entry = float(pos.get("entry_price") or pos.get("entry_premium") or 0.0)
            qty = float(
                pos.get("qty")
                or pos.get("quantity_units")
                or pos.get("quantity")
                or 0.0
            )
        except (TypeError, ValueError):
            continue

        # Sanity guard against broker-WS cross-wiring. The Fyers feed
        # occasionally attributes one instrument's value to another option
        # symbol (e.g. a deep-OTM stock put showing an index's spot price —
        # observed: KPITTECH 770 PE reading 702 = NIFTY 24000 PE's value).
        # The cross-wired values are index-magnitude, always many multiples
        # of a real option premium, whereas a genuine premium move between
        # the agent's 60s scans is small. Reject any live mark that diverges
        # from the agent's reliable reference (the price the agent just
        # serialized) by more than MAX_LIVE_DIVERGENCE_RATIO, and keep the
        # scan-cadence price instead. The reference must be > 0 to compare.
        ref_price = 0.0
        try:
            ref_price = float(
                pos.get("current_price")
                or pos.get("latest_premium")
                or 0.0
            )
        except (TypeError, ValueError):
            ref_price = 0.0
        if ref_price > 0 and (
            live > ref_price * MAX_LIVE_DIVERGENCE_RATIO
            or live < ref_price / MAX_LIVE_DIVERGENCE_RATIO
        ):
            logger.debug(
                f"[live_marks] rejected cross-wired mark for {primary}: "
                f"live={live} vs ref={ref_price} (ratio guard)"
            )
            pos["mark_source"] = "scan_guarded"
            continue

        mult = 1.0 if (force_long or _is_long(pos.get(side_field))) else -1.0
        pos["current_price"] = round(live, 4)
        if "latest_premium" in pos or "entry_premium" in pos:
            pos["latest_premium"] = round(live, 4)
        pos["unrealized_pnl"] = round(mult * (live - entry) * qty, 2)
        pos["return_pct"] = (
            round(mult * ((live - entry) / entry) * 100.0, 2) if entry else 0.0
        )
        pos["notional_value"] = round(live * qty, 2)
        pos["mark_source"] = "live_tick"
        # Stamp WHEN this mark was taken. Without it the payload carried a
        # live current_price next to the LAST SCAN's price_updated_at, so a
        # tick-fresh mark read as minutes old (observed 2026-08-03: prices
        # advancing every poll while the stamp sat frozen at 09:17). The mark
        # is at most `max_age_seconds` old by get_live_mark's own contract.
        pos["price_updated_at"] = datetime.now(IST).isoformat()
    return positions


async def overlay_nse_agent_status(status: dict[str, Any]) -> dict[str, Any]:
    """Overlay live marks onto the NSE strategy-agent status payload in place.

    Walks ``status["strategies"][*]["positions"]`` (long-premium option legs)
    and applies :func:`overlay_live_marks` with ``force_long=True``. Safe on
    any shape — silently does nothing if the structure isn't present.
    """
    if not isinstance(status, dict):
        return status
    strategies = status.get("strategies")
    if not isinstance(strategies, list):
        return status
    for strat in strategies:
        if isinstance(strat, dict) and isinstance(strat.get("positions"), list):
            await overlay_live_marks(strat["positions"], force_long=True)
    return status


async def overlay_watchlist_live_marks(
    rows: list[dict[str, Any]],
    *,
    ltp_field: str = "ltp",
    symbol_fields: tuple[str, ...] = ("trading_symbol", "instrument_key", "option_key"),
    max_age_seconds: float = 30.0,
) -> list[dict[str, Any]]:
    """Overlay live ticks onto S1 watchlist rows in place.

    The watchlist LTP is otherwise read straight from the periodic
    ``atm_option_watchlist_snapshots`` write (minutes-cadence). For any row
    whose option leg is on the WS tick feed (the S1 ATM index legs are
    subscribed by ``refresh_held_position_subscriptions``), this swaps in
    the live tick so the desk streams instead of step-changing on snapshot
    writes.

    A ratio guard rejects cross-wired ticks (Fyers occasionally attributes
    an index spot value to an option key). ``change_pct`` is recomputed off
    the row's prior-close anchor when available. Adds
    ``mark_source="live_tick"`` to rows it updated; ``"scan_guarded"`` to
    rows whose live tick failed the ratio guard.
    """
    if not rows:
        return rows
    from market_data.data_router import data_router

    for row in rows:
        if not isinstance(row, dict):
            continue
        candidates: list[str] = []
        for fld in symbol_fields:
            val = str(row.get(fld) or "").strip()
            if not val:
                continue
            mapped = registered_app_symbol(val)
            if mapped:
                candidates.append(mapped)
            candidates.append(val)

        live: Optional[float] = None
        for sym in candidates:
            if not sym:
                continue
            try:
                live = await data_router.get_live_mark(sym, max_age_seconds=max_age_seconds)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[live_marks] watchlist get_live_mark failed for {sym}: {exc}")
                live = None
            if live:
                break
        if not live or live <= 0:
            continue

        # Ratio guard against cross-wired ticks — compare to the snapshot LTP
        # the row already carries. Real intra-snapshot premium moves are small.
        try:
            ref = float(row.get(ltp_field) or 0.0)
        except (TypeError, ValueError):
            ref = 0.0
        if ref > 0 and (
            live > ref * MAX_LIVE_DIVERGENCE_RATIO
            or live < ref / MAX_LIVE_DIVERGENCE_RATIO
        ):
            row["mark_source"] = "scan_guarded"
            continue

        # Recompute change_pct off a prior-close anchor when present.
        prev_close = None
        for anchor_field in ("prev_close", "previous_close", "ltp_open"):
            raw = row.get(anchor_field)
            try:
                prev_close = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                prev_close = None
            if prev_close:
                break

        row[ltp_field] = round(live, 4)
        if prev_close and prev_close > 0:
            row["change_pct"] = round((live - prev_close) / prev_close * 100.0, 2)
        row["mark_source"] = "live_tick"
    return rows
