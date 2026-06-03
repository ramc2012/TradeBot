"""Upstox broker adapter using upstox-python-sdk."""
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
from typing import Any, Callable, Optional
import httpx
from loguru import logger

from brokers.base import (
    AuthToken, BrokerAdapter, FundsResponse, Holding, MarginResponse,
    OptionChain, OptionChainEntry, Order, OrderRequest, OrderResponse,
    Position, Tick, Trade, UserProfile,
)
from core.config import settings


class UpstoxAdapter(BrokerAdapter):
    """Adapter for Upstox broker (upstox-python-sdk v2)."""

    broker_name = "upstox"
    BASE_URL = "https://api.upstox.com/v2"
    AUTH_URL = "https://api.upstox.com/v2/login/authorization/token"

    def __init__(self):
        self._access_token: Optional[str] = None

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }

    def get_auth_url(self) -> str:
        """Generate Upstox OAuth2 PKCE authorization URL."""
        import urllib.parse
        params = {
            "client_id": settings.UPSTOX_API_KEY,
            "redirect_uri": settings.UPSTOX_REDIRECT_URI,
            "response_type": "code",
        }
        return f"https://api.upstox.com/v2/login/authorization/dialog?{urllib.parse.urlencode(params)}"

    async def authenticate(self, credentials: dict) -> AuthToken:
        """
        credentials:
          { access_token: str }  — use an existing Upstox JWT token directly
          { code: str }          — exchange OAuth auth code for token (sandbox flow)

        If `code` looks like a JWT (starts with 'eyJ'), treat it as a direct access token
        so users can paste their Upstox Pro / existing token without a separate exchange.
        """
        # Direct token (explicit key)
        if "access_token" in credentials:
            self._access_token = credentials["access_token"].strip()
            logger.info("Upstox: using direct access_token")
            return AuthToken(access_token=self._access_token)

        code = credentials.get("code", "").strip()
        if not code:
            raise ValueError("Either 'code' or 'access_token' is required")

        # Auto-detect: if the value is a JWT, use it directly as an access token
        if code.startswith("eyJ"):
            self._access_token = code
            logger.info("Upstox: JWT detected — using as direct access token (skip exchange)")
            return AuthToken(access_token=self._access_token)

        # Standard OAuth code exchange
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                self.AUTH_URL,
                data={
                    "code": code,
                    "client_id": settings.UPSTOX_API_KEY,
                    "client_secret": settings.UPSTOX_SECRET,
                    "redirect_uri": settings.UPSTOX_REDIRECT_URI,
                    "grant_type": "authorization_code",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        data = r.json()

        # Surface Upstox API errors clearly
        if r.status_code != 200 or "access_token" not in data:
            errors = data.get("errors", [])
            if errors:
                msgs = "; ".join(e.get("message", str(e)) for e in errors)
            else:
                msgs = data.get("message", data.get("error_description", str(data)))
            raise ValueError(f"Upstox token exchange failed ({r.status_code}): {msgs}")

        self._access_token = data["access_token"]
        refresh_token = data.get("refresh_token") or data.get("refreshToken")
        logger.info("Upstox authenticated via code exchange")
        return AuthToken(
            access_token=self._access_token,
            refresh_token=str(refresh_token).strip() or None,
            expires_at=datetime.utcnow() + timedelta(seconds=data.get("expires_in", 86400)),
        )

    async def refresh_token(self) -> AuthToken:
        raise RuntimeError("Upstox does not support refresh tokens for this login flow.")

    async def get_profile(self) -> UserProfile:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{self.BASE_URL}/user/profile", headers=self._headers())
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            # data may be None (sandbox returns null), a dict, or absent
            data = body.get("data") or {}
            if not isinstance(data, dict):
                data = {}
        except Exception as exc:
            logger.warning(f"Upstox get_profile failed ({exc}) — using placeholder")
            data = {}
        return UserProfile(
            user_id=data.get("user_id", "upstox_user"),
            name=data.get("user_name", "Upstox"),
            email=data.get("email", ""),
            mobile=data.get("mobile_number", ""),
            broker="upstox",
        )

    async def get_positions(self) -> list[Position]:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{self.BASE_URL}/portfolio/short-term-positions", headers=self._headers())
        positions = r.json().get("data", [])
        return [
            Position(
                symbol=p.get("trading_symbol", ""),
                exchange=p.get("exchange", "NSE"),
                instrument_type=p.get("instrument_type", "EQ"),
                qty=p.get("quantity", 0),
                avg_price=p.get("average_price", 0),
                ltp=p.get("last_price", 0),
                unrealized_pnl=p.get("unrealised_profit", 0),
                realized_pnl=p.get("realised_profit", 0),
            )
            for p in positions
        ]

    async def get_holdings(self) -> list[Holding]:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{self.BASE_URL}/portfolio/long-term-holdings", headers=self._headers())
        holdings = r.json().get("data", [])
        return [
            Holding(
                symbol=h.get("trading_symbol", ""),
                exchange=h.get("exchange", "NSE"),
                qty=h.get("quantity", 0),
                avg_price=h.get("average_price", 0),
                ltp=h.get("last_price", 0),
                pnl=h.get("pnl", 0),
            )
            for h in holdings
        ]

    async def get_order_book(self) -> list[Order]:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{self.BASE_URL}/order/retrieve-all", headers=self._headers())
        orders = r.json().get("data", [])
        return [
            Order(
                order_id=o.get("order_id", ""),
                symbol=o.get("trading_symbol", ""),
                exchange=o.get("exchange", "NSE"),
                action=o.get("transaction_type", "BUY"),
                order_type=o.get("order_type", "MARKET"),
                qty=o.get("quantity", 0),
                price=o.get("price", 0),
                status=o.get("status", ""),
                fill_price=o.get("average_price", None),
            )
            for o in orders
        ]

    async def get_trade_book(self) -> list[Trade]:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{self.BASE_URL}/order/trades/get-trades-for-day", headers=self._headers())
        trades = r.json().get("data", [])
        return [
            Trade(
                trade_id=t.get("trade_id", ""),
                order_id=t.get("order_id", ""),
                symbol=t.get("trading_symbol", ""),
                exchange=t.get("exchange", "NSE"),
                action=t.get("transaction_type", "BUY"),
                qty=t.get("quantity", 0),
                fill_price=t.get("average_price", 0),
                fill_time=datetime.now(),
            )
            for t in trades
        ]

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        payload = {
            "quantity": order.qty,
            "product": order.product,
            "validity": order.validity,
            "price": order.price or 0,
            "tag": "nomad-curie",
            "instrument_token": order.symbol,
            "order_type": order.order_type,
            "transaction_type": order.action,
            "disclosed_quantity": 0,
            "trigger_price": order.sl or 0,
            "is_amo": False,
        }
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{self.BASE_URL}/order/place",
                json=payload,
                headers=self._headers(),
            )
        data = r.json().get("data", {})
        return OrderResponse(
            order_id=data.get("order_id", ""),
            status="OPEN" if r.status_code == 200 else "REJECTED",
        )

    async def modify_order(self, order_id: str, params: dict) -> OrderResponse:
        payload = {"order_id": order_id, **params}
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.put(
                f"{self.BASE_URL}/order/modify",
                json=payload,
                headers=self._headers(),
            )
        return OrderResponse(
            order_id=order_id,
            status="OPEN" if r.status_code == 200 else "REJECTED",
        )

    async def cancel_order(self, order_id: str) -> bool:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.delete(
                f"{self.BASE_URL}/order/cancel",
                params={"order_id": order_id},
                headers=self._headers(),
            )
        return r.status_code == 200

    async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        joined = ",".join(symbols)
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{self.BASE_URL}/market-quote/ltp",
                params={"instrument_key": joined},
                headers=self._headers(),
            )
        data = r.json().get("data", {})
        return {
            v.get("instrument_token") or k: float(v.get("last_price", 0) or 0)
            for k, v in data.items()
        }

    async def search_instruments(
        self,
        *,
        query: str,
        exchanges: Optional[str] = None,
        segments: Optional[str] = None,
        instrument_types: Optional[str] = None,
        expiry: Optional[str] = None,
        atm_offset: Optional[int] = None,
        page_number: int = 1,
        records: int = 20,
    ) -> list[dict]:
        params: dict[str, Any] = {
            "query": str(query or "").strip(),
            "page_number": max(1, int(page_number or 1)),
            "records": max(1, min(int(records or 20), 30)),
        }
        if exchanges:
            params["exchanges"] = exchanges
        if segments:
            params["segments"] = segments
        if instrument_types:
            params["instrument_types"] = instrument_types
        if expiry:
            params["expiry"] = expiry
        if atm_offset is not None:
            params["atm_offset"] = int(atm_offset)

        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{self.BASE_URL}/instruments/search",
                params=params,
                headers=self._headers(),
            )
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200:
            errors = body.get("errors", [])
            if errors:
                detail = "; ".join(str(error.get("message") or error) for error in errors)
            else:
                detail = str(body.get("message") or body or r.text)
            raise ValueError(f"Upstox instrument search failed ({r.status_code}): {detail}")
        return list(body.get("data") or [])

    async def get_option_contracts(self, symbol: str, expiry: Optional[str] = None) -> list[dict]:
        params = {"instrument_key": symbol}
        if expiry:
            params["expiry_date"] = expiry
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{self.BASE_URL}/option/contract",
                params=params,
                headers=self._headers(),
            )
        return r.json().get("data", [])

    async def subscribe_websocket(
        self,
        symbols: list[str],
        on_tick_callback: Callable[[Tick], None],
    ) -> Any:
        """Open Upstox MarketDataStreamer v3 WebSocket."""
        try:
            import upstox_client
            configuration = upstox_client.Configuration()
            configuration.access_token = self._access_token

            streamer = upstox_client.MarketDataStreamerV3(
                upstox_client.ApiClient(configuration),
                symbols,
                "full",
            )

            def pick_day_ohlc(ohlc_entries: list[dict]) -> dict:
                for entry in ohlc_entries:
                    if str(entry.get("interval", "")).lower() in {"1d", "day"}:
                        return entry
                return ohlc_entries[-1] if ohlc_entries else {}

            def _top_of_book(container: dict) -> tuple[float, float, int, int]:
                # Upstox full mode (marketFF) and firstLevelWithGreeks carry the
                # real book under `bidAskQuote` (level fields bidP/bidQ/askP/askQ).
                # The old build_tick discarded it, so the auction-intelligence
                # order-flow path saw no sizes and fabricated the whole book.
                # Pull top-of-book; tolerate list-or-dict shape and missing keys
                # (index feeds carry no book → returns zeros, harmless).
                q = container.get("bidAskQuote") if isinstance(container, dict) else None
                if isinstance(q, list):
                    q = q[0] if q else None
                if isinstance(q, dict):
                    return (
                        float(q.get("bidP", 0) or 0),
                        float(q.get("askP", 0) or 0),
                        int(q.get("bidQ", 0) or 0),
                        int(q.get("askQ", 0) or 0),
                    )
                return (0.0, 0.0, 0, 0)

            def build_tick(feed_key: str, feed: dict) -> Optional[Tick]:
                ltpc = {}
                ohlc_entries: list[dict] = []
                volume = 0
                oi = 0
                bid = ask = 0.0
                bid_qty = ask_qty = 0
                total_buy_qty = total_sell_qty = 0

                if "fullFeed" in feed:
                    full_feed = feed.get("fullFeed", {})
                    full_union = full_feed.get("indexFF") or full_feed.get("marketFF") or {}
                    ltpc = full_union.get("ltpc", {})
                    ohlc_entries = (full_union.get("marketOHLC") or {}).get("ohlc", [])
                    volume = int(full_union.get("vtt", 0) or 0)
                    oi = int(full_union.get("oi", 0) or 0)
                    bid, ask, bid_qty, ask_qty = _top_of_book(full_union.get("marketLevel", {}) or {})
                    # Aggregate book depth (P1d) — Upstox full feed carries
                    # total buy/sell qty; real depth_imbalance source.
                    total_buy_qty = int(full_union.get("tbq", 0) or 0)
                    total_sell_qty = int(full_union.get("tsq", 0) or 0)
                elif "firstLevelWithGreeks" in feed:
                    first_level = feed.get("firstLevelWithGreeks", {})
                    ltpc = first_level.get("ltpc", {})
                    volume = int(first_level.get("vtt", 0) or 0)
                    oi = int(first_level.get("oi", 0) or 0)
                    bid, ask, bid_qty, ask_qty = _top_of_book(first_level.get("firstLevel", {}) or {})
                else:
                    ltpc = feed.get("ltpc", {})

                ltp = float(ltpc.get("ltp", 0) or 0)
                if ltp <= 0:
                    return None

                day_ohlc = pick_day_ohlc(ohlc_entries)
                prev_close = float(ltpc.get("cp", 0) or day_ohlc.get("close", 0) or ltp)

                return Tick(
                    symbol=feed.get("symbol", "") or feed.get("instrument_key", "") or feed_key,
                    ltp=ltp,
                    open=float(day_ohlc.get("open", prev_close) or prev_close),
                    high=float(day_ohlc.get("high", ltp) or ltp),
                    low=float(day_ohlc.get("low", ltp) or ltp),
                    close=prev_close,
                    volume=volume,
                    oi=oi,
                    bid=bid,
                    ask=ask,
                    bid_qty=bid_qty,
                    ask_qty=ask_qty,
                    total_buy_qty=total_buy_qty,
                    total_sell_qty=total_sell_qty,
                    timestamp=datetime.utcnow(),
                )

            def on_message(msg):
                for feed_key, feed in msg.get("feeds", {}).items():
                    tick = build_tick(feed_key, feed)
                    if tick:
                        on_tick_callback(tick)

            streamer.on("message", on_message)
            streamer.connect()
            return streamer
        except Exception as e:
            logger.error(f"Failed to start Upstox WebSocket: {e}")
            raise

    async def get_option_chain(self, symbol: str, expiry: str) -> OptionChain:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{self.BASE_URL}/option/chain",
                params={"instrument_key": symbol, "expiry_date": expiry},
                headers=self._headers(),
            )
        data = r.json().get("data", [])
        entries = []
        spot = 0.0
        for row in data:
            if spot == 0:
                spot = row.get("underlying_spot_price", 0)
            for otype in ["call_options", "put_options"]:
                opt = row.get(otype, {})
                if opt:
                    mkt = opt.get("market_data", {})
                    greek = opt.get("option_greeks", {})
                    entries.append(OptionChainEntry(
                        strike=row.get("strike_price", 0),
                        option_type="CE" if otype == "call_options" else "PE",
                        ltp=mkt.get("ltp", 0),
                        oi=mkt.get("oi", 0),
                        volume=mkt.get("volume", 0),
                        bid=mkt.get("bid_price", 0),
                        ask=mkt.get("ask_price", 0),
                        iv=greek.get("iv", None),
                        delta=greek.get("delta", None),
                        gamma=greek.get("gamma", None),
                        theta=greek.get("theta", None),
                        vega=greek.get("vega", None),
                        prev_oi=mkt.get("prev_oi", None),
                        prev_close=mkt.get("close_price", None),
                        instrument_key=opt.get("instrument_key", None),
                    ))
        return OptionChain(symbol=symbol, expiry=expiry, spot_price=spot, entries=entries)

    async def get_margins(self, orders: list[OrderRequest]) -> MarginResponse:
        payload = [
            {
                "instrument_token": o.symbol,
                "quantity": o.qty,
                "price": o.price or 0,
                "product": o.product,
                "transaction_type": o.action,
                "order_type": o.order_type,
            }
            for o in orders
        ]
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{self.BASE_URL}/charges/margin",
                json={"instruments": payload},
                headers=self._headers(),
            )
        data = r.json().get("data", {})
        return MarginResponse(
            required_margin=data.get("required_margin", 0),
            available_margin=data.get("available_margin", 0),
            utilized_margin=data.get("utilized_margin", 0),
        )

    async def get_funds(self) -> FundsResponse:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{self.BASE_URL}/user/get-funds-and-margin", headers=self._headers())
        data = r.json().get("data", {})
        equity = data.get("equity", {})
        return FundsResponse(
            available_cash=equity.get("available_margin", 0),
            used_margin=equity.get("used_margin", 0),
            total_balance=equity.get("net", 0),
        )
