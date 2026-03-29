"""5Paisa broker adapter using py5paisa SDK."""
from __future__ import annotations
from datetime import datetime
from typing import Any, Callable, Optional
import httpx
from loguru import logger

from brokers.base import (
    AuthToken, BrokerAdapter, FundsResponse, Holding, MarginResponse,
    OptionChain, OptionChainEntry, Order, OrderRequest, OrderResponse,
    Position, Tick, Trade, UserProfile,
)
from core.config import settings


class FivePaisaAdapter(BrokerAdapter):
    """Adapter for 5Paisa broker (py5paisa SDK)."""

    broker_name = "fivepaisa"

    def __init__(self):
        self._access_token: Optional[str] = None
        self._client = None  # py5paisa client

    def _get_py5paisa_client(self):
        if self._client is None:
            from py5paisa import FivePaisaClient
            # py5paisa v1.x only accepts cred dict — no email/passwd/dob kwargs
            self._client = FivePaisaClient(
                cred={
                    "APP_NAME": settings.FIVEPAISA_APP_NAME,
                    "APP_SOURCE": settings.FIVEPAISA_APP_SOURCE,
                    "USER_ID": settings.FIVEPAISA_USER_ID,
                    "PASSWORD": settings.FIVEPAISA_PASSWORD,
                    "USER_KEY": settings.FIVEPAISA_USER_KEY,
                    "ENCRYPTION_KEY": settings.FIVEPAISA_ENCRYPTION_KEY,
                },
            )
        return self._client

    async def authenticate(self, credentials: dict) -> AuthToken:
        """
        credentials: { totp: str } for TOTP-based login
        OR { access_token: str } if already have token
        """
        import asyncio
        if "access_token" in credentials:
            self._access_token = credentials["access_token"]
            return AuthToken(access_token=self._access_token)

        totp = credentials.get("totp", "")
        loop = asyncio.get_event_loop()
        client = self._get_py5paisa_client()

        try:
            # py5paisa v1.x signature: get_totp_session(client_code, totp, pin)
            # client_code = registered Email_ID (not the alphanumeric client code)
            # pin = account password (same as PASSWORD in credentials)
            email_or_id = settings.FIVEPAISA_EMAIL or settings.FIVEPAISA_USER_ID
            await loop.run_in_executor(None, lambda: client.get_totp_session(
                email_or_id,                    # Email_ID (registered email preferred)
                totp,                           # totp (6-digit from authenticator)
                settings.FIVEPAISA_PASSWORD,    # pin (account password)
            ))
            self._access_token = (
                getattr(client, "Jwt_token", None)
                or getattr(client, "jwt_token", None)
                or getattr(client, "access_token", None)
            )
            logger.info("5Paisa authenticated successfully")
            return AuthToken(access_token=self._access_token or "")
        except Exception as e:
            logger.error(f"5Paisa authentication failed: {e}")
            raise

    async def refresh_token(self) -> AuthToken:
        """5Paisa sessions refresh automatically via SDK; re-auth on expiry."""
        return AuthToken(access_token=self._access_token or "")

    async def get_profile(self) -> UserProfile:
        import asyncio
        client = self._get_py5paisa_client()
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, client.get_client_info)
        info = data.get("body", {}).get("ClientInfo", {})
        return UserProfile(
            user_id=info.get("ClientCode", ""),
            name=info.get("ClientName", ""),
            email=info.get("EmailId", ""),
            mobile=info.get("MobileNo", ""),
            broker="fivepaisa",
        )

    async def get_positions(self) -> list[Position]:
        import asyncio
        client = self._get_py5paisa_client()
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, client.positions)
        positions = []
        for p in (data or []):
            positions.append(Position(
                symbol=p.get("ScripName", ""),
                exchange=p.get("Exch", "N"),
                instrument_type=p.get("ExchType", "EQ"),
                qty=p.get("NetQty", 0),
                avg_price=p.get("NetRate", 0),
                ltp=p.get("LTP", 0),
                unrealized_pnl=p.get("MTOM", 0),
                realized_pnl=p.get("BookedPL", 0),
            ))
        return positions

    async def get_holdings(self) -> list[Holding]:
        import asyncio
        client = self._get_py5paisa_client()
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, client.holdings)
        return [
            Holding(
                symbol=h.get("Symbol", ""),
                exchange=h.get("Exch", "N"),
                qty=h.get("Quantity", 0),
                avg_price=h.get("AvgRate", 0),
                ltp=h.get("LTP", 0),
                pnl=h.get("CurrentGainLoss", 0),
            )
            for h in (data or [])
        ]

    async def get_order_book(self) -> list[Order]:
        import asyncio
        client = self._get_py5paisa_client()
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, client.order_book)
        return [
            Order(
                order_id=str(o.get("ExchOrdID", "")),
                symbol=o.get("ScripName", ""),
                exchange=o.get("Exch", "N"),
                action="BUY" if o.get("BuySell") == "B" else "SELL",
                order_type=o.get("OrdType", "MARKET"),
                qty=o.get("Qty", 0),
                price=o.get("Rate", 0),
                status=o.get("OrdStatus", ""),
                fill_price=o.get("TradedQtyRate", None),
            )
            for o in (data or [])
        ]

    async def get_trade_book(self) -> list[Trade]:
        import asyncio
        client = self._get_py5paisa_client()
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, client.trade_book)
        return [
            Trade(
                trade_id=str(t.get("ExchTrdID", "")),
                order_id=str(t.get("ExchOrdID", "")),
                symbol=t.get("ScripName", ""),
                exchange=t.get("Exch", "N"),
                action="BUY" if t.get("BuySell") == "B" else "SELL",
                qty=t.get("Qty", 0),
                fill_price=t.get("Rate", 0),
                fill_time=datetime.now(),
            )
            for t in (data or [])
        ]

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        import asyncio
        client = self._get_py5paisa_client()
        loop = asyncio.get_event_loop()
        order_type_map = {"BUY": "B", "SELL": "S"}
        result = await loop.run_in_executor(None, lambda: client.place_order(
            OrderType=order_type_map.get(order.action, "B"),
            Exchange=order.exchange[:1].upper(),
            ExchangeType=self._exchange_type(order.instrument_type),
            ScripCode=0,  # ScripCode needs to be resolved from symbol
            Qty=order.qty,
            Price=order.price or 0,
            StopLossPrice=order.sl or 0,
            IsIntraday=order.product == "INTRADAY",
        ))
        data = result.get("body", {}).get("BookedTradeDetail", {})
        return OrderResponse(
            order_id=str(data.get("ExchOrdID", "")),
            status="OPEN" if data else "REJECTED",
        )

    async def modify_order(self, order_id: str, params: dict) -> OrderResponse:
        return OrderResponse(order_id=order_id, status="OPEN", message="Modify not supported in 5Paisa SDK wrapper")

    async def cancel_order(self, order_id: str) -> bool:
        import asyncio
        client = self._get_py5paisa_client()
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: client.cancel_order(
            ExchOrderID=order_id,
            Exchange="N",
            ExchangeType="D",
            ScripCode=0,
        ))
        return bool(result)

    async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        return {s: 0.0 for s in symbols}  # 5Paisa LTP via market feed

    async def subscribe_websocket(
        self,
        symbols: list[str],
        on_tick_callback: Callable[[Tick], None],
    ) -> Any:
        """Subscribe to 5Paisa market data WebSocket."""
        try:
            from py5paisa.order import Market
            client = self._get_py5paisa_client()

            def on_message(msg):
                tick = Tick(
                    symbol=str(msg.get("Token", "")),
                    ltp=msg.get("LastRate", 0),
                    volume=msg.get("TotalQty", 0),
                    oi=msg.get("OI", 0),
                    timestamp=datetime.utcnow(),
                )
                on_tick_callback(tick)

            scrip_list = [{"Exch": "N", "ExchType": "D", "ScripCode": int(s)} for s in symbols]
            client.on_message = on_message
            client.get_market_feed(scrip_list)
            return client
        except Exception as e:
            logger.error(f"Failed to start 5Paisa WebSocket: {e}")
            raise

    async def get_option_chain(self, symbol: str, expiry: str) -> OptionChain:
        return OptionChain(symbol=symbol, expiry=expiry, spot_price=0.0, entries=[])

    async def get_margins(self, orders: list[OrderRequest]) -> MarginResponse:
        return MarginResponse(required_margin=0, available_margin=0, utilized_margin=0)

    async def get_funds(self) -> FundsResponse:
        import asyncio
        client = self._get_py5paisa_client()
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, client.margin)
        net = 0.0
        used = 0.0
        if data:
            limits = data.get("EquityLimit", {})
            net = limits.get("NetCash", 0)
            used = limits.get("MarginUsed", 0)
        return FundsResponse(
            available_cash=net,
            used_margin=used,
            total_balance=net + used,
        )

    @staticmethod
    def _exchange_type(instrument_type: str) -> str:
        return "D" if instrument_type in ("CE", "PE", "FUT") else "C"
