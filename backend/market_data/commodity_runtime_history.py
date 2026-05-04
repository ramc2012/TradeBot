"""Small bridge for strategy modules that need live MCX futures candles."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import text

from market_data.commodity_contract_specs import extract_commodity_root


UTC = timezone.utc

DEFAULT_COMMODITY_FUTURES: dict[str, str] = {
    "CRUDEOIL": "MCX:CRUDEOIL26MAYFUT",
}


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


async def _persist_commodity_spot_rows(
    *,
    underlying: str,
    instrument_key: str,
    rows: list[dict[str, Any]],
    interval: str,
) -> int:
    """Upsert commodity 1-min/etc. rows into underlying_spot_candles.

    Keeps MP/auction-intelligence pipelines aligned with the commodity desk
    by writing to the same table the index pipeline uses, so the data-status
    panel (`mp-data-status`) and `_build_db_spot_mp_row` can find them.
    """
    if not rows:
        return 0
    payload: list[dict[str, Any]] = []
    for row in rows:
        ts = _parse_time(row.get("time") or row.get("timestamp"))
        if ts is None:
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
                    "time": ts.astimezone(UTC),
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
        except (TypeError, ValueError):
            continue

    if not payload:
        return 0

    try:
        from db.database import AsyncSessionLocal

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

    When `persist=True` (default), rows are also upserted into
    `underlying_spot_candles` so MP/auction-intelligence/db_spot consumers
    see commodity history through the same table indices use.
    """
    normalized_root = str(root or "").strip().upper()
    from paper_engine.commodity_strategy_agent import CommodityStrategyAgent

    agent = CommodityStrategyAgent()
    configured_symbols = agent.get_symbols()
    selected_symbol = next(
        (
            symbol
            for symbol in configured_symbols
            if extract_commodity_root(symbol) == normalized_root
        ),
        DEFAULT_COMMODITY_FUTURES.get(normalized_root, ""),
    )
    if not selected_symbol:
        return [], normalized_root
    rows = await agent._load_history(  # noqa: SLF001 - shared runtime bridge for local strategy history.
        selected_symbol,
        interval=interval,
        lookback_days=lookback_days,
    )

    if persist and rows:
        await _persist_commodity_spot_rows(
            underlying=normalized_root,
            instrument_key=selected_symbol,
            rows=rows,
            interval=interval,
        )

    return rows, selected_symbol
