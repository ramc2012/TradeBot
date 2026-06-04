"""Abstract base class for all broker adapters."""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional


# ─── Shared Data Schemas ─────────────────────────────────────────────────────

@dataclass
class AuthToken:
    access_token: str
    refresh_token: Optional[str] = None
    expires_at: Optional[datetime] = None


@dataclass
class UserProfile:
    user_id: str
    name: str
    email: Optional[str] = None
    mobile: Optional[str] = None
    broker: str = ""


@dataclass
class Position:
    symbol: str
    exchange: str
    instrument_type: str
    qty: int
    avg_price: float
    ltp: float
    unrealized_pnl: float
    realized_pnl: float
    strike: Optional[float] = None
    expiry: Optional[str] = None
    option_type: Optional[str] = None
    product: str = "INTRADAY"


@dataclass
class Holding:
    symbol: str
    exchange: str
    qty: int
    avg_price: float
    ltp: float
    pnl: float


@dataclass
class Order:
    order_id: str
    symbol: str
    exchange: str
    action: str
    order_type: str
    qty: int
    price: float
    status: str
    fill_price: Optional[float] = None
    fill_time: Optional[datetime] = None
    instrument_type: str = "EQ"
    strike: Optional[float] = None
    expiry: Optional[str] = None
    option_type: Optional[str] = None


@dataclass
class Trade:
    trade_id: str
    order_id: str
    symbol: str
    exchange: str
    action: str
    qty: int
    fill_price: float
    fill_time: datetime


@dataclass
class OrderRequest:
    symbol: str
    exchange: str
    action: str          # BUY / SELL
    order_type: str      # MARKET / LIMIT / SL / SL_M
    qty: int
    instrument_type: str = "CE"
    price: Optional[float] = None
    sl: Optional[float] = None
    target: Optional[float] = None
    product: str = "INTRADAY"
    validity: str = "DAY"
    expiry: Optional[str] = None
    strike: Optional[float] = None
    option_type: Optional[str] = None
    # WS-0.4a — caller-supplied idempotency key. A request carrying an id already
    # seen this session is never re-sent to the broker; to genuinely retry a failed
    # order, mint a NEW id.
    client_order_id: Optional[str] = None


@dataclass
class OrderResponse:
    order_id: str
    status: str
    message: str = ""


@dataclass
class OptionChainEntry:
    strike: float
    option_type: str     # CE / PE
    ltp: float
    oi: int
    volume: int
    bid: float
    ask: float
    iv: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    prev_oi: Optional[float] = None
    prev_close: Optional[float] = None
    instrument_key: Optional[str] = None


@dataclass
class OptionChain:
    symbol: str
    expiry: str
    spot_price: float
    entries: list[OptionChainEntry] = field(default_factory=list)


@dataclass
class MarginResponse:
    required_margin: float
    available_margin: float
    utilized_margin: float


@dataclass
class FundsResponse:
    available_cash: float
    used_margin: float
    total_balance: float
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0


@dataclass
class Tick:
    symbol: str
    ltp: float
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0
    oi: int = 0
    bid: float = 0.0
    ask: float = 0.0
    bid_qty: int = 0
    ask_qty: int = 0
    # Aggregate order-book depth (total buy/sell qty across the book). Real
    # depth_imbalance source for auction-intelligence order flow (P1d). 0 for
    # index spot (no book); populated for futures/option book contracts.
    total_buy_qty: int = 0
    total_sell_qty: int = 0
    timestamp: Optional[datetime] = None


# ─── Abstract Adapter ────────────────────────────────────────────────────────

class BrokerAdapter(ABC):
    """Unified interface for all broker integrations."""

    broker_name: str = "base"

    @abstractmethod
    async def authenticate(self, credentials: dict) -> AuthToken:
        """Perform authentication and return tokens."""

    @abstractmethod
    async def refresh_token(self) -> AuthToken:
        """Refresh access token using refresh_token."""

    @abstractmethod
    async def get_profile(self) -> UserProfile:
        """Fetch logged-in user profile."""

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Fetch current open positions."""

    @abstractmethod
    async def get_holdings(self) -> list[Holding]:
        """Fetch equity holdings."""

    @abstractmethod
    async def get_order_book(self) -> list[Order]:
        """Fetch today's order book."""

    @abstractmethod
    async def get_trade_book(self) -> list[Trade]:
        """Fetch today's trade book."""

    @abstractmethod
    async def place_order(self, order: OrderRequest) -> OrderResponse:
        """Place a new order."""

    @abstractmethod
    async def modify_order(self, order_id: str, params: dict) -> OrderResponse:
        """Modify an existing order."""

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order. Returns True on success."""

    @abstractmethod
    async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        """Get Last Traded Price for a list of symbols."""

    @abstractmethod
    async def subscribe_websocket(
        self,
        symbols: list[str],
        on_tick_callback: Callable[[Tick], None],
    ) -> Any:
        """Open a WebSocket feed and call on_tick_callback for each tick."""

    @abstractmethod
    async def get_option_chain(self, symbol: str, expiry: str) -> OptionChain:
        """Fetch full option chain for a symbol and expiry."""

    @abstractmethod
    async def get_margins(self, orders: list[OrderRequest]) -> MarginResponse:
        """Calculate margin required for a basket of orders."""

    @abstractmethod
    async def get_funds(self) -> FundsResponse:
        """Get available funds and margin info."""

    # ── Helpers shared across all adapters ──────────────────────────────────

    def is_authenticated(self) -> bool:
        return hasattr(self, "_access_token") and bool(self._access_token)
