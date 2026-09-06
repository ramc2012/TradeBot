"""Resolve futures app-symbols to Upstox instrument keys, and register both ways.

Upstox's MarketDataStreamerV3 subscribes by instrument KEY (``NSE_FO|58072``,
``MCX_FO|483079``), never by a display symbol. ``to_broker_symbol`` only knew the
five index app-symbols, so every futures subscription was handed a raw string
like ``NSE:NIFTY26AUGFUT`` and was silently inert — no error, no ticks. Under
Fyers the same passthrough happened to be native format, which is why the tape
carried futures until Fyers died on 2026-08-07 and has carried only the five
index symbols since.

Consequence: ``market_ticks`` has no futures rows, so ``tick_fresh``,
``real_tick_cvd`` and ``confirmation_2_of_3`` are permanently False. They are
ANDed into every direction's gate set, so BOTH convergence lanes (NSE and MCX)
could never emit an actionable row — silent since 2026-08-07.

Resolution sources, both already trusted elsewhere in the app:
  * MCX  -> ``resolve_active_upstox_mcx_future`` (Upstox instrument master, rolls
    on the first session reaching expiry).
  * NSE / BSE index futures -> the ``index_futures_candles`` contract identity
    columns, which the backfill populates with the real instrument_key/expiry.

Everything here is best-effort: any failure leaves the caller with the previous
(inert) behaviour rather than breaking a working subscription.
"""
from __future__ import annotations

import asyncio
import gzip
import json
from datetime import date, datetime, timedelta, timezone
from time import monotonic
from typing import Any, Iterable

import httpx
from loguru import logger

from market_data.commodity_contract_specs import extract_commodity_root
from market_data.symbols import register_broker_symbol, registered_broker_symbols

IST = timezone(timedelta(hours=5, minutes=30))

# Re-resolve at most this often. Contracts roll monthly, so an hour is ample and
# keeps this off the hot subscribe path.
_TTL_SECONDS = 3600.0
_last_resolved_at: float | None = None
_last_session_date: date | None = None

# NSE F&O instrument master — the durable source for index-futures keys.
# index_futures_candles only holds contracts the backfill has already fetched
# (today: August only, and NIFTY/BANKNIFTY expire 25-Aug), so a table-only
# resolver would silently go inert at the next roll. The master always carries
# the current AND next contracts.
# The per-exchange NSE_FO endpoint returns HTTP 403; `complete.json.gz` is the
# one that serves (verified 2026-08-21) and carries both the current and next
# contracts — e.g. NIFTY NSE_FO|58072 (25-Aug) and NSE_FO|68407 (29-Sep).
_NSE_FO_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
_NSE_FO_TTL_SECONDS = 6 * 60 * 60
_nse_fo_cache: tuple[float, list[dict[str, Any]]] = (0.0, [])


async def _load_nse_fo_instruments() -> list[dict[str, Any]]:
    """Cached NSE_FO master. Serves a stale cache rather than [] on failure —
    an empty list would silently un-resolve a working contract."""
    global _nse_fo_cache
    cached_at, cached_rows = _nse_fo_cache
    if cached_rows and monotonic() - cached_at < _NSE_FO_TTL_SECONDS:
        return cached_rows
    try:
        from market_data.instrument_master import load_master
        rows = await asyncio.to_thread(load_master)
        _nse_fo_cache = (monotonic(), rows)
        return rows
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[futures-keys] NSE_FO master download failed: {exc}")
        return cached_rows


def _select_front_future(
    rows: list[dict[str, Any]], underlying: str, session_date: date
) -> str | None:
    """Nearest FUT contract for ``underlying`` that has not yet expired."""
    best: tuple[date, str] | None = None
    for raw in rows:
        if str(raw.get("instrument_type") or "").upper() != "FUT":
            continue
        name = str(
            raw.get("underlying_symbol") or raw.get("asset_symbol") or raw.get("name") or ""
        ).upper()
        if name != underlying:
            continue
        key = str(raw.get("instrument_key") or "").strip()
        if not key:
            continue
        expiry_raw = raw.get("expiry")
        try:
            # Upstox ships expiry as epoch millis on this master.
            expiry = (
                datetime.fromtimestamp(int(expiry_raw) / 1000, tz=timezone.utc).date()
                if isinstance(expiry_raw, (int, float))
                else date.fromisoformat(str(expiry_raw)[:10])
            )
        except Exception:  # noqa: BLE001
            continue
        if expiry < session_date:
            continue
        if best is None or expiry < best[0]:
            best = (expiry, key)
    return best[1] if best else None


