"""Small bridge for strategy modules that need live MCX futures candles."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger
from sqlalchemy import text

from market_data.commodity_contract_specs import extract_commodity_root


UTC = timezone.utc
# MCX trades 09:00–23:30 IST, so the session date is the IST calendar date; using
# the UTC date would roll the contract a day early during the evening session.
IST = timezone(timedelta(hours=5, minutes=30))

# DEPRECATED last-resort roots. Do NOT read the month in these values as a
# contract choice — it is only a carrier for the commodity ROOT.
#
# These were pinned to the JUNE-2026 contracts and never updated. Once
# ``config.symbols`` emptied (see commodity-symbols-wiped-jun-phantom-2026-08-06)
# this map became the ONLY candidate in ``load_commodity_history_rows``, so every
# write landed under an expired key — ``MCX:GOLD26JUNFUT`` was still receiving
# bars on 12-Aug-2026, two months after that contract died.
#
# The failure was silent because a dead month still RESOLVES: ``_candidate_rank``
# in upstox_commodity.py fuzzy-matches by |month distance|, so an expired symbol
# is quietly rounded to the nearest live contract. Verified 13-Aug-2026 —
# ``MCX:GOLD26JUNFUT`` and ``MCX:GOLD26OCTFUT`` hold byte-identical bars (spread
# exactly 0.00 on every overlapping minute), both being the real 05-Oct contract
# MCX_FO|483079. So the rows were CORRECT DATA UNDER A WRONG LABEL, which is
# worse than an error: nothing failed loudly, and a symbol-keyed reader silently
# splits one contract across two series.
#
# Note the fuzzy match is root-dependent, so a stale month is NOT a harmless
# alias: JUN rounded to OCT for GOLD but to AUG for the other seven roots.
#
# Contract selection now comes from ``resolve_active_upstox_mcx_future``, which
# reads the Upstox master and rolls on the first session reaching expiry. Keep
# this map only so a catalog outage still yields a usable root.
DEFAULT_COMMODITY_FUTURES: dict[str, str] = {
    "CRUDEOIL": "MCX:CRUDEOIL26AUGFUT",
    "GOLD": "MCX:GOLD26OCTFUT",
    "SILVERM": "MCX:SILVERM26AUGFUT",
    "NATURALGAS": "MCX:NATURALGAS26AUGFUT",
    "COPPER": "MCX:COPPER26AUGFUT",
    "ALUMINI": "MCX:ALUMINI26AUGFUT",
    "ZINCMINI": "MCX:ZINCMINI26AUGFUT",
    "NICKEL": "MCX:NICKEL26AUGFUT",
}

# Per-instrument lock so concurrent callers don't race on the same upsert.
_PERSIST_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}
# Track the latest in-DB time per (instrument_key, interval) once observed,
# so subsequent calls only insert genuinely new rows without re-querying.
_LATEST_PERSISTED: dict[tuple[str, str], datetime] = {}
_RECENT_REPAIR_WINDOW = timedelta(hours=3)


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _persist_lock(instrument_key: str, interval: str) -> asyncio.Lock:
    key = (instrument_key, interval)
    lock = _PERSIST_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _PERSIST_LOCKS[key] = lock
    return lock


async def _persist_commodity_spot_rows(
    *,
    underlying: str,
    instrument_key: str,
    rows: list[dict[str, Any]],
    interval: str,
) -> int:
    """Upsert *only new* commodity rows into underlying_spot_candles.

    Keeps MP/auction-intelligence pipelines aligned with the commodity desk
    while staying cheap: each invocation persists at most a handful of rows
    (the bars produced since the last successful persist), instead of
    re-upserting the full lookback window every call.
    """
    if not rows:
        return 0

    cache_key = (instrument_key, interval)
    lock = _persist_lock(instrument_key, interval)

    if lock.locked():
        # Another caller is already persisting for this instrument; skip to
        # avoid request-path contention on the same primary key set.
        return 0

    async with lock:
        try:
            from db.database import AsyncSessionLocal

            latest_known = _LATEST_PERSISTED.get(cache_key)
            if latest_known is None:
                # First observation: ask the DB once, then cache for the
                # lifetime of the process so subsequent calls are cheap.
                async with AsyncSessionLocal() as session:
                    latest_known = await session.scalar(
                        text(
                            """
                            SELECT MAX(time) FROM underlying_spot_candles
                            WHERE instrument_key = :instrument_key
                              AND interval = :interval
                            """
                        ),
                        {"instrument_key": instrument_key, "interval": interval},
                    )
                if latest_known is not None and latest_known.tzinfo is None:
                    latest_known = latest_known.replace(tzinfo=UTC)
                _LATEST_PERSISTED[cache_key] = latest_known or datetime.min.replace(tzinfo=UTC)
                latest_known = _LATEST_PERSISTED[cache_key]

            # A brand-new contract/interval has no persisted watermark.  The
            # sentinel above is datetime.min; subtracting the repair window
            # from it raises ``OverflowError: date value out of range`` and
            # silently prevents the first bars from ever being stored.
            min_utc = datetime.min.replace(tzinfo=UTC)
            repair_cutoff = (
                latest_known - _RECENT_REPAIR_WINDOW
                if latest_known > min_utc + _RECENT_REPAIR_WINDOW
                else min_utc
            )
            payload: list[dict[str, Any]] = []
            newest_seen = latest_known
            for row in rows:
                ts = _parse_time(row.get("time") or row.get("timestamp"))
                if ts is None:
                    continue
                ts = ts.astimezone(UTC)
                # Re-upsert a bounded recent window so small holes behind the tip
                # can self-heal on later reads without replaying the full lookback.
                if latest_known is not None and ts <= repair_cutoff:
                    continue
                close = row.get("close")
                try:
                    close_f = float(close) if close is not None else 0.0
                except (TypeError, ValueError):
                    continue
                if close_f <= 0:
                    continue
                try:
                    payload.append(
                        {
                            "time": ts,
                            "instrument_key": instrument_key,
                            "underlying": underlying.upper(),
                            "interval": interval,
                            "open": float(row.get("open") or close_f),
                            "high": float(row.get("high") or close_f),
                            "low": float(row.get("low") or close_f),
                            "close": close_f,
                            "volume": int(float(row.get("volume") or 0.0)),
                            "oi": int(float(row.get("oi") or 0.0)),
                            "source": "commodity_broker_history",
                        }
                    )
                    if newest_seen is None or ts > newest_seen:
                        newest_seen = ts
                except (TypeError, ValueError):
                    continue

            if not payload:
                return 0

            async with AsyncSessionLocal() as session:
                await session.execute(
                    text(
                        """
                        INSERT INTO underlying_spot_candles (
                            time, instrument_key, underlying, interval, open, high,
                            low, close, volume, oi, source, synced_at
                        ) VALUES (
                            :time, :instrument_key, :underlying, :interval, :open, :high,
                            :low, :close, :volume, :oi, :source, NOW()
                        )
                        ON CONFLICT (instrument_key, interval, time) DO UPDATE
                        SET open = EXCLUDED.open,
                            high = EXCLUDED.high,
                            low = EXCLUDED.low,
                            close = EXCLUDED.close,
                            volume = EXCLUDED.volume,
                            oi = EXCLUDED.oi,
                            source = EXCLUDED.source,
                            synced_at = NOW()
                        """
                    ),
                    payload,
                )
                await session.commit()

            if newest_seen is not None:
                _LATEST_PERSISTED[cache_key] = newest_seen
            return len(payload)
        except Exception as exc:
            logger.debug(
                f"[commodity_runtime_history] persist skipped for {underlying} ({instrument_key}): {exc}"
            )
            return 0


async def load_commodity_history_rows(
    root: str,
    *,
    interval: str = "1minute",
    lookback_days: int = 10,
    persist: bool = True,
) -> tuple[list[dict[str, Any]], str]:
    """Load recent MCX futures candles using the commodity strategy's resolver.

    The commodity agent already knows how to prefer Upstox-resolved MCX
    contracts and fall back to Fyers symbols, so this keeps MP/FMP/directional
    testing aligned with the live commodity desk.

    When `persist=True` (default), incremental persistence is scheduled as a
    fire-and-forget background task so the caller's request returns
    immediately. Only rows newer than the highest already-persisted timestamp
    are inserted, keeping each persist O(new_bars) rather than O(lookback).
    """
    normalized_root = str(root or "").strip().upper()
    from paper_engine.commodity_strategy_agent import CommodityStrategyAgent

    agent = CommodityStrategyAgent()
    configured_symbols = agent.get_symbols()
    active_futures_symbols = await agent._active_futures_symbols()  # noqa: SLF001 - shared runtime bridge.
    candidate_symbols: list[str] = []
    candidate_symbols.extend(
        active_symbol
        for configured_symbol, active_symbol in active_futures_symbols.items()
        if extract_commodity_root(configured_symbol) == normalized_root
    )
    candidate_symbols.extend(
        symbol
        for symbol in configured_symbols
        if extract_commodity_root(symbol) == normalized_root
    )
    candidate_symbols.extend(
        symbol
        for symbol in agent.get_selected_option_lookup_symbols().values()
        if extract_commodity_root(symbol) == normalized_root
    )
    # Catalog-derived front month BEFORE the static map, so a configured-symbol
    # gap can never again pin writes to a dead contract key (the 26JUN phantoms).
    # Best-effort: a catalog/broker outage must not take the history bridge down,
    # so any failure just falls through to the deprecated root map below.
    try:
        from market_data.upstox_commodity import resolve_active_upstox_mcx_future

        active_contract = await resolve_active_upstox_mcx_future(
            normalized_root,
            session_date=datetime.now(IST).date(),
        )
        active_symbol = str((active_contract or {}).get("symbol") or "")
        if active_symbol:
            candidate_symbols.append(active_symbol)
    except Exception as exc:  # noqa: BLE001 - resolution is advisory, never fatal.
        logger.debug(f"[commodity-history] active-contract resolve failed for {normalized_root}: {exc}")

    fallback_symbol = DEFAULT_COMMODITY_FUTURES.get(normalized_root, "")
    if fallback_symbol:
        candidate_symbols.append(fallback_symbol)
    candidate_symbols = list(dict.fromkeys(symbol for symbol in candidate_symbols if symbol))
    if not candidate_symbols:
        return [], normalized_root
    selected_symbol = candidate_symbols[0]
    rows: list[dict[str, Any]] = []
    for symbol in candidate_symbols:
        selected_symbol = symbol
        rows = await agent._load_history(  # noqa: SLF001 - shared runtime bridge for local strategy history.
            symbol,
            interval=interval,
            lookback_days=lookback_days,
        )
        if rows:
            break

    if persist and rows:
        # Fire-and-forget: persistence must never block the request path.
        try:
            asyncio.create_task(
                _persist_commodity_spot_rows(
                    underlying=normalized_root,
                    instrument_key=selected_symbol,
                    rows=rows,
                    interval=interval,
                ),
                name=f"persist-commodity-{selected_symbol}-{interval}",
            )
        except RuntimeError:
            # No running loop (e.g. called from sync context) — skip silently.
            pass

    return rows, selected_symbol
