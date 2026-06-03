"""Fyers broker adapter using fyers-apiv3 SDK."""
from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, date, datetime, timedelta
from typing import Any, Callable, Optional

import httpx
from loguru import logger

from brokers.base import (
    AuthToken, BrokerAdapter, FundsResponse, Holding, MarginResponse,
    OptionChain, OptionChainEntry, Order, OrderRequest, OrderResponse,
    Position, Tick, Trade, UserProfile,
)
from core.config import settings
from analytics.greeks import bs_greeks, implied_volatility


class FyersAdapter(BrokerAdapter):
    """Adapter for Fyers broker (fyers-apiv3)."""

    broker_name = "fyers"
    BASE_URL = "https://api-t1.fyers.in/api/v3"
    DATA_URL = "https://api-t1.fyers.in/data"

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

    def _set_token_state(
        self,
        access_token: str,
        *,
        refresh_token: Optional[str] = None,
    ) -> None:
        self._access_token = str(access_token or "").strip()
        if refresh_token:
            self._refresh_token = str(refresh_token).strip()
        if self._client is not None:
            self._client.headers.update({"Authorization": f"{settings.FYERS_APP_ID}:{self._access_token}"})

    async def _get_data_json(self, path: str, params: Optional[dict] = None) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                f"{self.DATA_URL}{path}",
                params=params or {},
                headers=self._auth_header(),
            )
        try:
            payload = response.json()
        except Exception as exc:
            body = response.text[:240]
            raise ValueError(f"Fyers data API returned non-JSON payload: {body}") from exc
        if response.status_code != 200:
            message = payload.get("message") if isinstance(payload, dict) else response.text[:240]
            raise ValueError(f"Fyers data API error {response.status_code}: {message}")
        if isinstance(payload, dict) and payload.get("s") == "error":
            raise ValueError(payload.get("message") or "Fyers data API returned an error")
        return payload

    @staticmethod
    def _expiry_date_to_epoch(expiry: str, expiry_rows: list[dict]) -> Optional[str]:
        try:
            target = datetime.strptime(expiry, "%Y-%m-%d").date()
        except ValueError:
            return None
        for row in expiry_rows:
            date_text = str(row.get("date") or "").strip()
            epoch = str(row.get("expiry") or "").strip()
            if not date_text or not epoch:
                continue
            try:
                parsed = datetime.strptime(date_text, "%d-%m-%Y").date()
            except ValueError:
                continue
            if parsed == target:
                return epoch
        return None

    @staticmethod
    def _epoch_to_iso_date(value: str) -> Optional[str]:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            return datetime.fromtimestamp(int(raw), UTC).date().isoformat()
        except Exception:
            return None

    @staticmethod
    def _coerce_float(*values: Any) -> float:
        for value in values:
            if value in (None, "", 0, "0", "0.0"):
                continue
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                continue
            if parsed:
                return parsed
        return 0.0

    async def get_historical_candles(
        self,
        symbol: str,
        resolution: str,
        range_from: str,
        range_to: str,
        cont_flag: int = 1,
    ) -> list[dict]:
        payload = await self._get_data_json(
            "/history",
            {
                "symbol": symbol,
                "resolution": resolution,
                "date_format": "1",
                "range_from": range_from,
                "range_to": range_to,
                "cont_flag": str(cont_flag),
            },
        )
        rows: list[dict] = []
        for candle in payload.get("candles", []):
            if not candle or len(candle) < 6:
                continue
            rows.append(
                {
                    "time": datetime.fromtimestamp(int(candle[0]), UTC).isoformat().replace("+00:00", "Z"),
                    "open": float(candle[1]),
                    "high": float(candle[2]),
                    "low": float(candle[3]),
                    "close": float(candle[4]),
                    "volume": int(candle[5] or 0),
                }
            )
        return rows

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
        credentials: { auth_code: str } OR { access_token: str }
        OR { refresh_token: str, pin: str }.
        """
        if "access_token" in credentials:
            self._set_token_state(
                credentials["access_token"],
                refresh_token=credentials.get("refresh_token"),
            )
            return AuthToken(
                access_token=self._access_token or "",
                refresh_token=self._refresh_token,
            )

        refresh_token = str(credentials.get("refresh_token") or "").strip()
        if refresh_token:
            pin = str(credentials.get("pin") or settings.FYERS_PIN or "").strip()
            if not pin:
                raise ValueError("Fyers PIN is required to refresh a saved refresh token")
            return await self._authenticate_with_refresh_token(refresh_token, pin)

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
            response = await asyncio.to_thread(session.generate_token)
            refresh_token = response.get("refresh_token") or response.get("refreshToken")
            self._set_token_state(response.get("access_token", ""), refresh_token=refresh_token)
            expires_at_raw = response.get("expires_at") or response.get("expiresAt")
            parsed_expiry: Optional[datetime] = None
            if expires_at_raw:
                try:
                    parsed_expiry = datetime.fromisoformat(str(expires_at_raw))
                    if parsed_expiry.tzinfo is None:
                        parsed_expiry = parsed_expiry.replace(tzinfo=UTC)
                except Exception:
                    parsed_expiry = None
            logger.info("Fyers authenticated successfully")
            return AuthToken(
                access_token=self._access_token or "",
                refresh_token=self._refresh_token or str(refresh_token).strip() or None,
                expires_at=parsed_expiry or datetime.now(UTC) + timedelta(hours=8),
            )
        except Exception as e:
            logger.error(f"Fyers authentication failed: {e}")
            raise

    async def _authenticate_with_refresh_token(self, refresh_token: str, pin: str) -> AuthToken:
        payload_base = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "pin": pin,
        }
        hash_inputs = (
            f"{settings.FYERS_APP_ID}:{settings.FYERS_SECRET}",
            f"{settings.FYERS_APP_ID}{settings.FYERS_SECRET}",
        )
        last_error: str | None = None
        async with httpx.AsyncClient(timeout=15.0) as client:
            for hash_input in hash_inputs:
                payload = {
                    **payload_base,
                    "appIdHash": hashlib.sha256(hash_input.encode()).hexdigest(),
                }
                response = await client.post(
                    f"{self.BASE_URL}/validate-refresh-token",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                try:
                    body = response.json()
                except Exception:
                    body = {"message": response.text[:240]}
                if response.status_code == 200 and body.get("access_token"):
                    new_refresh = body.get("refresh_token") or body.get("refreshToken") or refresh_token
                    self._set_token_state(body["access_token"], refresh_token=new_refresh)
                    expires_in = int(body.get("expires_in") or body.get("expiresIn") or 28800)
                    logger.info("Fyers refreshed access token using saved refresh token")
                    return AuthToken(
                        access_token=self._access_token or "",
                        refresh_token=self._refresh_token or refresh_token,
                        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
                    )
                last_error = body.get("message") or body.get("error_description") or str(body)
                if body.get("code") != -371:
                    break
        raise ValueError(f"Fyers refresh-token exchange failed: {last_error or 'unknown error'}")

    async def refresh_token(self) -> AuthToken:
        if not self._refresh_token:
            raise NotImplementedError("Fyers refresh_token is not available")
        pin = str(settings.FYERS_PIN or "").strip()
        if not pin:
            raise NotImplementedError("Fyers PIN is required to refresh the saved refresh_token")
        return await self._authenticate_with_refresh_token(self._refresh_token, pin)

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
        payload = await self._get_data_json("/quotes", {"symbols": ",".join(symbols)})
        quotes = payload.get("d", [])
        result = {}
        for q in quotes:
            sym = q.get("n", "")
            ltp = q.get("v", {}).get("lp", 0)
            result[sym] = ltp
        return result

    async def get_option_contracts(self, symbol: str, expiry: Optional[str] = None) -> list[dict]:
        payload = await self._get_data_json("/options-chain-v3", {"symbol": symbol, "strikecount": "1"})
        expiry_rows = payload.get("data", {}).get("expiryData", [])
        rows = []
        for row in expiry_rows:
            iso_expiry = None
            if row.get("date"):
                try:
                    iso_expiry = datetime.strptime(str(row["date"]), "%d-%m-%Y").date().isoformat()
                except ValueError:
                    iso_expiry = None
            if not iso_expiry:
                iso_expiry = self._epoch_to_iso_date(str(row.get("expiry") or ""))
            if not iso_expiry:
                continue
            rows.append(
                {
                    "expiry": iso_expiry,
                    "timestamp": str(row.get("expiry") or "").strip() or None,
                }
            )
        if expiry:
            rows = [row for row in rows if row.get("expiry") == expiry]
        return rows

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
            def _first(*keys):
                for k in keys:
                    v = msg.get(k)
                    if v is not None:
                        return v
                return 0
            # Fyers SymbolUpdate (data_val) carries REAL top-of-book under
            # bid_price/ask_price + bid_size/ask_size for tradable contracts
            # (futures/options). The old code read "bid"/"ask" — keys that
            # don't exist in data_val — and never set sizes, so bid_qty/ask_qty
            # defaulted to 0. Downstream the auction-intelligence order-flow
            # path then floored sizes to 1.0 and fabricated the whole book
            # (the book_pressure=0.1125 / candle-color tape we traced). Read
            # the real keys with a fallback to the legacy ones. Index spot
            # symbols carry no book, so these stay 0 there — harmless.
            tick = Tick(
                symbol=msg.get("symbol") or msg.get("n", ""),
                ltp=msg.get("ltp", 0),
                open=msg.get("open_price", 0),
                high=msg.get("high_price", 0),
                low=msg.get("low_price", 0),
                close=msg.get("prev_close_price", 0),
                volume=msg.get("vol_traded_today", 0),
                oi=msg.get("oi", 0),
                bid=_first("bid_price", "bid"),
                ask=_first("ask_price", "ask"),
                bid_qty=_first("bid_size", "bid_qty"),
                ask_qty=_first("ask_size", "ask_qty"),
                # Aggregate book depth (P1d) — Fyers SymbolUpdate carries the
                # whole-book buy/sell totals; real depth_imbalance source.
                total_buy_qty=_first("tot_buy_qty", "total_buy_qty"),
                total_sell_qty=_first("tot_sell_qty", "total_sell_qty"),
                timestamp=datetime.now(UTC),
            )
            callback(tick)
        except Exception as e:
            logger.error(f"Error parsing Fyers tick: {e}")

    async def get_option_chain(self, symbol: str, expiry: str) -> OptionChain:
        seed_payload = await self._get_data_json(
            "/options-chain-v3",
            {"symbol": symbol, "strikecount": "12"},
        )
        data = seed_payload.get("data", {})
        expiry_rows = data.get("expiryData", [])
        expiry_epoch = self._expiry_date_to_epoch(expiry, expiry_rows)
        if expiry_epoch:
            payload = await self._get_data_json(
                "/options-chain-v3",
                {"symbol": symbol, "strikecount": "12", "timestamp": expiry_epoch},
            )
            data = payload.get("data", {})

        expiry_iso = expiry
        if not expiry_iso:
            first_expiry = str((data.get("expiryData") or [{}])[0].get("expiry") or "").strip()
            expiry_iso = self._epoch_to_iso_date(first_expiry) or expiry

        entries = []
        spot_price = 0.0
        if data.get("optionsChain"):
            head = data["optionsChain"][0]
            # Fyers carries the underlying future price in `fp` for commodity chains.
            # Some responses also include an option-row `ltp`, which is not the
            # underlying and can skew ATM selection if it is preferred first.
            spot_price = self._coerce_float(
                head.get("fp"),
                data.get("fp"),
                head.get("underlying_price"),
                data.get("underlying_price"),
                head.get("underlyingPrice"),
                data.get("underlyingPrice"),
                head.get("underlying_ltp"),
                data.get("underlying_ltp"),
                head.get("underlyingLtp"),
                data.get("underlyingLtp"),
            )
        if spot_price <= 0:
            try:
                live_quotes = await self.get_ltp([symbol])
                spot_price = float(live_quotes.get(symbol, 0) or 0)
            except Exception as exc:
                logger.debug(f"Fyers option-chain spot fallback failed for {symbol}: {exc}")

        try:
            expiry_dt = datetime.strptime(expiry_iso, "%Y-%m-%d").date()
        except ValueError:
            expiry_dt = date.today()
        T = max(1e-6, (expiry_dt - date.today()).days / 365)

        for opt in data.get("optionsChain", []):
            option_type = str(opt.get("option_type") or "").upper()
            if option_type not in {"CE", "PE"}:
                continue
            strike = float(opt.get("strike_price", 0) or 0)
            ltp = float(opt.get("ltp", 0) or 0)
            prev_close = None
            if opt.get("ltpch") is not None:
                prev_close = round(ltp - float(opt.get("ltpch") or 0), 2)
            iv = delta = gamma = theta = vega = None
            if strike > 0 and ltp > 0 and spot_price > 0:
                try:
                    iv_value = implied_volatility(
                        market_price=ltp,
                        S=spot_price,
                        K=strike,
                        T=T,
                        r=0.065,
                        option_type=option_type,
                    )
                    if iv_value > 0:
                        greeks = bs_greeks(
                            S=spot_price,
                            K=strike,
                            T=T,
                            r=0.065,
                            sigma=iv_value,
                            option_type=option_type,
                            iv=iv_value,
                        )
                        iv = greeks.iv
                        delta = greeks.delta
                        gamma = greeks.gamma
                        theta = greeks.theta
                        vega = greeks.vega
                except Exception as exc:
                    logger.debug(f"Fyers Greek enrichment failed for {symbol} {strike} {option_type}: {exc}")
            entries.append(OptionChainEntry(
                strike=strike,
                option_type=option_type,
                ltp=ltp,
                oi=int(opt.get("oi", 0) or 0),
                volume=int(opt.get("volume", 0) or 0),
                bid=float(opt.get("bid", 0) or 0),
                ask=float(opt.get("ask", 0) or 0),
                iv=iv,
                delta=delta,
                gamma=gamma,
                theta=theta,
                vega=vega,
                prev_oi=float(opt.get("prev_oi", 0) or opt.get("prevOi", 0) or 0),
                prev_close=prev_close,
                instrument_key=opt.get("symbol", None),
            ))
        return OptionChain(
            symbol=symbol,
            expiry=expiry_iso,
            spot_price=spot_price,
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
