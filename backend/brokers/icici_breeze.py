"""ICICI Direct — Breeze Connect API adapter.

Authentication flow:
  1. GET /api/auth/icici-breeze/login-url  → returns URL
  2. User visits URL, logs in with ICICI credentials, copies the `apisession`
     token from the redirect URL query param.
  3. POST /api/auth/icici-breeze/connect  { "session_token": "..." }
     → backend calls generate_session() and stores the connection.

Historical data (key for Options MACD backtesting):
  breeze.get_historical_data_v2(
      interval="5minute",
      from_date="2023-01-01T07:00:00.000Z",
      to_date="2023-12-31T07:00:00.000Z",
      stock_code="NIFTY",
      exchange_code="NFO",
      product_type="options",
      expiry_date="2023-01-26T07:00:00.000Z",
      right="call",
      strike_price="18000")
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from brokers.base import (
    BrokerAdapter, AuthToken, UserProfile, Position, Holding,
    Order, Trade, OrderRequest, OrderResponse, OptionChain, OptionChainEntry,
    MarginResponse, FundsResponse, Tick,
)
from core.config import settings


class ICICIBreezeAdapter(BrokerAdapter):
    """ICICI Direct via Breeze Connect API."""

    broker_name = "icici_breeze"
    LOGIN_URL_TEMPLATE = "https://api.icicidirect.com/apiuser/login?api_key={api_key}"

    def __init__(self):
        self._breeze = None
        self._token: Optional[AuthToken] = None
        self._connected = False

    # ── Internal ─────────────────────────────────────────────────────────────

    def _get_breeze(self, force_refresh: bool = False):
        """
        Lazy-init BreezeConnect.

        In breeze-connect v1.0.63+, BreezeConnect.__init__ only stores the api_key —
        it does NOT fetch a public key over HTTP. Session validation happens on the
        first actual API call (get_customer_details, etc.).

        The previous "public key not available" error was caused by checking an attribute
        that does not exist in the current SDK version. Removed that check.
        """
        if self._breeze is None or force_refresh:
            api_key = settings.ICICI_BREEZE_API_KEY
            if not api_key:
                raise ValueError(
                    "ICICI_BREEZE_API_KEY is not set. "
                    "Save it via Settings → ICICI Direct → Save Credentials."
                )
            from breeze_connect import BreezeConnect  # type: ignore
            self._breeze = BreezeConnect(api_key=api_key)
            logger.debug(f"BreezeConnect initialized for key ending …{api_key[-6:]}")
        return self._breeze

    def get_login_url(self) -> str:
        """Return the ICICI Direct login URL for this app's API key."""
        return self.LOGIN_URL_TEMPLATE.format(
            api_key=settings.ICICI_BREEZE_API_KEY
        )

    def _run_sync(self, fn):
        """Run a sync function in a thread executor."""
        loop = asyncio.get_event_loop()
        return loop.run_in_executor(None, fn)

    # ── BrokerAdapter interface ───────────────────────────────────────────────

    async def authenticate(self, credentials: Dict[str, Any]) -> AuthToken:
        """
        credentials:
          session_token  (required) — from URL ?apisession=... after login
          api_secret     (optional) — overrides settings.ICICI_BREEZE_SECRET

        Raises clear errors if the public key fetch failed or the session is invalid.
        """
        session_token = credentials.get("session_token", "").strip()
        api_secret = credentials.get("api_secret", settings.ICICI_BREEZE_SECRET or "").strip()

        if not session_token:
            raise ValueError("session_token is required. Visit the ICICI login URL and copy the ?apisession= value.")
        if not api_secret:
            raise ValueError(
                "API Secret not configured. "
                "Save it via Settings → ICICI Direct → Save Credentials first."
            )

        def _do_auth():
            b = self._get_breeze()
            # generate_session(api_secret, session_token) makes an HTTP call
            # to ICICI's API (api_util) to validate the App Key + Session Token.
            # Possible outcomes:
            #   "Public Key does not exist." → api_key (App Key) is wrong/unregistered
            #   "Invalid session."           → session_token is wrong or expired
            #   "Resource not available."    → session_token expired (>30 seconds old)
            b.generate_session(api_secret=api_secret, session_token=session_token)

        try:
            await self._run_sync(_do_auth)
        except Exception as exc:
            err = str(exc).lower()
            if "api key" in err or "appkey" in err or "public key" in err:
                raise ValueError(
                    "ICICI Breeze API key is not recognised by ICICI's servers. "
                    "Please verify:\n"
                    "1. Log in to https://api.icicidirect.com → 'My Apps'\n"
                    "2. Create or activate your app if it doesn't exist\n"
                    "3. Copy the exact 'App Key' shown there into Settings → ICICI Direct → API Key\n"
                    "4. Also copy the 'Secret Key' (not the session token) into the Secret field\n"
                    "The App Key is typically a 10–20 character alphanumeric string."
                ) from exc
            if "session key" in err or "invalid session" in err:
                raise ValueError(
                    "ICICI Breeze session token is invalid or expired. "
                    "Session tokens expire within 30 seconds of generation.\n"
                    "Steps to get a fresh token:\n"
                    "1. Click 'Open ICICI Direct Login' on the Settings page\n"
                    "2. Log in with your ICICI Direct credentials\n"
                    "3. After redirect, copy the 'apisession' value from the URL\n"
                    "4. Paste it immediately into the Session Token field and click Connect"
                ) from exc
            if "expired" in err or "resource not available" in err:
                raise ValueError(
                    "ICICI Breeze session token has expired (tokens are valid for ~30 seconds). "
                    "Please generate a new token: click 'Open ICICI Direct Login', log in, "
                    "and paste the fresh apisession value immediately."
                ) from exc
            # Re-raise with original message for unknown errors
            raise ValueError(f"ICICI Breeze authentication failed: {exc}") from exc

        self._connected = True
        self._token = AuthToken(
            access_token=session_token,
            token_type="session",
        )
        logger.info("ICICI Breeze: session configured (token ends …%s)", session_token[-6:])
        return self._token

    async def refresh_token(self) -> AuthToken:
        """Breeze sessions expire daily; user must re-authenticate via login URL."""
        raise NotImplementedError(
            "ICICI Breeze sessions cannot be refreshed programmatically. "
            "Visit the login URL to get a new session token."
        )

    async def get_profile(self) -> UserProfile:
        def _fetch():
            b = self._get_breeze()
            return b.get_customer_details(
                api_session=self._token.access_token if self._token else ""
            )

        try:
            data = await self._run_sync(_fetch)
            # API returns {"Status": 200, "Success": {...}} on success
            # or {"Status": 401/500, "Error": "..."} on invalid session
            status = data.get("Status") if isinstance(data, dict) else None
            if status and int(status) not in (200, 0):
                error_msg = data.get("Error", "") or data.get("message", "")
                raise ValueError(
                    f"ICICI session rejected (HTTP {status}): {error_msg}. "
                    "Your session token may be expired or incorrect. "
                    "Click 'Open ICICI Direct Login' to get a fresh token."
                )
            success = (data.get("Success") or {}) if isinstance(data, dict) else {}
            return UserProfile(
                user_id=success.get("idirect_userid", "icici_user"),
                name=success.get("idirect_username", "ICICI Direct User"),
                email=success.get("idirect_emailid", ""),
                mobile=success.get("idirect_mobile", ""),
                broker="icici_breeze",
            )
        except Exception as e:
            logger.warning(f"ICICI profile fetch failed: {e}")
            return UserProfile(
                user_id="icici_user",
                name="ICICI Direct User",
                email="",
                mobile="",
                broker="icici_breeze",
            )

    async def get_positions(self) -> List[Position]:
        def _fetch():
            b = self._get_breeze()
            return b.get_portfolio_positions()

        try:
            resp = await self._run_sync(_fetch)
            rows = resp.get("Success", []) or []
            return [
                Position(
                    symbol=r.get("stock_code", ""),
                    exchange=r.get("exchange_code", "NSE"),
                    instrument_type=r.get("option_type", "EQ") or "EQ",
                    qty=int(r.get("quantity", 0)),
                    avg_price=float(r.get("average_price", 0)),
                    ltp=float(r.get("ltp", 0)),
                    unrealized_pnl=float(r.get("profit_loss", 0)),
                    realized_pnl=0.0,
                    strike=float(r.get("strike_price", 0)) if r.get("strike_price") else None,
                    expiry=str(r.get("expiry_date", "")) or None,
                    option_type=r.get("right", None),
                    product="INTRADAY",
                )
                for r in rows
            ]
        except Exception as e:
            logger.error(f"ICICI get_positions failed: {e}")
            return []

    async def get_holdings(self) -> List[Holding]:
        def _fetch():
            b = self._get_breeze()
            return b.get_portfolio_holdings()

        try:
            resp = await self._run_sync(_fetch)
            rows = resp.get("Success", []) or []
            return [
                Holding(
                    symbol=r.get("stock_code", ""),
                    exchange="NSE",
                    qty=int(r.get("quantity", 0)),
                    avg_price=float(r.get("average_price", 0)),
                    ltp=float(r.get("ltp", 0)),
                    pnl=float(r.get("profit_loss", 0)),
                )
                for r in rows
            ]
        except Exception as e:
            logger.error(f"ICICI get_holdings failed: {e}")
            return []

    async def get_funds(self) -> FundsResponse:
        def _fetch():
            b = self._get_breeze()
            return b.get_funds()

        try:
            resp = await self._run_sync(_fetch)
            data = (resp.get("Success") or [{}])[0] if resp.get("Success") else {}
            available = float(data.get("net_amount", 0))
            used = float(data.get("block_by_trade", 0))
            return FundsResponse(
                available_cash=available,
                used_margin=used,
                total_balance=available + used,
            )
        except Exception as e:
            logger.error(f"ICICI get_funds failed: {e}")
            return FundsResponse(available_cash=0, used_margin=0, total_balance=0)

    async def get_order_book(self) -> List[Order]:
        def _fetch():
            b = self._get_breeze()
            return b.get_order_list(
                exchange_code="NSE",
                from_date=None,
                to_date=None,
            )

        try:
            resp = await self._run_sync(_fetch)
            rows = resp.get("Success", []) or []
            return [
                Order(
                    order_id=str(r.get("order_id", "")),
                    symbol=r.get("stock_code", ""),
                    exchange=r.get("exchange_code", "NSE"),
                    action=r.get("action", "BUY").upper(),
                    order_type=r.get("order_type", "MARKET").upper(),
                    qty=int(r.get("quantity", 0)),
                    price=float(r.get("price", 0)),
                    status=r.get("order_status", "OPEN").upper(),
                )
                for r in rows
            ]
        except Exception as e:
            logger.error(f"ICICI get_order_book failed: {e}")
            return []

    async def get_trade_book(self) -> List[Trade]:
        def _fetch():
            b = self._get_breeze()
            return b.get_trade_list(
                exchange_code="NSE",
                from_date=None,
                to_date=None,
            )

        try:
            resp = await self._run_sync(_fetch)
            rows = resp.get("Success", []) or []
            return [
                Trade(
                    trade_id=str(r.get("trade_id", r.get("order_id", ""))),
                    order_id=str(r.get("order_id", "")),
                    symbol=r.get("stock_code", ""),
                    exchange=r.get("exchange_code", "NSE"),
                    action=r.get("action", "BUY").upper(),
                    qty=int(r.get("quantity", 0)),
                    fill_price=float(r.get("price", 0)),
                    fill_time=datetime.utcnow(),
                )
                for r in rows
            ]
        except Exception as e:
            logger.error(f"ICICI get_trade_book failed: {e}")
            return []

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        def _do():
            b = self._get_breeze()
            return b.place_order(
                stock_code=order.symbol,
                exchange_code=order.exchange or "NSE",
                product=order.product or "INTRADAY",
                action=order.action.lower(),
                order_type=order.order_type.lower(),
                stoploss=str(order.sl or "0"),
                quantity=str(order.qty),
                price=str(order.price or "0"),
                validity="DAY",
                validity_date="",
                disclosed_quantity="0",
                expiry_date=order.expiry or "",
                right=order.option_type.lower() if order.option_type else "others",
                strike_price=str(int(order.strike or 0)),
            )

        try:
            resp = await self._run_sync(_do)
            data = (resp.get("Success") or [{}])[0]
            return OrderResponse(
                order_id=str(data.get("order_id", "")),
                status="OPEN",
            )
        except Exception as e:
            logger.error(f"ICICI place_order failed: {e}")
            raise

    async def modify_order(self, order_id: str, params: Dict[str, Any]) -> OrderResponse:
        def _do():
            b = self._get_breeze()
            return b.modify_order(
                exchange_code="NSE",
                order_id=order_id,
                quantity=str(params.get("qty", "")),
                price=str(params.get("price", "")),
                stoploss=str(params.get("sl", "")),
                validity="DAY",
            )

        resp = await self._run_sync(_do)
        return OrderResponse(order_id=order_id, status="MODIFIED")

    async def cancel_order(self, order_id: str) -> bool:
        def _do():
            b = self._get_breeze()
            return b.cancel_order(exchange_code="NSE", order_id=order_id)

        try:
            resp = await self._run_sync(_do)
            return resp.get("Status") == 200
        except Exception as e:
            logger.error(f"ICICI cancel_order failed: {e}")
            return False

    async def get_ltp(self, symbols: List[str]) -> Dict[str, float]:
        def _do():
            b = self._get_breeze()
            result = {}
            for sym in symbols:
                try:
                    resp = b.get_quotes(
                        stock_code=sym,
                        exchange_code="NSE",
                        expiry_date="",
                        product_type="cash",
                        right="",
                        strike_price="",
                    )
                    data = (resp.get("Success") or [{}])[0]
                    result[sym] = float(data.get("ltp", 0))
                except Exception:
                    result[sym] = 0.0
            return result

        try:
            return await self._run_sync(_do)
        except Exception as e:
            logger.error(f"ICICI get_ltp failed: {e}")
            return {sym: 0.0 for sym in symbols}

    async def get_option_chain(self, symbol: str, expiry: str) -> OptionChain:
        def _do():
            b = self._get_breeze()
            return b.get_option_chain_quotes(
                stock_code=symbol,
                exchange_code="NFO",
                product_type="options",
                expiry_date=expiry,
                right="",
                strike_price="",
            )

        try:
            resp = await self._run_sync(_do)
            rows = resp.get("Success", []) or []
            entries = []
            spot = 0.0
            for r in rows:
                entries.append(OptionChainEntry(
                    strike=float(r.get("strike_price", 0)),
                    option_type="CE" if r.get("right", "").lower() == "call" else "PE",
                    ltp=float(r.get("ltp", 0)),
                    oi=int(r.get("open_interest", 0)),
                    volume=int(r.get("total_quantity_traded", 0)),
                    bid=float(r.get("best_bid_price", 0)),
                    ask=float(r.get("best_offer_price", 0)),
                    iv=float(r.get("implied_volatility", 0)) / 100 if r.get("implied_volatility") else None,
                ))
                if not spot and r.get("underlying_price"):
                    spot = float(r.get("underlying_price", 0))
            return OptionChain(symbol=symbol, expiry=expiry, spot_price=spot, entries=entries)
        except Exception as e:
            logger.error(f"ICICI get_option_chain failed: {e}")
            return OptionChain(symbol=symbol, expiry=expiry, spot_price=0, entries=[])

    async def get_margins(self, orders: List[OrderRequest]) -> MarginResponse:
        # Breeze doesn't have a direct basket margin API; return placeholder
        return MarginResponse(required_margin=0, available_margin=0, utilized_margin=0)

    async def subscribe_websocket(
        self,
        symbols: List[str],
        on_tick_callback: Callable[[Tick], None],
    ) -> Any:
        """Subscribe to live feed via Breeze WebSocket."""
        def _on_ticks(ticks):
            data = ticks if isinstance(ticks, list) else [ticks]
            for tick in data:
                on_tick_callback(Tick(
                    symbol=tick.get("symbol", ""),
                    ltp=float(tick.get("last", 0)),
                    open=float(tick.get("open", 0)),
                    high=float(tick.get("high", 0)),
                    low=float(tick.get("low", 0)),
                    volume=int(tick.get("volume", 0)),
                    oi=int(tick.get("oi", 0)),
                ))

        def _do_sub():
            b = self._get_breeze()
            b.ws_connect()
            b.on_ticks = _on_ticks
            for sym in symbols:
                b.subscribe_feeds(stock_token=sym)

        await self._run_sync(_do_sub)

    async def unsubscribe_ticks(self, symbols: List[str]) -> None:
        def _do():
            b = self._get_breeze()
            for sym in symbols:
                try:
                    b.unsubscribe_feeds(stock_token=sym)
                except Exception:
                    pass

        await self._run_sync(_do)

    # ── Historical Data (MACD Backtesting) ───────────────────────────────────

    async def get_historical_options(
        self,
        stock_code: str,
        expiry_date: str,
        right: str,
        strike_price: str,
        from_date: str,
        to_date: str,
        interval: str = "5minute",
    ) -> List[Dict]:
        """
        Fetch option premium OHLCV — key for Options MACD backtesting.

        Args:
            stock_code: e.g. "NIFTY", "BANKNIFTY", "RELIANCE"
            expiry_date: "2023-01-26T07:00:00.000Z"
            right: "call" or "put"
            strike_price: "18000"
            from_date: "2023-01-01T07:00:00.000Z"
            to_date: "2023-01-26T07:00:00.000Z"
            interval: "1minute", "5minute", "30minute", "1day"

        Returns:
            List of OHLCV dicts with keys: datetime, open, high, low, close, volume, open_interest
        """
        def _fetch():
            b = self._get_breeze()
            return b.get_historical_data_v2(
                interval=interval,
                from_date=from_date,
                to_date=to_date,
                stock_code=stock_code,
                exchange_code="NFO",
                product_type="options",
                expiry_date=expiry_date,
                right=right,
                strike_price=strike_price,
            )

        resp = await self._run_sync(_fetch)
        rows = resp.get("Success", []) or []
        return [
            {
                "datetime": r.get("datetime", ""),
                "open": float(r.get("open", 0)),
                "high": float(r.get("high", 0)),
                "low": float(r.get("low", 0)),
                "close": float(r.get("close", 0)),
                "volume": int(r.get("volume", 0)),
                "open_interest": int(r.get("open_interest", 0)),
            }
            for r in rows
        ]

    async def get_historical_equity(
        self,
        stock_code: str,
        from_date: str,
        to_date: str,
        interval: str = "5minute",
        exchange_code: str = "NSE",
    ) -> List[Dict]:
        """Fetch equity OHLCV for underlying price data."""
        def _fetch():
            b = self._get_breeze()
            return b.get_historical_data_v2(
                interval=interval,
                from_date=from_date,
                to_date=to_date,
                stock_code=stock_code,
                exchange_code=exchange_code,
                product_type="cash",
                expiry_date="",
                right="",
                strike_price="",
            )

        resp = await self._run_sync(_fetch)
        rows = resp.get("Success", []) or []
        return [
            {
                "datetime": r.get("datetime", ""),
                "open": float(r.get("open", 0)),
                "high": float(r.get("high", 0)),
                "low": float(r.get("low", 0)),
                "close": float(r.get("close", 0)),
                "volume": int(r.get("volume", 0)),
            }
            for r in rows
        ]
