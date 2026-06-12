"""WebSocket endpoint for real-time tick streaming via Redis pub/sub."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic

from fastapi.encoders import jsonable_encoder
from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger
from starlette.websockets import WebSocketState

from api.routers.auth import authenticate_websocket_client
from db.redis_client import get_redis


@dataclass
class _SnapshotCacheEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    payload: object | None = None
    encoded: str | None = None
    expires_at: float = 0.0


_snapshot_cache_lock = asyncio.Lock()
_snapshot_cache: dict[str, _SnapshotCacheEntry] = {}


def _is_socket_closed_error(exc: Exception) -> bool:
    return isinstance(exc, RuntimeError) and "Cannot call \"send\" once a close message has been sent" in str(exc)


def _socket_is_connected(websocket: WebSocket) -> bool:
    return (
        websocket.client_state == WebSocketState.CONNECTED
        and websocket.application_state == WebSocketState.CONNECTED
    )


async def _get_snapshot_cache_entry(channel: str) -> _SnapshotCacheEntry:
    async with _snapshot_cache_lock:
        entry = _snapshot_cache.get(channel)
        if entry is None:
            entry = _SnapshotCacheEntry()
            _snapshot_cache[channel] = entry
        return entry


async def _get_snapshot_payload(
    *,
    channel: str,
    payload_factory,
    cache_ttl_seconds: float,
) -> tuple[object, str]:
    entry = await _get_snapshot_cache_entry(channel)
    now = monotonic()
    if entry.payload is not None and entry.encoded is not None and entry.expires_at > now:
        return entry.payload, entry.encoded

    async with entry.lock:
        now = monotonic()
        if entry.payload is not None and entry.encoded is not None and entry.expires_at > now:
            return entry.payload, entry.encoded

        try:
            payload = await payload_factory()
            encoded = json.dumps(jsonable_encoder(payload), separators=(",", ":"), sort_keys=True)
            entry.payload = payload
            entry.encoded = encoded
            entry.expires_at = now + max(cache_ttl_seconds, 0.5)
            return payload, encoded
        except Exception:
            if entry.payload is not None and entry.encoded is not None:
                entry.expires_at = now + 1.0
                logger.warning(f"[WS] Reusing stale snapshot for {channel} after refresh failure")
                return entry.payload, entry.encoded
            raise


async def _accept_authenticated_socket(websocket: WebSocket, channel: str) -> dict:
    claims = authenticate_websocket_client(websocket)
    await websocket.accept()
    logger.info(f"[WS] Authenticated client subscribed to {channel}")
    return claims


async def _close_pubsub(pubsub) -> None:
    """Release a pub/sub's dedicated Redis connection on disconnect.

    unsubscribe() alone does NOT free the underlying connection — without this every
    WS disconnect leaks one Redis client, and a market day of UI reconnects eventually
    exhausts Redis maxclients ('max number of clients reached'), breaking tick pub/sub.
    """
    for name in ("aclose", "close"):
        closer = getattr(pubsub, name, None)
        if closer is None:
            continue
        try:
            result = closer()
            if hasattr(result, "__await__"):
                await result
        except Exception:
            pass
        return


async def ws_ticks(websocket: WebSocket, symbol: str):
    """Stream real-time ticks for a symbol via Redis pub/sub."""
    await _accept_authenticated_socket(websocket, f"ticks:{symbol}")
    pubsub = None
    try:
        redis = await get_redis()
        pubsub = redis.pubsub()
        await pubsub.subscribe(f"ticks:{symbol}")
    except Exception as exc:
        logger.warning(f"[WS] Redis tick stream unavailable for {symbol}; using snapshot fallback: {exc}")
        if pubsub is not None:
            await _close_pubsub(pubsub)  # subscribe() failed after pubsub() — release it or it leaks
        await _stream_tick_snapshot_fallback(websocket, symbol)
        return

    last_payload: str | None = None
    try:
        while _socket_is_connected(websocket):
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message and message["type"] == "message":
                data = message["data"]
                payload = data if isinstance(data, str) else data.decode()
            else:
                payload = await _tick_snapshot_payload(symbol)
            if payload != last_payload:
                await websocket.send_text(payload)
                last_payload = payload
    except WebSocketDisconnect:
        logger.info(f"[WS] Client disconnected from ticks:{symbol}")
    except Exception as e:
        logger.error(f"[WS] Error in ticks handler: {e}")
    finally:
        try:
            await pubsub.unsubscribe(f"ticks:{symbol}")
        except Exception:
            pass
        await _close_pubsub(pubsub)  # release the connection or it leaks → Redis maxclients


async def _tick_snapshot_payload(symbol: str) -> str:
    from api.routers.market import _latest_index_tick_snapshot
    from market_data import data_router

    snapshot = await _latest_index_tick_snapshot(
        symbol,
        data_router.get_ltp(symbol),
        source="data_router",
    )
    return json.dumps(
        {
            "symbol": symbol,
            "ltp": snapshot.ltp,
            "open": snapshot.open,
            "high": snapshot.high,
            "low": snapshot.low,
            "close": snapshot.close,
            "volume": snapshot.volume,
            "oi": snapshot.oi,
            "timestamp": snapshot.timestamp or datetime.now(timezone.utc).isoformat(),
            "source": snapshot.source,
            "stale": snapshot.stale,
            "stale_seconds": snapshot.stale_seconds,
        },
        separators=(",", ":"),
    )


async def _stream_tick_snapshot_fallback(websocket: WebSocket, symbol: str) -> None:
    """Keep ticker clients fed when Redis pub/sub is unavailable."""
    last_payload: str | None = None
    try:
        while True:
            payload = await _tick_snapshot_payload(symbol)
            if not _socket_is_connected(websocket):
                break
            if payload != last_payload:
                await websocket.send_text(payload)
                last_payload = payload
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        logger.info(f"[WS] Client disconnected from fallback ticks:{symbol}")
    except Exception as exc:
        if not _is_socket_closed_error(exc):
            logger.error(f"[WS] Fallback tick stream failed for {symbol}: {exc}")


async def _stream_snapshot(
    websocket: WebSocket,
    *,
    channel: str,
    interval_seconds: float,
    payload_factory,
    cache_ttl_seconds: float | None = None,
):
    """Push snapshot payloads only when the encoded value changes."""
    await _accept_authenticated_socket(websocket, channel)
    last_payload: str | None = None
    effective_cache_ttl = interval_seconds if cache_ttl_seconds is None else cache_ttl_seconds

    try:
        while True:
            try:
                _, encoded = await _get_snapshot_payload(
                    channel=channel,
                    payload_factory=payload_factory,
                    cache_ttl_seconds=effective_cache_ttl,
                )
                if encoded != last_payload:
                    if not _socket_is_connected(websocket):
                        break
                    await websocket.send_text(encoded)
                    last_payload = encoded
            except WebSocketDisconnect:
                logger.info(f"[WS] Client disconnected from {channel}")
                break
            except Exception as e:
                if _is_socket_closed_error(e):
                    logger.debug(f"[WS] Snapshot channel {channel} closed during send")
                    break
                logger.error(f"[WS] Error refreshing {channel}: {e}")
            await asyncio.sleep(interval_seconds)
    except WebSocketDisconnect:
        logger.info(f"[WS] Client disconnected from {channel}")
    except Exception as e:
        logger.error(f"[WS] Error in {channel}: {e}")


async def ws_positions(websocket: WebSocket):
    """Stream real-time position P&L updates."""
    await _accept_authenticated_socket(websocket, "positions")
    try:
        while True:
            from api.routers.trading import _get_or_create_paper_session
            _, _, portfolio = await _get_or_create_paper_session()
            data = json.dumps({
                "positions": portfolio.get_positions_list(),
                "summary": portfolio.get_summary(),
            })
            await websocket.send_text(data)
            await asyncio.sleep(1)  # push every second
    except WebSocketDisconnect:
        logger.info("[WS] Client disconnected from positions")
    except Exception as e:
        logger.error(f"[WS] Positions error: {e}")


async def ws_layout(websocket: WebSocket):
    """Stream broker status plus portfolio summary for the app shell."""

    async def payload_factory():
        from api.routers.auth import broker_status
        from api.routers.trading import get_mode, portfolio_summary

        return {
            "broker_status": await broker_status(),
            "portfolio_summary": await portfolio_summary(),
            "trading_mode": await get_mode(),
        }

    await _stream_snapshot(
        websocket,
        channel="layout",
        interval_seconds=5.0,
        payload_factory=payload_factory,
    )


async def ws_system_overview(websocket: WebSocket):
    """Stream homepage system overview snapshots."""

    async def payload_factory():
        from api.routers.system import system_overview

        return await system_overview()

    await _stream_snapshot(
        websocket,
        channel="system_overview",
        interval_seconds=10.0,
        payload_factory=payload_factory,
    )


async def ws_system_health(websocket: WebSocket):
    """Stream health page payloads."""

    async def payload_factory():
        from api.routers.system import system_health

        return await system_health()

    await _stream_snapshot(
        websocket,
        channel="system_health",
        interval_seconds=15.0,
        payload_factory=payload_factory,
    )


async def ws_strategy_overview(websocket: WebSocket):
    """Stream the strategy supervisor page snapshot."""

    async def payload_factory():
        from api.routers.auth import broker_status
        from api.routers.strategy import (
            get_agent_comments,
            get_data_status,
            get_open_signals,
            get_portfolio_stats,
        )
        from api.routers.trading import strategy_agent_status
        from market_data.live_marks import overlay_nse_agent_status

        return {
            "agent_status": await overlay_nse_agent_status(await strategy_agent_status()),
            "open_signals": await get_open_signals(),
            "comments": await get_agent_comments(),
            "brokers": await broker_status(),
            "pipeline": await get_data_status(),
            "live_portfolio": await get_portfolio_stats(),
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
        }

    await _stream_snapshot(
        websocket,
        channel="strategy_overview",
        interval_seconds=5.0,
        payload_factory=payload_factory,
    )


async def ws_strategy_dashboard(websocket: WebSocket):
    """Stream the NSE strategy execution desk snapshot."""

    async def payload_factory():
        from api.routers.trading import (
            get_kill_switch_state,
            get_orders,
            risk_status,
            strategy_agent_status,
            strategy_equity_history,
        )

        from market_data.live_marks import overlay_nse_agent_status

        return {
            "agent_status": await overlay_nse_agent_status(await strategy_agent_status()),
            "kill_switch_state": await get_kill_switch_state(),
            "orders": await get_orders(),
            "risk_status": await risk_status(),
            "equity_curves": await strategy_equity_history(),
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
        }

    await _stream_snapshot(
        websocket,
        channel="strategy_dashboard",
        interval_seconds=2.0,
        payload_factory=payload_factory,
    )


async def _build_positions_overview_structure() -> dict:
    """Gather the combined positions snapshot from all desks (NO live overlay).

    This is the expensive part (7 source reads) — it runs only on the 2s
    heartbeat / on connect, so DB load is unchanged from the old 2s cadence.
    Live marks are applied separately (cheap) on each tick.
    """
    from api.routers.commodity import commodity_strategy_status
    from api.routers.auction_intelligence import paper_positions as auction_paper_positions
    from api.routers.cbe import cbe_paper_positions
    from api.routers.directional_options import paper_positions as directional_paper_positions
    from api.routers.fractal_market_profile import fractal_market_profile_paper_positions
    from api.routers.gann_tp_delta import paper_agent_status as gann_paper_agent_status
    from api.routers.trading import get_positions, strategy_agent_status

    errors: dict[str, str] = {}

    async def settle(key: str, task):
        try:
            return await task
        except Exception as exc:  # pragma: no cover - defensive stream isolation
            errors[key] = str(exc)
            logger.warning(f"[WS] positions overview source failed: {key}: {exc}")
            return None

    (
        manual_positions, nse_status, commodity_status, directional_positions,
        gann_status, auction_positions, fractal_positions, cbe_positions,
    ) = await asyncio.gather(
        settle("manual", get_positions()),
        settle("nse", strategy_agent_status()),
        settle("commodity", commodity_strategy_status()),
        settle("directional", directional_paper_positions(symbol=None, status="all", limit=100)),
        settle("gann", gann_paper_agent_status(limit=100)),
        settle("auction", auction_paper_positions(symbol=None, status="all", limit=100)),
        settle("fractal", fractal_market_profile_paper_positions(symbol=None, status="all", limit=100)),
        settle("cbe", cbe_paper_positions(status="all", limit=100)),
    )
    return _slim_positions_overview_structure({
        "manual": manual_positions, "nse": nse_status, "commodity": commodity_status,
        "directional": directional_positions, "gann": gann_status,
        "auction": auction_positions, "fractal": fractal_positions,
        "cbe": cbe_positions, "errors": errors,
    })


# The combined stream is re-encoded once per second per client, so payload size
# IS the CPU bill (2026-06-12: an unslimmed 5-10MB frame pegged the core). The
# positions/analytics pages and the desk bridges read only positions, summaries
# and a recent-trades tail from this socket — every desk's full detail rides its
# own per-desk socket, so the heavy fields below are dead weight HERE.
_POSITIONS_OVERVIEW_TRADE_TAIL = 40
_POSITIONS_OVERVIEW_CLOSED_CAP = 30
_POSITIONS_OVERVIEW_NSE_STRATEGY_STRIP = (
    "meta", "audit_lanes", "signals", "historical_trades", "recent_events", "today_trades",
)
_POSITIONS_OVERVIEW_COMMODITY_STRIP = (
    "watchlist", "futures_watchlist", "signal_audit", "data_health",
    "historical_trades", "profile_history",
)


def _scalar_row(row):
    """Keep every scalar field of a position row, drop nested containers."""
    if not isinstance(row, dict):
        return row
    return {k: v for k, v in row.items() if not isinstance(v, (dict, list))}


def _cap_recent(rows: list, cap: int) -> list:
    """Cap a trade/position list to its most-recent entries regardless of order."""
    if len(rows) <= cap:
        return rows
    first = rows[0] if isinstance(rows[0], dict) else {}
    last = rows[-1] if isinstance(rows[-1], dict) else {}
    first_ts = str(first.get("closed_at") or first.get("exit_time") or first.get("entered_at") or "")
    last_ts = str(last.get("closed_at") or last.get("exit_time") or last.get("entered_at") or "")
    return rows[:cap] if first_ts >= last_ts else rows[-cap:]


def _slim_positions_overview_structure(structure: dict) -> dict:
    nse = structure.get("nse")
    if isinstance(nse, dict):
        nse = {**nse}  # shallow-copy before mutating — router payloads may be shared
        nse.pop("data_health", None)
        slim_strats = []
        for strat in nse.get("strategies") or []:
            if not isinstance(strat, dict):
                slim_strats.append(strat)
                continue
            strat = {k: v for k, v in strat.items() if k not in _POSITIONS_OVERVIEW_NSE_STRATEGY_STRIP}
            th = strat.get("trade_history")
            if isinstance(th, list):
                strat["trade_history"] = _cap_recent(th, _POSITIONS_OVERVIEW_TRADE_TAIL)
            slim_strats.append(strat)
        nse["strategies"] = slim_strats
        structure["nse"] = nse

    commodity = structure.get("commodity")
    if isinstance(commodity, dict):
        commodity = {k: v for k, v in commodity.items() if k not in _POSITIONS_OVERVIEW_COMMODITY_STRIP}
        th = commodity.get("trade_history")
        if isinstance(th, list):
            commodity["trade_history"] = _cap_recent(th, _POSITIONS_OVERVIEW_TRADE_TAIL)
        structure["commodity"] = commodity

    for lane in ("directional", "gann", "auction", "fractal", "cbe"):
        payload = structure.get(lane)
        if not isinstance(payload, dict):
            continue
        payload = {**payload}
        closed = payload.get("closed_positions")
        if isinstance(closed, list):
            payload["closed_positions"] = [_scalar_row(r) for r in _cap_recent(closed, _POSITIONS_OVERVIEW_CLOSED_CAP)]
        opened = payload.get("open_positions")
        if isinstance(opened, list):
            payload["open_positions"] = [_scalar_row(r) for r in opened]
        structure[lane] = payload
    return structure


async def _overlay_positions_overview(structure: dict) -> dict:
    """Re-mark the cached structure to live prices (cheap — reads hot-cache only).

    NSE legs are long-premium (force_long); commodity carries a BUY/SELL action.
    Both overlays mutate in place and are idempotent (recompute P&L from entry vs
    fresh mark), so re-running them on each tick streams live P&L. Legs without a
    live tick keep their scan-cadence mark.
    """
    from market_data.live_marks import overlay_nse_agent_status, overlay_live_marks

    nse_status = structure.get("nse")
    commodity_status = structure.get("commodity")
    if isinstance(nse_status, dict):
        await overlay_nse_agent_status(nse_status)
    if isinstance(commodity_status, dict) and isinstance(commodity_status.get("positions"), list):
        await overlay_live_marks(commodity_status["positions"], side_field="action")

    return {
        "manual": structure.get("manual"),
        # NB: the old "strategy" alias duplicated the full nse status in every
        # frame; all consumers read payload.nse first, so the alias is dropped.
        "nse": nse_status,
        "commodity": commodity_status,
        "directional": structure.get("directional"),
        "gann": structure.get("gann"),
        "auction": structure.get("auction"),
        "fractal": structure.get("fractal"),
        "cbe": structure.get("cbe"),
        "errors": structure.get("errors", {}),
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
    }


async def ws_positions_overview(websocket: WebSocket):
    """Event-driven combined positions stream.

    Structure (which positions exist) is rebuilt on a 2s heartbeat — same DB load
    as the old timer. Live P&L marks are re-applied within ≤0.4s of ANY tick (via
    the quotes:bus), so open-position P&L is sub-second instead of 2s. Identical
    frames are de-duplicated (ignoring the fetchedAt clock). Degrades to a 2s timer
    if Redis pub/sub is unavailable.
    """
    await _accept_authenticated_socket(websocket, "positions_overview")

    def _encode_core(payload: dict) -> str:
        # ONE jsonable_encoder pass over everything except the fetchedAt clock —
        # this string is both the dedup key and the wire-frame body. The payload
        # spans all 8 lanes (S1 status alone carries the trade book), so this
        # encode costs hundreds of ms at book scale.
        core = {k: v for k, v in payload.items() if k != "fetchedAt"}
        return json.dumps(jsonable_encoder(core), separators=(",", ":"))

    from market_data.quote_bus import QUOTES_BUS_CHANNEL

    structure: dict | None = None
    last_build = 0.0
    last_dedup: str | None = None
    last_emit_attempt = 0.0

    async def _emit_if_changed() -> bool:
        nonlocal last_dedup, last_emit_attempt
        if structure is None:
            return True
        # Throttle: during RTH the quotes:bus delivers a frame every ~150ms, and
        # encoding the full payload per frame SYNCHRONOUSLY pegged the core and
        # starved the event loop (2026-06-12: S1 scans stalled 47 min, MI killed
        # at 300s, commodity scans timing out — all one positions client).
        now = monotonic()
        if (now - last_emit_attempt) < 1.0:
            return True
        last_emit_attempt = now
        payload = await _overlay_positions_overview(structure)
        fetched_at = str(payload.get("fetchedAt") or datetime.now(timezone.utc).isoformat())
        # Encode off-loop: GIL timeslicing keeps tick fan-out and strategy loops
        # breathing even while a big frame is being serialized.
        key = await asyncio.to_thread(_encode_core, payload)
        if key != last_dedup:
            if not _socket_is_connected(websocket):
                return False
            await websocket.send_text(f'{key[:-1]},"fetchedAt":{json.dumps(fetched_at)}}}')
            last_dedup = key
        return True

    pubsub = None
    try:
        redis = await get_redis()
        pubsub = redis.pubsub()
        await pubsub.subscribe(QUOTES_BUS_CHANNEL)
    except Exception as exc:  # noqa: BLE001 — degrade to a timer loop
        logger.warning(f"[WS] positions overview pub/sub unavailable, using timer: {exc}")
        pubsub = None

    try:
        while _socket_is_connected(websocket):
            tick_arrived = False
            if pubsub is not None:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.4)
                tick_arrived = bool(message and message.get("type") == "message")
            else:
                await asyncio.sleep(2.0)

            now = monotonic()
            rebuilt = False
            if structure is None or (now - last_build) >= 2.0:
                structure = await _build_positions_overview_structure()
                last_build = now
                rebuilt = True

            if tick_arrived or rebuilt:
                if not await _emit_if_changed():
                    break
    except WebSocketDisconnect:
        logger.info("[WS] Client disconnected from positions_overview")
    except Exception as e:
        if not _is_socket_closed_error(e):
            logger.error(f"[WS] Error in positions_overview: {e}")
    finally:
        if pubsub is not None:
            try:
                await pubsub.unsubscribe(QUOTES_BUS_CHANNEL)
            except Exception:
                pass
            await _close_pubsub(pubsub)


# Fields stripped from every watchlist row in the 2s overview socket.
# They are large display-only objects (TPO letter maps, prior-session profile
# with its own tpo_letters) that change at the agent's 30s scan cadence, not
# tick-by-tick. The 8s commodity_watchlist socket carries the full rows so the
# detail modal still has all the chart data.
_OVERVIEW_WS_WATCHLIST_STRIP = frozenset({
    "mp_tpo_letters",
    "mp_tpo_counts",
    "prior_session_profile",
})


def _slim_watchlist_rows(rows: list) -> list:
    """Return watchlist rows with heavy display fields removed."""
    if not rows:
        return rows
    return [
        {k: v for k, v in row.items() if k not in _OVERVIEW_WS_WATCHLIST_STRIP}
        for row in rows
    ]


async def ws_commodity_overview(websocket: WebSocket):
    """Stream the commodity execution desk snapshot.

    Payload is kept under ~100 KB so it fits comfortably within the WebSocket
    frame budget.  Heavy display-only fields (TPO letter maps, prior-session
    profile — ~90 KB per symbol × 8 symbols = ~720 KB duplicated) are stripped
    from the watchlist rows here and served by the 8s commodity_watchlist
    socket instead. signal_audit (360 KB) is served by /api/audit/events.
    """

    async def payload_factory():
        from api.routers.commodity import (
            commodity_kill_switch_state,
            commodity_orders,
            commodity_positions,
            commodity_reports,
            commodity_strategy_status,
        )

        from market_data.live_marks import overlay_live_marks

        # Overlay per-tick marks onto the open positions so P&L streams
        # instead of refreshing only on the agent's 60s scan. Commodity
        # positions carry an "action" (BUY/SELL); legs not in the feed fall
        # back to the scan-cadence mark untouched.
        commodity_positions_live = await overlay_live_marks(
            await commodity_positions(),
            side_field="action",
        )
        status = await commodity_strategy_status()

        # Slim the status dict before streaming it.
        #
        # 1. Strip heavy per-row fields from watchlist arrays — these are
        #    served by the 8s watchlist socket and merged back on the client.
        #    "watchlist" is an alias for "futures_watchlist"; only keep one key
        #    to avoid shipping the same ~645 KB blob twice.
        status["futures_watchlist"] = _slim_watchlist_rows(
            status.get("futures_watchlist") or status.get("watchlist") or []
        )
        status.pop("watchlist", None)  # alias — deduplicate

        # 2. signal_audit (360 KB, 600 entries) is already polled separately
        #    via /api/audit/events. No need to push it on the 2s live channel.
        status.pop("signal_audit", None)

        return {
            "status": status,
            "kill_switch_state": await commodity_kill_switch_state(),
            "orders": await commodity_orders(limit=40),
            "positions": commodity_positions_live,
            "reports": await commodity_reports(limit=24),
            # Heartbeat. _stream_snapshot only pushes when the encoded payload
            # changes; without a moving field, the channel goes silent between
            # the agent's 60s scans even though the client is healthy and
            # expects a live feed. The timestamp guarantees a push every
            # interval so the UI's "last update" clock keeps ticking and a
            # dropped socket is detectable. (Per-tick position marks land in
            # Stage 2 via the live-mark overlay.)
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
        }

    await _stream_snapshot(
        websocket,
        channel="commodity_overview",
        interval_seconds=2.0,
        payload_factory=payload_factory,
    )


async def ws_commodity_watchlist(websocket: WebSocket):
    """Stream the commodity watchlist setup snapshot.

    Runs at 8s (slower than the overview).  Carries the full watchlist rows
    including the heavy TPO letter maps and prior-session profile objects that
    are stripped from the 2s overview socket.  The frontend merges these into
    the live signal/price rows received from the overview socket so the detail
    modal can render the full TPO chart.
    """

    async def payload_factory():
        from api.routers.commodity import (
            commodity_strategy_status,
            commodity_watchlist_snapshot,
        )

        # The commodity strategy agent already refreshes futures/options data
        # on its scan cadence. Do not make every websocket client force a live
        # option-chain refresh; that fans out into broker REST calls and trips
        # Fyers 429 limits, leaving rows stale for everyone.
        snapshot = await commodity_watchlist_snapshot(live_refresh=False)

        # Attach the full watchlist rows (with TPO/prior-session chart data)
        # that the overview socket strips. The client uses these for the detail
        # modal only — they change at scan cadence, not tick-by-tick.
        status = await commodity_strategy_status()
        snapshot["futures_watchlist"] = (
            status.get("futures_watchlist") or status.get("watchlist") or []
        )
        return snapshot

    await _stream_snapshot(
        websocket,
        channel="commodity_watchlist",
        interval_seconds=8.0,
        payload_factory=payload_factory,
    )


async def ws_market_watchlist(websocket: WebSocket):
    """Stream the market ATM watchlist snapshot for a selected expiry."""

    requested_expiry = str(websocket.query_params.get("expiry") or "").strip() or None

    async def payload_factory():
        from api.routers.market import (
            get_atm_watchlist,
            get_atm_watchlist_expiries,
        )

        expiry_payload = await get_atm_watchlist_expiries(
            expiry=requested_expiry,
            live_refresh=True,
        )
        effective_expiry = requested_expiry or expiry_payload.get("default_expiry")
        return {
            "expiry_catalog": expiry_payload,
            "watchlist": await get_atm_watchlist(
                expiry=effective_expiry,
                symbols=None,
                live_refresh=True,
            ),
        }

    await _stream_snapshot(
        websocket,
        channel=f"market_watchlist:{requested_expiry or 'default'}",
        interval_seconds=8.0,
        payload_factory=payload_factory,
    )


async def ws_market_option_chain(websocket: WebSocket, symbol: str):
    """Stream the selected market option chain."""

    requested_expiry = str(websocket.query_params.get("expiry") or "").strip() or None

    async def payload_factory():
        from api.routers.market import get_option_chain

        return await get_option_chain(symbol=symbol, expiry=requested_expiry)

    await _stream_snapshot(
        websocket,
        channel=f"market_option_chain:{symbol}:{requested_expiry or 'default'}",
        interval_seconds=5.0,
        payload_factory=payload_factory,
    )


async def ws_fractal_market_profile(websocket: WebSocket, symbol: str):
    """Stream the Fractal Market Profile desk snapshot for one symbol."""

    async def payload_factory():
        from api.routers.fractal_market_profile import fractal_market_profile_live_snapshot

        return await fractal_market_profile_live_snapshot(symbol=symbol)

    await _stream_snapshot(
        websocket,
        channel=f"fractal_market_profile:{symbol}",
        interval_seconds=5.0,
        payload_factory=payload_factory,
    )


async def ws_strategy_snapshot(websocket: WebSocket):
    """Generic per-desk live-snapshot stream (watchlist + analytics).

    Query params: ?desk=directional|gann|auction|fractal&symbol=NIFTY&timeframe=5minute
    Thin wrapper over each desk's existing REST live_snapshot fn — upgrades the
    desk's watchlist/analytics view from a 15-30s poll to an 8s push with instant
    reconnect. NB (honesty): spot for the 3 live indices is real-time inside this
    payload; greeks/IV/OI remain rebuild-cadence until F1 (chain builder) is on.
    """
    desk = str(websocket.query_params.get("desk") or "").strip().lower()
    symbol = str(websocket.query_params.get("symbol") or "NIFTY").strip().upper() or "NIFTY"
    timeframe = str(websocket.query_params.get("timeframe") or "").strip() or None

    async def payload_factory():
        if desk == "directional":
            from directional_options.service import directional_options_service
            return await directional_options_service.live_snapshot(symbol, timeframe or "5minute", 16)
        if desk == "gann":
            from gann_tp_delta.service import gann_tp_delta_service
            return await gann_tp_delta_service.live_snapshot(
                symbol,
                timeframe or "15minute",
                60,
                "auto_pivot",
                "median_tpd",
                None,
            )
        if desk == "auction":
            from api.routers.auction_intelligence import live_snapshot
            return await live_snapshot(symbol=symbol)
        if desk == "fractal":
            from api.routers.fractal_market_profile import fractal_market_profile_live_snapshot
            return await fractal_market_profile_live_snapshot(symbol=symbol)
        return {"error": f"unknown desk: {desk}"}

    await _stream_snapshot(
        websocket,
        channel=f"strategy_snapshot:{desk}:{symbol}:{timeframe or 'default'}",
        interval_seconds=8.0,
        payload_factory=payload_factory,
    )


async def ws_quotes(websocket: WebSocket):
    """Multiplexed, event-driven live quote tape — the terminal hot path.

    Forwards the quote_bus's coalesced multi-symbol frames the INSTANT they are
    published to Redis (the ws_proposals ``listen()`` pattern) — there is NO
    ``asyncio.sleep`` / snapshot timer in this path, so glass-to-glass latency is
    bounded only by the 150 ms coalesce window + network, not a 1-15 s poll floor.

    The stream is unfiltered (all changed symbols per frame); the frontend tick
    store filters to the symbols each component subscribes to. On connect we replay
    a one-shot snapshot frame so a freshly-opened grid paints immediately.
    """
    await _accept_authenticated_socket(websocket, "quotes")

    # Paint instantly: last-known value for every symbol the bus has seen.
    try:
        from market_data.quote_bus import quote_bus
        await websocket.send_text(quote_bus.snapshot_frame())
    except Exception as exc:  # noqa: BLE001 — snapshot is best-effort
        logger.debug(f"[WS] quotes snapshot replay failed: {exc}")

    from market_data.quote_bus import QUOTES_BUS_CHANNEL

    pubsub = None
    try:
        redis = await get_redis()
        pubsub = redis.pubsub()
        await pubsub.subscribe(QUOTES_BUS_CHANNEL)
    except Exception as exc:  # noqa: BLE001 — Redis down → degrade, don't blackout
        logger.warning(f"[WS] quotes pub/sub unavailable, closing: {exc}")
        if pubsub is not None:
            await _close_pubsub(pubsub)  # B2: release the half-open conn or it leaks → Redis maxclients
        return

    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            if not _socket_is_connected(websocket):
                break
            data = message["data"]
            await websocket.send_text(data if isinstance(data, str) else data.decode())
    except WebSocketDisconnect:
        logger.info("[WS] Client disconnected from quotes")
    except Exception as e:
        if not _is_socket_closed_error(e):
            logger.error(f"[WS] Error in quotes handler: {e}")
    finally:
        try:
            await pubsub.unsubscribe(QUOTES_BUS_CHANNEL)
        except Exception:
            pass
        await _close_pubsub(pubsub)  # release the connection or it leaks → Redis maxclients


async def ws_depth(websocket: WebSocket, symbol: str):
    """Stream the 5-level DOM ladder for one focused symbol (event-driven).

    On connect, triggers a ref-counted incremental DepthUpdate subscription on the
    live Fyers WS client; forwards depth:{symbol} frames via listen() (no timer);
    releases the depth ref + pub/sub connection on disconnect. `symbol` is the
    broker-native key (same key the quote tape uses), so no translation is needed.
    """
    await _accept_authenticated_socket(websocket, f"depth:{symbol}")
    from market_data import data_router

    try:
        await data_router.subscribe_depth(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[WS] depth subscribe trigger failed for {symbol}: {exc}")

    pubsub = None
    try:
        redis = await get_redis()
        pubsub = redis.pubsub()
        await pubsub.subscribe(f"depth:{symbol}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[WS] depth pub/sub unavailable for {symbol}: {exc}")
        if pubsub is not None:
            await _close_pubsub(pubsub)  # B2: release the half-open conn or it leaks
        try:
            await data_router.unsubscribe_depth(symbol)
        except Exception:
            pass
        return

    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            if not _socket_is_connected(websocket):
                break
            data = message["data"]
            await websocket.send_text(data if isinstance(data, str) else data.decode())
    except WebSocketDisconnect:
        logger.info(f"[WS] Client disconnected from depth:{symbol}")
    except Exception as e:
        if not _is_socket_closed_error(e):
            logger.error(f"[WS] Error in depth handler: {e}")
    finally:
        try:
            await pubsub.unsubscribe(f"depth:{symbol}")
        except Exception:
            pass
        await _close_pubsub(pubsub)
        try:
            await data_router.unsubscribe_depth(symbol)
        except Exception:
            pass


async def ws_proposals(websocket: WebSocket):
    """Stream real-time agent proposals via Redis pub/sub."""
    await _accept_authenticated_socket(websocket, "proposals")
    pubsub = None
    try:
        redis = await get_redis()
        pubsub = redis.pubsub()
        await pubsub.subscribe("proposals")
    except Exception as exc:  # B3: init was previously unguarded → a subscribe() failure leaked the conn
        logger.warning(f"[WS] proposals pub/sub unavailable: {exc}")
        if pubsub is not None:
            await _close_pubsub(pubsub)
        return
    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                data = message["data"]
                await websocket.send_text(data if isinstance(data, str) else data.decode())
    except WebSocketDisconnect:
        logger.info("[WS] Client disconnected from proposals")
    except Exception as e:
        logger.error(f"[WS] Proposals error: {e}")
    finally:
        try:
            await pubsub.unsubscribe("proposals")
        except Exception:
            pass
        await _close_pubsub(pubsub)  # release the connection or it leaks → Redis maxclients
