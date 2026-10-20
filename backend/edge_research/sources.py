"""Bounded read-only candle reads and Upstox GETs; no history repair or orders."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import gzip
import json
import os
from pathlib import Path

import httpx

from sqlalchemy import text

from edge_research.engine import IST, number, ts


def quote_time(raw):
    try:
        if isinstance(raw, str) and raw.isdigit():
            raw = float(raw)
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(
                raw / 1000 if raw > 10**11 else raw, tz=timezone.utc
            ).isoformat()
        return ts(raw).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def normalize_quote(raw: dict, observed_at: str) -> dict:
    def depth(side):
        return [
            {"price": number(r.get("price")), "quantity": number(r.get("quantity"))}
            for r in ((raw.get("depth") or {}).get(side) or [])[:5]
            if isinstance(r, dict)
        ]

    bids, asks = depth("buy"), depth("sell")
    return {
        "instrument_key": raw.get("instrument_token") or raw.get("instrument_key"),
        "time": quote_time(raw.get("timestamp")),
        "observed_at": observed_at,
        "last_trade_time": quote_time(raw.get("last_trade_time")),
        "bid": bids[0]["price"] if bids else None,
        "ask": asks[0]["price"] if asks else None,
        "bid_size": bids[0]["quantity"] if bids else None,
        "ask_size": asks[0]["quantity"] if asks else None,
        "bids": bids,
        "asks": asks,
        "ltp": number(raw.get("last_price")),
        "volume": number(raw.get("volume")),
        "oi": number(raw.get("oi")),
        "source": "upstox_full_quote",
    }


def aggregate_five(rows: list[dict], observed_at: str) -> list[dict]:
    buckets = defaultdict(list)
    for row in rows:
        start = ts(row["time"])
        if start + timedelta(minutes=1) <= ts(observed_at):
            buckets[
                start.replace(minute=(start.minute // 5) * 5, second=0, microsecond=0)
            ].append(row)
    out = []
    for start, group in sorted(buckets.items()):
        group.sort(key=lambda r: ts(r["time"]))
        times = [ts(r["time"]) for r in group]
        if times != [
            start + timedelta(minutes=i) for i in range(5)
        ] or start + timedelta(minutes=5) > ts(observed_at):
            continue
        if any(
            any(number(r.get(k)) is None for k in ("open", "high", "low", "close"))
            for r in group
        ):
            continue
        out.append(
            {
                "start": start.isoformat(),
                "end": (start + timedelta(minutes=5)).isoformat(),
                "available_at": observed_at,
                "open": float(group[0]["open"]),
                "high": max(float(r["high"]) for r in group),
                "low": min(float(r["low"]) for r in group),
                "close": float(group[-1]["close"]),
                "volume": sum(number(r.get("volume")) or 0 for r in group),
            }
        )
    # Keep only the contiguous tail. Missing minutes remain missing, never fill.
    cut = len(out) - 1
    while cut > 0 and ts(out[cut]["start"]) == ts(out[cut - 1]["end"]):
        cut -= 1
    return out[cut:][-90:]


class QuoteReader:
    """Only two broker GET endpoints are reachable through this reader."""

    def __init__(self, token):
        self.token = token

    async def get(self, path, params):
        if path not in {"/market-quote/quotes", "/option/chain"}:
            raise ValueError("Endpoint outside research data allowlist")
        from brokers.rate_limiter import CLASS_BULK, UPSTOX_DATA_LIMITER, broker_class

        with broker_class(CLASS_BULK):
            await UPSTOX_DATA_LIMITER.acquire()
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    "https://api.upstox.com/v2" + path,
                    params=params,
                    headers={
                        "Authorization": "Bearer " + self.token,
                        "Accept": "application/json",
                    },
                )
                response.raise_for_status()
                return response.json()


class LiveSource:
    def __init__(self):
        self.adapter = None
        self.greek_cache = {}
        self.mcx_master = None
        self.engine = None

    async def close(self):
        if self.engine is not None:
            await self.engine.dispose()

    async def _adapter(self):
        # Do not import api.routers.auth: its import-time credential migration
        # can write to the application store. Read/decrypt only this one field.
        from core.security import decrypt_token

        path = Path(os.environ.get("CREDENTIALS_FILE", "credentials.json"))
        token = None
        if path.exists():
            raw = json.loads(path.read_text())
            credentials = raw.get("data", raw).get("upstox") or {}
            # Match the app's saved-token policy for DATA ONLY. The reader
            # cannot access any order endpoint; no trading session is enabled.
            self.credential_role = (
                "analytics_token"
                if credentials.get("analytics_token")
                else "access_token"
            )
            token = credentials.get(self.credential_role)
        if token:
            token = (
                decrypt_token(token[len("fernet::") :])
                if token.startswith("fernet::")
                else token
            )
            self.adapter = QuoteReader(token)
        else:
            self.adapter = None
        return self.adapter

    async def _future(self, symbol, now):
        if self.mcx_master is None or self.mcx_master[0] != now.astimezone(IST).date():
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(
                    "https://assets.upstox.com/market-quote/instruments/exchange/MCX.json.gz"
                )
                response.raise_for_status()
                body = response.content
                raw = json.loads(
                    gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body
                )
                self.mcx_master = (now.astimezone(IST).date(), raw)
        contracts = []
        for r in self.mcx_master[1]:
            if (
                r.get("instrument_type") != "FUT"
                or r.get("segment") != "MCX_FO"
                or str(r.get("underlying_symbol") or r.get("name") or "").upper()
                != symbol
            ):
                continue
            expiry = datetime.fromtimestamp(
                float(r["expiry"]) / 1000, tz=timezone.utc
            ).date()
            if expiry <= now.astimezone(IST).date():
                continue
            contracts.append(
                {
                    "symbol": f"MCX:{symbol}{expiry:%y%b}FUT".upper(),
                    "root": symbol,
                    "instrument_key": r["instrument_key"],
                    "trading_symbol": r["trading_symbol"],
                    "expiry": expiry.isoformat(),
                    "lot_size": int(r.get("lot_size") or 0),
                    "pnl_units_per_lot": int(number(r.get("qty_multiplier")) or 0),
                    "depth_required": 1,
                    "depth_quantity_basis": "MCX_contract_count",
                    "metadata_source": "upstox_mcx_instrument_master",
                }
            )
        return min(contracts, key=lambda r: r["expiry"], default=None)

    async def capture(self, symbol, market, held_contracts):
        from core.trading_calendar import trading_calendar
        from core.config import settings
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

        if self.engine is None:
            # Enforced on EVERY new connection, including before its first
            # statement. This process cannot accidentally write application SQL.
            self.engine = create_async_engine(
                settings.DATABASE_URL,
                pool_size=1,
                max_overflow=0,
                connect_args={
                    "timeout": 8,
                    "server_settings": {
                        "default_transaction_read_only": "on",
                        "statement_timeout": "8000",
                    },
                },
            )
        Session = async_sessionmaker(self.engine, expire_on_commit=False)

        now = datetime.now(timezone.utc)
        status = trading_calendar.exchange_status(market, now)
        sessions = status.get("today_sessions") or []
        closing = max((s["close"] for s in sessions), default="00:00")
        session_close = datetime.combine(
            now.astimezone(IST).date(),
            datetime.strptime(closing, "%H:%M").time(),
            tzinfo=IST,
        )
        frame = {
            "symbol": symbol,
            "market": market,
            "observed_at": now.isoformat(),
            "market_open": status["is_open"],
            "session_exit_due": 0 <= (session_close - now).total_seconds() <= 60,
            "bars": [],
            "contracts": {},
            "alternatives": [],
            "greeks": [],
            "coverage": [],
        }
        if not frame["market_open"]:
            return frame, []
        adapter = await self._adapter()
        if not adapter:
            frame["coverage"].append("upstox_token_unavailable")
            return frame, []
        frame["data_credential_role"] = self.credential_role
        fut = None
        if market == "MCX":
            # Discover metadata by GET only; do not call the catalog archiver.
            try:
                fut = await self._future(symbol, now)
            except httpx.HTTPError:
                frame["coverage"].append("contract_master_unavailable")
        session_start = now.astimezone(IST).replace(
            hour=9, minute=0 if market == "MCX" else 15, second=0, microsecond=0
        )
        async with Session() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            if await session.scalar(text("SHOW transaction_read_only")) != "on":
                raise RuntimeError("Research connection is not read-only")
            frame["database_read_only"] = True
            await session.execute(text("SET LOCAL statement_timeout = '8000ms'"))
            allowed = [fut["instrument_key"], fut["symbol"]] if fut else []
            result = await session.execute(
                text(
                    """
                SELECT time,open,high,low,close,volume,instrument_key,source,synced_at
                FROM underlying_spot_candles
                WHERE underlying=:symbol AND interval='1minute'
                  AND time>=CAST(:start AS timestamptz)
                  AND time<CAST(:now AS timestamptz) - interval '1 minute'
                  AND synced_at<=CAST(:now AS timestamptz)
                  AND (CAST(:is_index AS boolean) OR instrument_key=ANY(CAST(:allowed AS text[])))
                ORDER BY time ASC
            """
                ),
                {
                    "symbol": symbol,
                    "start": session_start,
                    "now": now,
                    "is_index": market == "NSE",
                    "allowed": allowed,
                },
            )
            raw = [dict(r) for r in result.mappings().all()]
            # One coherent source/instrument, never concatenate duplicates or rolls.
            grouped = defaultdict(list)
            for row in raw:
                grouped[(row["instrument_key"], row["source"])].append(row)
            coherent = max(
                grouped.values(),
                key=lambda g: (max(r["time"] for r in g), len(g)),
                default=[],
            )
            stamp = datetime.now(timezone.utc).isoformat()
            for r in coherent:
                r["time"] = r["time"].isoformat()
            frame["bars"] = aggregate_five(coherent, stamp)
            frame["candle_source"] = (
                {
                    "instrument_key": coherent[0]["instrument_key"],
                    "source": coherent[0]["source"],
                }
                if coherent
                else None
            )
            underlying_key = None
            if market == "NSE" and frame["bars"]:
                result = await session.execute(
                    text(
                        "SELECT underlying_key,spot_instrument_key FROM fo_underlying_catalog WHERE symbol=:s"
                    ),
                    {"s": symbol},
                )
                meta = result.mappings().first()
                underlying_key = (
                    (meta["underlying_key"] or meta["spot_instrument_key"])
                    if meta
                    else None
                )
                result = await session.execute(
                    text(
                        """
                    SELECT instrument_key,trading_symbol,expiry,strike,option_type,lot_size
                    FROM fo_contract_catalog WHERE underlying=:s AND option_type IN ('CE','PE')
                      AND instrument_key LIKE 'NSE_FO|%' AND expiry>:today
                    ORDER BY expiry ASC,ABS(strike-:spot),strike ASC LIMIT 12
                """
                    ),
                    {
                        "s": symbol,
                        "today": now.astimezone(IST).date(),
                        "spot": frame["bars"][-1]["close"],
                    },
                )
                contracts = [dict(r) for r in result.mappings().all()]
                expiry = min((r["expiry"] for r in contracts), default=None)
                for r in contracts:
                    if r["expiry"] != expiry:
                        continue
                    c = {
                        "instrument_key": r["instrument_key"],
                        "trading_symbol": r["trading_symbol"],
                        "expiry": r["expiry"].isoformat(),
                        "strike": float(r["strike"]),
                        "instrument_type": r["option_type"],
                        "lot_size": int(r["lot_size"] or 0),
                        "available_at": stamp,
                    }
                    frame["alternatives"].append(c)
                    frame["contracts"].setdefault(c["instrument_type"], c)
            elif fut:
                c = {**fut, "instrument_type": "FUT", "available_at": stamp}
                frame["contracts"]["FUT"] = c
                frame["alternatives"].append(c)
        candidates = {
            c["instrument_key"]: c for c in [*frame["alternatives"], *held_contracts]
        }
        quotes = []
        if candidates:
            try:
                raw = await adapter.get(
                    "/market-quote/quotes", {"instrument_key": ",".join(candidates)}
                )
            except httpx.HTTPError:
                raw = {}
                frame["coverage"].append("quote_request_failed")
            received = datetime.now(timezone.utc).isoformat()
            for item in (raw.get("data") or {}).values():
                q = normalize_quote(item, received)
                if q["instrument_key"] in candidates:
                    quotes.append(q)
        if market == "NSE" and underlying_key and frame["contracts"]:
            expiry = next(iter(frame["contracts"].values()))["expiry"]
            cache_key = (underlying_key, expiry)
            cached = self.greek_cache.get(cache_key)
            if not cached or (now - cached[0]).total_seconds() >= 60:
                try:
                    chain = await adapter.get(
                        "/option/chain",
                        {"instrument_key": underlying_key, "expiry_date": expiry},
                    )
                    available = datetime.now(timezone.utc).isoformat()
                    greeks = []
                    for row in chain.get("data", []):
                        for leg in ("call_options", "put_options"):
                            e = row.get(leg) or {}
                            if e.get("instrument_key") not in candidates:
                                continue
                            values = {
                                **(e.get("market_data") or {}),
                                **(e.get("option_greeks") or {}),
                            }
                            greeks.append(
                                {
                                    "instrument_key": e["instrument_key"],
                                    "available_at": available,
                                    "exchange_asof": None,
                                    **{
                                        k: number(values.get(k))
                                        for k in (
                                            "iv",
                                            "delta",
                                            "gamma",
                                            "theta",
                                            "vega",
                                            "oi",
                                            "volume",
                                        )
                                    },
                                }
                            )
                    self.greek_cache[cache_key] = (now, greeks)
                except Exception:
                    frame["coverage"].append("greeks_request_unavailable")
            frame["greeks"] = self.greek_cache.get(cache_key, (None, []))[1]
        frame["coverage"].append("aggressor_flow_unavailable")
        if market == "MCX" and not frame["bars"]:
            frame["coverage"].append("exact_futures_candles_unavailable")
        frame["observed_at"] = datetime.now(timezone.utc).isoformat()
        return frame, quotes
