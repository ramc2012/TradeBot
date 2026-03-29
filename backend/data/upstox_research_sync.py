from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from loguru import logger
from sqlalchemy import text

from analysis.backtest import MACDBacktester, UpstoxAuthError
from analysis.instruments import get_first_trading_day_after
from db.database import AsyncSessionLocal


SECONDS_PER_YEAR = 365.25 * 24 * 60 * 60
IST = timezone(timedelta(hours=5, minutes=30))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_ts(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bs_d1_d2(spot: float, strike: float, tte_years: float, rate: float, sigma: float) -> tuple[float, float]:
    variance = sigma * math.sqrt(tte_years)
    d1 = (
        math.log(spot / strike)
        + (rate + 0.5 * sigma * sigma) * tte_years
    ) / variance
    d2 = d1 - variance
    return d1, d2


def _bs_price(
    option_type: str,
    spot: float,
    strike: float,
    tte_years: float,
    rate: float,
    sigma: float,
) -> float:
    d1, d2 = _bs_d1_d2(spot, strike, tte_years, rate, sigma)
    discount = math.exp(-rate * tte_years)
    if option_type == "CE":
        return spot * _norm_cdf(d1) - strike * discount * _norm_cdf(d2)
    return strike * discount * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def _implied_volatility(
    option_type: str,
    premium: float,
    spot: float,
    strike: float,
    tte_years: float,
    rate: float,
) -> Optional[float]:
    if premium <= 0 or spot <= 0 or strike <= 0 or tte_years <= 0:
        return None

    intrinsic = max(spot - strike, 0.0) if option_type == "CE" else max(strike - spot, 0.0)
    if premium <= intrinsic:
        return None

    lo, hi = 1e-4, 5.0
    try:
        lo_price = _bs_price(option_type, spot, strike, tte_years, rate, lo)
        hi_price = _bs_price(option_type, spot, strike, tte_years, rate, hi)
    except (ValueError, ZeroDivisionError):
        return None

    if premium < lo_price or premium > hi_price:
        return None

    for _ in range(80):
        mid = (lo + hi) / 2.0
        mid_price = _bs_price(option_type, spot, strike, tte_years, rate, mid)
        if abs(mid_price - premium) < 1e-6:
            return mid
        if mid_price > premium:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2.0


def _option_greeks(
    option_type: str,
    spot: float,
    strike: float,
    tte_years: float,
    rate: float,
    sigma: float,
) -> tuple[float, float, float, float]:
    d1, d2 = _bs_d1_d2(spot, strike, tte_years, rate, sigma)
    discount = math.exp(-rate * tte_years)
    pdf = _norm_pdf(d1)

    if option_type == "CE":
        delta = _norm_cdf(d1)
        theta = (
            -(spot * pdf * sigma) / (2.0 * math.sqrt(tte_years))
            - rate * strike * discount * _norm_cdf(d2)
        ) / 365.0
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = (
            -(spot * pdf * sigma) / (2.0 * math.sqrt(tte_years))
            + rate * strike * discount * _norm_cdf(-d2)
        ) / 365.0

    gamma = pdf / (spot * sigma * math.sqrt(tte_years))
    vega = spot * pdf * math.sqrt(tte_years) / 100.0
    return delta, gamma, theta, vega


@dataclass
class SyncSummary:
    universe_rows: int = 0
    underlyings_synced: int = 0
    expiries_discovered: int = 0
    contracts_discovered: int = 0
    spot_candles_stored: int = 0
    selection_spots_refreshed: int = 0
    option_candles_stored: int = 0
    contracts_completed: int = 0
    contracts_empty: int = 0
    chain_metrics_refreshed: int = 0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class UpstoxResearchSync:
    def __init__(
        self,
        access_token: str,
        from_date: date,
        to_date: date,
        interval: str = "30minute",
        risk_free_rate: float = 0.06,
        upstox_gap_seconds: float = 1.2,
    ) -> None:
        self.client = MACDBacktester(access_token=access_token)
        self.client.rate_limit_delay = upstox_gap_seconds
        self.from_date = from_date
        self.to_date = to_date
        self.interval = interval
        self.risk_free_rate = risk_free_rate
        self._spot_cache: dict[str, dict[str, float]] = {}

    async def _prime_underlying_meta_cache(self) -> int:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT symbol, kind, spot_instrument_key, underlying_key
                    FROM fo_underlying_catalog
                    WHERE spot_instrument_key IS NOT NULL
                      AND underlying_key IS NOT NULL
                """)
            )
            rows = result.fetchall()

        primed = 0
        for row in rows:
            self.client._underlying_meta_cache[row.symbol] = {
                "spot_instrument_key": row.spot_instrument_key,
                "underlying_key": row.underlying_key,
                "segment": "NSE_INDEX" if row.kind == "INDEX" else "NSE_EQ",
                "display_name": row.symbol,
            }
            primed += 1
        return primed

    async def _fetch_chunked_candles(
        self,
        instrument_key: str,
        from_date: date,
        to_date: date,
        chunk_days: int = 60,
    ) -> list[dict]:
        merged: dict[str, dict] = {}
        cursor = from_date
        while cursor <= to_date:
            window_end = min(cursor + timedelta(days=chunk_days - 1), to_date)
            candles = await self.client._fetch_candles_from_upstox(
                instrument_key,
                cursor,
                window_end,
            )
            for candle in candles:
                merged[str(candle["time"])] = candle
            cursor = window_end + timedelta(days=1)
        return [merged[key] for key in sorted(merged)]

    async def _upsert_universe(self) -> int:
        universe = await self.client.fetch_fo_universe()
        rows = [
            {"symbol": symbol, "kind": "INDEX"}
            for symbol in sorted(universe["indices"])
        ] + [
            {"symbol": symbol, "kind": "STOCK"}
            for symbol in sorted(universe["stocks"])
        ]
        if not rows:
            return 0

        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO fo_underlying_catalog (symbol, kind, updated_at)
                    VALUES (:symbol, :kind, NOW())
                    ON CONFLICT (symbol) DO UPDATE
                    SET kind = EXCLUDED.kind,
                        updated_at = NOW()
                """),
                rows,
            )
            await session.commit()
        return len(rows)

    async def _discover_underlyings(self, limit: int) -> tuple[int, int]:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT symbol
                    FROM fo_underlying_catalog
                    WHERE expiries_synced_at IS NULL
                       OR expiries_synced_at < NOW() - INTERVAL '1 day'
                    ORDER BY CASE WHEN expiries_synced_at IS NULL THEN 0 ELSE 1 END,
                             CASE WHEN kind = 'INDEX' THEN 0 ELSE 1 END,
                             expiries_synced_at NULLS FIRST,
                             symbol
                    LIMIT :limit
                """),
                {"limit": limit},
            )
            symbols = [row.symbol for row in result.fetchall()]

        synced = 0
        expiries_stored = 0
        for symbol in symbols:
            meta = await self.client._resolve_underlying_metadata(symbol)
            expiry_dates = await self.client._fetch_expiry_dates(symbol)
            monthly_expiries, prev_map = self.client._select_monthly_expiries(
                expiry_dates,
                self.from_date,
                self.to_date,
            )
            expiry_rows = []
            for expiry in monthly_expiries:
                prev_expiry = prev_map.get(expiry)
                expiry_rows.append(
                    {
                        "underlying": symbol,
                        "expiry": expiry,
                        "previous_monthly_expiry": prev_expiry,
                        "selection_date": (
                            get_first_trading_day_after(prev_expiry)
                            if prev_expiry
                            else None
                        ),
                    }
                )

            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("""
                        UPDATE fo_underlying_catalog
                        SET spot_instrument_key = COALESCE(:spot_instrument_key, spot_instrument_key),
                            underlying_key = COALESCE(:underlying_key, underlying_key),
                            expiries_synced_at = NOW(),
                            updated_at = NOW()
                        WHERE symbol = :symbol
                    """),
                    {
                        "symbol": symbol,
                        "spot_instrument_key": (meta or {}).get("spot_instrument_key"),
                        "underlying_key": (meta or {}).get("underlying_key"),
                    },
                )
                if expiry_rows:
                    await session.execute(
                        text("""
                            INSERT INTO fo_expiry_catalog (
                                underlying, expiry, previous_monthly_expiry,
                                selection_date, updated_at
                            )
                            VALUES (
                                :underlying, :expiry, :previous_monthly_expiry,
                                :selection_date, NOW()
                            )
                            ON CONFLICT (underlying, expiry) DO UPDATE
                            SET previous_monthly_expiry = EXCLUDED.previous_monthly_expiry,
                                selection_date = EXCLUDED.selection_date,
                                updated_at = NOW()
                        """),
                        expiry_rows,
                    )
                await session.commit()

            synced += 1
            expiries_stored += len(expiry_rows)
            logger.info(
                f"Synced expiry metadata for {symbol}: {len(expiry_rows)} monthly expiries"
            )

        return synced, expiries_stored

    async def _discover_contracts(self, limit: int) -> tuple[int, int]:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    WITH underlying_progress AS (
                        SELECT
                            underlying,
                            COUNT(*) FILTER (WHERE sync_status = 'complete') AS complete_contracts
                        FROM fo_contract_catalog
                        GROUP BY underlying
                    )
                    SELECT e.underlying, e.expiry
                    FROM fo_expiry_catalog e
                    JOIN fo_underlying_catalog u
                      ON u.symbol = e.underlying
                    LEFT JOIN underlying_progress p
                      ON p.underlying = e.underlying
                    WHERE contracts_discovered_at IS NULL
                    ORDER BY COALESCE(p.complete_contracts, 0),
                             CASE WHEN u.kind = 'STOCK' THEN 0 ELSE 1 END,
                             e.expiry DESC,
                             e.underlying
                    LIMIT :limit
                """),
                {"limit": limit},
            )
            rows = result.fetchall()

        discovered_expiries = 0
        contract_rows = 0
        for row in rows:
            underlying = row.underlying
            expiry = row.expiry
            try:
                contracts = await self.client._fetch_expired_contracts(underlying, expiry)
            except UpstoxAuthError as exc:
                logger.error(
                    f"Stopping contract discovery because Upstox authentication failed: {exc}"
                )
                raise

            payload = []
            for contract in contracts:
                option_type = str(contract.get("instrument_type", "")).upper()
                if option_type not in ("CE", "PE"):
                    continue
                strike = contract.get("strike_price") or contract.get("strike")
                if strike is None:
                    continue
                payload.append(
                    {
                        "instrument_key": contract.get("instrument_key"),
                        "trading_symbol": contract.get("trading_symbol"),
                        "underlying": underlying,
                        "expiry": expiry,
                        "strike": float(strike),
                        "option_type": option_type,
                        "lot_size": contract.get("lot_size"),
                        "tick_size": contract.get("tick_size"),
                        "minimum_lot": contract.get("minimum_lot"),
                        "freeze_quantity": contract.get("freeze_quantity"),
                        "candle_from_date": expiry - timedelta(days=365),
                        "candle_to_date": expiry,
                    }
                )

            async with AsyncSessionLocal() as session:
                if payload:
                    await session.execute(
                        text("""
                            INSERT INTO fo_contract_catalog (
                                instrument_key, trading_symbol, underlying, expiry,
                                strike, option_type, lot_size, tick_size,
                                minimum_lot, freeze_quantity, candle_from_date,
                                candle_to_date, updated_at
                            )
                            VALUES (
                                :instrument_key, :trading_symbol, :underlying, :expiry,
                                :strike, :option_type, :lot_size, :tick_size,
                                :minimum_lot, :freeze_quantity, :candle_from_date,
                                :candle_to_date, NOW()
                            )
                            ON CONFLICT (instrument_key) DO UPDATE
                            SET trading_symbol = EXCLUDED.trading_symbol,
                                underlying = EXCLUDED.underlying,
                                expiry = EXCLUDED.expiry,
                                strike = EXCLUDED.strike,
                                option_type = EXCLUDED.option_type,
                                lot_size = EXCLUDED.lot_size,
                                tick_size = EXCLUDED.tick_size,
                                minimum_lot = EXCLUDED.minimum_lot,
                                freeze_quantity = EXCLUDED.freeze_quantity,
                                candle_from_date = EXCLUDED.candle_from_date,
                                candle_to_date = EXCLUDED.candle_to_date,
                                updated_at = NOW()
                        """),
                        payload,
                    )
                await session.execute(
                    text("""
                        UPDATE fo_expiry_catalog
                        SET contracts_discovered_at = NOW(),
                            contract_count = :contract_count,
                            updated_at = NOW()
                        WHERE underlying = :underlying
                          AND expiry = :expiry
                    """),
                    {
                        "underlying": underlying,
                        "expiry": expiry,
                        "contract_count": len(payload),
                    },
                )
                await session.commit()

            discovered_expiries += 1
            contract_rows += len(payload)
            logger.info(
                f"Discovered {len(payload)} contracts for {underlying} {expiry}"
            )

        return discovered_expiries, contract_rows

    async def _sync_spot_history(self, limit: int) -> int:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT symbol, kind, spot_instrument_key, spot_range_start, spot_range_end
                    FROM fo_underlying_catalog
                    WHERE spot_instrument_key IS NOT NULL
                      AND (
                        spot_synced_at IS NULL
                        OR spot_range_start IS NULL
                        OR spot_range_end IS NULL
                        OR spot_range_start > :from_date
                        OR spot_range_end < :to_date
                      )
                    ORDER BY CASE WHEN kind = 'INDEX' THEN 0 ELSE 1 END,
                             COALESCE(spot_range_end, DATE '1900-01-01'),
                             symbol
                    LIMIT :limit
                """),
                {
                    "limit": limit,
                    "from_date": self.from_date,
                    "to_date": self.to_date,
                },
            )
            rows = result.fetchall()

        stored_rows = 0
        for row in rows:
            fetch_windows: list[tuple[date, date]] = []
            if row.spot_range_start is None or row.spot_range_end is None:
                fetch_windows.append((self.from_date, self.to_date))
            else:
                if row.spot_range_start > self.from_date:
                    fetch_windows.append(
                        (self.from_date, row.spot_range_start - timedelta(days=1))
                    )
                if row.spot_range_end < self.to_date:
                    fetch_windows.append(
                        (row.spot_range_end + timedelta(days=1), self.to_date)
                    )

            merged: dict[str, dict] = {}
            for window_start, window_end in fetch_windows:
                if window_start > window_end:
                    continue
                candles = await self._fetch_chunked_candles(
                    row.spot_instrument_key,
                    window_start,
                    window_end,
                )
                for candle in candles:
                    merged[str(candle["time"])] = candle

            candles = [merged[key] for key in sorted(merged)]
            if not candles:
                logger.warning(f"No spot candles returned for {row.symbol}")
                continue

            payload = [
                {
                    "time": _parse_iso_ts(candle["time"]),
                    "instrument_key": row.spot_instrument_key,
                    "underlying": row.symbol,
                    "interval": self.interval,
                    "open": float(candle["open"]),
                    "high": float(candle["high"]),
                    "low": float(candle["low"]),
                    "close": float(candle["close"]),
                    "volume": int(candle.get("volume", 0)),
                    "oi": int(candle.get("oi", 0)),
                    "source": "upstox_spot",
                }
                for candle in candles
            ]

            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("""
                        INSERT INTO underlying_spot_candles (
                            time, instrument_key, underlying, interval, open, high,
                            low, close, volume, oi, source, synced_at
                        )
                        VALUES (
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
                    """),
                    payload,
                )
                await session.execute(
                    text("""
                        UPDATE fo_underlying_catalog
                        SET spot_synced_at = NOW(),
                            spot_range_start = LEAST(
                                COALESCE(spot_range_start, :spot_range_start),
                                :spot_range_start
                            ),
                            spot_range_end = GREATEST(
                                COALESCE(spot_range_end, :spot_range_end),
                                :spot_range_end
                            ),
                            updated_at = NOW()
                        WHERE symbol = :symbol
                    """),
                    {
                        "symbol": row.symbol,
                        "spot_range_start": self.from_date,
                        "spot_range_end": self.to_date,
                    },
                )
                await session.commit()

            self._spot_cache.pop(row.symbol, None)
            stored_rows += len(payload)
            logger.info(f"Stored {len(payload)} spot candles for {row.symbol}")

        return stored_rows

    async def _refresh_selection_spots(self) -> int:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    WITH first_selection_bar AS (
                        SELECT DISTINCT ON (e.underlying, e.expiry)
                               e.underlying,
                               e.expiry,
                               s.time,
                               s.close
                        FROM fo_expiry_catalog e
                        JOIN underlying_spot_candles s
                          ON s.underlying = e.underlying
                         AND s.interval = :interval
                         AND timezone('Asia/Kolkata', s.time)::date = e.selection_date
                        WHERE e.selection_date IS NOT NULL
                          AND s.close IS NOT NULL
                        ORDER BY e.underlying, e.expiry, s.time
                    )
                    UPDATE fo_expiry_catalog e
                    SET selection_spot_time = f.time,
                        selection_spot_price = f.close,
                        updated_at = NOW()
                    FROM first_selection_bar f
                    WHERE e.underlying = f.underlying
                      AND e.expiry = f.expiry
                      AND (
                        e.selection_spot_time IS DISTINCT FROM f.time
                        OR e.selection_spot_price IS DISTINCT FROM f.close
                      )
                    RETURNING 1
                """),
                {"interval": self.interval},
            )
            rows = result.fetchall()
            await session.commit()
        return len(rows)

    async def _load_spot_map(self, underlying: str) -> dict[str, float]:
        if underlying in self._spot_cache:
            return self._spot_cache[underlying]

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT time, close
                    FROM underlying_spot_candles
                    WHERE underlying = :underlying
                      AND interval = :interval
                    ORDER BY time
                """),
                {"underlying": underlying, "interval": self.interval},
            )
            mapping = {
                row.time.astimezone(timezone.utc).isoformat(): float(row.close)
                for row in result.fetchall()
                if row.close is not None
            }
        self._spot_cache[underlying] = mapping
        return mapping

    def _build_option_rows(
        self,
        contract: dict,
        candles: list[dict],
        spot_map: dict[str, float],
    ) -> list[dict]:
        expiry_date = contract["expiry"]
        expiry_dt = datetime.combine(expiry_date, time(15, 30), tzinfo=IST)
        option_type = contract["option_type"]
        strike = float(contract["strike"])
        instrument_key = contract["instrument_key"]

        rows = []
        for candle in candles:
            ts = _parse_iso_ts(candle["time"])
            ts_key = ts.astimezone(timezone.utc).isoformat()
            spot = spot_map.get(ts_key)
            tte_years = max((expiry_dt - ts.astimezone(IST)).total_seconds() / SECONDS_PER_YEAR, 0.0)

            iv = delta = gamma = theta = vega = None
            if spot and tte_years > 0:
                iv = _implied_volatility(
                    option_type=option_type,
                    premium=float(candle["close"]),
                    spot=float(spot),
                    strike=strike,
                    tte_years=tte_years,
                    rate=self.risk_free_rate,
                )
                if iv:
                    delta, gamma, theta, vega = _option_greeks(
                        option_type=option_type,
                        spot=float(spot),
                        strike=strike,
                        tte_years=tte_years,
                        rate=self.risk_free_rate,
                        sigma=iv,
                    )

            rows.append(
                {
                    "time": ts,
                    "instrument_key": instrument_key,
                    "trading_symbol": contract["trading_symbol"],
                    "underlying": contract["underlying"],
                    "market": "NSE",
                    "expiry": expiry_date,
                    "strike": strike,
                    "option_type": option_type,
                    "interval": self.interval,
                    "open": float(candle["open"]),
                    "high": float(candle["high"]),
                    "low": float(candle["low"]),
                    "close": float(candle["close"]),
                    "volume": int(candle.get("volume", 0)),
                    "oi": int(candle.get("oi", 0)),
                    "iv": iv,
                    "delta": delta,
                    "gamma": gamma,
                    "theta": theta,
                    "vega": vega,
                    "underlying_price": float(spot) if spot is not None else None,
                    "time_to_expiry_years": tte_years if tte_years > 0 else None,
                    "source": "upstox_expired",
                }
            )
        return rows

    async def _rebuild_chain_metrics(self, touched_pairs: set[tuple[str, date]]) -> int:
        refreshed = 0
        async with AsyncSessionLocal() as session:
            for underlying, expiry in touched_pairs:
                await session.execute(
                    text("""
                        DELETE FROM fo_option_chain_metrics
                        WHERE underlying = :underlying
                          AND expiry = :expiry
                          AND interval = :interval
                    """),
                    {
                        "underlying": underlying,
                        "expiry": expiry,
                        "interval": self.interval,
                    },
                )
                await session.execute(
                    text("""
                        INSERT INTO fo_option_chain_metrics (
                            time, underlying, expiry, interval,
                            ce_contracts, pe_contracts,
                            ce_oi, pe_oi, ce_volume, pe_volume,
                            oi_pcr, volume_pcr, underlying_price, synced_at
                        )
                        SELECT
                            time,
                            underlying,
                            expiry,
                            interval,
                            SUM(CASE WHEN option_type = 'CE' THEN 1 ELSE 0 END) AS ce_contracts,
                            SUM(CASE WHEN option_type = 'PE' THEN 1 ELSE 0 END) AS pe_contracts,
                            SUM(CASE WHEN option_type = 'CE' THEN COALESCE(oi, 0) ELSE 0 END) AS ce_oi,
                            SUM(CASE WHEN option_type = 'PE' THEN COALESCE(oi, 0) ELSE 0 END) AS pe_oi,
                            SUM(CASE WHEN option_type = 'CE' THEN COALESCE(volume, 0) ELSE 0 END) AS ce_volume,
                            SUM(CASE WHEN option_type = 'PE' THEN COALESCE(volume, 0) ELSE 0 END) AS pe_volume,
                            CASE
                                WHEN SUM(CASE WHEN option_type = 'CE' THEN COALESCE(oi, 0) ELSE 0 END) = 0
                                THEN NULL
                                ELSE
                                    SUM(CASE WHEN option_type = 'PE' THEN COALESCE(oi, 0) ELSE 0 END)::NUMERIC
                                    / NULLIF(SUM(CASE WHEN option_type = 'CE' THEN COALESCE(oi, 0) ELSE 0 END), 0)
                            END AS oi_pcr,
                            CASE
                                WHEN SUM(CASE WHEN option_type = 'CE' THEN COALESCE(volume, 0) ELSE 0 END) = 0
                                THEN NULL
                                ELSE
                                    SUM(CASE WHEN option_type = 'PE' THEN COALESCE(volume, 0) ELSE 0 END)::NUMERIC
                                    / NULLIF(SUM(CASE WHEN option_type = 'CE' THEN COALESCE(volume, 0) ELSE 0 END), 0)
                            END AS volume_pcr,
                            AVG(underlying_price) AS underlying_price,
                            NOW()
                        FROM option_premium_candles
                        WHERE underlying = :underlying
                          AND expiry = :expiry
                          AND interval = :interval
                          AND instrument_key IS NOT NULL
                        GROUP BY time, underlying, expiry, interval
                    """),
                    {
                        "underlying": underlying,
                        "expiry": expiry,
                        "interval": self.interval,
                    },
                )
                refreshed += 1
            await session.commit()
        return refreshed

    async def _sync_contract_candles(self, limit: int) -> tuple[int, int, int, int]:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    WITH underlying_progress AS (
                        SELECT
                            underlying,
                            COUNT(*) FILTER (WHERE sync_status = 'complete') AS complete_contracts,
                            COUNT(*) FILTER (WHERE sync_status = 'pending') AS pending_contracts
                        FROM fo_contract_catalog
                        GROUP BY underlying
                    ),
                    initial_contracts AS (
                        SELECT
                            c.instrument_key,
                            c.trading_symbol,
                            c.underlying,
                            c.expiry,
                            c.strike,
                            c.option_type,
                            c.candle_from_date,
                            c.candle_to_date,
                            u.kind,
                            e.selection_spot_price,
                            ABS(c.strike - COALESCE(e.selection_spot_price, c.strike)) AS strike_gap,
                            COALESCE(p.complete_contracts, 0) AS underlying_complete_contracts,
                            COALESCE(p.pending_contracts, 0) AS underlying_pending_contracts,
                            ROW_NUMBER() OVER (
                                PARTITION BY c.underlying, c.expiry, c.option_type
                                ORDER BY
                                    CASE WHEN e.selection_spot_price IS NULL THEN 1 ELSE 0 END,
                                    ABS(c.strike - COALESCE(e.selection_spot_price, c.strike)),
                                    c.strike
                            ) AS strike_rank
                        FROM fo_contract_catalog c
                        JOIN fo_underlying_catalog u
                          ON u.symbol = c.underlying
                        LEFT JOIN fo_expiry_catalog e
                          ON e.underlying = c.underlying
                         AND e.expiry = c.expiry
                        LEFT JOIN underlying_progress p
                          ON p.underlying = c.underlying
                        WHERE c.sync_status = 'pending'
                    )
                    SELECT
                        instrument_key,
                        trading_symbol,
                        underlying,
                        expiry,
                        strike,
                        option_type,
                        candle_from_date,
                        candle_to_date,
                        selection_spot_price,
                        strike_gap
                    FROM initial_contracts
                    WHERE strike_rank <= 2
                    ORDER BY underlying_complete_contracts ASC,
                             CASE WHEN kind = 'STOCK' THEN 0 ELSE 1 END,
                             expiry DESC,
                             strike_rank ASC,
                             underlying ASC,
                             CASE WHEN option_type = 'CE' THEN 0 ELSE 1 END,
                             strike_gap ASC,
                             strike ASC
                    LIMIT :limit
                """),
                {"limit": limit},
            )
            rows = result.fetchall()

        stored_rows = 0
        completed = 0
        empty = 0
        touched_pairs: set[tuple[str, date]] = set()

        for row in rows:
            spot_map = await self._load_spot_map(row.underlying)
            try:
                candles = await self.client._fetch_candles_from_upstox(
                    row.instrument_key,
                    row.candle_from_date,
                    row.candle_to_date,
                )
            except UpstoxAuthError as exc:
                logger.error(
                    f"Stopping contract sync because Upstox authentication failed: {exc}"
                )
                raise

            status = "empty"
            payload = []
            if candles:
                payload = self._build_option_rows(
                    {
                        "instrument_key": row.instrument_key,
                        "trading_symbol": row.trading_symbol,
                        "underlying": row.underlying,
                        "expiry": row.expiry,
                        "strike": row.strike,
                        "option_type": row.option_type,
                    },
                    candles,
                    spot_map,
                )
                status = "complete"

            async with AsyncSessionLocal() as session:
                if payload:
                    await session.execute(
                        text("""
                            INSERT INTO option_premium_candles (
                                time, instrument_key, trading_symbol, underlying, market,
                                expiry, strike, option_type, interval, open, high, low,
                                close, volume, oi, iv, delta, gamma, theta, vega,
                                underlying_price, source, synced_at, time_to_expiry_years
                            )
                            VALUES (
                                :time, :instrument_key, :trading_symbol, :underlying, :market,
                                :expiry, :strike, :option_type, :interval, :open, :high, :low,
                                :close, :volume, :oi, :iv, :delta, :gamma, :theta, :vega,
                                :underlying_price, :source, NOW(), :time_to_expiry_years
                            )
                            ON CONFLICT (instrument_key, interval, time) DO UPDATE
                            SET trading_symbol = EXCLUDED.trading_symbol,
                                underlying = EXCLUDED.underlying,
                                market = EXCLUDED.market,
                                expiry = EXCLUDED.expiry,
                                strike = EXCLUDED.strike,
                                option_type = EXCLUDED.option_type,
                                open = EXCLUDED.open,
                                high = EXCLUDED.high,
                                low = EXCLUDED.low,
                                close = EXCLUDED.close,
                                volume = EXCLUDED.volume,
                                oi = EXCLUDED.oi,
                                iv = EXCLUDED.iv,
                                delta = EXCLUDED.delta,
                                gamma = EXCLUDED.gamma,
                                theta = EXCLUDED.theta,
                                vega = EXCLUDED.vega,
                                underlying_price = EXCLUDED.underlying_price,
                                source = EXCLUDED.source,
                                synced_at = NOW(),
                                time_to_expiry_years = EXCLUDED.time_to_expiry_years
                        """),
                        payload,
                    )

                await session.execute(
                    text("""
                        UPDATE fo_contract_catalog
                        SET sync_status = :sync_status,
                            candle_count = :candle_count,
                            first_candle_time = :first_candle_time,
                            last_candle_time = :last_candle_time,
                            last_synced_at = NOW(),
                            last_error = NULL,
                            updated_at = NOW()
                        WHERE instrument_key = :instrument_key
                    """),
                    {
                        "instrument_key": row.instrument_key,
                        "sync_status": status,
                        "candle_count": len(payload),
                        "first_candle_time": (
                            _parse_iso_ts(payload[0]["time"]) if payload else None
                        ),
                        "last_candle_time": (
                            _parse_iso_ts(payload[-1]["time"]) if payload else None
                        ),
                    },
                )
                await session.commit()

            if payload:
                stored_rows += len(payload)
                completed += 1
                touched_pairs.add((row.underlying, row.expiry))
                logger.info(
                    f"Stored {len(payload)} option candles for {row.trading_symbol}"
                )
            else:
                empty += 1
                logger.warning(f"No candles returned for {row.trading_symbol}")

        refreshed = await self._rebuild_chain_metrics(touched_pairs) if touched_pairs else 0
        return stored_rows, completed, empty, refreshed

    async def get_db_summary(self) -> dict:
        async with AsyncSessionLocal() as session:
            candles = await session.execute(
                text("""
                    SELECT
                        COUNT(*) AS option_candles,
                        COUNT(DISTINCT instrument_key) AS option_contracts,
                        COUNT(DISTINCT underlying) AS option_underlyings
                    FROM option_premium_candles
                    WHERE instrument_key IS NOT NULL
                """)
            )
            spot = await session.execute(
                text("""
                    SELECT COUNT(*) AS spot_candles
                    FROM underlying_spot_candles
                """)
            )
            chain = await session.execute(
                text("""
                    SELECT COUNT(*) AS chain_rows
                    FROM fo_option_chain_metrics
                """)
            )
            contract_status = await session.execute(
                text("""
                    SELECT sync_status, COUNT(*) AS contracts
                    FROM fo_contract_catalog
                    GROUP BY sync_status
                    ORDER BY sync_status
                """)
            )

            candle_row = candles.fetchone()
            return {
                "option_candles": int(candle_row.option_candles or 0),
                "option_contracts": int(candle_row.option_contracts or 0),
                "option_underlyings": int(candle_row.option_underlyings or 0),
                "spot_candles": int(spot.scalar() or 0),
                "chain_metric_rows": int(chain.scalar() or 0),
                "contract_status": {
                    row.sync_status: int(row.contracts)
                    for row in contract_status.fetchall()
                },
            }

    async def run_once(
        self,
        underlying_limit: int = 25,
        expiry_limit: int = 80,
        spot_limit: int = 25,
        contract_limit: int = 120,
    ) -> dict:
        summary = SyncSummary()
        primed = await self._prime_underlying_meta_cache()
        if primed:
            logger.info(f"Primed {primed} underlying metadata rows from Timescale cache")
        summary.universe_rows = await self._upsert_universe()
        summary.underlyings_synced, summary.expiries_discovered = await self._discover_underlyings(
            limit=underlying_limit
        )
        discovered_expiries, summary.contracts_discovered = await self._discover_contracts(
            limit=expiry_limit
        )
        summary.spot_candles_stored = await self._sync_spot_history(limit=spot_limit)
        summary.selection_spots_refreshed = await self._refresh_selection_spots()
        (
            summary.option_candles_stored,
            summary.contracts_completed,
            summary.contracts_empty,
            summary.chain_metrics_refreshed,
        ) = await self._sync_contract_candles(limit=contract_limit)

        db_summary = await self.get_db_summary()
        payload = {
            "run_summary": summary.to_dict(),
            "db_summary": db_summary,
            "discovered_expiry_batches": discovered_expiries,
            "from_date": self.from_date.isoformat(),
            "to_date": self.to_date.isoformat(),
            "interval": self.interval,
            "completed_at": _utc_now().isoformat(),
        }
        logger.info(f"Research sync run complete: {payload}")
        return payload

    async def run_forever(
        self,
        poll_minutes: int = 30,
        underlying_limit: int = 25,
        expiry_limit: int = 80,
        spot_limit: int = 25,
        contract_limit: int = 120,
    ) -> None:
        while True:
            try:
                self.to_date = max(self.to_date, date.today())
                await self.run_once(
                    underlying_limit=underlying_limit,
                    expiry_limit=expiry_limit,
                    spot_limit=spot_limit,
                    contract_limit=contract_limit,
                )
            except Exception as exc:
                logger.exception(f"Recurring research sync failed: {exc}")
            await asyncio.sleep(poll_minutes * 60)
