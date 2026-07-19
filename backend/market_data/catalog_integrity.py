"""Uniqueness invariant for ``fo_underlying_catalog`` instrument keys.

Why this exists
---------------
``underlying_spot_candles`` is keyed ``(instrument_key, interval, time)`` — the
``underlying`` column is a *label*, not part of the primary key. So if two
catalog symbols ever share one ``spot_instrument_key``, the two names silently
overwrite each other bar-for-bar: whichever writer lands last wins the row and
relabels it. Nothing errors, nothing warns, and the tape for BOTH names becomes
a random interleave of two different instruments.

That is not hypothetical. ``M&M`` carried MARUTI's ISIN (``NSE_EQ|INE585B01010``;
M&M's real ISIN is ``INE101A01026``) and on 2026-07-17 the 1minute live_tick
rows under that key ranged 23.50 … 24,180 against a ~13,800 underlying — 313 of
351 rows out of band. It also explains why stock 30m coverage always read 210 of
211: exactly one of the colliding pair could own a clean grid on any given day.

The defence is two-layer:

1. **Write-time** (``filter_key_collisions``): a catalog upsert never assigns an
   instrument key that a *different* symbol already owns. The offending row is
   dropped and logged loudly rather than silently clobbering the tape.
2. **Startup/bootstrap** (``assert_unique_spot_keys``): a loud, explicit
   assertion that the invariant holds, so a bad row introduced by any path
   (manual SQL, a legacy import, a future writer) surfaces immediately instead
   of quietly poisoning features for days.

Both are cheap: ``fo_underlying_catalog`` is ~211 rows.
"""
from __future__ import annotations

from typing import Any, Iterable

from loguru import logger
from sqlalchemy import text


class CatalogKeyCollision(RuntimeError):
    """Two catalog symbols share one instrument key — the tape is corruptible."""


_COLLISION_SQL = """
    SELECT spot_instrument_key AS key, array_agg(symbol ORDER BY symbol) AS symbols
    FROM fo_underlying_catalog
    WHERE spot_instrument_key IS NOT NULL AND spot_instrument_key <> ''
    GROUP BY spot_instrument_key
    HAVING COUNT(*) > 1
    ORDER BY spot_instrument_key
"""


async def find_spot_key_collisions(session) -> list[dict[str, Any]]:
    """Return every ``spot_instrument_key`` claimed by more than one symbol."""
    result = await session.execute(text(_COLLISION_SQL))
    return [
        {"instrument_key": str(row.key), "symbols": [str(s) for s in (row.symbols or [])]}
        for row in result.fetchall()
    ]


async def assert_unique_spot_keys(session, *, raise_on_collision: bool = False) -> list[dict[str, Any]]:
    """Assert no two catalog underlyings share a ``spot_instrument_key``.

    Logs an ERROR per collision (these corrupt live spot data silently, so they
    must never be debug-level). Raises :class:`CatalogKeyCollision` when
    ``raise_on_collision`` is set — used by the bootstrap path, where a bad
    catalog is worth failing loudly over. Startup calls it non-raising so a
    single bad row cannot take the whole backend dark.
    """
    collisions = await find_spot_key_collisions(session)
    if not collisions:
        logger.info("[catalog integrity] spot_instrument_key uniqueness OK")
        return []
    for collision in collisions:
        logger.error(
            "[catalog integrity] INSTRUMENT KEY COLLISION: "
            f"{collision['instrument_key']} is claimed by {collision['symbols']} — "
            "these names OVERWRITE each other in underlying_spot_candles "
            "(PK is instrument_key/interval/time). Spot data for all of them is "
            "untrustworthy until the catalog is corrected."
        )
    if raise_on_collision:
        raise CatalogKeyCollision(
            f"{len(collisions)} colliding spot_instrument_key(s): "
            + "; ".join(f"{c['instrument_key']} -> {c['symbols']}" for c in collisions)
        )
    return collisions


