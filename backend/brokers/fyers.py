"""Fyers broker adapter using fyers-apiv3 SDK."""
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


class FyersAdapter(BrokerAdapter):
    """Adapter for Fyers broker (fyers-apiv3)."""

    broker_name = "fyers"
    BASE_URL = "https://api-t1.fyers.in/api/v3"

    def __init__(self):
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._fyers_id: Optional[str] = None  # app_id:access_token
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.BASE_URL,
                headers={"Authorization": f"{settings.FYERS_APP_ID}:{self._access_token}"},
                timeout=10.0,
            )
        return self._client

    def _auth_header(self) -> dict:
        return {"Authorization": f"{settings.FYERS_APP_ID}:{self._access_token}"}

    def get_auth_url(self) -> str:
        """Generate Fyers auth URL for OAuth redirect flow."""
        from fyers_apiv3 import fyersModel
        session = fyersModel.SessionModel(
            client_id=settings.FYERS_APP_ID,
            secret_key=settings.FYERS_SECRET,
            redirect_uri=settings.FYERS_REDIRECT_URI,
            response_type="code",
            grant_type="authorization_code",
        )
        return session.generate_authcode()

    async def authenticate(self, credentials: dict) -> AuthToken:
        """
        credentials: { auth_code: str }  OR  { access_token: str }
        """
        if "access_token" in credentials:
            self._access_token = credentials["access_token"]
            return AuthToken(access_token=self._access_token)

        auth_code = credentials["auth_code"]
        try:
            from fyers_apiv3 import fyersModel
            session = fyersModel.SessionModel(
                client_id=settings.FYERS_APP_ID,
                secret_key=settings.FYERS_SECRET,
                redirect_uri=settings.FYERS_REDIRECT_URI,
                response_type="code",
                grant_type="authorization_code",
            )
            session.set_token(auth_code)
            response = session.generate_token()
            self._access_token = response.get("access_token", "")
            logger.info("Fyers authenticated successfully")
            return AuthToken(
                access_token=self._access_token,
                expires_at=datetime.utcnow() + timedelta(hours=8),
            )
        except Exception as e:
            logger.error(f"Fyers authentication failed: {e}")
            raise

    async def refresh_token(self) -> AuthToken:
        """Fyers tokens don't refresh — re-auth required each session."""
        raise NotImplementedError("Fyers requires re-authentication each day")

    async def get_profile(self) -> UserProfile:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{self.BASE_URL}/profile",
                headers=self._auth_header(),
            )
            data = r.json().get("data", {})
        return UserProfile(
            user_id=data.get("fy_id", ""),
            name=data.get("name", ""),
            email=data.get("email_id", ""),
            mobile=data.get("mobile_number", ""),
            broker="fyers",
        )

    async def get_positions(self) -> list[Position]:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{self.BASE_URL}/positions", headers=self._auth_header())
        net_positions = r.json().get("netPositions", [])
        result = []
        for p in net_positions:
            result.append(Position(
                symbol=p.get("symbol", ""),
                exchange=p.get("exchange", "NSE"),
                instrument_type=p.get("type", "EQ"),
                qty=p.get("netQty", 0),
                avg_price=p.get("netAvg", 0),
                ltp=p.get("ltp", 0),
                unrealized_pnl=p.get("unrealizedProfit", 0),
                realized_pnl=p.get("realizedProfit", 0),
            ))
        return result

    async def get_holdings(self) -> list[Holding]:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{self.BASE_URL}/holdings", headers=self._auth_header())
        holdings = r.json().get("holdings", [])
        return [
            Holding(
                symbol=h.get("symbol", ""),
                exchange="NSE",
                qty=h.get("quantity", 0),
                avg_price=h.get("costPrice", 0),
                ltp=h.get("ltp", 0),
                pnl=h.get("pl", 0),
            )
            for h in holdings
        ]

    async def get_order_book(self) -> list[Order]:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{self.BASE_URL}/orders", headers=self._auth_header())
        orders = r.json().get("orderBook", [])
        return [
            Order(
                order_id=o.get("id", ""),
                symbol=o.get("symbol", ""),
                exchange=str(o.get("exchange", "NSE")),
                action="BUY" if o.get("side") == 1 else "SELL",
                order_type=self._map_order_type(o.get("type", 2)),
                qty=o.get("qty", 0),
                price=o.get("limitPrice", 0),
                status=o.get("status", ""),
                fill_price=o.get("tradedPrice", None),
            )
            for o in orders
        ]

    async def get_trade_book(self) -> list[Trade]:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{self.BASE_URL}/tradebook", headers=self._auth_header())
        trades = r.json().get("tradeBook", [])
        return [
            Trade(
                trade_id=t.get("tradeNumber", ""),
                order_id=t.get("orderNumber", ""),
                symbol=t.get("symbol", ""),
                exchange=str(t.get("exchange", "NSE")),
                action="BUY" if t.get("side") == 1 else "SELL",
                qty=t.get("tradedQty", 0),
                fill_price=t.get("tradePrice", 0),
                fill_time=datetime.now(),
            )
            for t in trades
        ]

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        payload = {
            "symbol": order.symbol,
            "qty": order.qty,
            "type": self._to_fyers_order_type(order.order_type),
            "side": 1 if order.action == "BUY" else -1,
            "productType": order.product,
            "limitPrice": order.price or 0,
            "stopPrice": order.sl or 0,
            "validity": order.validity,
            "disclosedQty": 0,
            "offlineOrder": False,
        }
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{self.BASE_URL}/orders/sync",
                json=payload,
                headers=self._auth_header(),
            )
        data = r.json()
        return OrderResponse(
            order_id=data.get("id", ""),
            status="OPEN" if data.get("s") == "ok" else "REJECTED",
            message=data.get("message", ""),
        )

    async def modify_order(self, order_id: str, params: dict) -> OrderResponse:
        payload = {"id": order_id, **params}
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.patch(
                f"{self.BASE_URL}/orders/sync",
                json=payload,
                headers=self._auth_header(),
            )
        data = r.json()
        return OrderResponse(
            order_id=order_id,
            status="OPEN" if data.get("s") == "ok" else "REJECTED",
        )

    async def cancel_order(self, order_id: str) -> bool:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.delete(
                f"{self.BASE_URL}/orders/sync",
                params={"id": order_id},
                headers=self._auth_header(),
            )
        return r.json().get("s") == "ok"

    async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        joined = ",".join(symbols)
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{self.BASE_URL}/quotes",
                params={"symbols": joined},
                headers=self._auth_header(),
            )
        quotes = r.json().get("d", [])
        result = {}
        for q in quotes:
            sym = q.get("n", "")
            ltp = q.get("v", {}).get("lp", 0)
            result[sym] = ltp
        return result

    async def subscribe_websocket(
        self,
        symbols: list[str],
        on_tick_callback: Callable[[Tick], None],
    ) -> Any:
        """Open Fyers WebSocket for real-time data."""
        try:
            from fyers_apiv3.FyersWebsocket import data_ws
            client = data_ws.FyersDataSocket(
                access_token=f"{settings.FYERS_APP_ID}:{self._access_token}",
                log_path="",
                litemode=False,
                write_to_file=False,
                reconnect=True,
                on_connect=lambda: logger.info("Fyers WS connected"),
                on_close=lambda: logger.warning("Fyers WS closed"),
                on_error=lambda e: logger.error(f"Fyers WS error: {e}"),
                on_message=lambda msg: self._handle_tick(msg, on_tick_callback),
            )
            client.connect()
            client.subscribe(symbols=symbols, data_type="SymbolUpdate")
            return client
        except Exception as e:
            logger.error(f"Failed to start Fyers WebSocket: {e}")
            raise

    def _handle_tick(self, msg: dict, callback: Callable[[Tick], None]):
        try:
            tick = Tick(
                symbol=msg.get("symbol", ""),
                ltp=msg.get("ltp", 0),
                open=msg.get("open_price", 0),
                high=msg.get("high_price", 0),
                low=msg.get("low_price", 0),
                close=msg.get("prev_close_price", 0),
                volume=msg.get("vol_traded_today", 0),
                oi=msg.get("oi", 0),
                bid=msg.get("bid", 0),
                ask=msg.get("ask", 0),
                timestamp=datetime.utcnow(),
            )
            callback(tick)
        except Exception as e:
            logger.error(f"Error parsing Fyers tick: {e}")

    async def get_option_chain(self, symbol: str, expiry: str) -> OptionChain:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{self.BASE_URL}/options/chain",
                params={"symbol": symbol, "strikecount": 10, "timestamp": expiry},
                headers=self._auth_header(),
            )
        data = r.json().get("data", {})
        entries = []
        for opt in data.get("optionsChain", []):
            entries.append(OptionChainEntry(
                strike=opt.get("strikePrice", 0),
                option_type=opt.get("option_type", "CE"),
                ltp=opt.get("ltp", 0),
                oi=opt.get("oi", 0),
                volume=opt.get("volume", 0),
                bid=opt.get("bid", 0),
                ask=opt.get("ask", 0),
                iv=opt.get("iv", None),
                prev_oi=opt.get("prevOi", None) or opt.get("prev_oi", None),
                prev_close=opt.get("prevClose", None) or opt.get("close_price", None),
                instrument_key=opt.get("symbol", None),
            ))
        return OptionChain(
            symbol=symbol,
            expiry=expiry,
            spot_price=data.get("underlyingValue", 0),
            entries=entries,
        )

    async def get_margins(self, orders: list[OrderRequest]) -> MarginResponse:
        payloads = [
            {
                "symbol": o.symbol,
                "qty": o.qty,
                "side": 1 if o.action == "BUY" else -1,
                "type": self._to_fyers_order_type(o.order_type),
                "productType": o.product,
                "limitPrice": o.price or 0,
            }
            for o in orders
        ]
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{self.BASE_URL}/margin",
                json={"data": payloads},
                headers=self._auth_header(),
            )
        data = r.json().get("data", {})
        return MarginResponse(
            required_margin=data.get("totalRequired", 0),
            available_margin=data.get("marginAvailable", 0),
            utilized_margin=data.get("utilized", 0),
        )

    async def get_funds(self) -> FundsResponse:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{self.BASE_URL}/funds", headers=self._auth_header())
        fund_data = r.json().get("fund_limit", [])
        fund_map = {f.get("title", ""): f.get("equityAmount", 0) for f in fund_data}
        return FundsResponse(
            available_cash=fund_map.get("Total Balance", 0),
            used_margin=fund_map.get("Utilized Amount", 0),
            total_balance=fund_map.get("Total Balance", 0),
        )

    @staticmethod
    def _map_order_type(code: int) -> str:
        return {1: "LIMIT", 2: "MARKET", 3: "SL", 4: "SL_M"}.get(code, "MARKET")

    @staticmethod
    def _to_fyers_order_type(order_type: str) -> int:
        return {"LIMIT": 1, "MARKET": 2, "SL": 3, "SL_M": 4}.get(order_type, 2)