def _is_futures_symbol(app_symbol: str) -> bool:
    s = str(app_symbol or "").strip().upper()
    return s.endswith("FUT") and (":" in s)


async def _resolve_mcx(app_symbol: str, session_date: date) -> str | None:
    from market_data.upstox_commodity import resolve_active_upstox_mcx_future

    root = extract_commodity_root(app_symbol)
    if not root:
        return None
    contract = await resolve_active_upstox_mcx_future(root, session_date=session_date)
    return str((contract or {}).get("instrument_key") or "") or None


async def _resolve_index_future(app_symbol: str, session_date: date) -> str | None:
    """Live index-futures key from index_futures_candles' contract identity.

    Bounded to non-expired contracts and ordered by nearest expiry, so it tracks
    the front month across a roll without any hardcoded month code.
    """
    from sqlalchemy import text

    from db.database import AsyncSessionLocal

    underlying = str(app_symbol).split(":", 1)[-1].upper()
    for token in ("26", "FUT"):  # strip the trailing YYMONFUT to leave the root
        idx = underlying.find(token)
        if idx > 0:
            underlying = underlying[:idx]
            break
    underlying = underlying.rstrip("0123456789")
    if not underlying:
        return None
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT instrument_key FROM index_futures_candles "
                    "WHERE underlying = :u AND expiry >= :d AND instrument_key <> '' "
                    "ORDER BY expiry ASC LIMIT 1"
                ),
                {"u": underlying, "d": session_date},
            )
        ).first()
    if row and row[0]:
        return str(row[0])
    # Table miss (a contract the backfill has not fetched — e.g. right after a
    # roll). Fall back to the live master so the lane does not go inert.
    return _select_front_future(await _load_nse_fo_instruments(), underlying, session_date)


async def resolve_and_register(app_symbols: Iterable[str], *, force: bool = False) -> dict[str, str]:
    """Resolve every futures symbol in ``app_symbols`` and register both directions.

    Returns the newly-registered mapping. Never raises: on any failure the symbol
    is simply left unregistered and the caller falls back to prior behaviour.
    """
    global _last_resolved_at, _last_session_date

    session_date = datetime.now(IST).date()
    now = asyncio.get_running_loop().time()
    fresh = (
        not force
        and _last_resolved_at is not None
        and _last_session_date == session_date
        and (now - _last_resolved_at) < _TTL_SECONDS
    )

    wanted = [s for s in dict.fromkeys(app_symbols) if _is_futures_symbol(s)]
    known = registered_broker_symbols()
    pending = [s for s in wanted if s not in known]
    if fresh and not pending:
        return {}

    resolved: dict[str, str] = {}
    for symbol in (wanted if not fresh else pending):
        try:
            if symbol.upper().startswith("MCX"):
                key = await asyncio.wait_for(_resolve_mcx(symbol, session_date), timeout=20.0)
            else:
                key = await asyncio.wait_for(_resolve_index_future(symbol, session_date), timeout=10.0)
        except Exception as exc:  # noqa: BLE001 - advisory; never break subscribe
            logger.debug(f"[futures-keys] resolve failed for {symbol}: {exc}")
            continue
        if key:
            register_broker_symbol(symbol, key)
            resolved[symbol] = key

    _last_resolved_at = now
    _last_session_date = session_date
    if resolved:
        logger.info(
            "[futures-keys] registered "
            + ", ".join(f"{app}->{key}" for app, key in sorted(resolved.items()))
        )
    unresolved = [s for s in wanted if s not in registered_broker_symbols()]
    if unresolved:
        logger.warning(
            f"[futures-keys] {len(unresolved)} futures symbol(s) unresolved and will "
            f"subscribe INERT (no ticks): {', '.join(sorted(unresolved))}"
        )
    return resolved