async def filter_key_collisions(
    session,
    rows: Iterable[dict[str, Any]],
    *,
    key_field: str = "spot_instrument_key",
    symbol_field: str = "symbol",
) -> list[dict[str, Any]]:
    """Drop upsert rows that would assign a key another symbol already owns.

    Also de-duplicates *within* the incoming batch, so a single resolve pass
    that maps two symbols onto one key cannot introduce a collision either.
    Returns the rows that are safe to write.
    """
    candidates = [dict(row) for row in rows]
    if not candidates:
        return []

    result = await session.execute(
        text(
            """
            SELECT symbol, spot_instrument_key
            FROM fo_underlying_catalog
            WHERE spot_instrument_key IS NOT NULL AND spot_instrument_key <> ''
            """
        )
    )
    owner_by_key: dict[str, str] = {
        str(row.spot_instrument_key): str(row.symbol) for row in result.fetchall()
    }

    safe: list[dict[str, Any]] = []
    for row in candidates:
        symbol = str(row.get(symbol_field) or "").strip()
        key = str(row.get(key_field) or "").strip()
        if not symbol or not key:
            safe.append(row)
            continue
        owner = owner_by_key.get(key)
        if owner is not None and owner.upper() != symbol.upper():
            logger.error(
                f"[catalog integrity] REFUSING to map {symbol} -> {key}: "
                f"that instrument key already belongs to {owner}. Writing it "
                "would make the two names overwrite each other in "
                "underlying_spot_candles. Dropping this mapping."
            )
            continue
        owner_by_key[key] = symbol
        safe.append(row)
    return safe


def filter_foreign_contracts(
    symbol: str,
    rows: Iterable[dict[str, Any]],
    *,
    symbol_field: str = "underlying",
    trading_symbol_field: str = "trading_symbol",
) -> list[dict[str, Any]]:
    """Drop option contracts whose broker trading symbol names another underlying.

    The option-store counterpart to :func:`filter_key_collisions`. The chain
    writer stamps ``underlying = <the symbol we asked for>`` onto whatever the
    broker returned, and the upsert's ``ON CONFLICT`` clause overwrites the
    ``underlying`` column. So one crossed chain fetch relabels the other name's
    contracts, and every premium candle written through those keys then wears
    the wrong company's name. That is exactly how ~33k MARUTI option bars ended
    up filed under ``M&M`` (strikes 11,600-16,400 against a ~3,164 underlying).

    The broker's own ``trading_symbol`` ("MARUTI 13200 CE 30 JUN 26") is an
    external anchor: it is issued by the exchange alongside the contract and is
    independent of the symbol we requested. When its leading token names a
    *different* underlying, the row is foreign — drop it loudly.

    The match is exact on the leading token. Rows with no trading symbol are
    kept — fail open on *missing* metadata, fail closed on *contradictory*
    metadata. A corporate rename whose historical contracts still carry the old
    ticker (LTIM -> LTM, TATAMOTORS -> TMPV) would be dropped by this rule, but
    live chain fetches return the current ticker: as of 2026-07-20 every one of
    the 68 such rows in ``fo_contract_catalog`` is an already-expired contract,
    so nothing live is affected. And the failure direction is the safe one — the
    name goes dataless, its lane skips it for lack of data, and an ERROR is
    logged. An honest empty beats one company's premiums wearing another's name.
    """
    requested = str(symbol or "").strip().upper()
    candidates = [dict(row) for row in rows]
    if not requested or not candidates:
        return candidates

    safe: list[dict[str, Any]] = []
    for row in candidates:
        raw = str(row.get(trading_symbol_field) or "").strip().upper()
        head = raw.split(" ", 1)[0] if raw else ""
        if not head or head == requested:
            safe.append(row)
            continue
        logger.error(
            f"[catalog integrity] REFUSING to file contract '{raw}' under "
            f"{requested} ({row.get(symbol_field)!r}): the broker's own trading "
            f"symbol names {head}. Writing it would relabel {head}'s contracts "
            "and file its premium candles under the wrong company. Dropping."
        )
    return safe
