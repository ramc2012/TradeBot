from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

from loguru import logger
from sqlalchemy import text

from analysis.backtest import MACDBacktester, UpstoxAuthError
from analysis.instruments import get_first_trading_day_after, get_fo_market
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
    DISCOVERY_COMMON_STRIKES = 2
    DISCOVERY_SIDE_FALLBACK = 0
    PRIORITY_SKIP_REASON = "Skipped outside prioritized strike window"
    DISCOVERY_BACKLOG_MULTIPLIER = 2
    DISCOVERY_BACKLOG_FLOOR = 240
    SINGLE_CALL_30MINUTE_WINDOW_DAYS = 366
    EXPIRY_LOOKAHEAD_DAYS = 60

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

    def _expiry_metadata_to_date(self, today: Optional[date] = None) -> date:
        reference_day = today or date.today()
        return max(self.to_date, reference_day + timedelta(days=self.EXPIRY_LOOKAHEAD_DAYS))

    def _expired_contract_discovery_to_date(self, today: Optional[date] = None) -> date:
        reference_day = today or date.today()
        return min(self.to_date, reference_day)

    async def _get_backlog_snapshot(self) -> dict[str, int]:
        async with AsyncSessionLocal() as session:
            contract_status = await session.execute(
                text("""
                    SELECT
                        COUNT(*) FILTER (WHERE sync_status = 'pending') AS pending_contracts,
                        COUNT(*) FILTER (WHERE sync_status = 'complete') AS complete_contracts,
                        COUNT(*) FILTER (WHERE sync_status = 'empty') AS empty_contracts
                    FROM fo_contract_catalog
                """)
            )
            expiry_status = await session.execute(
                text("""
                    SELECT
                        COUNT(*) FILTER (WHERE contracts_discovered_at IS NULL) AS undiscovered_expiry_batches
                    FROM fo_expiry_catalog
                """)
            )
            row = contract_status.fetchone()
        return {
            "pending_contracts": int(row.pending_contracts or 0),
            "complete_contracts": int(row.complete_contracts or 0),
            "empty_contracts": int(row.empty_contracts or 0),
            "undiscovered_expiry_batches": int(expiry_status.scalar() or 0),
        }

    @classmethod
    def _should_pause_discovery(
        cls,
        *,
        pending_contracts: int,
        contract_limit: int,
    ) -> bool:
        backlog_threshold = max(cls.DISCOVERY_BACKLOG_FLOOR, contract_limit * cls.DISCOVERY_BACKLOG_MULTIPLIER)
        return pending_contracts > backlog_threshold

    @staticmethod
    def _select_contract_sync_batch(
        rows: list[dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]]:
        if limit <= 0 or not rows:
            return []

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(row["underlying"])].append(row)

        ordered_underlyings = sorted(
            grouped,
            key=lambda underlying: (
                int(grouped[underlying][0].get("underlying_pending_contracts") or len(grouped[underlying])),
                grouped[underlying][0].get("kind") != "INDEX",
                -int(grouped[underlying][0].get("underlying_complete_contracts") or 0),
                underlying,
            ),
        )

        selected: list[dict[str, Any]] = []
        for underlying in ordered_underlyings:
            contracts = grouped[underlying]
            if selected and len(selected) + len(contracts) > limit:
                continue
            if not selected and len(contracts) > limit:
                selected.extend(contracts[:limit])
                break
            selected.extend(contracts)
            if len(selected) >= limit:
                break

        return selected[:limit]

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
        if (
            self.interval == "30minute"
            and (to_date - from_date).days <= self.SINGLE_CALL_30MINUTE_WINDOW_DAYS
        ):
            return await self.client._fetch_candles_from_upstox(
                instrument_key,
                from_date,
                to_date,
            )

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
        try:
            universe = await self.client.fetch_fo_universe()
        except Exception as exc:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text("SELECT COUNT(*) FROM fo_underlying_catalog")
                )
                cached_rows = int(result.scalar() or 0)
            if cached_rows > 0:
                logger.warning(
                    "Could not refresh NSE F&O universe; reusing "
                    f"{cached_rows} cached underlying rows: {exc}"
                )
                return 0
            raise

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
        expiry_to_date = self._expiry_metadata_to_date()
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
            try:
                meta = await self.client._resolve_underlying_metadata(symbol)
                expiry_dates = await self.client._fetch_expiry_dates(symbol)
            except UpstoxAuthError as exc:
                logger.error(
                    f"Stopping expiry discovery because Upstox authentication failed: {exc}"
                )
                raise
            except Exception as exc:
                logger.warning(f"Skipping expiry metadata for {symbol}: {exc}")
                continue
            monthly_expiries, prev_map = self.client._select_monthly_expiries(
                expiry_dates,
                self.from_date,
                expiry_to_date,
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

    @staticmethod
    def _strike_center(
        strikes: list[float],
        selection_spot_price: Optional[float],
    ) -> float:
        if selection_spot_price is not None and selection_spot_price > 0:
            return float(selection_spot_price)
        ordered = sorted(strikes)
        if not ordered:
            return 0.0
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2.0

    def _prioritized_contract_keys(
        self,
        contracts: list[dict[str, Any]],
        selection_spot_price: Optional[float],
    ) -> set[str]:
        by_type: dict[str, dict[float, str]] = {"CE": {}, "PE": {}}
        all_strikes: list[float] = []

        for contract in contracts:
            instrument_key = contract.get("instrument_key")
            option_type = str(contract.get("option_type") or contract.get("instrument_type") or "").upper()
            if not instrument_key or option_type not in ("CE", "PE"):
                continue
            try:
                strike = float(contract.get("strike") or contract.get("strike_price") or 0.0)
            except (TypeError, ValueError):
                continue
            if strike <= 0:
                continue
            by_type[option_type][strike] = str(instrument_key)
            all_strikes.append(strike)

        if not all_strikes:
            return set()

        center = self._strike_center(all_strikes, selection_spot_price)
        priority_keys: set[str] = set()
        selected_per_type = {"CE": 0, "PE": 0}

        common_strikes = sorted(
            set(by_type["CE"]) & set(by_type["PE"]),
            key=lambda strike: (abs(strike - center), strike),
        )
        for strike in common_strikes[: self.DISCOVERY_COMMON_STRIKES]:
            for option_type in ("CE", "PE"):
                instrument_key = by_type[option_type][strike]
                if instrument_key not in priority_keys:
                    priority_keys.add(instrument_key)
                    selected_per_type[option_type] += 1

        for option_type in ("CE", "PE"):
            ranked_strikes = sorted(
                by_type[option_type],
                key=lambda strike: (abs(strike - center), strike),
            )
            for strike in ranked_strikes:
                if selected_per_type[option_type] >= self.DISCOVERY_SIDE_FALLBACK:
                    break
                instrument_key = by_type[option_type][strike]
                if instrument_key in priority_keys:
                    continue
                priority_keys.add(instrument_key)
                selected_per_type[option_type] += 1

        return priority_keys

    def _filter_useful_contracts(
        self,
        contracts: list[dict[str, Any]],
        selection_spot_price: Optional[float],
    ) -> list[dict[str, Any]]:
        priority_keys = self._prioritized_contract_keys(contracts, selection_spot_price)
        if not priority_keys:
            return []
        return [
            contract
            for contract in contracts
            if contract.get("instrument_key") in priority_keys
        ]

    def _desired_contract_state(
        self,
        *,
        current_status: Optional[str],
        current_last_error: Optional[str],
        prioritized: bool,
    ) -> tuple[str, Optional[str]]:
        normalized_status = str(current_status or "pending").lower()
        if prioritized:
            if normalized_status in {"complete", "empty"}:
                return normalized_status, current_last_error
            return "pending", None
        return "skipped", self.PRIORITY_SKIP_REASON

    async def _discover_contracts(self, limit: int) -> tuple[int, int]:
        discovery_cutoff = self._expired_contract_discovery_to_date()
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
                    SELECT e.underlying, e.expiry, e.selection_spot_price
                    FROM fo_expiry_catalog e
                    JOIN fo_underlying_catalog u
                      ON u.symbol = e.underlying
                    LEFT JOIN underlying_progress p
                      ON p.underlying = e.underlying
                    WHERE contracts_discovered_at IS NULL
                      AND e.expiry <= :discovery_cutoff
                    ORDER BY COALESCE(p.complete_contracts, 0),
                             CASE WHEN u.kind = 'STOCK' THEN 0 ELSE 1 END,
                             e.expiry DESC,
                             e.underlying
                    LIMIT :limit
                """),
                {
                    "discovery_cutoff": discovery_cutoff,
                    "limit": limit,
                },
            )
            rows = result.fetchall()

        discovered_expiries = 0
        contract_rows = 0
        for row in rows:
            underlying = row.underlying
            expiry = row.expiry
            selection_spot_price = (
                float(row.selection_spot_price)
                if row.selection_spot_price is not None
                else None
            )
            try:
                contracts = await self.client._fetch_expired_contracts(underlying, expiry)
            except UpstoxAuthError as exc:
                logger.error(
                    f"Stopping contract discovery because Upstox authentication failed: {exc}"
                )
                raise

            payload = []
            useful_contracts = self._filter_useful_contracts(contracts, selection_spot_price)
            for contract in useful_contracts:
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
                        "market": get_fo_market(underlying),
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
                                strike, option_type, lot_size, market, tick_size,
                                minimum_lot, freeze_quantity, candle_from_date,
                                candle_to_date, updated_at
                            )
                            VALUES (
                                :instrument_key, :trading_symbol, :underlying, :expiry,
                                :strike, :option_type, :lot_size, :market, :tick_size,
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
                                market = EXCLUDED.market,
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

    async def _reprioritize_contract_backlog(self, expiry_limit: Optional[int] = None) -> int:
        limit_clause = ""
        params: dict[str, Any] = {}
        if expiry_limit is not None and expiry_limit > 0:
            limit_clause = "LIMIT :expiry_limit"
            params["expiry_limit"] = expiry_limit

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(f"""
                    WITH target_expiries AS (
                        SELECT
                            c.underlying,
                            c.expiry,
                            MAX(
                                CASE
                                    WHEN c.sync_status IN ('pending', 'complete', 'empty')
                                    THEN 1
                                    ELSE 0
                                END
                            ) AS has_active_contracts,
                            MAX(COALESCE(c.updated_at, c.created_at)) AS last_touched_at
                        FROM fo_contract_catalog c
                        WHERE c.sync_status IN ('pending', 'skipped', 'complete', 'empty')
                        GROUP BY c.underlying, c.expiry
                        ORDER BY has_active_contracts DESC,
                                 last_touched_at DESC,
                                 c.expiry DESC,
                                 c.underlying
                        {limit_clause}
                    )
                    SELECT
                        c.instrument_key,
                        c.underlying,
                        c.expiry,
                        c.strike,
                        c.option_type,
                        c.sync_status,
                        c.last_error,
                        e.selection_spot_price
                    FROM fo_contract_catalog c
                    JOIN target_expiries t
                      ON t.underlying = c.underlying
                     AND t.expiry = c.expiry
                    LEFT JOIN fo_expiry_catalog e
                      ON e.underlying = c.underlying
                     AND e.expiry = c.expiry
                    WHERE c.sync_status IN ('pending', 'skipped', 'complete', 'empty')
                    ORDER BY c.underlying, c.expiry, c.option_type, c.strike
                """),
                params,
            )
            rows = result.mappings().all()

        grouped_rows: dict[tuple[str, date], list[dict[str, Any]]] = defaultdict(list)
        selection_spot_map: dict[tuple[str, date], Optional[float]] = {}
        for row in rows:
            key = (row["underlying"], row["expiry"])
            grouped_rows[key].append(dict(row))
            if key not in selection_spot_map:
                selection_spot_map[key] = (
                    float(row["selection_spot_price"])
                    if row["selection_spot_price"] is not None
                    else None
                )

        updates: list[dict[str, Any]] = []
        for key, contracts in grouped_rows.items():
            priority_keys = self._prioritized_contract_keys(contracts, selection_spot_map.get(key))
            for contract in contracts:
                desired_status, desired_last_error = self._desired_contract_state(
                    current_status=contract["sync_status"],
                    current_last_error=contract.get("last_error"),
                    prioritized=contract["instrument_key"] in priority_keys,
                )
                if (
                    contract["sync_status"] == desired_status
                    and contract.get("last_error") == desired_last_error
                ):
                    continue
                updates.append(
                    {
                        "instrument_key": contract["instrument_key"],
                        "sync_status": desired_status,
                        "last_error": desired_last_error,
                    }
                )

        if not updates:
            return 0

        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    UPDATE fo_contract_catalog
                    SET sync_status = :sync_status,
                        last_error = :last_error,
                        updated_at = NOW()
                    WHERE instrument_key = :instrument_key
                """),
                updates,
            )
            await session.commit()
        return len(updates)

    async def _sync_spot_history(self, limit: int) -> int:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    WITH actual_spot_ranges AS (
                        SELECT
                            underlying,
                            MIN(timezone('Asia/Kolkata', time)::date) AS actual_range_start,
                            MAX(timezone('Asia/Kolkata', time)::date) AS actual_range_end
                        FROM underlying_spot_candles
                        WHERE interval = :interval
                        GROUP BY underlying
                    )
                    SELECT
                        u.symbol,
                        u.kind,
                        u.spot_instrument_key,
                        u.spot_range_start,
                        u.spot_range_end,
                        a.actual_range_start,
                        a.actual_range_end
                    FROM fo_underlying_catalog u
                    LEFT JOIN actual_spot_ranges a
                      ON a.underlying = u.symbol
                    WHERE u.spot_instrument_key IS NOT NULL
                      AND (
                        u.spot_synced_at IS NULL
                        OR u.spot_range_start IS NULL
                        OR u.spot_range_end IS NULL
                        OR a.actual_range_start IS NULL
                        OR a.actual_range_end IS NULL
                        OR a.actual_range_start > :from_date
                        OR a.actual_range_end < :to_date
                        OR u.spot_range_start IS DISTINCT FROM a.actual_range_start
                        OR u.spot_range_end IS DISTINCT FROM a.actual_range_end
                      )
                    ORDER BY CASE WHEN u.kind = 'INDEX' THEN 0 ELSE 1 END,
                             COALESCE(a.actual_range_end, u.spot_range_end, DATE '1900-01-01'),
                             u.symbol
                    LIMIT :limit
                """),
                {
                    "interval": self.interval,
                    "limit": limit,
                    "from_date": self.from_date,
                    "to_date": self.to_date,
                },
            )
            rows = result.fetchall()

        stored_rows = 0
        for row in rows:
            fetch_windows: list[tuple[date, date]] = []
            current_start = row.actual_range_start or row.spot_range_start
            current_end = row.actual_range_end or row.spot_range_end
            if current_start is None or current_end is None:
                fetch_windows.append((self.from_date, self.to_date))
            else:
                if current_start > self.from_date:
                    fetch_windows.append(
                        (self.from_date, current_start - timedelta(days=1))
                    )
                if current_end < self.to_date:
                    fetch_windows.append(
                        (current_end + timedelta(days=1), self.to_date)
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
                if (
                    current_start is not None
                    and current_end is not None
                    and (
                        row.spot_range_start != current_start
                        or row.spot_range_end != current_end
                    )
                ):
                    async with AsyncSessionLocal() as session:
                        await session.execute(
                            text("""
                                UPDATE fo_underlying_catalog
                                SET spot_range_start = :spot_range_start,
                                    spot_range_end = :spot_range_end,
                                    updated_at = NOW()
                                WHERE symbol = :symbol
                            """),
                            {
                                "symbol": row.symbol,
                                "spot_range_start": current_start,
                                "spot_range_end": current_end,
                            },
                        )
                        await session.commit()
                    logger.info(
                        f"Repaired spot range metadata for {row.symbol}: "
                        f"{current_start} to {current_end}"
                    )
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
            payload_dates = [
                item["time"].astimezone(IST).date()
                for item in payload
            ]
            actual_range_start = min(
                [date_value for date_value in (current_start, *payload_dates) if date_value is not None],
                default=None,
            )
            actual_range_end = max(
                [date_value for date_value in (current_end, *payload_dates) if date_value is not None],
                default=None,
            )

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
                            spot_range_start = :spot_range_start,
                            spot_range_end = :spot_range_end,
                            updated_at = NOW()
                        WHERE symbol = :symbol
                    """),
                    {
                        "symbol": row.symbol,
                        "spot_range_start": actual_range_start,
                        "spot_range_end": actual_range_end,
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
                        kind,
                        selection_spot_price,
                        strike_gap,
                        underlying_complete_contracts,
                        underlying_pending_contracts
                    FROM initial_contracts
                    WHERE strike_rank <= 2
                    ORDER BY underlying_pending_contracts ASC,
                             underlying_complete_contracts DESC,
                             CASE WHEN kind = 'INDEX' THEN 0 ELSE 1 END,
                             expiry DESC,
                             strike_rank ASC,
                             underlying ASC,
                             CASE WHEN option_type = 'CE' THEN 0 ELSE 1 END,
                             strike_gap ASC,
                             strike ASC
                """),
            )
            rows = [dict(row._mapping) for row in result.fetchall()]

        rows = self._select_contract_sync_batch(rows, limit)

        stored_rows = 0
        completed = 0
        empty = 0
        touched_pairs: set[tuple[str, date]] = set()

        for row in rows:
            spot_map = await self._load_spot_map(row["underlying"])
            fallback_to_date = min(self.to_date, row["expiry"])
            fallback_from_date = max(
                self.from_date,
                fallback_to_date - timedelta(days=365),
            )
            fetch_from_date = row["candle_from_date"] or fallback_from_date
            fetch_to_date = row["candle_to_date"] or fallback_to_date
            if fetch_to_date < fetch_from_date:
                fetch_from_date = fetch_to_date
            try:
                candles = await self.client._fetch_candles_from_upstox(
                    row["instrument_key"],
                    fetch_from_date,
                    fetch_to_date,
                )
            except UpstoxAuthError as exc:
                if str(row["instrument_key"]).count("|") < 2:
                    logger.error(
                        f"Stopping contract sync because Upstox authentication failed: {exc}"
                    )
                    raise
                logger.warning(
                    "Skipping expired option contract after Upstox rejected the "
                    f"expired-candle request for {row['trading_symbol']}: {exc}"
                )
                async with AsyncSessionLocal() as session:
                    await session.execute(
                        text("""
                            UPDATE fo_contract_catalog
                            SET sync_status = 'empty',
                                candle_count = 0,
                                candle_from_date = :candle_from_date,
                                candle_to_date = :candle_to_date,
                                first_candle_time = NULL,
                                last_candle_time = NULL,
                                last_synced_at = NOW(),
                                last_error = :last_error,
                                updated_at = NOW()
                            WHERE instrument_key = :instrument_key
                        """),
                        {
                            "instrument_key": row["instrument_key"],
                            "candle_from_date": fetch_from_date,
                            "candle_to_date": fetch_to_date,
                            "last_error": str(exc)[:500],
                        },
                    )
                    await session.commit()
                empty += 1
                continue

            status = "empty"
            payload = []
            if candles:
                payload = self._build_option_rows(
                    {
                        "instrument_key": row["instrument_key"],
                        "trading_symbol": row["trading_symbol"],
                        "underlying": row["underlying"],
                        "expiry": row["expiry"],
                        "strike": row["strike"],
                        "option_type": row["option_type"],
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
                            candle_from_date = :candle_from_date,
                            candle_to_date = :candle_to_date,
                            first_candle_time = :first_candle_time,
                            last_candle_time = :last_candle_time,
                            last_synced_at = NOW(),
                            last_error = NULL,
                            updated_at = NOW()
                        WHERE instrument_key = :instrument_key
                    """),
                    {
                        "instrument_key": row["instrument_key"],
                        "sync_status": status,
                        "candle_count": len(payload),
                        "candle_from_date": fetch_from_date,
                        "candle_to_date": fetch_to_date,
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
                touched_pairs.add((row["underlying"], row["expiry"]))
                logger.info(
                    f"Stored {len(payload)} option candles for {row['trading_symbol']}"
                )
            else:
                empty += 1
                if row["expiry"] < date.today():
                    logger.info(f"No expired candles returned for {row['trading_symbol']}; marking empty.")
                else:
                    logger.warning(f"No candles returned for {row['trading_symbol']}")

        refreshed = await self._rebuild_chain_metrics(touched_pairs) if touched_pairs else 0
        return stored_rows, completed, empty, refreshed

    async def get_db_summary(self) -> dict:
        async with AsyncSessionLocal() as session:
            candles = await session.execute(
                text("""
                    SELECT
                        COUNT(*) AS option_candles,
                        COUNT(DISTINCT o.instrument_key) AS option_contracts,
                        COUNT(DISTINCT o.underlying) AS option_underlyings
                    FROM option_premium_candles o
                    JOIN fo_contract_catalog c
                      ON c.instrument_key = o.instrument_key
                    WHERE o.instrument_key IS NOT NULL
                      AND c.sync_status <> 'skipped'
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
        self.client.reset_rate_limit_stats()
        primed = await self._prime_underlying_meta_cache()
        if primed:
            logger.info(f"Primed {primed} underlying metadata rows from Timescale cache")
        summary.universe_rows = await self._upsert_universe()
        summary.underlyings_synced, summary.expiries_discovered = await self._discover_underlyings(
            limit=underlying_limit
        )
        summary.spot_candles_stored = await self._sync_spot_history(limit=spot_limit)
        summary.selection_spots_refreshed = await self._refresh_selection_spots()
        backlog_before = await self._get_backlog_snapshot()
        discovery_paused = self._should_pause_discovery(
            pending_contracts=backlog_before["pending_contracts"],
            contract_limit=contract_limit,
        )
        discovered_expiries = 0
        if discovery_paused:
            logger.info(
                "Skipping new contract discovery this pass to drain pending backlog "
                f"({backlog_before['pending_contracts']} pending > focus threshold)."
            )
        else:
            discovered_expiries, summary.contracts_discovered = await self._discover_contracts(
                limit=expiry_limit
            )
        await self._reprioritize_contract_backlog()
        (
            summary.option_candles_stored,
            summary.contracts_completed,
            summary.contracts_empty,
            summary.chain_metrics_refreshed,
        ) = await self._sync_contract_candles(limit=contract_limit)

        # Daily F&O risk ingest (MWPL + ban list). The function below
        # is idempotent and the inserts upsert by (snapshot_date, symbol)
        # so calling it on every research sync pass is safe — NSE only
        # refreshes the files once per day (~18:30 IST).
        try:
            from market_data.fo_risk_ingest import ingest_fo_risk_snapshot

            risk_summary = await ingest_fo_risk_snapshot()
            logger.info(
                f"[ResearchSync] FO risk ingest: mwpl={risk_summary.mwpl_inserted} "
                f"ban={risk_summary.ban_inserted} errors={len(risk_summary.errors)}"
            )
        except Exception as exc:
            logger.warning(f"[ResearchSync] FO risk ingest skipped: {exc}")
            risk_summary = None

        db_summary = await self.get_db_summary()
        contract_status = db_summary.get("contract_status") or {}
        # Surface the per-batch caps + design intent so the operator can
        # immediately see why X contracts are "skipped" without having to
        # grep the source. Skipped is a deliberate priority-window
        # filter, not an error.
        design_notes = {
            "discovery_common_strikes": self.DISCOVERY_COMMON_STRIKES,
            "discovery_side_fallback": self.DISCOVERY_SIDE_FALLBACK,
            "discovery_backlog_floor": self.DISCOVERY_BACKLOG_FLOOR,
            "skipped_reason": (
                "Contracts outside the prioritized strike window are tagged 'skipped' by "
                f"design — DISCOVERY_COMMON_STRIKES={self.DISCOVERY_COMMON_STRIKES} keeps "
                "only the closest CE/PE pair to spot. Increase to widen coverage."
            ),
            "skipped_contracts": int(contract_status.get("skipped") or 0),
        }
        payload = {
            "run_summary": summary.to_dict(),
            "db_summary": db_summary,
            "design_notes": design_notes,
            "discovered_expiry_batches": discovered_expiries,
            "backlog_before": backlog_before,
            "focus_mode": "backlog_drain" if discovery_paused else "discovery_and_sync",
            "fo_risk_ingest": risk_summary.to_dict() if risk_summary is not None else None,
            "api_calls": {
                "total": int(sum(self.client.api_call_counts.values())),
                "by_endpoint": dict(sorted(self.client.api_call_counts.items())),
            },
            "rate_limit": {
                "inter_call_delay_seconds": self.client.rate_limit_delay,
                "hits": self.client.rate_limit_hits,
                "backoff_seconds": round(self.client.rate_limit_backoff_seconds, 2),
            },
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
                self.to_date = self._expiry_metadata_to_date()
                await self.run_once(
                    underlying_limit=underlying_limit,
                    expiry_limit=expiry_limit,
                    spot_limit=spot_limit,
                    contract_limit=contract_limit,
                )
            except Exception as exc:
                logger.exception(f"Recurring research sync failed: {exc}")
            await asyncio.sleep(poll_minutes * 60)
